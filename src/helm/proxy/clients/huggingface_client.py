from copy import deepcopy
import torch
from dataclasses import asdict
from transformers import AutoModelForCausalLM, AutoTokenizer, GenerationConfig
from typing import Any, Dict, List

from helm.common.cache import Cache, CacheConfig
from helm.common.hierarchical_logger import htrack_block, hlog
from helm.common.request import EMBEDDING_UNAVAILABLE_REQUEST_RESULT, Request, RequestResult, Sequence, Token
from helm.common.tokenization_request import (
    TokenizationRequest,
    TokenizationRequestResult,
    DecodeRequest,
    DecodeRequestResult,
    TokenizationToken,
)
from .client import Client, wrap_request_time, truncate_sequence, cleanup_tokens
from .huggingface_tokenizer import HuggingFaceTokenizers
from helm.proxy.clients.huggingface_model_registry import (
    get_huggingface_model_config,
    HuggingFaceModelConfig,
    HuggingFaceHubModelConfig,
    HuggingFaceLocalModelConfig,
)
from threading import Lock

from helm.benchmark.hidden_geometry.utils import hidden_states_process

from einops import reduce
import numpy as np
import copy

# Map of HELM model name to Hugging Face Hub model name where they differ.
_KNOWN_MODEL_ALIASES: Dict[str, str] = {
    "huggingface/gpt2": "gpt2",
    "huggingface/starcoder": "bigcode/starcoder",
}


class HuggingFaceServer:
    def __init__(self, model_config: HuggingFaceModelConfig):
        if torch.cuda.is_available():
            hlog("CUDA is available, initializing with a GPU...")
            self.device: str = "cuda:0"
        else:
            self.device = "cpu"
        model_kwargs = {}
        # If the HuggingFace model is stored locally, it will have a path defined and we should load it from there.
        # Otherwise, download it from the HuggingFace hub by passing in its identifier.
        if isinstance(model_config, HuggingFaceLocalModelConfig):
            model_name = model_config.path
        elif isinstance(model_config, HuggingFaceHubModelConfig):
            model_name = model_config.model_id
            if model_config.revision:
                model_kwargs["revision"] = model_config.revision
        else:
            raise Exception(f"Unknown type of model_config: {model_config}")
        with htrack_block(f"Loading Hugging Face model for config {model_config}"):
            # we can set if the model should return the hidden states also in the generate method, and we take the condition from the adapter_spec
            model_kwargs["output_hidden_states"] = True
            model_kwargs["device_map"]="auto"
            #model_kwargs["low_cpu_mem_usage"]=True
            #model_kwargs["torch_dtype"]=torch.float16
            # WARNING this may fail if your GPU does not have enough memory
            # I'm addding output_hidden_states=True to the model to get the hidden states
            # self.model = AutoModelForCausalLM.from_pretrained(model_name, trust_remote_code=True, **model_kwargs).to(
            #     self.device
            # )
            self.model = AutoModelForCausalLM.from_pretrained(model_name, trust_remote_code=True, **model_kwargs)
            #self.model.to_bettertransformer()
        with htrack_block(f"Loading Hugging Face tokenizer model for config {model_config}"):
            self.tokenizer = AutoTokenizer.from_pretrained(model_name, **model_kwargs)
            
    def serve_request(self, raw_requests: Dict[str, Any]):
        prompts = [raw_request["prompt"] for raw_request in raw_requests]
        encoded_input = self.tokenizer(prompts, return_tensors="pt", return_token_type_ids=False).to(
            self.device
        )
        len_tokens_questions = []
        for raw_request in raw_requests:
            raw_request = deepcopy(raw_request)
            raw_request["do_sample"] = True
            raw_request["return_dict_in_generate"] = True
            raw_request["output_scores"] = True
            top_k_per_token: int = raw_request["top_k_per_token"]
            del raw_request["top_k_per_token"]
            #-----------------------------------------
            # getting index of last question
            index_in_prompt = raw_request["prompt"].rfind("Question")
            tokens_question = self.tokenizer(raw_request["prompt"][index_in_prompt:], return_tensors="pt", return_token_type_ids=False)
            len_tokens_question = tokens_question["input_ids"].shape[1]
            len_tokens_questions.append(len_tokens_question)
            #-----------------------------------------
            
            if len(raw_request["stop_sequences"]) > 0:
                stop_sequence_ids = self.tokenizer(
                    raw_request["stop_sequences"], return_token_type_ids=False, add_special_tokens=False
                )
                assert len(stop_sequence_ids.input_ids) == 1, "Total number of stop words should be 1."
                assert len(stop_sequence_ids.input_ids[0]) == 1, "Total number of tokens in each stop word should be 1."
                del raw_request["stop_sequences"]
                raw_request["eos_token_id"] = stop_sequence_ids.input_ids[0][0]

            # Strip out irrelevant parameters
            relevant_raw_request = {
                key: raw_request[key]
                for key in raw_request
                if key not in ["engine", "prompt", "echo_prompt", "stop_sequences"]
            }

        # TODO: using GenerationConfig
        #-----------------------------------------
        # Use HuggingFace's `generate` method.
        output = self.model.generate(**encoded_input, **relevant_raw_request)
        sequences = output.sequences
        scores = output.scores
        
        out = []
        for raw_request, len_tokens_question in zip(raw_requests,len_tokens_questions):
            #storing hidden states
            if raw_request["output_hidden_states"]:
                # instance_hiddenstates = {"len_tokens_question": len_tokens_question, "hidden_states" : torch.cat(output.hidden_states[0])}
                # hidden_states = hidden_states_process(instance_hiddenstates)
                #print(f'{"x"*100}\n{hidden_states["sum"].shape=}\n{hidden_states["last"].shape=}\n{"x"*100}')
                # del instance_hiddenstates

                hs = torch.cat(output.hidden_states[0][-len_tokens_question:]).detach().cpu().numpy()
                # cropped_hidden_states = [copy.deepcopy(i[-len_tokens_question:,:].mean(0).detach().cpu().numpy()) for i in output.hidden_states[0]]
                # # for i in output.hidden_states[0]:
                # #     cropped_hidden_states.append(copy.deepcopy(i[-len_tokens_question:,:].mean(0).detach().cpu().numpy()))
                # hidden_states = cropped_hidden_states
                # del output
                #hidden_states =reduce(copy.deepcopy(hs[:,-len_tokens_question:,:]), "l s d -> l d", "mean")
                hidden_states = {"last": copy.deepcopy(hs[:,-1,:]), "sum":reduce(copy.deepcopy(hs[:,-len_tokens_question:,:]), "l s d -> l d", "mean")}
            else:
                hidden_states = None

            # Compute logprobs for each completed sequence.
            all_logprobs_of_chosen_tokens = []
            all_top_logprobs_dicts = []
            for completion_id in range(raw_request["num_return_sequences"]):
                logprobs_of_chosen_tokens = []
                top_logprobs_dicts = []
                for i in range(len(sequences[completion_id]) - len(encoded_input.input_ids[0])):
                    logprobs = torch.nn.functional.log_softmax(scores[i][completion_id], dim=0)

                    # Get top tokens in terms of log probability.
                    topk_logprobs = torch.topk(logprobs, k=top_k_per_token)
                    top_logprobs_dicts.append(
                        {
                            self.tokenizer.convert_ids_to_tokens(k.item()): v.item()
                            for (k, v) in zip(topk_logprobs.indices, topk_logprobs.values)
                        }
                    )

                    # Get log probability of chosen token.
                    j = i + len(encoded_input.input_ids[0])
                    logprobs_of_chosen_tokens.append(logprobs[sequences[completion_id][j]].item())
                all_logprobs_of_chosen_tokens.append(logprobs_of_chosen_tokens)
                all_top_logprobs_dicts.append(top_logprobs_dicts)

            # Remove prompt from the start of each sequence if echo_prompt is False.
            if not raw_request["echo_prompt"]:
                sequences = [sequence[len(encoded_input.input_ids[0]) :] for sequence in sequences]

            all_tokens = [[self.tokenizer.decode(token) for token in sequence_tokens] for sequence_tokens in sequences]
            all_decoded_text = self.tokenizer.batch_decode(sequences)

            completions = []
            for decoded_text, tokens, logprobs_of_chosen_tokens, top_logprobs_dicts in zip(
                all_decoded_text, all_tokens, all_logprobs_of_chosen_tokens, all_top_logprobs_dicts
            ):
                completions.append(
                    {
                        "text": decoded_text,
                        "tokens": tokens,
                        "logprobs": logprobs_of_chosen_tokens,
                        "top_logprobs_dicts": top_logprobs_dicts,
                        "hidden_states": hidden_states
                    }
                )
            torch.cuda.empty_cache()
            out.append({"completions": completions, "input_length": len(encoded_input.input_ids[0])})
        return out

#     def serve_request(self, raw_request: Dict[str, Any]):
#         encoded_input = self.tokenizer(raw_request["prompt"], return_tensors="pt", return_token_type_ids=False).to(
#             self.device
#         )

#         raw_request = deepcopy(raw_request)
#         raw_request["do_sample"] = True
#         raw_request["return_dict_in_generate"] = True
#         raw_request["output_scores"] = True
#         top_k_per_token: int = raw_request["top_k_per_token"]
#         del raw_request["top_k_per_token"]
#         #-----------------------------------------
#         # getting index of last question
#         index_in_prompt = raw_request["prompt"].rfind("Question")
#         tokens_question = self.tokenizer(raw_request["prompt"][index_in_prompt:], return_tensors="pt", return_token_type_ids=False)
#         len_tokens_question = tokens_question["input_ids"].shape[1]
#         #-----------------------------------------
        
#         if len(raw_request["stop_sequences"]) > 0:
#             stop_sequence_ids = self.tokenizer(
#                 raw_request["stop_sequences"], return_token_type_ids=False, add_special_tokens=False
#             )
#             assert len(stop_sequence_ids.input_ids) == 1, "Total number of stop words should be 1."
#             assert len(stop_sequence_ids.input_ids[0]) == 1, "Total number of tokens in each stop word should be 1."
#             del raw_request["stop_sequences"]
#             raw_request["eos_token_id"] = stop_sequence_ids.input_ids[0][0]

#         # Strip out irrelevant parameters
#         relevant_raw_request = {
#             key: raw_request[key]
#             for key in raw_request
#             if key not in ["engine", "prompt", "echo_prompt", "stop_sequences"]
#         }

#         # TODO: using GenerationConfig
#         #-----------------------------------------
#         # Use HuggingFace's `generate` method.
#         output = self.model.generate(**encoded_input, **relevant_raw_request)
#         sequences = output.sequences
#         scores = output.scores
        
#         #storing hidden states
#         if raw_request["output_hidden_states"]:
#             # instance_hiddenstates = {"len_tokens_question": len_tokens_question, "hidden_states" : torch.cat(output.hidden_states[0])}
#             # hidden_states = hidden_states_process(instance_hiddenstates)
#             #print(f'{"x"*100}\n{hidden_states["sum"].shape=}\n{hidden_states["last"].shape=}\n{"x"*100}')
#             # del instance_hiddenstates

#             hs = torch.cat(output.hidden_states[0][-len_tokens_question:]).detach().cpu().numpy()
#             # cropped_hidden_states = [copy.deepcopy(i[-len_tokens_question:,:].mean(0).detach().cpu().numpy()) for i in output.hidden_states[0]]
#             # # for i in output.hidden_states[0]:
#             # #     cropped_hidden_states.append(copy.deepcopy(i[-len_tokens_question:,:].mean(0).detach().cpu().numpy()))
#             # hidden_states = cropped_hidden_states
#             # del output
#             #hidden_states =reduce(copy.deepcopy(hs[:,-len_tokens_question:,:]), "l s d -> l d", "mean")
#             hidden_states = {"last": copy.deepcopy(hs[:,-1,:]), "sum":reduce(copy.deepcopy(hs[:,-len_tokens_question:,:]), "l s d -> l d", "mean")}
#         else:
#             hidden_states = None

#         # Compute logprobs for each completed sequence.
#         all_logprobs_of_chosen_tokens = []
#         all_top_logprobs_dicts = []
#         for completion_id in range(raw_request["num_return_sequences"]):
#             logprobs_of_chosen_tokens = []
#             top_logprobs_dicts = []
#             for i in range(len(sequences[completion_id]) - len(encoded_input.input_ids[0])):
#                 logprobs = torch.nn.functional.log_softmax(scores[i][completion_id], dim=0)

#                 # Get top tokens in terms of log probability.
#                 topk_logprobs = torch.topk(logprobs, k=top_k_per_token)
#                 top_logprobs_dicts.append(
#                     {
#                         self.tokenizer.convert_ids_to_tokens(k.item()): v.item()
#                         for (k, v) in zip(topk_logprobs.indices, topk_logprobs.values)
#                     }
#                 )

#                 # Get log probability of chosen token.
#                 j = i + len(encoded_input.input_ids[0])
#                 logprobs_of_chosen_tokens.append(logprobs[sequences[completion_id][j]].item())
#             all_logprobs_of_chosen_tokens.append(logprobs_of_chosen_tokens)
#             all_top_logprobs_dicts.append(top_logprobs_dicts)

#         # Remove prompt from the start of each sequence if echo_prompt is False.
#         if not raw_request["echo_prompt"]:
#             sequences = [sequence[len(encoded_input.input_ids[0]) :] for sequence in sequences]

#         all_tokens = [[self.tokenizer.decode(token) for token in sequence_tokens] for sequence_tokens in sequences]
#         all_decoded_text = self.tokenizer.batch_decode(sequences)

#         completions = []
#         for decoded_text, tokens, logprobs_of_chosen_tokens, top_logprobs_dicts in zip(
#             all_decoded_text, all_tokens, all_logprobs_of_chosen_tokens, all_top_logprobs_dicts
#         ):
#             completions.append(
#                 {
#                     "text": decoded_text,
#                     "tokens": tokens,
#                     "logprobs": logprobs_of_chosen_tokens,
#                     "top_logprobs_dicts": top_logprobs_dicts,
#                     "hidden_states": hidden_states
#                 }
#             )
#         torch.cuda.empty_cache()
#         return {"completions": completions, "input_length": len(encoded_input.input_ids[0])}


_servers_lock: Lock = Lock()
_servers: Dict[str, HuggingFaceServer] = {}


def _get_singleton_server(model_config: HuggingFaceModelConfig) -> HuggingFaceServer:
    """Lookup or create a new HuggingFaceServer that will be shared among all threads.

    When --num-threads > 1, multiple threads will attempt to instantiate
    `HuggingFaceServer`s simultaneously. Since we have limited GPU memory, we want to
    just share a single copy of each model we are using. So, this function uses a lock
    to make sure that for each model, only one thread creates a HuggingFaceServer.
    The other threads can share that same server in the global _servers dictionary."""
    global _servers_lock
    global _servers
    with _servers_lock:
        if model_config.model_id not in _servers:
            _servers[model_config.model_id] = HuggingFaceServer(model_config)
    return _servers[model_config.model_id]


class HuggingFaceClient(Client):
    def __init__(self, cache_config: CacheConfig):
        self.cache = Cache(cache_config)
        self.model_server_instances: Dict[str, HuggingFaceServer] = {}

    def get_model_server_instance(self, model: str) -> HuggingFaceServer:
        model_config = get_huggingface_model_config(model)
        # Special-case some models in so that users don't have to enable them with --enable-huggingface-models
        if not model_config:
            if model in _KNOWN_MODEL_ALIASES:
                model_config = HuggingFaceHubModelConfig.from_string(_KNOWN_MODEL_ALIASES[model])
            else:
                model_config = HuggingFaceHubModelConfig.from_string(model)
        #return HuggingFaceServer(model_config)
        return _get_singleton_server(model_config)

    def make_request(self, requests: list[Request], **kwargs) -> RequestResult:
        # Embedding not supported for this model
        if requests[0].embedding:
            return EMBEDDING_UNAVAILABLE_REQUEST_RESULT

        # Only a single stop sequence is supported as we can only pass in a single value for `eos_token_id`
        raw_requests = []
        for request in requests:
            if len(request.stop_sequences) > 1:
                raise ValueError("More than one stop sequence is not supported.")

            raw_request = {
                "engine": request.model_engine,
                "prompt": request.prompt,
                "temperature": 1e-7 if request.temperature == 0 else request.temperature,
                "num_return_sequences": request.num_completions,
                "max_new_tokens": request.max_tokens,
                "top_p": request.top_p,
                "echo_prompt": request.echo_prompt,
                "top_k_per_token": request.top_k_per_token,
                "stop_sequences": request.stop_sequences,
                "output_hidden_states": kwargs.get("hidden_states", False)
            }
            raw_requests.append(raw_request)
        # Get cached model server instance if possible (to save on model and tokenizer
        # loading times).
        model_server_instance: HuggingFaceServer = self.get_model_server_instance(requests[0].model)

        try:

            def do_it():
                return model_server_instance.serve_request(raw_requests)
            responses =  do_it()
            
        except Exception as e:  # Do something if error is encountered.
            error: str = f"HuggingFace error: {e}"
            return RequestResult(success=False, cached=False, error=error, completions=[], embedding=[])

        requests_results = []
        for response, request in zip(responses, requests):
            completions = []
            for raw_completion in response["completions"]:
                sequence_logprob: float = 0
                tokens: List[Token] = []
                if request.echo_prompt:
                    # Add prompt to list of generated tokens.
                    generated_tokens = raw_completion["tokens"][response["input_length"] :]
                    for token_text in raw_completion["tokens"][: response["input_length"]]:
                        tokens.append(Token(text=token_text, logprob=0.0, top_logprobs={}))
                else:
                    generated_tokens = raw_completion["tokens"]

                # Compute logprob for the entire sequence.
                for token_text, logprob, top_logprobs_dict in zip(
                    generated_tokens, raw_completion["logprobs"], raw_completion["top_logprobs_dicts"]
                ):
                    tokens.append(Token(text=token_text, logprob=logprob, top_logprobs=top_logprobs_dict))
                    sequence_logprob += logprob
                
                #modifying the hidden states so to keep only the last token and the avg of the tokens of the question
                hidden_states = raw_completion["hidden_states"]

                completion = Sequence(text=raw_completion["text"], logprob=sequence_logprob, tokens=tokens, hidden_states=hidden_states)
                completion = truncate_sequence(completion, request)
                completions.append(completion)
            requests_results.append(RequestResult(
                success=True,
                cached=False,
                request_time=response["request_time"],
                request_datetime=response.get("request_datetime"),
                completions=completions,
                embedding=[],
            ))
        return requests_results

    # def make_request(self, request: Request, **kwargs) -> RequestResult:
    #     # Embedding not supported for this model
    #     if request.embedding:
    #         return EMBEDDING_UNAVAILABLE_REQUEST_RESULT

    #     # Only a single stop sequence is supported as we can only pass in a single value for `eos_token_id`
    #     if len(request.stop_sequences) > 1:
    #         raise ValueError("More than one stop sequence is not supported.")

    #     raw_request = {
    #         "engine": request.model_engine,
    #         "prompt": request.prompt,
    #         "temperature": 1e-7 if request.temperature == 0 else request.temperature,
    #         "num_return_sequences": request.num_completions,
    #         "max_new_tokens": request.max_tokens,
    #         "top_p": request.top_p,
    #         "echo_prompt": request.echo_prompt,
    #         "top_k_per_token": request.top_k_per_token,
    #         "stop_sequences": request.stop_sequences,
    #         "output_hidden_states": kwargs.get("hidden_states", False)
    #     }

    #     # Get cached model server instance if possible (to save on model and tokenizer
    #     # loading times).
    #     model_server_instance: HuggingFaceServer = self.get_model_server_instance(request.model)

    #     try:

    #         def do_it():
    #             return model_server_instance.serve_request(raw_request)

    #         cache_key = Client.make_cache_key(raw_request, request)
    #         response, cached = self.cache.get(cache_key, wrap_request_time(do_it))
    #     except Exception as e:  # Do something if error is encountered.
    #         error: str = f"HuggingFace error: {e}"
    #         return RequestResult(success=False, cached=False, error=error, completions=[], embedding=[])

        
    #     completions = []
    #     for raw_completion in response["completions"]:
    #         sequence_logprob: float = 0
    #         tokens: List[Token] = []
    #         if request.echo_prompt:
    #             # Add prompt to list of generated tokens.
    #             generated_tokens = raw_completion["tokens"][response["input_length"] :]
    #             for token_text in raw_completion["tokens"][: response["input_length"]]:
    #                 tokens.append(Token(text=token_text, logprob=0.0, top_logprobs={}))
    #         else:
    #             generated_tokens = raw_completion["tokens"]

    #         # Compute logprob for the entire sequence.
    #         for token_text, logprob, top_logprobs_dict in zip(
    #             generated_tokens, raw_completion["logprobs"], raw_completion["top_logprobs_dicts"]
    #         ):
    #             tokens.append(Token(text=token_text, logprob=logprob, top_logprobs=top_logprobs_dict))
    #             sequence_logprob += logprob
            
    #         #modifying the hidden states so to keep only the last token and the avg of the tokens of the question
    #         hidden_states = raw_completion["hidden_states"]

    #         completion = Sequence(text=raw_completion["text"], logprob=sequence_logprob, tokens=tokens, hidden_states=hidden_states)
    #         completion = truncate_sequence(completion, request)
    #         completions.append(completion)

    #     return RequestResult(
    #         success=True,
    #         cached=cached,
    #         request_time=response["request_time"],
    #         request_datetime=response.get("request_datetime"),
    #         completions=completions,
    #         embedding=[],
    #     )

    def tokenize(self, request: TokenizationRequest) -> TokenizationRequestResult:
        tokenizer = HuggingFaceTokenizers.get_tokenizer(request.tokenizer)
        cache_key = asdict(request)

        try:

            def do_it():
                if request.encode:
                    if request.truncation:
                        tokens = tokenizer.encode(
                            request.text,
                            truncation=request.truncation,
                            max_length=request.max_length,
                            add_special_tokens=False,
                        )
                    else:
                        tokens = tokenizer.encode(request.text, add_special_tokens=False)
                else:
                    if "gpt" in request.tokenizer or request.tokenizer in [
                        "bigscience/bloom",
                        "Writer/palmyra-base",
                        "facebook/opt-66b",
                    ]:
                        # These models already handle the "▁" character correctly with the
                        # convert_tokens_to_string method. We prefer to use this method instead
                        # of the hacky cleanup_tokens method below as it might handle cases
                        # we haven't thought of in cleanup_tokens.
                        tokens = [
                            tokenizer.convert_tokens_to_string([token]) for token in tokenizer.tokenize(request.text)
                        ]
                    else:
                        # Tokenizes the text and returns the tokens as a list of strings,
                        # not a list of token objects (otherwise "Hello world" would be"
                        # ["Hello", "▁world"] and not ["Hello", " world"])
                        # We could do this with a simple replace like this:
                        # tokens = [tokenizer.convert_tokens_to_string([i]) for i in tokenizer.tokenize(request.text)]
                        # But this replaces all the "▁" characters by "", which is not what we want.
                        # This would be problematic as tokenize(" Hello", encode=False) would return ["Hello"]
                        # Just like tokenize("Hello", encode=False) would return ["Hello"].
                        tokens = tokenizer.tokenize(request.text)
                        tokens = cleanup_tokens(tokens, request.tokenizer)
                return {"tokens": tokens}

            result, cached = self.cache.get(cache_key, wrap_request_time(do_it))
        except Exception as e:
            error: str = f"HuggingFace error: {e}"
            return TokenizationRequestResult(success=False, cached=False, error=error, text="", tokens=[])

        return TokenizationRequestResult(
            success=True,
            cached=cached,
            text=request.text,
            tokens=[TokenizationToken(value) for value in result["tokens"]],
            request_time=result["request_time"],
        )

    def decode(self, request: DecodeRequest) -> DecodeRequestResult:
        tokenizer = HuggingFaceTokenizers.get_tokenizer(request.tokenizer)
        cache_key = asdict(request)

        try:

            def do_it():
                return {
                    "text": tokenizer.decode(
                        request.tokens, clean_up_tokenization_spaces=request.clean_up_tokenization_spaces
                    )
                }

            result, cached = self.cache.get(cache_key, wrap_request_time(do_it))
        except Exception as e:
            error: str = f"HuggingFace error: {e}"
            return DecodeRequestResult(success=False, cached=False, error=error, text="")

        return DecodeRequestResult(
            success=True, cached=cached, text=result["text"], request_time=result["request_time"]
        )

#
# Copyright 2023 The LLM-on-Ray Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

import re
import torch
from transformers import AutoTokenizer, StoppingCriteriaList
from typing import List, AsyncGenerator, Tuple, Union
from llm_on_ray.inference.inference_config import InferenceConfig, ModelGenerateResult
from llm_on_ray.inference.utils import StoppingCriteriaSub
from abc import ABC, abstractmethod

SinglePromptInput = str
MultiplePromptInput = List[str]
MllmPromptInput = Tuple[List[str], List[str]]  # (prompts, images)
GenerateInput = Union[SinglePromptInput, MultiplePromptInput, MllmPromptInput]
GenerateOutput = Union[ModelGenerateResult, List[ModelGenerateResult], None]


class Predictor(ABC):
    def __init__(self, infer_conf: InferenceConfig) -> None:
        self.infer_conf = infer_conf
        self.tokenizer = AutoTokenizer.from_pretrained(
            infer_conf.model_description.tokenizer_name_or_path,
            **infer_conf.model_description.config.dict(),
        )
        self.device = torch.device(infer_conf.device)
        # now deepspeed predictor don't have the model
        # so configure_tokenizer cannot be called
        # this should be solved in the next pr
        # where it is also a worker
        # This can be removed then
        if self.tokenizer.pad_token_id is None:
            self.tokenizer.pad_token_id = self.tokenizer.eos_token_id

        prompt = infer_conf.model_description.prompt
        stop_words = prompt.stop_words
        stop_words_ids = [
            self.tokenizer(stop_word, return_tensors="pt").input_ids.squeeze()
            for stop_word in stop_words
        ]
        self.stopping_criteria = StoppingCriteriaList([StoppingCriteriaSub(stops=stop_words_ids)])
        self.input_length = None

    def tokenize_inputs(self, text):
        input_tokens = self.tokenizer(text, return_tensors="pt", padding=True)
        input_ids = input_tokens.input_ids
        self.input_length = input_ids.size()[1]
        input_ids = input_ids.to(device=self.device)
        return input_ids, self.input_length

    def configure_tokenizer(self, model_name):
        model = self.model
        tokenizer = self.tokenizer
        if re.search("llama", model.config.architectures[0], re.IGNORECASE):
            # unwind broken decapoda-research config
            model.generation_config.pad_token_id = 0
            model.generation_config.bos_token_id = 1
            model.generation_config.eos_token_id = 2

        if (
            hasattr(model.generation_config, "pad_token_id")
            and model.generation_config.pad_token_id is not None
            and "chatglm" not in model_name
        ):
            tokenizer.pad_token_id = model.generation_config.pad_token_id
        if (
            hasattr(model.generation_config, "eos_token_id")
            and model.generation_config.eos_token_id is not None
            and "chatglm" not in model_name
        ):
            tokenizer.eos_token_id = model.generation_config.eos_token_id
        if (
            hasattr(model.generation_config, "bos_token_id")
            and model.generation_config.bos_token_id is not None
        ):
            tokenizer.bos_token_id = model.generation_config.bos_token_id

        if tokenizer.pad_token_id is None:
            model.generation_config.pad_token_id = tokenizer.pad_token_id = tokenizer.eos_token_id

        if model.generation_config.eos_token_id is None:
            model.generation_config.eos_token_id = tokenizer.eos_token_id

        if not model.config.is_encoder_decoder:
            tokenizer.padding_side = "left"

        if tokenizer.pad_token is None and tokenizer.pad_token_id is None:
            tokenizer.pad_token = tokenizer.eos_token
            model.generation_config.pad_token_id = model.generation_config.eos_token_id

    @abstractmethod
    def generate(
        self,
        input: GenerateInput,
        **config,
    ) -> GenerateOutput:
        pass

    async def generate_async(self, input: GenerateInput, **config) -> Union[str, List[str]]:
        pass

    # output is streamed into streamer
    def streaming_generate(self, prompt: str, streamer, **config) -> None:
        pass

    def get_streamer(self):
        pass

    async def stream_results(self, results_generator) -> AsyncGenerator[str, None]:
        pass

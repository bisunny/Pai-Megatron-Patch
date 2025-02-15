# Copyright (c) 2023 Alibaba PAI and Nvidia Megatron-LM Team.
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

import torch

from megatron import get_args
from megatron.core import tensor_parallel
from megatron.model.enums import AttnMaskType
from megatron.model.module import MegatronModule
from megatron.model.utils import init_method_normal
from megatron.model.utils import scaled_init_method_normal

from .language_model import get_language_model
from .language_model import parallel_lm_logits


def post_language_model_processing(lm_output, labels, logit_weights,
                                   parallel_output, fp16_lm_cross_entropy):
    """
    This function is used for post-processing the output of the language model.

    Args:
        lm_output: The language model output tensor of shape [sequence_length, batch_size, hidden_size].
        labels: The labels tensor of shape [batch_size, sequence_length].
        logit_weights: The logit weights tensor of shape [parallel_output_size, hidden_size].
        parallel_output: The parallel output tensor of shape [parallel_output_size, hidden_size].
        fp16_lm_cross_entropy: A flag indicating whether to use FP16 for the cross-entropy calculation.

    Returns:
        If the labels are None, the function returns the output tensor as is, transposed to shape [batch_size, sequence_length, hidden_size].
        If the labels are provided, the function calculates the cross-entropy loss and returns the loss tensor transposed to shape [batch_size, sequence_length].

    """

    # Output. Format [s b h]
    output = parallel_lm_logits(lm_output, logit_weights, parallel_output)

    if labels is None:
        # [s b h] => [b s h]
        return output.transpose(0, 1).contiguous()
    else:
        # [b s] => [s b]
        labels = labels.transpose(0, 1).contiguous()
        if fp16_lm_cross_entropy:
            assert output.dtype == torch.half
            loss = tensor_parallel.vocab_parallel_cross_entropy(
                output, labels)
        else:
            loss = tensor_parallel.vocab_parallel_cross_entropy(
                output.float(), labels)

        # [s b] => [b, s]
        loss = loss.transpose(0, 1).contiguous()
        return loss


class GPTModel(MegatronModule):
    """GPT-2 Language model."""
    def __init__(self,
                 num_tokentypes=0,
                 parallel_output=True,
                 pre_process=True,
                 post_process=True):
        """
        Initializes the GPTModel object.

        Args:
            num_tokentypes (int, optional): Number of token types. Defaults to 0.
            parallel_output (bool, optional): Whether to use parallel output. Defaults to True.
            pre_process (bool, optional): Whether to perform pre-processing. Defaults to True.
            post_process (bool, optional): Whether to perform post-processing. Defaults to True.
        """
        args = get_args()
        super(GPTModel, self).__init__(
            share_word_embeddings=not args.untie_embeddings_and_output_weights)

        self.parallel_output = parallel_output
        self.pre_process = pre_process
        self.post_process = post_process
        self.fp16_lm_cross_entropy = args.fp16_lm_cross_entropy
        self.untie_embeddings_and_output_weights =\
            args.untie_embeddings_and_output_weights

        self.language_model, self._language_model_key = get_language_model(
            num_tokentypes=num_tokentypes,
            add_pooler=False,
            encoder_attn_mask_type=AttnMaskType.causal,
            init_method=init_method_normal(args.init_method_std),
            scaled_init_method=scaled_init_method_normal(
                args.init_method_std, args.num_layers),
            pre_process=self.pre_process,
            post_process=self.post_process)

        if not args.untie_embeddings_and_output_weights:
            self.initialize_word_embeddings(init_method_normal)

    def set_input_tensor(self, input_tensor):
        """See megatron.model.transformer.set_input_tensor()"""
        self.language_model.set_input_tensor(input_tensor)

    def forward(self,
                input_ids,
                position_ids=None,
                attention_mask=None,
                labels=None,
                inference_params=None):
        """
        Performs forward pass computation of the language model.

        Args:
            input_ids (Tensor): Input tensor representing the token ids.
            position_ids (Tensor, optional): Input tensor representing the position ids. Defaults to None.
            attention_mask (Tensor, optional): Input tensor representing the attention mask. Defaults to None.
            labels (Tensor, optional): Input tensor representing the labels. Defaults to None.
            inference_params (dict, optional): Additional parameters for inference. Defaults to None.

        Returns:
            Tensor: Output of the language model.
        """

        lm_output = self.language_model(input_ids,
                                        position_ids,
                                        attention_mask,
                                        inference_params=inference_params)

        if self.post_process:
            return post_language_model_processing(
                lm_output, labels, self.language_model.output_layer.weight
                if self.untie_embeddings_and_output_weights else
                self.word_embeddings_weight(), self.parallel_output,
                self.fp16_lm_cross_entropy)
        else:
            return lm_output

    def state_dict_for_save_checkpoint(self, prefix='', keep_vars=False):
        """
        Returns a dictionary containing the state of the model for checkpoint saving.

        Args:
            prefix (str, optional): Prefix to prepend to the state_dict keys. Defaults to ''.
            keep_vars (bool, optional): Whether to keep variables in the state_dict. Defaults to False.

        Returns:
            dict: State dictionary of the model.
        """
        state_dict_ = {}
        state_dict_[self._language_model_key] \
            = self.language_model.state_dict_for_save_checkpoint(
                prefix=prefix, keep_vars=keep_vars)
        # Save word_embeddings.
        if self.post_process and not\
                self.pre_process and not\
                self.untie_embeddings_and_output_weights:
            state_dict_[self._word_embeddings_for_head_key] \
                = self.word_embeddings.state_dict(prefix=prefix,
                                                  keep_vars=keep_vars)
        return state_dict_

    def load_state_dict(self, state_dict, strict=True):
        """
        Customized load.
        Args:
            state_dict (dict): State dictionary containing the model state.
            strict (bool, optional): Whether to strictly enforce that the keys in the state_dict match the keys returned by the model's state_dict() function. Defaults to True.
        """

        # Load word_embeddings.
        if self.post_process and not\
                self.pre_process and not\
                self.untie_embeddings_and_output_weights:
            self.word_embeddings.load_state_dict(
                state_dict[self._word_embeddings_for_head_key], strict=strict)
        if self._language_model_key in state_dict:
            state_dict = state_dict[self._language_model_key]
        self.language_model.load_state_dict(state_dict, strict=strict)

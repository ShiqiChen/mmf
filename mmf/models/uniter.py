# Copyright (c) Facebook, Inc. and its affiliates.

# Initial version was taken from https://github.com/ChenRocks/UNITER/
# and adapted for MMF.

from dataclasses import asdict, dataclass, field
from typing import Any

import torch
from mmf.utils.general import retry_n
from omegaconf import OmegaConf
from torch import nn
from transformers.modeling_bert import BertConfig, BertEmbeddings, BertModel, BertPooler


class UniterImageEmbeddings(nn.Module):
    """
    Image Embeddings used by UNITER.
    Code modified from https://github.com/ChenRocks/UNITER/blob/master/model/model.py
    Performs a linear projection then normalization over image and position features.
    """

    @dataclass
    class Config:
        img_dim: int = 2048
        hidden_size: int = 768
        eps: float = 1e-12
        hidden_dropout_prob: float = 0
        pos_dim: int = 7

    def __init__(self, config: Config, *args, **kwargs):
        super().__init__()
        config = OmegaConf.create({**asdict(self.Config()), **config})

        self.img_linear = nn.Linear(config.img_dim, config.hidden_size)
        self.img_layer_norm = nn.LayerNorm(config.hidden_size, eps=config.eps)
        self.pos_layer_norm = nn.LayerNorm(config.hidden_size, eps=config.eps)
        self.pos_linear = nn.Linear(config.pos_dim, config.hidden_size)
        self.mask_embedding = nn.Embedding(2, config.img_dim, padding_idx=0)

        self.final_layer_norm = nn.LayerNorm(config.hidden_size, eps=config.eps)
        self.dropout = nn.Dropout(config.hidden_dropout_prob)

    def forward(self, img_feat, img_pos_feat, type_embeddings, img_masks=None):
        if img_masks is not None:
            self.mask_embedding.weight.data[0, :].fill_(0)
            mask = self.mask_embedding(img_masks.long())
            img_feat = img_feat + mask

        transformed_im = self.img_layer_norm(self.img_linear(img_feat))
        transformed_pos = self.pos_layer_norm(self.pos_linear(img_pos_feat))
        embeddings = transformed_im + transformed_pos + type_embeddings
        embeddings = self.final_layer_norm(embeddings)
        embeddings = self.dropout(embeddings)
        return embeddings


class UniterModelBase(nn.Module):
    """ Modification for Joint Vision-Language Encoding
    """

    @dataclass
    class TextEmbeddingConfig:
        vocab_size: int = 30522
        hidden_size: int = 768
        max_position_embeddings: int = 512
        eps: float = 1e-12
        hidden_dropout_prob: float = 0
        pad_token_id: int = 0
        type_vocab_size: int = 2

    @dataclass
    class Config:
        hidden_size: int = 768
        eps: float = 1e-12
        hidden_dropout_prob: float = 0
        random_init: bool = False
        bert_model_name: str = "bert-base-uncased"
        text_embeddings: Any = field(default_factory=lambda: {})
        image_embeddings: UniterImageEmbeddings.Config = UniterImageEmbeddings.Config()
        encoder: Any = field(default_factory=lambda: {})

    def __init__(self, config):
        super().__init__()
        self.config = config = OmegaConf.create({**asdict(self.Config()), **config})

        text_embedding_config = OmegaConf.create(
            {**asdict(self.TextEmbeddingConfig()), **config.text_embeddings}
        )
        bert_config = BertConfig.from_pretrained(config.bert_model_name)
        bert_config.update(text_embedding_config)
        self.embeddings = BertEmbeddings(bert_config)

        self.img_embeddings = UniterImageEmbeddings(config.image_embeddings)

        bert_model_name = config["bert_model_name"]
        hf_config = retry_n(
            6,
            BertConfig.from_pretrained,
            bert_model_name,
            **OmegaConf.to_container(config.encoder),
        )
        hf_config.update(config.encoder)
        if config["random_init"]:
            self.encoder = BertModel(hf_config).encoder
        else:
            self.encoder = retry_n(
                6, BertModel.from_pretrained, bert_model_name, config=hf_config
            ).encoder

        self.pooler = BertPooler(config)

    def _compute_txt_embeddings(self, input_ids, position_ids, token_type_ids=None):
        output = self.embeddings(
            input_ids=input_ids,
            position_ids=position_ids,
            token_type_ids=token_type_ids,
        )
        return output

    def _compute_img_embeddings(
        self, img_feat, img_pos_feat, img_masks=None, img_type_ids=None
    ):
        if img_type_ids is None:
            img_type_ids = torch.ones_like(img_feat[:, :, 0].long())
        img_type_embeddings = self.embeddings.token_type_embeddings(img_type_ids)
        output = self.img_embeddings(
            img_feat, img_pos_feat, img_type_embeddings, img_masks
        )
        return output

    def _compute_img_txt_embeddings(
        self,
        input_ids,
        position_ids,
        img_feat,
        img_pos_feat,
        gather_index,
        img_masks=None,
        txt_type_ids=None,
        img_type_ids=None,
    ):
        txt_emb = self._compute_txt_embeddings(input_ids, position_ids, txt_type_ids)
        img_emb = self._compute_img_embeddings(
            img_feat, img_pos_feat, img_masks, img_type_ids
        )
        # be ok with embeddings with padding
        # TODO: add gather_index and require less work
        # # align back to most compact input
        # gather_index = gather_index.unsqueeze(-1).expand(
        #     -1, -1, self.config.hidden_size
        # )
        # embedding_output = torch.gather(
        #     torch.cat([txt_emb, img_emb], dim=1), dim=1, index=gather_index
        # )
        embedding_output = torch.cat([txt_emb, img_emb], dim=1)
        return embedding_output

    def forward(
        self,
        input_ids,
        position_ids,
        img_feat,
        img_pos_feat,
        attention_mask,
        gather_index=None,
        img_masks=None,
        output_hidden_states=False,
        txt_type_ids=None,
        img_type_ids=None,
    ):
        # compute self-attention mask
        extended_attention_mask = attention_mask.unsqueeze(1).unsqueeze(2)
        extended_attention_mask = extended_attention_mask.to(
            dtype=next(self.parameters()).dtype
        )  # fp16 compatibility
        extended_attention_mask = (1.0 - extended_attention_mask) * -10000.0

        # embedding layer
        if input_ids is None:
            # image only
            embedding_output = self._compute_img_embeddings(
                img_feat, img_pos_feat, img_masks, img_type_ids
            )
        elif img_feat is None:
            # text only
            embedding_output = self._compute_txt_embeddings(
                input_ids, position_ids, txt_type_ids
            )
        else:
            embedding_output = self._compute_img_txt_embeddings(
                input_ids,
                position_ids,
                img_feat,
                img_pos_feat,
                gather_index,
                img_masks,
                txt_type_ids,
                img_type_ids,
            )

        encoded_layers = self.encoder(
            embedding_output,
            attention_mask=extended_attention_mask,
            output_hidden_states=output_hidden_states,
        )
        if not output_hidden_states:
            encoded_layers = encoded_layers[-1]
        return encoded_layers
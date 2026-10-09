"""bge-m3 embeddings on CPU: one dense vector and one sparse (lexical) vector per text.

Hand-written instead of FlagEmbedding (which pulls in datasets/accelerate/peft). It follows
FlagEmbedding's BGEM3 inference exactly:
- dense: the [CLS] hidden state, L2-normalised (1024 dims);
- sparse: relu(sparse_linear(hidden_state)) per token, special tokens (cls, eos, pad, unk)
  dropped, the maximum weight kept per token id, only weights > 0.
tests/test_embedder_parity.py checks this against reference outputs that FlagEmbedding
produced (tests/fixtures/bge_m3_reference.json), with the tolerances in docs/DECISIONS-phase3a.md.

The model revision is pinned. `fingerprint()` covers everything that changes the vectors
(model, revision, dims, max_length, sparse settings); the indexer stores it per point so a
change forces re-embedding instead of keeping stale vectors.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field

MODEL_ID = "BAAI/bge-m3"
MODEL_REVISION = "5617a9f61b028005a4858fdac845db406aefb181"
MODEL_FILES = ["config.json", "pytorch_model.bin", "sentencepiece.bpe.model", "special_tokens_map.json",
               "tokenizer.json", "tokenizer_config.json", "sparse_linear.pt"]


@dataclass(frozen=True)
class EmbeddingConfig:
    model_id: str = MODEL_ID
    revision: str = MODEL_REVISION
    dense_dim: int = 1024
    dense_pooling: str = "cls"
    dense_normalized: bool = True
    max_length: int = 512
    sparse_activation: str = "relu"
    sparse_pooling: str = "max_per_token_id"
    sparse_drop_special_tokens: bool = True
    dtype: str = "float32"

    def fingerprint(self) -> str:
        """Stable hash of every setting that affects the vectors."""
        canonical = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()[:16]


@dataclass
class Embedding:
    dense: list[float]
    sparse: dict[int, float] = field(default_factory=dict)  # token id -> weight


class BgeM3Embedder:
    """Loads the pinned model once (about 0.7 GiB at rest, about 2.2 GiB peak while encoding)."""

    def __init__(self, config: EmbeddingConfig = EmbeddingConfig(), batch_size: int = 32) -> None:
        import torch
        from huggingface_hub import hf_hub_download
        from transformers import AutoModel, AutoTokenizer

        self.config = config
        self.batch_size = batch_size
        self._torch = torch

        def fetch(name: str) -> str:
            try:  # cached files first: no network on every load
                return hf_hub_download(config.model_id, name, revision=config.revision, local_files_only=True)
            except Exception:
                return hf_hub_download(config.model_id, name, revision=config.revision)

        paths = {name: fetch(name) for name in MODEL_FILES}
        model_dir = paths["config.json"].rsplit("/", 1)[0]
        self.tokenizer = AutoTokenizer.from_pretrained(model_dir)
        self.model = AutoModel.from_pretrained(model_dir, dtype=getattr(torch, config.dtype)).eval()
        hidden = self.model.config.hidden_size
        if hidden != config.dense_dim:
            raise ValueError(f"model hidden size {hidden} != configured dense_dim {config.dense_dim}")
        self.sparse_linear = torch.nn.Linear(hidden, 1)
        self.sparse_linear.load_state_dict(torch.load(paths["sparse_linear.pt"], map_location="cpu"))
        self.sparse_linear.eval()
        tok = self.tokenizer
        self._special = {tok.cls_token_id, tok.eos_token_id, tok.pad_token_id, tok.unk_token_id}

    def encode(self, texts: list[str]) -> list[Embedding]:
        out: list[Embedding] = []
        for start in range(0, len(texts), self.batch_size):
            out.extend(self._encode_batch(texts[start : start + self.batch_size]))
        return out

    def encode_query(self, text: str) -> Embedding:
        return self._encode_batch([text])[0]  # bge-m3 uses no query instruction

    def _encode_batch(self, texts: list[str]) -> list[Embedding]:
        torch = self._torch
        with torch.inference_mode():
            enc = self.tokenizer(texts, padding=True, truncation=True, max_length=self.config.max_length,
                                 return_tensors="pt")
            hidden = self.model(**enc).last_hidden_state
            dense = torch.nn.functional.normalize(hidden[:, 0], dim=-1)
            weights = torch.relu(self.sparse_linear(hidden)).squeeze(-1)
        results = []
        for ids, token_weights, vector in zip(enc["input_ids"].tolist(), weights.tolist(), dense.tolist()):
            sparse: dict[int, float] = {}
            for token_id, weight in zip(ids, token_weights):
                if token_id in self._special or weight <= 0:
                    continue
                if weight > sparse.get(token_id, 0.0):
                    sparse[token_id] = weight
            results.append(Embedding(dense=vector, sparse=sparse))
        return results

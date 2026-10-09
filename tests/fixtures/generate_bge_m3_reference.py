"""Generate tests/fixtures/bge_m3_reference.json with FlagEmbedding (the reference implementation).

NOT part of the project environment: FlagEmbedding is deliberately not a dependency. Run it in a
separate venv that has FlagEmbedding, torch (CPU) and transformers, e.g.:

    ~/.cache/shopmate-bench/venv/bin/python tests/fixtures/generate_bge_m3_reference.py

The texts are written for this fixture (not taken from the golden set). The output records the
library versions and the pinned model revision; tests/test_embedder_parity.py compares the
project's hand-written embedder (app/retrieval/embedder.py) against it.
"""

from __future__ import annotations

import json
import platform
from importlib.metadata import version
from pathlib import Path

from FlagEmbedding import BGEM3FlagModel
from huggingface_hub import snapshot_download

MODEL_ID = "BAAI/bge-m3"
REVISION = "5617a9f61b028005a4858fdac845db406aefb181"  # must equal app/retrieval/embedder.py
MAX_LENGTH = 512
OUT = Path(__file__).with_name("bge_m3_reference.json")

LONG = (
    "Áo khoác gió nam hai lớp được may từ vải dù chống thấm nhẹ, lớp lót lưới thoáng khí giúp "
    "người mặc không bị bí khi di chuyển dưới trời nắng hoặc mưa phùn. Mũ trùm có dây rút điều "
    "chỉnh, tay áo bo chun, túi khóa kéo hai bên sâu, đựng vừa điện thoại và ví. "
)
TEXTS = {
    "co_dau": "Áo sơ mi nam tay dài màu trắng, chất liệu cotton thoáng mát, phù hợp đi làm.",
    "khong_dau": "ao so mi nam tay dai mau trang chat lieu cotton thoang mat",
    "teencode": "shop ơi có áo khoác nữ size M ko, bn tiền v ạ",
    "policy": "Điều 2. Thời hạn đổi trả: đổi kích cỡ hoặc màu sắc trong vòng 7 ngày kể từ ngày nhận hàng.",
    "short": "giày chạy bộ",
    "mixed": "sneaker trắng đế độn size 38-39 freeship",
    "long_truncated": LONG * 12,  # well over 512 tokens: exercises truncation
}


def main() -> None:
    path = snapshot_download(MODEL_ID, revision=REVISION,
                             allow_patterns=["*.json", "*.model", "pytorch_model.bin", "*.pt"])
    model = BGEM3FlagModel(path, use_fp16=False, devices="cpu")
    names = list(TEXTS)
    out = model.encode([TEXTS[n] for n in names], batch_size=4, max_length=MAX_LENGTH,
                       return_dense=True, return_sparse=True, return_colbert_vecs=False)
    token_counts = [len(model.tokenizer(TEXTS[n], truncation=False)["input_ids"]) for n in names]
    reference = {
        "generator": "FlagEmbedding BGEM3FlagModel.encode (use_fp16=False, cpu)",
        "versions": {p: version(p) for p in ("FlagEmbedding", "torch", "transformers")} | {"python": platform.python_version()},
        "model_id": MODEL_ID,
        "revision": REVISION,
        "max_length": MAX_LENGTH,
        "items": [
            {
                "name": name,
                "text": TEXTS[name],
                "untruncated_tokens": n_tokens,
                "dense": [round(float(x), 7) for x in dense],
                "sparse": {str(k): round(float(v), 7) for k, v in sorted(sparse.items(), key=lambda kv: int(kv[0]))},
            }
            for name, n_tokens, dense, sparse in zip(names, token_counts, out["dense_vecs"], out["lexical_weights"])
        ],
    }
    OUT.write_text(json.dumps(reference, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"wrote {OUT}: {len(names)} texts, versions {reference['versions']}")


if __name__ == "__main__":
    main()

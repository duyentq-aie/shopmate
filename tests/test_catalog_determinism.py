"""The catalog generator must be byte-for-byte deterministic for a given seed."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from app.catalog.schema import CATALOG_PATH, Catalog, load_catalog
from app.config import REPO_ROOT
from scripts.generate_catalog import DEFAULT_SEED, build_catalog, render


def _run_generator(out, hash_seed: str) -> bytes:
    # Separate processes with different PYTHONHASHSEED values catch any accidental
    # dependence on set/dict hash ordering.
    env = {**os.environ, "PYTHONHASHSEED": hash_seed}
    subprocess.run(
        [sys.executable, "-m", "scripts.generate_catalog", "--out", str(out)],
        cwd=REPO_ROOT, env=env, check=True, capture_output=True,
    )
    return out.read_bytes()


def test_two_runs_are_byte_identical_and_match_committed_file(tmp_path):
    first = _run_generator(tmp_path / "a.json", "1")
    second = _run_generator(tmp_path / "b.json", "12345")
    assert first == second
    assert first == CATALOG_PATH.read_bytes(), "data/seed/catalog.vi.json is stale; regenerate it"


def test_in_process_render_matches_file():
    assert render(build_catalog(DEFAULT_SEED)).encode("utf-8") == CATALOG_PATH.read_bytes()


def test_different_seed_gives_different_catalog():
    assert render(build_catalog(DEFAULT_SEED + 1)) != render(build_catalog(DEFAULT_SEED))


def test_committed_catalog_shape():
    catalog = load_catalog()
    assert 240 <= len(catalog.products) <= 260
    assert 5 <= len(catalog.categories) <= 6
    out_of_stock = [p for p in catalog.products if p.stock == 0]
    assert 0.08 <= len(out_of_stock) / len(catalog.products) <= 0.12
    assert all(p.price % 1000 == 0 for p in catalog.products)
    assert catalog.synthetic is True


def test_schema_rejects_bad_product():
    data = load_catalog().model_dump(mode="json")
    data["products"][0]["price"] = 123_456  # not rounded to 1,000
    with pytest.raises(ValueError):
        Catalog.model_validate(data)
    data = load_catalog().model_dump(mode="json")
    data["products"][0]["sizes"] = ["L", "M"]  # not canonical order
    with pytest.raises(ValueError):
        Catalog.model_validate(data)

"""The dev perturbation set for Phase 4 experiment 1: eval/perturbation/dev_folded.json.

Every answerable dev retrieval item (product_search with SKUs, policy_question with a clause) with
its question passed through app.retrieval.folding.fold and the gold left unchanged. It measures how
retrieval copes when diacritics are missing, on enough items (33) for a paired bootstrap to mean
something, which the 6 real khong_dau items cannot.

Limitation, also written into the file: machine-stripped text is not how real users type. It has no
typos and no abbreviations beyond those already in the original question, so this is a robustness
test of diacritic handling, not a substitute for real queries.

Built deterministically from the DEV split only (the guarded loader); the file carries the hash of
its content and of the dev items it came from, and load_dev_folded refuses a file that was edited or
that no longer matches the dev items.

Usage (from the repo root):
    uv run python -m eval.perturbation.dev_folded           # write eval/perturbation/dev_folded.json
    uv run python -m eval.perturbation.dev_folded --check   # exit 1 if the committed file is stale
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from app.config import REPO_ROOT
from app.retrieval.folding import FOLDING_VERSION, fold
from eval.golden.loader import items_hash, load_split
from eval.golden.schema import GoldenItem

PERTURBATION_PATH = REPO_ROOT / "eval" / "perturbation" / "dev_folded.json"
RETRIEVAL_INTENTS = ("product_search", "policy_question")
ANSWERABLE_SOURCES = ("skus", "policy_clause")
LIMITATION = ("Machine-stripped text is not how real users type: it has no typos and no abbreviations beyond "
              "those already in the original question. This set is a robustness test of diacritic handling, "
              "not a substitute for real queries.")


class PerturbationError(Exception):
    """The perturbation file is edited, stale or inconsistent with the dev items."""


def _selected(items: list[GoldenItem]) -> list[GoldenItem]:
    not_dev = [i.id for i in items if i.split != "dev"]
    if not_dev:
        raise ValueError(f"the perturbation set is built from dev items only; not dev: {not_dev}")
    return sorted((i for i in items if i.intent in RETRIEVAL_INTENTS and i.gold_source.type in ANSWERABLE_SOURCES),
                  key=lambda i: i.id)


def content_sha256(data: dict) -> str:
    canonical = json.dumps({k: v for k, v in data.items() if k != "sha256"}, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_dev_folded(items: list[GoldenItem]) -> dict:
    selected = _selected(items)
    entries = [{"id": i.id, "intent": i.intent, "query_style": i.query_style,
                "gold_source": i.gold_source.model_dump(mode="json"),
                "original_question": i.question, "question": fold(i.question)} for i in selected]
    data = {
        "description": "Dev retrieval items with the question passed through app.retrieval.folding.fold; gold unchanged.",
        "limitation": LIMITATION,
        "source": {"split": "dev", "intents": list(RETRIEVAL_INTENTS), "gold_types": list(ANSWERABLE_SOURCES),
                   "items_sha256": items_hash(selected), "folding_version": FOLDING_VERSION},
        "n_items": len(entries),
        "n_changed": sum(e["question"] != e["original_question"] for e in entries),
        "items": entries,
    }
    data["sha256"] = content_sha256(data)
    return data


def load_dev_folded(dev_items: list[GoldenItem], path: Path = PERTURBATION_PATH) -> tuple[list[GoldenItem], str]:
    """The perturbed items (dev items with the folded question) and the file's content hash.

    Raises PerturbationError if the file's hash does not match its content, if the dev items it was
    built from are not the current ones, or if a folded question is not fold(original)."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("sha256") != content_sha256(data):
        raise PerturbationError(f"{path.name}: content hash does not match the file (edited?)")
    selected = _selected(dev_items)
    if data["source"]["items_sha256"] != items_hash(selected):
        raise PerturbationError(f"{path.name}: its source dev items differ from the current ones; rebuild it")
    by_id = {e["id"]: e for e in data["items"]}
    if sorted(by_id) != [i.id for i in selected]:
        raise PerturbationError(f"{path.name}: item ids differ from the current dev items")
    perturbed = []
    for item in selected:
        if by_id[item.id]["question"] != fold(item.question):
            raise PerturbationError(f"{item.id}: folded question is not fold(original question)")
        perturbed.append(item.model_copy(update={"question": by_id[item.id]["question"]}))
    return perturbed, data["sha256"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="exit 1 if the committed file differs")
    args = parser.parse_args(argv)
    built = build_dev_folded(load_split("dev"))
    text = json.dumps(built, ensure_ascii=False, indent=2) + "\n"
    if args.check:
        current = PERTURBATION_PATH.read_text(encoding="utf-8") if PERTURBATION_PATH.is_file() else ""
        if current != text:
            print(f"STALE: {PERTURBATION_PATH.relative_to(REPO_ROOT)} differs from a fresh build", file=sys.stderr)
            return 1
        print(f"{PERTURBATION_PATH.name} is current ({built['n_items']} items, sha256={built['sha256'][:12]})")
        return 0
    PERTURBATION_PATH.write_text(text, encoding="utf-8")
    print(f"{PERTURBATION_PATH.name}: {built['n_items']} items ({built['n_changed']} changed), sha256={built['sha256']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

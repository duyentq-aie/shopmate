"""Push the committed DEV results to Langfuse as a dataset and one experiment run (docs/DECISIONS-langfuse.md, L8-L9).

    uv run python -m eval.push_langfuse_dev            # dry run (the default): reads, checks and prints what would be sent; no client, no network
    uv run python -m eval.push_langfuse_dev --push     # sends: needs LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY (Langfuse commands only, no Gemini call)

What is sent: the dataset `shopmate-dev` (one item per dev question: the question through `mask`, the expected intent and gold) and one run over it that REPLAYS
the answers recorded in eval/results/phase8_dev/answer_v2.json, so no model is called. Three scores per item, computed from the recorded run:
  answer_correct  BOOLEAN  the Phase 6 score of the item (reply names a gold SKU / cites the gold clause / behaves as the golden label says)
  router          BOOLEAN  the first turn was routed to the gold intent
  top5_first_hit_rr  NUMERIC  reciprocal rank of the first gold SKU (or clause) among what the agent retrieved (its top 5); only items that have a gold SKU
                           or clause. It is the rank in the agent's own top-5 of the dev run, NOT the pre-registered `product_mrr` / `policy_mrr` of Phase 8.
Order and ticket items are withheld: their question, reply and gold are replaced by "[withheld: <intent>, N ký tự]".

What it will not do: it reads golden items only through load_split("dev"), accepts only a file directly inside eval/results/phase8_dev/ whose provenance says
"dev only" and whose dev-split hash is the current one, and has no option that names another split. The test split and the test results are out of its reach."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.config import REPO_ROOT
from app.observability.factory import connect
from app.observability.masking import clean_deep, clean_text
from eval.golden.loader import items_hash, load_split
from eval.retrieval_metrics import first_rank

RESULTS_DIR = REPO_ROOT / "eval" / "results" / "phase8_dev"
DEFAULT_RESULT = RESULTS_DIR / "answer_v2.json"
DATASET_NAME = "shopmate-dev"
WITHHELD_INTENTS = frozenset({"order_status", "create_ticket"})


class Refused(Exception):
    """The file or the state is not one this script may push."""


def canonical_sha256(data: dict) -> str:
    """The same hash the runners write as `deterministic_sha256` (eval/run_retrieval_eval.py; a test keeps them equal)."""
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def load_result(path: Path, dev_items, *, dev_hash: str) -> dict:
    path = Path(path).resolve()
    if path.parent != RESULTS_DIR.resolve():
        raise Refused(f"{path} is not directly inside eval/results/phase8_dev/: only the dev results of Phase 8 can be pushed")
    result = json.loads(path.read_text(encoding="utf-8"))
    deterministic = result["deterministic"]
    provenance = deterministic["provenance"]
    if provenance.get("golden_items_read") != "dev only":
        raise Refused(f"the result says it read {provenance.get('golden_items_read')!r}, not 'dev only'")
    if provenance.get("dev_split_sha256") != dev_hash:
        raise Refused("the result was made on another dev split than the current one (dev_split_sha256 differs)")
    if result.get("deterministic_sha256") != canonical_sha256(deterministic):
        raise Refused("deterministic_sha256 does not match the numbers in the file: it was edited")
    in_result, in_dev = {i["id"] for i in deterministic["items"]}, {i.id for i in dev_items}
    if in_result != in_dev:
        raise Refused(f"the result and the dev split have different items: only in the result {sorted(in_result - in_dev)}, only in dev {sorted(in_dev - in_result)}")
    return result


@dataclass
class Record:
    golden_id: str
    id: str
    input: dict
    expected_output: dict
    metadata: dict
    output: dict = field(default_factory=dict)


def _gold(item) -> tuple[dict, list[str] | None]:
    source = item.gold_source
    if item.intent == "product_search":
        return ({"skus": list(source.skus)}, list(source.skus)) if source.type == "skus" else ({"type": "no_answer"}, None)
    if item.intent == "policy_question":
        return {"document": source.document, "clause": int(source.clause)}, [f"{source.document}#{int(source.clause)}"]
    if item.intent in WITHHELD_INTENTS:
        return {"type": "order"}, None
    return {"type": getattr(source, "type", None)}, None


def build_records(result: dict, dev_items, *, secrets: list[str] | tuple[str, ...] = ()) -> list[Record]:
    deterministic = result["deterministic"]
    provenance = deterministic["provenance"]
    recorded = {i["id"]: i for i in deterministic["items"]}
    prompt = provenance.get("answer_prompt", {}).get("name", "answer")
    records = []
    for item in sorted(dev_items, key=lambda i: i.id):
        done = recorded[item.id]
        withheld = item.intent in WITHHELD_INTENTS
        question, reply = item.question, done["reply"]
        if withheld:
            question, reply = f"[withheld: {item.intent}, {len(question)} ký tự]", f"[withheld: {item.intent}, {len(reply)} ký tự]"
        gold, gold_keys = _gold(item)
        records.append(Record(
            golden_id=item.id, id=f"shopmate-dev-{item.id}",
            input=clean_deep({"question": question}, secrets),
            expected_output={"intent": item.intent, "gold": gold, "gold_keys": gold_keys},
            metadata={"golden_id": item.id, "intent": item.intent, "split": "dev", "answer_prompt": prompt, "source_commit": provenance.get("git_commit"),
                      "withheld": withheld},
            output={"reply": clean_text(reply, secrets), "routed_to": done["routed_to"], "correct": bool(done["correct"]), "check": done.get("check"),
                    "llm_calls": done.get("llm_calls"), "steps": done.get("steps"),
                    "retrieved_keys": [str(s) for s in done["retrieved_skus"]] + [f"{doc}#{int(clause)}" for doc, clause in done["retrieved_clauses"]]}))
    return records


# --- the three scores (functions of the recorded run only) ------------------------------------------------------------------------------


def _correct(output: dict) -> bool:
    return bool(output["correct"])


def _routed(output: dict, expected_output: dict) -> bool:
    return output["routed_to"] == expected_output["intent"]


def _reciprocal_rank(output: dict, expected_output: dict) -> float | None:
    keys = expected_output.get("gold_keys")
    if not keys:
        return None
    rank = first_rank(output["retrieved_keys"], set(keys))
    return 1.0 / rank if rank else 0.0


def answer_correct(*, input, output, expected_output=None, metadata=None, **kwargs):  # noqa: A002 - the names the SDK passes
    from langfuse import Evaluation

    return Evaluation(name="answer_correct", value=int(_correct(output)), data_type="BOOLEAN", comment=output.get("check"))


def router(*, input, output, expected_output=None, metadata=None, **kwargs):  # noqa: A002
    from langfuse import Evaluation

    return Evaluation(name="router", value=int(_routed(output, expected_output)), data_type="BOOLEAN", comment=f"routed to {output['routed_to']}, gold {expected_output['intent']}")


def top5_first_hit_rr(*, input, output, expected_output=None, metadata=None, **kwargs):  # noqa: A002
    from langfuse import Evaluation

    value = _reciprocal_rank(output, expected_output)
    return None if value is None else Evaluation(name="top5_first_hit_rr", value=value, data_type="NUMERIC", comment="1 / rank of the first gold among the agent's top-5 retrieved, dev run (not the pre-registered product_mrr / policy_mrr)")


def summarise(records: list[Record]) -> dict:
    ranks = [r for r in (_reciprocal_rank(x.output, x.expected_output) for x in records) if r is not None]
    n = len(records)
    return {"items": n, "withheld": sum(1 for r in records if r.metadata["withheld"]), "top5_first_hit_rr_items": len(ranks),
            "answer_correct": sum(_correct(r.output) for r in records) / n, "router": sum(_routed(r.output, r.expected_output) for r in records) / n,
            "top5_first_hit_rr": sum(ranks) / len(ranks) if ranks else None}


def run_push(result: dict, dev_items, *, connection, secrets) -> int:
    records = build_records(result, dev_items, secrets=secrets)
    provenance = result["deterministic"]["provenance"]
    client = connection.client
    client.create_dataset(name=DATASET_NAME, description="The ShopMate golden DEV questions with their gold labels (never the test split).",
                          metadata={"split": "dev", "dev_split_sha256": provenance["dev_split_sha256"]})
    for record in records:
        client.create_dataset_item(dataset_name=DATASET_NAME, id=record.id, input=record.input, expected_output=record.expected_output, metadata=record.metadata)
    by_golden_id = {r.golden_id: r for r in records}

    def replay(*, item, **kwargs):
        return by_golden_id[item.metadata["golden_id"]].output  # what the dev run recorded: no model is called

    prompt = provenance.get("answer_prompt", {}).get("name", "answer")
    outcome = client.get_dataset(DATASET_NAME).run_experiment(
        name=f"shopmate-dev {prompt} (replay of the committed dev run)", run_name=f"{prompt}@{result['deterministic_sha256'][:12]}",
        description="Replay of eval/results/phase8_dev; the scores are computed from the recorded run.", task=replay, evaluators=[answer_correct, router, top5_first_hit_rr],
        metadata={"split": "dev", "answer_prompt": prompt, "source_commit": str(provenance.get("git_commit")), "replayed": "true"})
    client.flush()
    print(json.dumps({"mode": "push", **summarise(records), "dataset_run_url": getattr(outcome, "dataset_run_url", None)}, ensure_ascii=False, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--results", default=str(DEFAULT_RESULT), help="a result file directly inside eval/results/phase8_dev/")
    parser.add_argument("--push", action="store_true", help="send to Langfuse (default: dry run, nothing is sent)")
    args = parser.parse_args(argv)
    dev_items = sorted(load_split("dev"), key=lambda i: i.id)
    try:
        result = load_result(Path(args.results), dev_items, dev_hash=items_hash(dev_items))
    except Refused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1
    if not args.push:
        records = build_records(result, dev_items)
        print(json.dumps({"mode": "dry-run", **summarise(records), "sample": [vars(r) for r in records[:2]]}, ensure_ascii=False, indent=2))
        return 0
    connection = connect()
    if connection is None:
        print("REFUSED: Langfuse is not configured: set LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY (environment or .env) and run again", file=sys.stderr)
        return 1
    return run_push(result, dev_items, connection=connection, secrets=connection.secrets)


if __name__ == "__main__":
    raise SystemExit(main())

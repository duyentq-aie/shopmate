"""eval/push_langfuse_dev.py (docs/DECISIONS-langfuse.md, L8-L9): the committed DEV results as a Langfuse dataset and an experiment run, replayed from the file
(no Gemini call). It can only ever read the dev split and the phase8_dev results directory. Hand-made items and results here; one test reads the real committed
dev results, which are public in the repository."""

from __future__ import annotations

import ast
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from eval import push_langfuse_dev as push

REPO = Path(__file__).resolve().parent.parent
DEV_RESULT = REPO / "eval" / "results" / "phase8_dev" / "answer_v2.json"
SCRIPT = REPO / "eval" / "push_langfuse_dev.py"
FAKE_KEY = "fake-" + "x" * 28


def gold(kind: str, **fields) -> SimpleNamespace:
    return SimpleNamespace(type=kind, **fields)


ITEMS = [
    SimpleNamespace(id="gs-001", intent="product_search", question="áo khoác nam xanh rêu, mail khachhang02@example.com", gold_source=gold("skus", skus=["SM-NAM-0001", "SM-NAM-0009"])),
    SimpleNamespace(id="gs-002", intent="product_search", question="váy dạ hội đính đá", gold_source=gold("no_answer", reason="không có")),
    SimpleNamespace(id="gs-003", intent="policy_question", question="đổi trả trong mấy ngày", gold_source=gold("clause", document="doi-tra", clause=2)),
    SimpleNamespace(id="gs-004", intent="order_status", question="đơn {order_number} của khachhang02@example.com đâu rồi", gold_source=gold("order", provided_identity="khachhang02@example.com")),
    SimpleNamespace(id="gs-005", intent="smalltalk", question="chào shop", gold_source=gold("none")),
]


def result_item(item_id, intent, correct, *, routed_to=None, skus=(), clauses=(), reply="trả lời"):
    return {"id": item_id, "intent": intent, "correct": correct, "check": "gold_sku", "reason": "", "routed_to": routed_to or intent, "retrieved_skus": list(skus),
            "retrieved_clauses": [list(c) for c in clauses], "llm_calls": 3, "steps": 6, "reply": reply}


def make_result(dev_hash: str = "d" * 64, **provenance) -> dict:
    deterministic = {"items": [
        result_item("gs-001", "product_search", True, skus=["SM-NAM-0003", "SM-NAM-0009"]),  # the first gold SKU is the second hit
        result_item("gs-002", "product_search", True, skus=[]),
        result_item("gs-003", "policy_question", False, clauses=[("doi-tra", 1), ("doi-tra", 2)], reply="Điều 2: đổi trong 7 ngày"),
        result_item("gs-004", "order_status", True, reply="Đơn 1234 đang giao tới 12 Lê Lợi"),
        result_item("gs-005", "smalltalk", True, routed_to="out_of_scope", reply="Chào bạn"),
    ], "per_intent": {"all": {"n": 5, "correct": 4, "accuracy": 0.8}},
        "provenance": {"golden_items_read": "dev only", "dev_split_sha256": dev_hash, "git_commit": "c" * 40, "model_id": "gemini-3.5-flash-lite",
                       "answer_prompt": {"name": "answer_v2", "sha256": "a" * 64}, **provenance}}
    return {"deterministic": deterministic, "deterministic_sha256": push.canonical_sha256(deterministic), "measurements": {}}


def write(tmp_path: Path, result: dict, name="answer_v2.json", folder="phase8_dev") -> Path:
    path = tmp_path / "eval" / "results" / folder / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    return path


@pytest.fixture
def results_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(push, "RESULTS_DIR", tmp_path / "eval" / "results" / "phase8_dev")
    return tmp_path


# --- what may be pushed -----------------------------------------------------------------------------------------------------------------


def test_only_a_file_directly_inside_the_phase8_dev_results_directory_is_accepted(results_dir):
    for folder in ("phase8_test", "phase6", "../.."):
        path = write(results_dir, make_result(), folder=folder)
        with pytest.raises(push.Refused, match="phase8_dev"):
            push.load_result(path, ITEMS, dev_hash="d" * 64)


def test_the_real_test_results_directory_is_refused_even_by_name():
    with pytest.raises(push.Refused, match="phase8_dev"):
        push.load_result(REPO / "eval" / "results" / "phase8_test" / "answer_v2.json", ITEMS, dev_hash="d" * 64)


def test_a_result_that_says_it_read_more_than_dev_is_refused(results_dir):
    path = write(results_dir, make_result(golden_items_read="dev and test"))
    with pytest.raises(push.Refused, match="dev only"):
        push.load_result(path, ITEMS, dev_hash="d" * 64)


def test_a_result_made_on_another_dev_set_is_refused(results_dir):
    path = write(results_dir, make_result(dev_hash="e" * 64))
    with pytest.raises(push.Refused, match="dev split"):
        push.load_result(path, ITEMS, dev_hash="d" * 64)


def test_an_edited_result_is_refused(results_dir):
    result = make_result()
    result["deterministic"]["items"][0]["correct"] = False
    path = write(results_dir, result)
    with pytest.raises(push.Refused, match="deterministic_sha256"):
        push.load_result(path, ITEMS, dev_hash="d" * 64)


def test_a_result_with_an_item_the_dev_split_does_not_have_is_refused(results_dir):
    result = make_result()
    result["deterministic"]["items"].append(result_item("gs-999", "smalltalk", True))
    result["deterministic_sha256"] = push.canonical_sha256(result["deterministic"])
    path = write(results_dir, result)
    with pytest.raises(push.Refused, match="gs-999"):
        push.load_result(path, ITEMS, dev_hash="d" * 64)


def test_a_valid_dev_result_is_loaded(results_dir):
    path = write(results_dir, make_result())
    assert push.load_result(path, ITEMS, dev_hash="d" * 64)["deterministic"]["provenance"]["git_commit"] == "c" * 40


# --- the records ------------------------------------------------------------------------------------------------------------------------


def records():
    return {r.golden_id: r for r in push.build_records(make_result(), ITEMS, secrets=[FAKE_KEY])}


def test_every_dev_item_becomes_one_record_with_a_masked_question_and_its_gold():
    got = records()
    assert list(got) == ["gs-001", "gs-002", "gs-003", "gs-004", "gs-005"]
    assert got["gs-001"].input == {"question": "áo khoác nam xanh rêu, mail [email]"}
    assert got["gs-001"].expected_output == {"intent": "product_search", "gold": {"skus": ["SM-NAM-0001", "SM-NAM-0009"]}, "gold_keys": ["SM-NAM-0001", "SM-NAM-0009"]}
    assert got["gs-003"].expected_output["gold_keys"] == ["doi-tra#2"] and got["gs-002"].expected_output["gold_keys"] is None
    assert got["gs-001"].id == "shopmate-dev-gs-001" and got["gs-001"].metadata["split"] == "dev" and got["gs-001"].metadata["source_commit"] == "c" * 40


def test_order_items_are_withheld_in_question_reply_and_gold():
    order = records()["gs-004"]
    assert order.input == {"question": f"[withheld: order_status, {len(ITEMS[3].question)} ký tự]"}
    assert order.output["reply"] == "[withheld: order_status, 32 ký tự]"
    assert order.expected_output["gold"] == {"type": "order"} and order.metadata["withheld"] is True
    assert "khachhang02" not in json.dumps([order.input, order.expected_output, order.output, order.metadata], ensure_ascii=False)


def test_the_replayed_output_is_what_the_dev_run_recorded():
    out = records()["gs-003"].output
    assert out["reply"] == "Điều 2: đổi trong 7 ngày" and out["correct"] is False and out["routed_to"] == "policy_question"
    assert out["retrieved_keys"] == ["doi-tra#1", "doi-tra#2"]


# --- the three scores -------------------------------------------------------------------------------------------------------------------


def score(evaluator, record):
    return evaluator(input=record.input, output=record.output, expected_output=record.expected_output, metadata=record.metadata)


def test_answer_correct_is_a_boolean_score_of_what_the_dev_run_recorded():
    from langfuse import Evaluation

    got = records()
    results = [score(push.answer_correct, got["gs-001"]), score(push.answer_correct, got["gs-003"])]
    assert all(isinstance(r, Evaluation) and r.name == "answer_correct" and r.data_type == "BOOLEAN" for r in results)
    assert [r.value for r in results] == [1, 0]


def test_router_is_true_when_the_routed_intent_is_the_gold_intent():
    got = records()
    assert score(push.router, got["gs-001"]).value == 1 and score(push.router, got["gs-005"]).value == 0  # smalltalk routed to out_of_scope
    assert score(push.router, got["gs-001"]).data_type == "BOOLEAN"


def test_top5_first_hit_rr_is_the_reciprocal_rank_of_the_first_gold_and_only_for_items_that_have_gold():
    got = records()
    first, policy = score(push.top5_first_hit_rr, got["gs-001"]), score(push.top5_first_hit_rr, got["gs-003"])
    assert (first.name, first.value, first.data_type) == ("top5_first_hit_rr", 0.5, "NUMERIC") and policy.value == 0.5
    assert score(push.top5_first_hit_rr, got["gs-002"]) is None and score(push.top5_first_hit_rr, got["gs-004"]) is None and score(push.top5_first_hit_rr, got["gs-005"]) is None


def test_a_gold_that_was_never_retrieved_scores_zero_not_nothing():
    record = records()["gs-001"]
    record.output["retrieved_keys"] = ["SM-NAM-0003"]
    assert score(push.top5_first_hit_rr, record).value == 0.0


def test_the_summary_is_what_the_scores_add_up_to():
    summary = push.summarise(list(records().values()))
    assert summary["items"] == 5 and summary["withheld"] == 1 and summary["top5_first_hit_rr_items"] == 2
    assert summary["answer_correct"] == pytest.approx(4 / 5) and summary["router"] == pytest.approx(4 / 5) and summary["top5_first_hit_rr"] == pytest.approx(0.5)


# --- the real committed dev results -----------------------------------------------------------------------------------------------------


def test_the_committed_dev_results_pair_up_with_the_dev_split_and_the_dry_run_sends_nothing(monkeypatch, capsys):
    monkeypatch.setattr(push, "connect", lambda *a, **k: (_ for _ in ()).throw(AssertionError("a dry run must not even build a client")))
    assert push.main([]) == 0
    out = json.loads(capsys.readouterr().out)
    committed = json.loads(DEV_RESULT.read_text(encoding="utf-8"))["deterministic"]["per_intent"]["all"]
    assert out["mode"] == "dry-run" and out["items"] == committed["n"] == 71
    assert out["answer_correct"] == pytest.approx(committed["correct"] / committed["n"])
    assert 0 < out["top5_first_hit_rr"] <= 1 and out["withheld"] > 0 and out["router"] <= 1


# --- pushing ----------------------------------------------------------------------------------------------------------------------------


class FakeDataset:
    def __init__(self, items):
        self.items, self.experiments = items, []

    def run_experiment(self, *, name, task, evaluators=(), run_name=None, description=None, metadata=None, **kwargs):
        rows = []
        for item in self.items:
            output = task(item=item)
            scores = [e(input=item.input, output=output, expected_output=item.expected_output, metadata=item.metadata) for e in evaluators]
            rows.append((item, output, [s for s in scores if s is not None]))
        self.experiments.append({"name": name, "run_name": run_name, "metadata": metadata, "rows": rows})
        return SimpleNamespace(dataset_run_url="https://langfuse.example.test/run/1")  # the SDK's ExperimentResult has it as an attribute


class FakeClient:
    def __init__(self):
        self.datasets, self.items, self.flushed, self.dataset = [], {}, 0, None

    def create_dataset(self, **kwargs):
        self.datasets.append(kwargs)

    def create_dataset_item(self, **kwargs):
        self.items[kwargs["id"]] = SimpleNamespace(**kwargs)

    def get_dataset(self, name):
        self.dataset = FakeDataset([self.items[k] for k in sorted(self.items)])
        return self.dataset

    def flush(self):
        self.flushed += 1


def test_push_creates_the_dataset_its_items_and_one_experiment_run_without_any_gemini_call():
    client = FakeClient()
    connection = SimpleNamespace(client=client, sdk=None, secrets=[FAKE_KEY])
    assert push.run_push(make_result(), ITEMS, connection=connection, secrets=[FAKE_KEY]) == 0
    assert client.datasets[0]["name"] == "shopmate-dev" and len(client.items) == 5 and client.flushed == 1
    (experiment,) = client.dataset.experiments
    assert experiment["run_name"].startswith("answer_v2@") and experiment["metadata"]["split"] == "dev"
    by_id = {row[0].id: row for row in experiment["rows"]}
    assert [s.name for s in by_id["shopmate-dev-gs-001"][2]] == ["answer_correct", "router", "top5_first_hit_rr"]
    assert [s.name for s in by_id["shopmate-dev-gs-005"][2]] == ["answer_correct", "router"]
    sent = json.dumps([vars(i) for i in client.items.values()] + [row[1] for row in experiment["rows"]], ensure_ascii=False)
    assert "khachhang02" not in sent and "Lê Lợi" not in sent and "[email]" in sent


def test_push_without_keys_does_nothing_and_says_so(monkeypatch, capsys):
    monkeypatch.setattr(push, "connect", lambda *a, **k: None)
    assert push.main(["--push"]) == 1
    assert "LANGFUSE_PUBLIC_KEY" in capsys.readouterr().err


# --- the test split stays locked --------------------------------------------------------------------------------------------------------


def test_the_script_reads_golden_items_only_through_load_split_dev_and_has_no_way_to_pick_another_split():
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "id", getattr(n.func, "attr", None)) == "load_split"]
    assert len(calls) == 1 and [a.value for a in calls[0].args] == ["dev"] and calls[0].keywords == []
    options = [a.value for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "add_argument" for a in n.args[:1] if isinstance(a, ast.Constant)]
    assert "--split" not in options and set(options) == {"--results", "--push"}
    assert "phase8_test" not in SCRIPT.read_text(encoding="utf-8")  # the test results are refused by the directory rule, never mentioned


def test_the_hash_is_the_one_the_runners_write():
    from eval.run_retrieval_eval import canonical_sha256 as runner_hash

    sample = {"b": [1, 2, {"é": "đ"}], "a": None}
    assert push.canonical_sha256(sample) == runner_hash(sample)
    assert push.canonical_sha256(json.loads(DEV_RESULT.read_text(encoding="utf-8"))["deterministic"]) == json.loads(DEV_RESULT.read_text(encoding="utf-8"))["deterministic_sha256"]

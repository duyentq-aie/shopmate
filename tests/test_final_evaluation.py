"""The final evaluation (eval/final_evaluation.py): its statistics, the two populations, the guards of the test split and one whole run over hand-made items.

Nothing here reads a golden item: the items are made by hand (tests/test_eval_phase3b._item), the components are the fakes of the Phase 5 and Phase 6 runner tests, and
`load_split` is replaced wherever a guard must be shown to stop before the split is opened."""

from __future__ import annotations

import ast
import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from app.llm.errors import DailyLimitExhausted
from app.retrieval.search import SearchFilters
from eval import external_state as es
from eval import final_evaluation as fe
from eval.retrieval_metrics import bootstrap_ci
from tests.test_eval_phase3b import _item
from tests.test_external_state import RAW_EMAIL, FakeQdrant, FakeSaleor, nodes, points, registered_from
from tests.test_phase5_runner import FakeExtractor as Phase5Extractor
from tests.test_phase5_runner import FakeRouter as Phase5Router
from tests.test_phase6_runner import FILTERS, OWNER, ROUTES, make

REPO = Path(__file__).resolve().parent.parent


# --- the statistics ----------------------------------------------------------------------------------------------------------------------------


def test_the_percentile_bootstrap_is_deterministic_and_brackets_the_estimate():
    values = [float(v) for v in range(1, 72)]
    first, second = fe.percentile_ci(values, 0.95), fe.percentile_ci(values, 0.95)
    assert first == second and first["n"] == 71
    assert first["ci_low"] <= first["estimate"] <= first["ci_high"] and first["estimate"] == 67.0  # index round(0.95 * 70) = 66 -> the 67th value
    assert fe.percentile_ci(values, 0.5)["estimate"] == 36.0 and fe.percentile_ci([], 0.5)["estimate"] is None
    assert fe.percentile_ci([5.0] * 10, 0.95) == {"n": 10, "estimate": 5.0, "ci_low": 5.0, "ci_high": 5.0}


def test_a_proportion_has_the_wilson_interval_and_only_that_and_it_is_not_a_point_when_every_item_succeeds():
    all_yes = fe.summarise("proportion", [1.0] * 33)
    assert (all_yes["successes"], all_yes["n"], all_yes["estimate"], all_yes["interval"]) == (33, 33, 1.0, "wilson")
    assert all_yes["ci_low"] == pytest.approx(0.8957, abs=1e-3) and all_yes["ci_high"] == 1.0  # what 33 items can show: not [1, 1], which a percentile bootstrap would give
    mixed = fe.summarise("proportion", [1.0] * 23 + [0.0])
    assert mixed["ci_low"] == pytest.approx(0.7976, abs=1e-3) and mixed["ci_high"] == pytest.approx(0.9926, abs=1e-3)
    assert set(mixed) == {"n", "successes", "estimate", "ci_low", "ci_high", "interval"}  # no bootstrap numbers beside it, no second interval
    assert fe.summarise("proportion", [0.0] * 5)["ci_low"] == 0.0 and fe.summarise("proportion", [])["estimate"] is None and fe.wilson(0, 0) is None
    assert bootstrap_ci([1.0] * 33)["ci_low"] == 1.0  # the thing the registration avoids for a proportion


def test_wilson_matches_the_textbook_values():
    assert fe.wilson(71, 71)[0] == pytest.approx(0.94867, abs=1e-4)
    assert fe.wilson(5, 5)[0] == pytest.approx(0.5655, abs=1e-3)  # five items of five: the interval is wide, as it must be
    low, high = fe.wilson(1, 39)
    assert (low, high) == (pytest.approx(0.0046, abs=1e-3), pytest.approx(0.1319, abs=1e-3))


def test_the_means_the_percentiles_and_the_cost_have_the_percentile_bootstrap_of_the_earlier_phases():
    mean = fe.summarise("mean", [1.0, 0.5, 0.25, 1.0])
    assert mean["interval"] == "bootstrap" and (mean["ci_low"], mean["ci_high"]) == (bootstrap_ci([1.0, 0.5, 0.25, 1.0])["ci_low"], bootstrap_ci([1.0, 0.5, 0.25, 1.0])["ci_high"])
    assert "successes" not in mean and fe.summarise("p95", [1.0, 2.0, 3.0])["interval"] == "bootstrap"
    cost = fe.summarise("cost", [0.001, 0.003])
    assert cost["estimate"] == pytest.approx(2.0) and cost["interval"] == "bootstrap" and fe.summarise("mean", [])["estimate"] is None


def test_the_registered_kind_of_interval_of_every_metric():
    kinds = {name: kind for name, kind, _ in fe.METRICS}
    assert {n for n, k in kinds.items() if k == "proportion"} == {"router_accuracy", "extractor_exact_match", "no_answer_correct", "answer_correct", "guardrail_fallback_rate"}
    assert {n for n, k in kinds.items() if k != "proportion"} == {"product_mrr", "policy_mrr", "product_mrr_gold_routing", "latency_p50_ms", "latency_p95_ms", "cost_per_1000_usd"}
    assert "answer_correct" in kinds  # a primary metric


def test_the_bootstrap_settings_are_the_ones_of_the_earlier_phases():
    from eval import retrieval_metrics as rm

    assert (fe.BOOTSTRAP_RESAMPLES, fe.BOOTSTRAP_SEED, fe.CONFIDENCE) == (10_000, 20261009, 0.95) == (rm.BOOTSTRAP_RESAMPLES, rm.BOOTSTRAP_SEED, rm.CONFIDENCE)


# --- the two populations -----------------------------------------------------------------------------------------------------------------------


def test_the_second_population_leaves_out_exactly_the_excluded_ids_and_both_use_the_same_settings():
    values = {name: {} for name, _, _ in fe.METRICS}
    values["router_accuracy"] = {"gs-1": 1.0, "gs-2": 1.0, "gs-3": 0.0}
    values["product_mrr"] = {"gs-1": 1.0}
    both = fe.populations(values, {"gs-1"})
    assert both["all"]["router_accuracy"]["n"] == 3 and both["without_excluded"]["router_accuracy"]["n"] == 2
    assert both["all"]["router_accuracy"]["successes"] == 2 and both["without_excluded"]["router_accuracy"]["successes"] == 1
    assert both["all"]["product_mrr"]["n"] == 1 and both["without_excluded"]["product_mrr"]["n"] == 0 and both["without_excluded"]["product_mrr"]["estimate"] is None
    nothing = fe.populations(values, set())  # no item matched the hash: the second figure is the first, and the report says so
    assert nothing["all"] == nothing["without_excluded"]


def test_the_question_fingerprint_is_the_one_recorded_in_a0_the_exact_utf8_text():
    gold = {"type": "order", "external_reference": "shopmate-seed-order-005", "order_number": "88", "expected_status": "UNFULFILLED", "provided_identity": "none",
            "expected_behavior": "ask_for_verification"}
    item = _item("gs-001", "order_status", gold, question="đơn {order_number} của mình")
    assert fe.question_sha256(item) == hashlib.sha256("đơn {order_number} của mình".encode("utf-8")).hexdigest()  # the placeholder literal, no substitution, no normalisation
    assert len(fe.EXCLUDED_TEST_QUESTION_SHA256) == 64 and fe.EXCLUDED_TEST_QUESTION_SHA256 == "3b9975a677521f1dda7bac87ffa7098bc3d748f5724545ee185947a1e86d8692"


# --- the guards ---------------------------------------------------------------------------------------------------------------------------------


def git(repo: Path, *args: str) -> str:
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com", "PATH": "/usr/bin:/bin"}
    return subprocess.run(["git", *args], cwd=repo, env=env, capture_output=True, text=True, check=True).stdout.strip()


def commit(repo: Path, files: dict[str, str], message: str) -> str:
    for name, text in files.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture()
def repo(tmp_path):
    git(tmp_path, "init", "-q")
    dev = commit(tmp_path, {"app/a.py": "x = 1\n", "eval/run.py": "y = 1\n", "docs/PHASE8-PREREG.md": "draft\n", "tests/t.py": "z = 1\n"}, "dev run")
    freeze = commit(tmp_path, {"docs/PHASE8-PREREG.md": "frozen\n", "eval/final_evaluation.py": "new\n", "eval/external_state.py": "new\n", "tests/t.py": "z = 2\n"}, "freeze")  # the freeze adds the script, the state fingerprints and tests
    git(tmp_path, "tag", "v1.0-frozen")
    return tmp_path, dev, freeze


def test_head_at_the_commit_of_the_freeze_tag_with_unchanged_code_since_the_dev_run_is_frozen(repo):
    path, dev, freeze = repo
    assert fe.resolve_freeze_tag(path, "v1.0-frozen") == freeze == fe.resolve_freeze_tag(path, "refs/tags/v1.0-frozen")
    assert fe.frozen_problems(path, "v1.0-frozen", dev) == []


def test_an_annotated_tag_is_followed_to_its_commit(repo):
    path, dev, freeze = repo
    git(path, "tag", "-a", "-m", "annotated", "v1.0-annotated")
    assert fe.resolve_freeze_tag(path, "v1.0-annotated") == freeze and fe.frozen_problems(path, "v1.0-annotated", dev) == []


def test_a_later_commit_may_change_files_that_do_not_touch_a_result_and_nothing_else(repo):
    path, dev, _ = repo
    commit(path, {"docs/notes.md": "a note\n", "tests/t.py": "z = 3\n", "eval/results/phase8_test/final_evaluation.json": "{}\n", ".github/workflows/ci.yml": "x\n",
                  "docs/PHASE8-COMMITS.md": "commits\n"}, "docs, tests, CI and results")
    assert fe.frozen_problems(path, "v1.0-frozen", dev) == []
    for name, text in (("app/a.py", "x = 2\n"), ("eval/final_evaluation.py", "changed\n"), ("eval/external_state.py", "changed\n"), ("docs/PHASE8-PREREG.md", "edited after the freeze\n"), ("prompts/p.md", "new prompt\n")):
        commit(path, {name: text}, f"changed {name}")
        problems = fe.frozen_problems(path, "v1.0-frozen", dev)
        assert problems and name in " ".join(problems), name  # the code, the script, the registration and the prompts are frozen with the tag


def test_code_that_changed_between_the_dev_run_and_the_freeze_is_refused_but_the_script_and_tests_are_not(repo):
    path, dev, _ = repo
    commit(path, {"eval/run.py": "y = 2\n"}, "the evaluation code changed")
    git(path, "tag", "-f", "v1.0-frozen")
    problems = fe.frozen_problems(path, "v1.0-frozen", dev)
    assert problems and "eval/run.py" in problems[0] and "official dev run" in problems[0]
    assert fe.frozen_problems(path, "v1.0-frozen", git(path, "rev-parse", "HEAD")) == []  # a dev run made at the changed commit would be the right reference


def test_a_ref_that_is_not_a_tag_is_not_a_freeze_and_missing_arguments_are_refused(repo):
    path, dev, freeze = repo
    git(path, "branch", "v1.0-frozen-branch")
    for ref in ("v1.0-frozen-branch", freeze, "HEAD", "nonexistent"):
        assert "not a tag" in fe.frozen_problems(path, ref, dev)[0], ref  # a branch moves, a SHA is not the registered name
    assert "required" in fe.frozen_problems(path, None, dev)[0] and "required" in fe.frozen_problems(path, "v1.0-frozen", None)[0]
    assert "not a commit" in fe.frozen_problems(path, "v1.0-frozen", "0" * 40)[0]


def test_a_head_that_does_not_descend_from_the_tag_is_refused(repo):
    path, dev, _ = repo
    git(path, "checkout", "-q", dev)  # the tree of the dev run, before the freeze: HEAD is older than the tag
    problems = fe.frozen_problems(path, "v1.0-frozen", dev)
    assert problems and "does not descend" in problems[0]


def test_the_attempts_are_limited_to_the_first_run_and_two_reruns_and_a_finding_is_not_rerun(tmp_path):
    assert fe.attempts_problems(tmp_path) == []
    fe.log_attempt(tmp_path, kind="run", ended="qdrant_unreachable", rerun_allowed=True)
    fe.log_attempt(tmp_path, kind="run", ended="llm_429_daily", rerun_allowed=True)
    assert fe.attempts_problems(tmp_path) == []  # two failed attempts, a third run is allowed
    fe.log_attempt(tmp_path, kind="run", ended="llm_unavailable", rerun_allowed=True)
    assert "3 attempts" in fe.attempts_problems(tmp_path)[0]
    other = tmp_path / "other"
    fe.log_attempt(other, kind="run", ended="llm_invalid_output", rerun_allowed=False)
    assert "not an infrastructure error" in fe.attempts_problems(other)[0]


def test_a_refusal_of_a_guard_is_written_to_the_attempts_but_it_is_not_an_attempt_and_blocks_nothing(tmp_path):
    fe.log_attempt(tmp_path, kind="run", ended="qdrant_unreachable", rerun_allowed=True)
    for _ in range(5):
        fe.log_attempt(tmp_path, kind="refusal", guard="external_state", ended="external_state_differs", opened_test_split=False, differences=["x"])
    assert fe.attempts_problems(tmp_path) == []  # one attempt, five refusals: a second run is allowed
    fe.log_attempt(tmp_path, kind="run", ended="llm_unavailable", rerun_allowed=True)
    fe.log_attempt(tmp_path, kind="run", ended="llm_429_daily", rerun_allowed=True)
    assert "3 attempts" in fe.attempts_problems(tmp_path)[0]
    other = tmp_path / "other"
    fe.log_attempt(other, kind="run", ended="llm_invalid_output", rerun_allowed=False)
    fe.log_attempt(other, kind="refusal", guard="external_state", ended="external_state_differs", rerun_allowed=True)
    assert "not an infrastructure error" in fe.attempts_problems(other)[0]  # the finding is still the last attempt


def test_the_rerun_eligible_types_are_exactly_the_infrastructure_errors_of_the_tracer():
    from app.tracing import ERROR_TYPES

    assert fe.RERUN_ELIGIBLE <= set(ERROR_TYPES)
    for result_not_infrastructure in ("llm_invalid_output", "filter_out_of_vocab", "answer_guardrail_fallback", "agent_step_cap_reached", "readonly_violation", "llm_request_error",
                                      "llm_cache_miss_offline", "internal_error", "ticket_write_failed", "llm_429_minute"):
        assert result_not_infrastructure not in fe.RERUN_ELIGIBLE


@pytest.mark.parametrize("argv", [["--split", "test", "--rehearsal"], ["--split", "test", "--rehearse-exclusion"], ["--split", "test", "--overwrite"]])
def test_the_test_split_has_no_rehearsal_and_no_overwrite(argv, monkeypatch, capsys):
    monkeypatch.setattr(fe, "load_split", lambda *a, **k: pytest.fail("the split must not be opened"))
    assert fe.main(argv) == 1 and "no rehearsal and no overwrite" in capsys.readouterr().err


def test_the_test_split_is_not_opened_without_a_clean_tree_the_freeze_commit_and_the_dev_run(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(fe, "load_split", lambda *a, **k: pytest.fail("the split must not be opened"))
    monkeypatch.setattr(fe, "resolve_results_dir", lambda name: tmp_path / "phase8_test")
    monkeypatch.setattr(fe, "check_clean_tree", lambda **kw: (_ for _ in ()).throw(fe.DirtyWorkingTree("not clean: x.py")))
    assert fe.main(["--split", "test"]) == 1 and "not clean" in capsys.readouterr().err
    monkeypatch.setattr(fe, "check_clean_tree", lambda **kw: "a" * 40)
    assert fe.main(["--split", "test"]) == 1 and "must be v1.0-frozen" in capsys.readouterr().err  # clean, but no --freeze-ref and --dev-run-sha
    assert fe.main(["--split", "test", "--freeze-ref", "v0.9", "--dev-run-sha", "b" * 40]) == 1 and "must be v1.0-frozen" in capsys.readouterr().err  # only the registered tag stands for the freeze
    assert fe.main(["--split", "test", "--freeze-ref", "v1.0-frozen", "--dev-run-sha", "b" * 40]) == 1  # the tag does not exist in the test environment, or the sha does not
    (tmp_path / "phase8_test").mkdir()
    (tmp_path / "phase8_test" / "final_evaluation.json").write_text("{}", encoding="utf-8")
    assert fe.main(["--split", "test", "--freeze-ref", "v1.0-frozen", "--dev-run-sha", "b" * 40]) == 1 and "run once" in capsys.readouterr().err  # a result exists: no second one


TEST_ARGV = ["--split", "test", "--freeze-ref", "v1.0-frozen", "--dev-run-sha", "b" * 40]


@pytest.fixture()
def passed_the_git_guards(monkeypatch, tmp_path):
    """Everything before the external-state guard passes (clean tree, the tag, the dev run, the attempts); the split must not be opened unless the test says so."""
    monkeypatch.setattr(fe, "resolve_results_dir", lambda name: tmp_path / "phase8_test")
    monkeypatch.setattr(fe, "check_clean_tree", lambda **kw: "a" * 40)
    monkeypatch.setattr(fe, "frozen_problems", lambda *a, **k: [])
    monkeypatch.setattr(fe, "resolve_freeze_tag", lambda *a, **k: "c" * 40)
    monkeypatch.setattr(fe, "load_split", lambda *a, **k: pytest.fail("the split must not be opened"))
    return tmp_path / "phase8_test"


def use_state(monkeypatch, registered_points, live_points, live_nodes):
    """The registered values are those of a fake state; the 'live' services are fakes too."""
    monkeypatch.setattr(es, "REGISTERED_STATE", registered_from(es.current_state(FakeQdrant(registered_points), FakeSaleor(nodes()))))
    monkeypatch.setattr(es, "live_clients", lambda: (FakeQdrant(live_points), FakeSaleor(live_nodes)))


def test_a_state_outside_the_repository_that_is_not_the_registered_one_stops_the_run_before_the_split_is_opened_and_is_recorded(passed_the_git_guards, monkeypatch, capsys):
    results = passed_the_git_guards
    changed_points = points()
    changed_points[3] = (changed_points[3][0], {"doc_type": "product", "sku": "RE-INDEXED"})
    changed_orders = nodes()
    changed_orders[97] = {**changed_orders[97], "status": "CANCELED"}
    use_state(monkeypatch, points(), changed_points, changed_orders)
    assert fe.main(TEST_ARGV) == 1  # load_split was replaced by a function that fails the test if it is called
    err = capsys.readouterr().err
    assert "REFUSED" in err and "test split was not opened" in err
    assert "qdrant.payloads_sha256: registered" in err and "saleor_orders.rows_sha256: registered" in err and "order #97: differs from the seed file in" in err
    assert "qdrant.points" not in err  # only the parts that differ
    assert RAW_EMAIL not in err and "@" not in err
    entries = [json.loads(line) for line in (results / "attempts.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(entries) == 1 and entries[0]["kind"] == "refusal" and entries[0]["guard"] == "external_state" and entries[0]["opened_test_split"] is False
    assert any(d.startswith("qdrant.payloads_sha256") for d in entries[0]["differences"]) and RAW_EMAIL not in json.dumps(entries)
    assert not (results / "final_evaluation.json").exists() and fe.attempts_problems(results) == []  # a refusal is not an attempt: after the state is put back, a run is allowed


def test_a_service_that_is_down_stops_the_run_before_the_split_is_opened(passed_the_git_guards, monkeypatch, capsys):
    def unreachable():
        raise ConnectionError("connection refused")

    monkeypatch.setattr(es, "live_clients", unreachable)
    assert fe.main(TEST_ARGV) == 1
    assert "could not be read: ConnectionError" in capsys.readouterr().err
    assert json.loads((passed_the_git_guards / "attempts.jsonl").read_text(encoding="utf-8").splitlines()[0])["ended"] == "external_state_differs"


def test_the_split_is_opened_only_after_the_registered_state_is_seen(passed_the_git_guards, monkeypatch, capsys):
    opened = []
    use_state(monkeypatch, points(), points(), nodes())

    def stop_at_the_split(*args, **kwargs):
        opened.append(kwargs)
        raise RuntimeError("reached the loader")

    monkeypatch.setattr(fe, "load_split", stop_at_the_split)
    with pytest.raises(RuntimeError, match="reached the loader"):
        fe.main(TEST_ARGV)
    assert opened == [{"final_evaluation": True}] and "OPENING THE TEST SPLIT" in capsys.readouterr().err
    assert not (passed_the_git_guards / "attempts.jsonl").exists()  # nothing was refused


def test_the_state_guard_comes_after_the_git_guards_and_before_the_split_and_the_rehearsal_does_not_use_it():
    source = (REPO / "eval" / "final_evaluation.py").read_text(encoding="utf-8")
    main_source = source[source.index("def main("):]
    assert main_source.index("frozen_problems(REPO_ROOT") < main_source.index("external_state.check()") < main_source.index("load_split(args.split")
    assert main_source.count("external_state.check()") == 1 and "if test:" in main_source[: main_source.index("external_state.check()")]


def test_a_dev_run_on_a_dirty_tree_is_refused_unless_it_says_it_is_a_rehearsal(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(fe, "resolve_results_dir", lambda name: tmp_path / "x")
    monkeypatch.setattr(fe, "check_clean_tree", lambda **kw: (_ for _ in ()).throw(fe.DirtyWorkingTree("not clean: x.py")))
    assert fe.main(["--split", "dev"]) == 1 and "not clean" in capsys.readouterr().err


def test_only_the_final_evaluation_module_and_the_loader_tests_ever_name_the_flag_that_opens_the_test_split():
    allowed = {"eval/final_evaluation.py", "tests/test_eval_phase3b.py", "tests/test_final_evaluation.py"}
    found = {}
    for path in sorted(REPO.rglob("*.py")):
        relative = path.relative_to(REPO).as_posix()
        if relative.startswith(("eval/golden/", ".venv/", ".git/")) or relative in allowed or "__pycache__" in relative:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.keyword) and node.arg == "final_evaluation":
                found.setdefault(relative, []).append(node.lineno)
    assert found == {}


# --- a whole run over hand-made items ------------------------------------------------------------------------------------------------------


NO_ANSWER = {"type": "no_answer", "reason": "không có"}


def order(behaviour: str, identity: str) -> dict:
    return {"type": "order", "external_reference": "shopmate-seed-order-005", "order_number": "88", "expected_status": "UNFULFILLED", "provided_identity": identity,
            "expected_behavior": behaviour}


ITEMS = [
    _item("gs-901", "product_search", {"type": "skus", "skus": ["SM-NAM-0001"]}, filters={"kind": "Áo khoác nam", "color": "Xanh rêu"}, question="áo khoác nam xanh rêu còn không shop"),
    _item("gs-902", "product_search", NO_ANSWER, filters={"kind": "Áo len nữ", "color": "Xanh dương", "size": "XL"}, question="áo len nữ màu xanh dương size XL có không shop"),
    _item("gs-903", "policy_question", {"type": "policy_clause", "document": "doi-tra", "clause": 2}, question="đổi size trong bao lâu vậy shop"),
    _item("gs-904", "order_status", order("answer", "matching_email"), question="đơn {order_number} của mình đang ở đâu, email " + OWNER),
    _item("gs-905", "order_status", order("ask_for_verification", "none"), question="đơn {order_number} của mình đang ở đâu rồi"),
    _item("gs-906", "order_status", order("refuse", "mismatched_email"), question="đơn {order_number} của mình, email sai@example.com"),
    _item("gs-907", "create_ticket", order("answer", "matching_email"), question="khiếu nại đơn {order_number} giao thiếu hàng, email " + OWNER),
    _item("gs-908", "create_ticket", NO_ANSWER, question="nhân viên tư vấn thái độ chưa tốt, mình muốn phản ánh"),
    _item("gs-909", "smalltalk", NO_ANSWER, question="chào shop nhé"),
    _item("gs-910", "out_of_scope", NO_ANSWER, question="thời tiết hôm nay thế nào"),
]
EXTRACTIONS = {"áo khoác nam xanh rêu còn không shop": SearchFilters(kind="Áo khoác nam", color="Xanh rêu"),  # exactly the expected filters
               "áo len nữ màu xanh dương size XL có không shop": SearchFilters(kind="Áo len nữ", color="Xanh dương")}  # misses the size: not an exact match


def search(query, filters, k, mode):
    if filters and filters.get("doc_type") == "policy":
        return ["policy:doi-tra:1", "policy:doi-tra:2"]  # the gold clause at rank 2
    if "áo khoác nam" in query:
        return ["product:SM-NAM-0002", "product:SM-NAM-0001"]  # the gold SKU at rank 2
    return []


def final_run(tmp_path):
    harness, _, _ = make(tmp_path)
    router = Phase5Router(ROUTES | {"đơn 88 của mình đang ở đâu, email " + OWNER: "order_status"})
    outcome = fe.evaluate_final(ITEMS, harness, router, Phase5Extractor(EXTRACTIONS), search, prices=(0.30, 2.50))
    return outcome, harness


def test_a_whole_run_gives_every_metric_for_both_populations_and_the_two_evaluations_agree(tmp_path):
    outcome, harness = final_run(tmp_path)
    assert outcome["complete"] is True
    values = outcome["values"]
    assert set(values["router_accuracy"].values()) == {1.0} and len(values["router_accuracy"]) == 10  # every item
    assert values["extractor_exact_match"] == {"gs-901": 1.0, "gs-902": 0.0}  # the product items only
    assert values["product_mrr"] == {"gs-901": 0.5} and values["policy_mrr"] == {"gs-903": 0.5}  # the gold at rank 2
    assert values["no_answer_correct"] == {"gs-902": 1.0}  # routed to product_search, nothing returned
    assert set(values["guardrail_fallback_rate"]) == {"gs-901", "gs-903", "gs-904"} and set(values["guardrail_fallback_rate"].values()) == {0.0}  # only items with an LLM-written answer
    assert set(values["answer_correct"].values()) == {1.0} and len(values["latency_p50_ms"]) == len(values["cost_per_1000_usd"]) == 10
    both = fe.populations(values, {"gs-901"})
    assert both["all"]["product_mrr"]["n"] == 1 and both["without_excluded"]["product_mrr"]["n"] == 0
    assert both["all"]["router_accuracy"]["successes"] == 10 and both["without_excluded"]["router_accuracy"]["successes"] == 9
    checks = fe.cross_checks(both["all"], outcome["r6"])
    assert checks == {"answer_correct_matches_phase6": True, "cost_matches_phase6": True}
    assert [row["id"] for row in outcome["table"]] == sorted(i.id for i in ITEMS) and sum(row["answers_written"] for row in outcome["table"]) == 3


def test_the_document_is_deterministic_json_the_report_has_both_figures_and_the_excluded_item_is_never_named(tmp_path):
    outcome, _ = final_run(tmp_path)
    provenance = {"split": "dev", "rehearsal": False, "git_commit": "c" * 40, "git_dirty": False}
    document = fe.build_document(ITEMS, outcome, {"gs-901"}, provenance, {"live_calls_this_run": 0, "cache_hits_this_run": 12, "wall_seconds": 1.0})
    again = fe.build_document(ITEMS, outcome, {"gs-901"}, provenance, {"live_calls_this_run": 0, "cache_hits_this_run": 12, "wall_seconds": 9.9})
    assert document["deterministic_sha256"] == again["deterministic_sha256"]  # the wall time is a measurement, not part of the hash
    assert json.loads(json.dumps(document, ensure_ascii=False)) == document
    d = document["deterministic"]
    assert d["excluded_matched"] == 1 and set(d["metrics"]) == {"all", "without_excluded"} and not any(k.startswith("latency_") for k in d["metrics"]["all"])
    assert set(d["phase5"]["extraction"]) == {"metrics", "metrics_without_in_stock"}  # the label conflict of in_stock is reported both ways, as in Phase 5
    assert set(document["measurements"]["latency_ms"]) == {"all", "without_excluded"}
    assert d["intervals"] == {"proportions": {"interval": "wilson", "confidence": 0.95},
                              "mrr_latency_cost": {"interval": "percentile bootstrap", "confidence": 0.95, "resamples": 10000, "seed": 20261009}}
    kinds = {name: kind for name, kind, _ in fe.METRICS}
    for pop in d["metrics"].values():
        assert {name: entry["interval"] for name, entry in pop.items()} == {name: ("wilson" if kinds[name] == "proportion" else "bootstrap") for name in pop}
    report = fe.render_report(document)
    for name, _, _ in fe.METRICS:
        assert f"| {name} |" in report
    assert "all items" in report and "without the excluded item" in report and "matched 1 of 1" in report and "**Wilson 95%**" in report and "No pass/fail threshold" in report
    assert "percentile bootstrap over items** (95%, 10,000 resamples, seed 20261009)" in report and "### By author" in report and "### By intent" in report
    assert "gs-901" not in json.dumps(d["metrics"]) and "gs-901" not in report  # the excluded id is in no summary


def test_a_quota_that_ends_the_run_is_a_clean_stop_and_nothing_is_written_as_a_result(tmp_path):
    from tests.test_phase6_runner import GroundedLLM

    harness, _, _ = make(tmp_path, llm=GroundedLLM(error=DailyLimitExhausted("daily quota used up")))
    router = Phase5Router(ROUTES | {"đơn 88 của mình đang ở đâu, email " + OWNER: "order_status"})
    outcome = fe.evaluate_final(ITEMS, harness, router, Phase5Extractor(EXTRACTIONS), search)
    assert outcome["complete"] is False and outcome["stopped"]["reason"].startswith("DailyLimitExhausted")
    assert "values" not in outcome  # no half result to report


# --- the registered secondary analysis --------------------------------------------------------------------------------------------------------------------


AUTHORED = [item.model_copy(update={"author": "agent_draft"}) if item.id in {"gs-901", "gs-903", "gs-905", "gs-907", "gs-909"} else item for item in ITEMS]


def test_the_items_are_grouped_by_author_and_by_intent_in_a_fixed_order():
    groups = fe.groups_of(AUTHORED)
    assert list(groups["by_author"]) == ["human", "agent_draft"] and [len(v) for v in groups["by_author"].values()] == [5, 5]
    assert list(groups["by_intent"]) == ["product_search", "policy_question", "order_status", "create_ticket", "out_of_scope", "smalltalk"]
    assert {k: len(v) for k, v in groups["by_intent"].items()} == {"product_search": 2, "policy_question": 1, "order_status": 3, "create_ticket": 2, "out_of_scope": 1, "smalltalk": 1}
    assert sorted(i for ids in groups["by_intent"].values() for i in ids) == sorted(i.id for i in ITEMS) == sorted(i for ids in groups["by_author"].values() for i in ids)  # every item once


def test_the_secondary_analysis_gives_wilson_proportions_means_and_flags_every_small_group(tmp_path):
    outcome, _ = final_run(tmp_path)
    secondary = fe.breakdown(outcome["values"], fe.groups_of(AUTHORED)["by_intent"])
    product = secondary["product_search"]
    assert product["n_items"] == 2 and product["too_small_to_interpret"] is True  # fewer than 8 items: marked, not read
    assert product["proportions"]["router_accuracy"]["successes"] == 2 and product["proportions"]["extractor_exact_match"]["successes"] == 1
    assert product["proportions"]["extractor_exact_match"]["interval"] == "wilson" and product["proportions"]["extractor_exact_match"]["ci_low"] == fe.wilson(1, 2)[0]
    assert product["means"]["product_mrr"] == {"n": 1, "estimate": 0.5} and product["means"]["policy_mrr"] == {"n": 0, "estimate": None}  # a mean, no interval
    assert secondary["policy_question"]["means"]["policy_mrr"] == {"n": 1, "estimate": 0.5}
    assert secondary["smalltalk"]["proportions"]["extractor_exact_match"]["n"] == 0  # a metric that does not apply to the group is empty, not zero
    assert all(entry["too_small_to_interpret"] for entry in secondary.values())
    by_author = fe.breakdown(outcome["values"], fe.groups_of(AUTHORED)["by_author"])
    assert by_author["human"]["n_items"] == by_author["agent_draft"]["n_items"] == 5
    assert by_author["human"]["proportions"]["answer_correct"]["successes"] + by_author["agent_draft"]["proportions"]["answer_correct"]["successes"] == 10


def test_the_secondary_analysis_is_for_all_items_only_so_that_it_never_shows_which_group_the_excluded_item_is_in(tmp_path):
    outcome, _ = final_run(tmp_path)
    provenance = {"split": "dev", "rehearsal": True, "git_commit": "c" * 40, "git_dirty": False}
    with_exclusion = fe.build_document(AUTHORED, outcome, {"gs-901"}, provenance, {})["deterministic"]["secondary"]
    without = fe.build_document(AUTHORED, outcome, set(), provenance, {})["deterministic"]["secondary"]
    assert with_exclusion == without  # the same table whether or not an item is excluded
    assert sum(entry["n_items"] for entry in with_exclusion["by_author"].values()) == 10 == sum(entry["n_items"] for entry in with_exclusion["by_intent"].values())
    assert "all items only" in with_exclusion["description"] and "fewer than 8 items" in with_exclusion["description"]
    report = fe.render_report(fe.build_document(AUTHORED, outcome, {"gs-901"}, provenance, {"live_calls_this_run": 0, "cache_hits_this_run": 0}))
    section = report[report.index("## Registered secondary analysis"):]
    assert "(small)" in section and "The samples are small" in section and "[0." in section and "human" in section and "agent_draft" in section and "policy_question" in section
    assert "gs-901" not in section


def test_the_per_item_table_carries_the_author_for_every_row(tmp_path):
    outcome, _ = final_run(tmp_path)
    assert {row["id"]: row["author"] for row in outcome["table"]} == {item.id: item.author for item in ITEMS}


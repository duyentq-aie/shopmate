"""The CI set-up (docs/DECISIONS-phase7.md, D7.20 to D7.23): the workflow file, the offline integrity check of the committed results, and the network guard."""

from __future__ import annotations

import copy
import json
import re
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from eval import verify_committed_results as verify
from tests import network_guard

REPO = Path(__file__).resolve().parent.parent
WORKFLOW = REPO / ".github" / "workflows" / "ci.yml"
CI = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
TEXT = WORKFLOW.read_text(encoding="utf-8")


def steps(job: str) -> list[dict]:
    return CI["jobs"][job]["steps"]


# --- the workflow -----------------------------------------------------------------------------------------------------------------------


def test_the_workflow_runs_on_push_and_pull_request_with_read_only_permissions_and_cancels_old_runs():
    assert set(CI[True]) == {"push", "pull_request"}  # PyYAML reads the key `on` as True
    assert CI["permissions"] == {"contents": "read"}
    assert CI["concurrency"]["cancel-in-progress"] is True and "${{ github.ref }}" in CI["concurrency"]["group"]
    for name, job in CI["jobs"].items():
        extra = job.get("permissions", {})
        assert all(level == "read" for level in extra.values()), (name, extra)  # no job asks for more than read


def test_the_three_jobs_are_test_integrity_and_secrets():
    assert set(CI["jobs"]) == {"test", "integrity", "secrets"}
    assert all(job["runs-on"] == "ubuntu-latest" for job in CI["jobs"].values())


def test_the_test_job_syncs_the_frozen_lock_on_python_3_12_and_runs_the_unit_suite_without_the_service_tests():
    uses = [s["uses"] for s in steps("test") if "uses" in s]
    assert uses[0].startswith("actions/checkout@v") and uses[1].startswith("astral-sh/setup-uv@v")
    assert next(s for s in steps("test") if s.get("uses", "").startswith("astral-sh/setup-uv"))["with"]["python-version"] == "3.12"
    runs = [s["run"] for s in steps("test") if "run" in s]
    assert runs[0] == "uv sync --frozen"
    assert runs[1] == 'uv run pytest -q -m "not integration and not live_llm"'  # the brief's `not integration`, plus the tests that would spend Gemini quota


def test_actions_are_pinned_to_a_major_tag_and_none_is_a_branch():
    for line in re.findall(r"uses:\s*(\S+)", TEXT):
        assert re.fullmatch(r"[\w.-]+/[\w.-]+@v\d+", line), line


def test_no_secret_of_ours_is_used_the_only_secrets_reference_is_the_run_token_of_gitleaks():
    references = re.findall(r"secrets\.(\w+)", TEXT)
    assert references == ["GITHUB_TOKEN"]
    assert "GITHUB_TOKEN" in json.dumps(CI["jobs"]["secrets"]) and "GITHUB_TOKEN" not in json.dumps(CI["jobs"]["test"])


def test_the_environment_makes_the_run_offline_whatever_a_test_does():
    env = CI["env"]
    assert env["GEMINI_API_KEY"] == "" and env["HF_HUB_OFFLINE"] == "1" and env["TRANSFORMERS_OFFLINE"] == "1"
    assert env["SHOPMATE_BLOCK_NETWORK"] == "1" and env["SHOPMATE_CHAT_FAKE"] == "1"
    assert not any(re.search(r"(KEY|TOKEN|SECRET|PASSWORD)", name) and value for name, value in env.items())  # no value of a secret-looking name


def test_the_secrets_job_scans_the_whole_history():
    checkout, scan = steps("secrets")
    assert checkout["with"]["fetch-depth"] == 0 and scan["uses"].startswith("gitleaks/gitleaks-action@v")


def test_the_integrity_job_runs_the_offline_check():
    assert steps("integrity")[-1]["run"] == "uv run python -m eval.verify_committed_results"


def test_the_unit_suite_marks_every_service_test_so_the_ci_command_excludes_it():
    config = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    assert "integration:" in config and "live_llm:" in config  # both markers are registered
    for name in ("test_retrieval_integration.py", "test_agent_tools_integration.py"):
        assert "pytestmark = pytest.mark.integration" in (REPO / "tests" / name).read_text(encoding="utf-8"), name
    assert "pytestmark = pytest.mark.live_llm" in (REPO / "tests" / "test_llm_live.py").read_text(encoding="utf-8")


def test_a_test_that_needs_qdrant_the_model_or_saleor_is_marked_integration():
    """Any test file that opens the real services (connect(), BgeM3Embedder(), the `embedder` fixture) has the marker on the module or on that test."""
    needle = re.compile(r"\bconnect\(\)|BgeM3Embedder\(|ReadOnlySaleorClient\(api_url|def test_\w+\([^)]*\bembedder\b")
    for path in sorted((REPO / "tests").glob("test_*.py")):
        text = path.read_text(encoding="utf-8")
        if not needle.search(text):
            continue
        assert "pytest.mark.integration" in text, f"{path.name} uses a real service or the model but has no integration marker"


# --- the integrity check ------------------------------------------------------------------------------------------------------------------


def test_the_committed_results_pass_the_integrity_check():
    assert verify.hash_problems() == [] and verify.comparison_problems() == [] and verify.phase6_problems() == []
    result = subprocess.run([sys.executable, "-m", "eval.verify_committed_results"], cwd=REPO, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.fixture
def results_copy(tmp_path):
    target = tmp_path / "results"
    shutil.copytree(REPO / "eval" / "results", target)
    return target


def test_an_edited_number_in_a_result_file_is_found(results_copy):
    path = results_copy / "phase6_after_a21" / "answer_v2.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["deterministic"]["per_intent"]["all"]["correct"] += 1
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    problems = verify.hash_problems(results_copy)
    assert len(problems) == 1 and "phase6_after_a21/answer_v2.json" in problems[0] and "deterministic_sha256" in problems[0]


def test_an_edited_analysis_is_found_but_a_change_of_provenance_alone_is_not(results_copy):
    path = results_copy / "phase6" / "answer_v2.analysis.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["provenance"]["git_commit"] = "0" * 40  # the one thing a re-run after a commit changes: not part of the hash
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    assert verify.hash_problems(results_copy) == []
    data["per_intent"]["policy_question"]["fallbacks"] = 5
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    assert any("analysis_sha256" in p for p in verify.hash_problems(results_copy))


def test_a_result_file_without_any_hash_is_a_problem(results_copy):
    (results_copy / "stray.json").write_text("{}", encoding="utf-8")
    assert any("stray.json" in p and "neither" in p for p in verify.hash_problems(results_copy))


def test_a_comparison_that_does_not_match_its_runs_is_found(results_copy):
    path = results_copy / "comparison.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["deterministic"]["hybrid_minus_dense"]["none"]["product_search"]["rr"]["mean_diff"] += 0.5
    data["deterministic_sha256"] = verify.canonical_sha256(data["deterministic"])  # a forger who also fixes the hash is still caught by the recomputation
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    assert verify.hash_problems(results_copy) == []
    assert any("differs from the committed file" in p for p in verify.comparison_problems(results_copy))


def test_a_phase6_analysis_that_disagrees_with_the_official_result_is_found(tmp_path):
    analysis = json.loads(verify.PHASE6_ANALYSIS.read_text(encoding="utf-8"))
    official = json.loads(verify.PHASE6_OFFICIAL.read_text(encoding="utf-8"))
    a, o = tmp_path / "a.json", tmp_path / "o.json"
    o.write_text(json.dumps(official), encoding="utf-8")
    assert verify.phase6_problems(verify.PHASE6_ANALYSIS, o) == []
    bad = copy.deepcopy(analysis)
    bad["per_intent"]["order_status"]["correct"] -= 1
    bad["per_intent"]["product_search"]["fallbacks"] += 1
    bad["per_intent"]["all"]["fallbacks"] += 1
    bad["per_intent"]["product_search"]["items_with_fallback"] = ["gs-001"]
    a.write_text(json.dumps(bad), encoding="utf-8")
    problems = verify.phase6_problems(a, o)
    assert any("order_status" in p for p in problems) and any("fallbacks" in p for p in problems) and any("items_with_fallback" in p for p in problems)


# --- the network guard -------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("address,public", [(("127.0.0.1", 80), False), (("::1", 80, 0, 0), False), (("10.0.0.5", 80), False), (("192.168.1.9", 80), False),
                                            (("172.20.3.4", 80), False), (("169.254.1.1", 80), False), (("8.8.8.8", 53), True), (("142.250.80.46", 443), True),
                                            (("2001:4860:4860::8888", 443, 0, 0), True), (("generativelanguage.googleapis.com", 443), True), ("/tmp/socket", False)])
def test_only_public_addresses_are_refused(address, public):
    assert network_guard.is_public_address(address) is public


def test_the_installed_guard_refuses_a_public_connect_before_any_packet_and_lets_loopback_through(monkeypatch):
    network_guard.install(monkeypatch)
    with socket.socket() as probe:
        with pytest.raises(OSError, match="SHOPMATE_BLOCK_NETWORK"):
            probe.connect(("8.8.8.8", 53))
        with pytest.raises(OSError, match="SHOPMATE_BLOCK_NETWORK"):
            probe.connect_ex(("1.1.1.1", 443))
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0)); server.listen(1)
        with socket.socket() as client:
            client.connect(server.getsockname())  # loopback is allowed: the monitor tests start real servers


def test_the_session_fixture_wires_the_guard_when_the_variable_is_set():
    """A child pytest run with SHOPMATE_BLOCK_NETWORK=1: the probe test (skipped otherwise) must see its public connect refused by conftest's autouse fixture."""
    env = {**__import__("os").environ, "SHOPMATE_BLOCK_NETWORK": "1", "SHOPMATE_PROBE": "1"}
    result = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests/probe_network_guard.py"], cwd=REPO, env=env, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0 and "1 passed" in result.stdout, result.stdout + result.stderr

"""docs/RESULTS-final.md and the README's test column say what eval/results/phase8_test/final_evaluation.json says, and nothing else.

Three layers: the committed document is byte for byte what scripts/render_results_final.py makes from the result files; a second check that does not use the
script's own formatting re-derives every figure of the tables from the JSON and finds it in the document; and the registered text (PREREG section 10, D8.6)
is in the document unchanged. Nothing here opens the test split: the result file is a committed artefact, read like any other."""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

from scripts import render_results_final as rr

REPO = Path(__file__).resolve().parent.parent
DOC = REPO / "docs" / "RESULTS-final.md"
README = REPO / "README.md"

pytestmark = pytest.mark.skipif(not rr.TEST_RESULT.is_file(), reason="the test-split result is not in this checkout")


@pytest.fixture(scope="module")
def test_doc() -> dict:
    return json.loads(rr.TEST_RESULT.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def dev_doc() -> dict:
    return json.loads(rr.DEV_RESULT.read_text(encoding="utf-8"))


def section(text: str, start: str, end: str) -> str:
    return text[text.index(start):text.index(end)]


def metric(doc: dict, figure: str, name: str) -> dict:
    if name.startswith("latency"):
        return doc["measurements"]["latency_ms"][figure][name]
    return doc["deterministic"]["metrics"][figure][name]


def numbers(name: str, e: dict) -> list[str]:
    """Every figure of one cell, formatted here and not by the script."""
    places = 1 if name.startswith("latency") else 4 if name.startswith("cost") else 3
    out = [f"{e['estimate']:.{places}f}", f"[{e['ci_low']:.{places}f}, {e['ci_high']:.{places}f}]"]
    out.append(f"({e['successes']}/{e['n']})" if e.get("successes") is not None else f"(n={e['n']})")
    return out


# --- the document is what the files give ------------------------------------------------------------------------------------------------------


def test_the_committed_document_is_what_the_result_files_give():
    assert DOC.read_text(encoding="utf-8") == rr.render()


def test_the_run_the_document_describes_is_the_one_registered(test_doc):
    d, runs = test_doc["deterministic"], rr.attempts()
    assert d["split"] == "test" and d["n_items"] == 33 and d["provenance"]["rehearsal"] is False and d["provenance"]["git_dirty"] is False
    assert d["provenance"]["freeze"]["ref"] == "v1.0-frozen"
    assert [a["kind"] for a in runs] == ["run"] and runs[0]["ended"] == "complete" and runs[0]["split"] == "test" and runs[0]["live"] is True
    assert runs[0]["sha256"] == test_doc["deterministic_sha256"]
    assert test_doc["measurements"]["live_calls_this_run"] == runs[0]["live_calls"]
    assert test_doc["deterministic_sha256"] in DOC.read_text(encoding="utf-8")


def test_every_figure_of_the_results_table_is_in_the_row_of_its_metric(test_doc, dev_doc):
    text = DOC.read_text(encoding="utf-8")
    table = section(text, "## 1. Results", "## 2. Large dev-test gaps")
    for name, *_ in rr.METRICS:
        rows = [line for line in table.splitlines() if line.startswith(f"| {name} |")]
        assert len(rows) == 1, name
        cells = [c.strip() for c in rows[0].strip("|").split("|")]
        for cell_text, (doc, figure) in zip(cells[2:], ((dev_doc, "all"), (test_doc, "all"), (test_doc, "without_excluded"))):
            expected = numbers(name, metric(doc, figure, name))
            assert cell_text == f"{expected[0]} {expected[2]} {expected[1]}", (name, figure, cell_text)


def test_the_two_figures_have_the_registered_populations(test_doc):
    d = test_doc["deterministic"]
    assert d["excluded_matched"] == 1
    text = DOC.read_text(encoding="utf-8")
    assert f"test, all items ({d['n_items']})" in text and f"test, without the excluded item ({d['n_items'] - 1})" in text


def test_the_list_of_large_gaps_follows_the_registered_rule(test_doc, dev_doc):
    """PREREG section 7: large = the dev point estimate lies outside the registered 95% interval of the test result."""
    expected = []
    for name, *_ in rr.METRICS:
        dev = metric(dev_doc, "all", name)["estimate"]
        if any(dev < metric(test_doc, f, name)["ci_low"] or dev > metric(test_doc, f, name)["ci_high"] for f in rr.FIGURES):
            expected.append(name)
    text = DOC.read_text(encoding="utf-8")
    listed = re.findall(r"^- `(\w+)`: the dev estimate", section(text, "## 2. Large dev-test gaps", "## 3. "), re.M)
    assert listed == expected
    assert expected, "the run has large gaps: if this fails the document must say 'no metric', which the script does"
    assert section(text, "## 2. Large dev-test gaps", "## 3. ").count("the dev number was optimistic") == len(expected)


def test_the_secondary_tables_have_every_group_with_its_size_and_the_small_flag(test_doc):
    text = DOC.read_text(encoding="utf-8")
    secondary = test_doc["deterministic"]["secondary"]
    for key, heading in (("by_author", "### By author"), ("by_intent", "### By intent")):
        block = section(text, heading, "### By intent" if key == "by_author" else "## 4. ")
        for group, g in secondary[key].items():
            rows = [line for line in block.splitlines() if re.match(rf"\| {group}( \(small\))? \|", line)]
            assert len(rows) == 1, group
            cells = [c.strip() for c in rows[0].strip("|").split("|")]
            assert cells[1] == str(g["n_items"])
            assert ("(small)" in rows[0].split("|")[1]) == g["too_small_to_interpret"]
            for column, p in g["proportions"].items():
                if p["n"]:
                    assert f"{p['successes']}/{p['n']} = {p['estimate']:.3f} [{p['ci_low']:.3f}, {p['ci_high']:.3f}]" in rows[0], (group, column)
            for column, m in g["means"].items():
                if m["n"]:
                    assert f"{m['estimate']:.3f} (n={m['n']})" in rows[0], (group, column)
    assert "8 items" in text  # the threshold of the small flag, read from the file by the script


def test_the_registered_text_is_in_the_document_unchanged():
    text = DOC.read_text(encoding="utf-8")
    prereg = rr.PREREG.read_text(encoding="utf-8")
    decisions = rr.DECISIONS.read_text(encoding="utf-8")
    assert rr.prereg_section_10(prereg) in text
    assert rr.decision_d86(decisions) in text
    for heading in re.findall(r"^### .+$", rr.prereg_section_10(prereg), re.M):
        assert heading in text


def test_the_discussion_comes_after_every_table():
    text = DOC.read_text(encoding="utf-8")
    assert text.index("## 1. Results") < text.index("## 2. ") < text.index("## 3. ") < text.index("## 4. ") < text.index("## 5. Discussion") < text.index("## 6. ")
    assert "no threshold" in text.lower() or "no pass/fail" in text.lower()


# --- the README --------------------------------------------------------------------------------------------------------------------------------


def test_the_readme_test_column_has_both_figures_of_every_row_it_fills(test_doc):
    readme = README.read_text(encoding="utf-8")
    cells = rr.readme_cells()
    for key, cell_text in cells.items():
        assert cell_text in readme, key
    assert "docs/RESULTS-final.md" in readme
    # the cell of a metric names both the figure on all items and the one without the excluded item
    for key in ("router", "extraction", "no_answer", "answers", "guardrail", "cost"):
        assert cells[key].count("without the excluded item") == 1


def test_the_readme_no_longer_says_the_test_split_is_unevaluated():
    readme = README.read_text(encoding="utf-8")
    assert "No test number is claimed here" not in readme
    assert "dev split only" not in readme.split("## Quickstart")[0]


# --- the script reads files and nothing else ---------------------------------------------------------------------------------------------------


def test_the_script_never_touches_the_golden_loader_or_a_service():
    source = (REPO / "scripts" / "render_results_final.py").read_text(encoding="utf-8")
    for forbidden in ("load_split", "eval.golden", "golden.vi", "GOLDEN_PATH", "load_golden", "final_evaluation" + "="):
        assert forbidden not in source, forbidden
    imported = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported <= {"__future__", "argparse", "json", "re", "sys", "pathlib", "app"}, imported

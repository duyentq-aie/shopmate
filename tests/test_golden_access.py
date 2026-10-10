"""Nothing outside eval/golden/ may open golden.vi.json. Evaluation code reads golden items only through
eval.golden.loader.load_split("dev"); the test split stays locked (docs/DECISIONS-phase6.md, A0).

What is looked for is a REFERENCE to the file, so that no new module can read it by accident:
- importing or naming `GOLDEN_PATH` or `load_golden` (the raw path and the loader that returns every item, test split
  included), and
- a string literal that is the path of the file (`"golden.vi.json"`, `"eval/golden/golden.vi.json"`, ...).
Prose about the file in a docstring or an assertion message is not a reference. Provenance that needs a hash of the file
goes through eval.golden.loader.golden_file_sha256().
"""

from __future__ import annotations

import ast
import hashlib
from pathlib import Path

from eval.golden.loader import golden_file_sha256

REPO = Path(__file__).resolve().parent.parent
FORBIDDEN_NAMES = {"GOLDEN_PATH", "load_golden"}
FILE_NAME = "golden.vi.json"
SKIPPED_DIRS = {".venv", ".git", "__pycache__", "node_modules", ".pytest_cache"}
# The tests OF the golden tooling itself: they exist to check the frozen test-split hash, the schema and the merge, so they
# must see every item. Everything else (every other test, app/, scripts/, the rest of eval/) is held to the rule.
TOOLING_TESTS = {"tests/test_golden.py", "tests/test_frozen_test_split.py", "tests/test_merge_human_items.py"}
THIS_FILE = "tests/test_golden_access.py"


def references(source: str) -> list[str]:
    """What in this Python source refers to the golden file or to the loader of all its items."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            found.append(f"name {node.id}")
        elif isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_NAMES:
            found.append(f"attribute {node.attr}")
        elif isinstance(node, ast.alias) and node.name.split(".")[-1] in FORBIDDEN_NAMES:
            found.append(f"import {node.name}")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.endswith(FILE_NAME) and len(node.value) < 80:
            found.append(f"path literal {node.value!r}")  # a path, not a sentence that mentions it
    return found


def offenders(root: Path, allowed: set[str]) -> dict[str, list[str]]:
    result = {}
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root).as_posix()
        if SKIPPED_DIRS & set(path.relative_to(root).parts) or relative.startswith("eval/golden/") or relative in allowed:
            continue
        found = references(path.read_text(encoding="utf-8"))
        if found:
            result[relative] = found
    return result


def test_the_only_exceptions_are_the_three_tests_of_the_golden_tooling_listed_by_name():
    """Approved by the owner (docs/DECISIONS-phase6.md, A0): these three check the schema, the frozen test-split hash and the merge, so they see every item."""
    assert TOOLING_TESTS == {"tests/test_golden.py", "tests/test_frozen_test_split.py", "tests/test_merge_human_items.py"}
    assert all((REPO / name).is_file() for name in TOOLING_TESTS)


def test_no_module_outside_eval_golden_references_the_golden_file():
    assert offenders(REPO, TOOLING_TESTS | {THIS_FILE}) == {}


def test_the_guard_itself_catches_each_kind_of_reference_and_leaves_clean_code_alone(tmp_path):
    (tmp_path / "eval" / "golden").mkdir(parents=True)
    bad = {
        "a_import.py": "from eval.golden.schema import GOLDEN_PATH\n",
        "b_attribute.py": "import eval.golden.schema as s\nitems = s.load_golden()\n",
        "c_literal.py": 'text = open("eval/golden/golden.vi.json", encoding="utf-8").read()\n',
        "d_path.py": 'from pathlib import Path\np = Path("x") / "golden.vi.json"\n',
        "e_loader.py": "from eval.golden.schema import load_golden\n",
    }
    for name, source in bad.items():
        (tmp_path / name).write_text(source, encoding="utf-8")
    (tmp_path / "clean.py").write_text('"""Reads golden.vi.json only through the loader."""\nfrom eval.golden.loader import load_split\nitems = load_split("dev")\n',
                                       encoding="utf-8")
    (tmp_path / "eval" / "golden" / "inside.py").write_text("from eval.golden.schema import GOLDEN_PATH\n", encoding="utf-8")
    (tmp_path / "allowed.py").write_text("from eval.golden.schema import load_golden\n", encoding="utf-8")
    assert set(offenders(tmp_path, {"allowed.py"})) == set(bad)  # not clean.py, not eval/golden/, not the allowed file


def test_the_file_hash_for_provenance_comes_from_the_golden_package(tmp_path):
    sample = tmp_path / "sample.json"
    sample.write_bytes(b'{"items": []}\n')
    assert golden_file_sha256(sample) == hashlib.sha256(b'{"items": []}\n').hexdigest()

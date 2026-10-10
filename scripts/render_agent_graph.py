"""Rewrite the diagram of docs/AGENT-DESIGN.md from the compiled agent graph, so the picture is the code and not a drawing.

    uv run python -m scripts.render_agent_graph          # rewrites the block between <!-- graph:start --> and <!-- graph:end -->
    uv run python -m scripts.render_agent_graph --check  # exit 1 if the document is out of date (tests/test_agent_design_doc.py does this too)
"""

from __future__ import annotations

import argparse
import re
import sys

from app.agent.graph import Deps, build_graph
from app.config import REPO_ROOT

DOC = REPO_ROOT / "docs" / "AGENT-DESIGN.md"
BLOCK = re.compile(r"(<!-- graph:start -->\n)(.*?)(\n<!-- graph:end -->)", re.S)


def mermaid() -> str:
    graph = build_graph(Deps(router=None, extractor=None, tools=None, answerer=None))  # nothing is called when a graph is built
    return graph.get_graph().draw_mermaid().strip("\n")


def render() -> str:
    return f"```mermaid\n{mermaid()}\n```"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="only check, do not write")
    args = parser.parse_args(argv)
    text = DOC.read_text(encoding="utf-8")
    current = BLOCK.search(text).group(2)
    if current == render():
        print("docs/AGENT-DESIGN.md: the diagram is up to date")
        return 0
    if args.check:
        print("docs/AGENT-DESIGN.md: the diagram is out of date; run: uv run python -m scripts.render_agent_graph", file=sys.stderr)
        return 1
    DOC.write_text(BLOCK.sub(lambda m: m.group(1) + render() + m.group(3), text), encoding="utf-8")
    print("docs/AGENT-DESIGN.md: the diagram was rewritten")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

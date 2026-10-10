"""docs/AGENT-DESIGN.md must describe the agent that exists: the node table, the diagram and the spans are compared with the code
(app/agent/graph.py). If this test fails, one of them changed alone."""

from __future__ import annotations

import ast
import re
from pathlib import Path

from langgraph.graph import END, START

from app.agent.graph import NODE_TARGETS, Deps, build_graph
from scripts.render_agent_graph import BLOCK, DOC, render

REPO = Path(__file__).resolve().parent.parent
TABLE = re.compile(r"<!-- nodes:start -->\n(.*?)\n<!-- nodes:end -->", re.S)
SPANS_OPENED_BY_THE_NODE = {"answer", "reply", "ticket_confirm"}  # the others (router, extractor, retrieval, order_lookup) open inside the component or tool


def table_rows() -> dict[str, dict]:
    rows = {}
    for line in TABLE.search(DOC.read_text(encoding="utf-8")).group(1).splitlines()[2:]:  # skip the header and the separator
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        name = cells[0].strip("`")
        targets = () if cells[3] == "none" else tuple(t.strip().strip("`") for t in cells[3].split(","))
        rows[name] = {"kind": cells[1], "spans": {x.strip().strip("`") for x in cells[2].split(" or ")}, "targets": targets, "bound": cells[4]}
    return rows


def test_the_node_table_of_the_design_doc_is_the_node_list_of_the_code():
    rows = table_rows()
    assert set(rows) == set(NODE_TARGETS)
    for name, targets in NODE_TARGETS.items():
        assert set(rows[name]["targets"]) == set(targets), name


def test_the_compiled_graph_has_exactly_the_documented_edges():
    graph = build_graph(Deps(router=None, extractor=None, tools=None, answerer=None)).get_graph()
    edges = {(e.source, e.target) for e in graph.edges}
    expected = {(START, "intake")}
    for name, targets in NODE_TARGETS.items():
        expected |= {(name, target) for target in targets} | {(name, END)}  # every node can also end the turn (the step cap)
    assert edges == expected
    assert set(graph.nodes) == set(NODE_TARGETS) | {START, END}


def test_the_diagram_of_the_design_doc_is_the_one_drawn_from_the_code():
    assert BLOCK.search(DOC.read_text(encoding="utf-8")).group(2) == render(), "run: uv run python -m scripts.render_agent_graph"


def test_the_spans_of_the_table_are_the_spans_the_nodes_open():
    tree = ast.parse((REPO / "app" / "agent" / "graph.py").read_text(encoding="utf-8"))
    functions = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name in NODE_TARGETS}
    assert set(functions) == set(NODE_TARGETS)
    for name, row in table_rows().items():
        opened = {call.args[0].value for call in ast.walk(functions[name]) if isinstance(call, ast.Call)
                  and getattr(call.func, "id", "") == "span" and call.args and isinstance(call.args[0], ast.Constant)}
        assert opened == (row["spans"] & SPANS_OPENED_BY_THE_NODE), (name, opened, row["spans"])


def test_the_bounds_stated_in_the_doc_are_the_constants_of_the_code():
    from app.agent import graph

    text = DOC.read_text(encoding="utf-8")
    assert (graph.MAX_STEPS, graph.MAX_CLARIFY, graph.MAX_VERIFY_FAILURES, graph.MAX_CONFIRM_ASKS) == (8, 2, 3, 2)
    for phrase in ("| Clarifying turns for one request | **2** |", "| Failed verifications | **3** per conversation |",
                   "| Ticket confirmation | **2** asks |", "| Steps per turn | **8** (`max_steps`), LangGraph `recursion_limit` 10"):
        assert phrase in text, phrase

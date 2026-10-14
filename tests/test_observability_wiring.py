"""Where Langfuse is switched on (docs/DECISIONS-langfuse.md, L1 and L7): the one entry point that serves real customers, and nowhere else.
Tests, the evaluation runners and the demo script build their own Tracer without a sink, so a key in .env can never make a test run send traces."""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import app.chat_api as chat_api

REPO = Path(__file__).resolve().parent.parent


def test_the_tracer_of_the_live_backend_gets_the_sink_the_factory_builds(monkeypatch):
    sentinel = object()
    monkeypatch.setattr(chat_api, "build_sink", lambda: sentinel)
    assert chat_api.build_tracer().sink is sentinel


def test_without_keys_the_factory_gives_none_and_the_tracer_runs_as_before(monkeypatch):
    monkeypatch.setattr(chat_api, "build_sink", lambda: None)
    assert chat_api.build_tracer().sink is None


def test_the_live_backend_uses_that_tracer():
    assert "tracer=build_tracer()" in inspect.getsource(chat_api.build_live_backend)


def test_no_other_module_switches_langfuse_on():
    """Only the chat API's entry point uses `build_sink` in code (the factory defines it) (prose about it does not count); a library module or a runner that did would send traces from a test."""
    users = []
    for path in sorted(REPO.rglob("*.py")):
        relative = path.relative_to(REPO).as_posix()
        if relative.startswith((".venv/", ".git/", "tests/")) or "__pycache__" in relative:
            continue
        names = {node.id for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))) if isinstance(node, ast.Name)}
        names |= {node.name for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))) if isinstance(node, ast.alias)}
        if "build_sink" in names:
            users.append(relative)
    assert users == ["app/chat_api.py"]

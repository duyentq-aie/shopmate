"""Shared fixtures. Heavy resources (the bge-m3 model) load once per session, only when used."""

from __future__ import annotations

import os

import pytest


@pytest.fixture(scope="session", autouse=True)
def _no_public_network():
    """CI sets SHOPMATE_BLOCK_NETWORK=1 (.github/workflows/ci.yml): no test may reach a public address. Off by default, so a local run is unchanged."""
    if os.environ.get("SHOPMATE_BLOCK_NETWORK") != "1":
        yield
        return
    from tests import network_guard

    with pytest.MonkeyPatch.context() as monkeypatch:
        network_guard.install(monkeypatch)
        yield


@pytest.fixture(scope="session")
def embedder():
    from app.retrieval.embedder import BgeM3Embedder

    return BgeM3Embedder()

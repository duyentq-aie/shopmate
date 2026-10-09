"""Shared fixtures. Heavy resources (the bge-m3 model) load once per session, only when used."""

from __future__ import annotations

import pytest


@pytest.fixture(scope="session")
def embedder():
    from app.retrieval.embedder import BgeM3Embedder

    return BgeM3Embedder()

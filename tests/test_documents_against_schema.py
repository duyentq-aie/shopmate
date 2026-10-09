"""Validate every GraphQL document against Saleor's SDL, without a live Saleor.

The SDL is a versioned fixture copied from the Saleor image the stack runs
(tests/fixtures/saleor-schema-3.23.40.graphql, BSD-3-Clause, see the file header).
These tests never skip: a missing fixture is a failure. Set SALEOR_SCHEMA_SDL to
validate against another schema file, e.g. after upgrading the Saleor image.
"""

from __future__ import annotations

import os
from functools import cache
from pathlib import Path

import pytest
from graphql import build_schema, parse, validate

from app.saleor.readonly_client import ALLOWED_OPERATIONS
from scripts.seedlib.documents import ALL_DOCUMENTS

FIXTURE = Path(__file__).parent / "fixtures" / "saleor-schema-3.23.40.graphql"
SDL_PATH = Path(os.environ.get("SALEOR_SCHEMA_SDL") or FIXTURE)


@cache
def _schema():
    assert SDL_PATH.is_file(), f"Saleor schema fixture missing: {SDL_PATH}"
    return build_schema(SDL_PATH.read_text(encoding="utf-8"))


def test_schema_fixture_is_present_and_versioned():
    assert FIXTURE.is_file()
    header = FIXTURE.read_text(encoding="utf-8").splitlines()[:7]
    assert any("3.23.40" in line for line in header)
    assert (FIXTURE.parent / "SALEOR-LICENSE").is_file()


@pytest.mark.parametrize("name", sorted(ALLOWED_OPERATIONS))
def test_runtime_document_is_valid(name):
    errors = validate(_schema(), parse(ALLOWED_OPERATIONS[name].document))
    assert not errors, [e.message for e in errors]


@pytest.mark.parametrize("name", sorted(ALL_DOCUMENTS))
def test_seeder_document_is_valid(name):
    errors = validate(_schema(), parse(ALL_DOCUMENTS[name]))
    assert not errors, [e.message for e in errors]

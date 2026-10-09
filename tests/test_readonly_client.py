"""The runtime client must refuse anything but allowlisted queries BEFORE any network call."""

from __future__ import annotations

import json

import httpx
import pytest

from app.saleor import readonly_client as rc
from app.saleor.readonly_client import (
    ALLOWED_OPERATIONS,
    InvalidVariablesError,
    Operation,
    ReadOnlySaleorClient,
    ReadOnlyViolation,
    UnknownOperationError,
)

URL = "http://saleor.test/graphql/"


class RecordingTransport(httpx.BaseTransport):
    """Fake transport: records requests and answers with canned data."""

    def __init__(self, data: dict | None = None):
        self.requests: list[httpx.Request] = []
        self.data = data or {}

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(200, json={"data": self.data})


@pytest.fixture
def transport():
    return RecordingTransport({"channel": {"id": "Q2hhbm5lbDox", "slug": "vn"}})


@pytest.fixture
def client(transport):
    with ReadOnlySaleorClient(URL, "test-token", transport=transport) as c:
        yield c


def _inject(monkeypatch, name: str, document: str) -> None:
    """Simulate a bad allowlist entry (bypassing the import-time check)."""
    monkeypatch.setitem(rc._ALLOWLIST, name, Operation(name, document, rc.ChannelBySlugVars))


def test_allowlisted_query_is_sent(client, transport):
    data = client.execute("ChannelBySlug", {"slug": "vn"})
    assert data["channel"]["slug"] == "vn"
    assert len(transport.requests) == 1
    body = json.loads(transport.requests[0].content)
    assert body["operationName"] == "ChannelBySlug"
    assert body["variables"] == {"slug": "vn"}
    assert body["query"].lstrip().startswith("query ChannelBySlug")
    assert transport.requests[0].headers["Authorization"] == "Bearer test-token"


def test_unknown_operation_name_is_rejected(client, transport):
    with pytest.raises(UnknownOperationError):
        client.execute("orderCancel", {"id": "T3JkZXI6MQ=="})
    assert transport.requests == []


def test_raw_graphql_text_as_name_is_rejected(client, transport):
    with pytest.raises(UnknownOperationError):
        client.execute('mutation { orderCancel(id: "x") { order { id } } }')
    assert transport.requests == []


def test_mutation_is_rejected(monkeypatch, client, transport):
    _inject(monkeypatch, "CancelOrder", 'mutation CancelOrder { orderCancel(id: "x") { order { id } } }')
    with pytest.raises(ReadOnlyViolation, match="mutation"):
        client.execute("CancelOrder", {"slug": "vn"})
    assert transport.requests == []


def test_subscription_is_rejected(monkeypatch, client, transport):
    _inject(monkeypatch, "Events", "subscription Events { event { __typename } }")
    with pytest.raises(ReadOnlyViolation, match="subscription"):
        client.execute("Events", {"slug": "vn"})
    assert transport.requests == []


def test_multi_operation_document_is_rejected(monkeypatch, client, transport):
    _inject(
        monkeypatch,
        "Sneaky",
        'query Sneaky { shop { name } }\nmutation Other { orderCancel(id: "x") { order { id } } }',
    )
    with pytest.raises(ReadOnlyViolation, match="exactly one operation"):
        client.execute("Sneaky", {"slug": "vn"})
    assert transport.requests == []


def test_two_queries_in_one_document_are_rejected(monkeypatch, client, transport):
    _inject(monkeypatch, "Twice", "query Twice { shop { name } }\nquery Again { shop { name } }")
    with pytest.raises(ReadOnlyViolation, match="exactly one operation"):
        client.execute("Twice", {"slug": "vn"})
    assert transport.requests == []


def test_anonymous_or_misnamed_query_is_rejected(monkeypatch, client, transport):
    _inject(monkeypatch, "Named", "{ shop { name } }")
    with pytest.raises(ReadOnlyViolation, match="name"):
        client.execute("Named", {"slug": "vn"})
    assert transport.requests == []


def test_unparseable_document_is_rejected(monkeypatch, client, transport):
    _inject(monkeypatch, "Broken", "query Broken { shop { name }")
    with pytest.raises(ReadOnlyViolation, match="does not parse"):
        client.execute("Broken", {"slug": "vn"})
    assert transport.requests == []


def test_invalid_or_extra_variables_are_rejected(client, transport):
    with pytest.raises(InvalidVariablesError):
        client.execute("ChannelBySlug", {"slug": "VN; DROP"})
    with pytest.raises(InvalidVariablesError):
        client.execute("ChannelBySlug", {"slug": "vn", "query": "mutation { x }"})
    assert transport.requests == []


def test_allowlist_is_read_only_and_contains_only_queries():
    with pytest.raises(TypeError):
        ALLOWED_OPERATIONS["Evil"] = None  # type: ignore[index]
    for name, operation in ALLOWED_OPERATIONS.items():
        rc.assert_single_query(operation.document, name)
        assert "mutation" not in operation.document.lower()


def test_client_has_no_raw_query_entry_point():
    public = {n for n in dir(ReadOnlySaleorClient) if not n.startswith("_")}
    assert public == {"execute", "close"}

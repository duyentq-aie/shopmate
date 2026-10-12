"""The fingerprints of the state outside the repository (eval/external_state.py): the Qdrant collection and the seed orders in Saleor.

Everything here uses fake clients: no service is needed and nothing is read from or written to one."""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from qdrant_client import models

from eval import external_state as es

REPO = Path(__file__).resolve().parent.parent
RAW_EMAIL = "khachhang99@example.com"


# --- fakes ---------------------------------------------------------------------------------------------------------------------------------------------


class FakeQdrant:
    """The three calls the module may make, over a list of (id, payload)."""

    def __init__(self, points, vectors=None, sparse=None):
        self.points = points
        params = SimpleNamespace(
            vectors=vectors or {"dense": models.VectorParams(size=4, distance=models.Distance.COSINE)},
            sparse_vectors=sparse or {"sparse": models.SparseVectorParams(), "folded": models.SparseVectorParams(modifier=models.Modifier.IDF)},
        )
        self.info = SimpleNamespace(config=SimpleNamespace(params=params))
        self.calls: list[str] = []

    def get_collection(self, name):
        self.calls.append("get_collection")
        return self.info

    def count(self, collection_name, exact):
        self.calls.append("count")
        assert exact is True
        return SimpleNamespace(count=len(self.points))

    def scroll(self, collection_name, limit, offset, with_payload, with_vectors):
        self.calls.append("scroll")
        assert with_payload is True and with_vectors is False  # the vectors are not read
        start = offset or 0
        chunk = self.points[start:start + limit]
        nxt = start + limit if start + limit < len(self.points) else None
        return [SimpleNamespace(id=pid, payload=payload) for pid, payload in chunk], nxt


class FakeSaleor:
    """Answers OrderByNumber from a dict number -> node; refuses any other operation."""

    def __init__(self, nodes):
        self.nodes = nodes
        self.operations: list[str] = []
        self.closed = False

    def execute(self, operation, variables=None):
        self.operations.append(operation)
        assert operation == "OrderByNumber"
        node = self.nodes.get(int(variables["number"]))
        return {"orders": {"edges": [{"node": node}] if node else []}}

    def close(self):
        self.closed = True


def points(n=7):
    return [(f"id-{i:02d}", {"doc_type": "product" if i % 3 else "policy", "sku": f"SM-{i:04d}", "price_vnd": 1000 * i, "color": "Đen"}) for i in range(n)]


def node(number, status="UNFULFILLED", amount=300000, lines=2, email=RAW_EMAIL):
    return {"number": str(number), "status": status, "userEmail": email, "total": {"gross": {"amount": float(amount), "currency": "VND"}}, "lines": [{}] * lines}


def nodes(numbers=es.SEED_ORDER_NUMBERS):
    return {n: node(n, amount=100000 + n) for n in numbers}


# --- the Qdrant fingerprint ---------------------------------------------------------------------------------------------------------------------


def test_the_qdrant_fingerprint_has_the_count_the_vector_layout_and_the_digest_of_the_payload_list():
    fp = es.qdrant_fingerprint(FakeQdrant(points()), "col", page=3)
    assert fp["collection"] == "col" and fp["points"] == 7 and fp["scrolled"] == 7 and fp["by_doc_type"] == {"policy": 3, "product": 4}
    assert fp["vectors"] == {"dense": {"size": 4, "distance": "Cosine"}, "sparse": {"modifier": "none"}, "folded": {"modifier": "idf"}}
    assert re.fullmatch(r"[0-9a-f]{64}", fp["payloads_sha256"])


def test_the_digest_does_not_depend_on_the_order_the_points_come_in_or_on_the_page_size_nor_on_the_order_of_the_keys_of_a_payload():
    base = es.qdrant_fingerprint(FakeQdrant(points()), "col", page=2)
    shuffled = list(reversed(points()))
    assert es.qdrant_fingerprint(FakeQdrant(shuffled), "col", page=100)["payloads_sha256"] == base["payloads_sha256"]
    reordered = [(pid, dict(reversed(list(payload.items())))) for pid, payload in points()]
    assert es.qdrant_fingerprint(FakeQdrant(reordered), "col")["payloads_sha256"] == base["payloads_sha256"]


@pytest.mark.parametrize("change", ["payload", "id", "added", "removed"])
def test_a_change_of_one_payload_one_id_or_the_set_of_points_changes_the_digest(change):
    pts = points()
    base = es.qdrant_fingerprint(FakeQdrant(pts), "col")
    if change == "payload":
        pts[2] = (pts[2][0], {**pts[2][1], "price_vnd": 1})
    elif change == "id":
        pts[2] = ("id-zz", pts[2][1])
    elif change == "added":
        pts.append(("id-99", {"doc_type": "product"}))
    else:
        pts.pop()
    changed = es.qdrant_fingerprint(FakeQdrant(pts), "col")
    assert changed["payloads_sha256"] != base["payloads_sha256"]


def test_a_change_of_the_vector_layout_is_seen():
    base = es.qdrant_fingerprint(FakeQdrant(points()), "col")
    other = es.qdrant_fingerprint(FakeQdrant(points(), vectors={"dense": models.VectorParams(size=8, distance=models.Distance.COSINE)}), "col")
    assert other["vectors"] != base["vectors"] and other["payloads_sha256"] == base["payloads_sha256"]


def test_the_qdrant_client_is_only_asked_to_read():
    client = FakeQdrant(points())
    es.qdrant_fingerprint(client, "col")
    assert set(client.calls) == {"get_collection", "count", "scroll"}


# --- the Saleor fingerprint ---------------------------------------------------------------------------------------------------------------------


def test_the_orders_fingerprint_reads_thirty_orders_by_number_through_the_one_allowlisted_query_and_keeps_no_email():
    saleor = FakeSaleor(nodes())
    fp = es.saleor_orders_fingerprint(saleor)
    assert fp["numbers"] == "84-113" and fp["found"] == 30 and len(fp["_rows"]) == 30 and set(saleor.operations) == {"OrderByNumber"} and len(saleor.operations) == 30
    assert fp["_rows"][0].startswith("84|UNFULFILLED|100084.00 VND|2|") and re.fullmatch(r"[0-9a-f]{64}", fp["_rows"][0].split("|")[-1])
    assert RAW_EMAIL not in json.dumps(fp, ensure_ascii=False) and not any("@" in row for row in fp["_rows"])
    assert RAW_EMAIL not in json.dumps(es.public(fp)) and "_rows" not in es.public(fp)


def test_a_status_a_total_a_line_an_email_or_a_missing_order_changes_the_digest_and_the_email_is_compared_case_insensitively():
    base = es.saleor_orders_fingerprint(FakeSaleor(nodes()))["rows_sha256"]
    for number, change in [(90, {"status": "CANCELED"}), (91, {"amount": 1}), (92, {"lines": 5}), (93, {"email": "someone.else@example.com"})]:
        altered = nodes()
        altered[number] = node(number, **{"amount": 100000 + number, **change})
        assert es.saleor_orders_fingerprint(FakeSaleor(altered))["rows_sha256"] != base, number
    gone = nodes()
    del gone[100]
    missing = es.saleor_orders_fingerprint(FakeSaleor(gone))
    assert missing["found"] == 29 and missing["rows_sha256"] != base
    upper = nodes()
    upper[95] = node(95, amount=100095, email=RAW_EMAIL.upper() + " ")
    assert es.saleor_orders_fingerprint(FakeSaleor(upper))["rows_sha256"] == es.saleor_orders_fingerprint(FakeSaleor({**nodes(), 95: node(95, amount=100095)}))["rows_sha256"]


# --- the comparison -----------------------------------------------------------------------------------------------------------------------------


def state(qdrant_points=None, order_nodes=None):
    qdrant = FakeQdrant(qdrant_points or points())
    saleor = FakeSaleor(order_nodes or nodes())
    return es.current_state(qdrant, saleor)


def registered_from(now):
    return {source: {k: v for k, v in es.public(fp).items() if k not in ("scrolled", "by_doc_type")} for source, fp in now.items()}


def test_the_registered_values_equal_the_current_ones_when_nothing_changed():
    now = state()
    assert es.state_problems(registered_from(now), state()) == []


def test_what_differs_is_named_with_the_registered_and_the_current_value_and_no_email():
    recorded = registered_from(state())
    changed = nodes()
    changed[88] = node(88, status="CANCELED", amount=100088)
    pts = points()
    pts[1] = (pts[1][0], {"doc_type": "product", "sku": "OTHER"})
    seed = json.loads((REPO / "data" / "seed" / "orders.vi.json").read_text(encoding="utf-8"))
    problems = es.state_problems(recorded, state(pts, changed), seed)
    text = "\n".join(problems)
    assert "qdrant.payloads_sha256: registered" in text and "saleor_orders.rows_sha256: registered" in text
    assert "qdrant.points" not in text and "qdrant.vectors" not in text  # only what differs
    assert "order #88: differs from the seed file in" in text and "status" in text.split("order #88")[1].splitlines()[0]
    assert RAW_EMAIL not in text and "@" not in text


def test_a_service_that_cannot_be_read_is_a_difference_not_a_pass_and_the_saleor_client_is_closed():
    def unreachable():
        raise ConnectionError("connection refused")

    assert "could not be read: ConnectionError" in "\n".join(es.check(registered_from(state()), clients=unreachable))
    saleor = FakeSaleor(nodes())
    assert es.check(registered_from(state()), clients=lambda: (FakeQdrant(points()), saleor)) == [] and saleor.closed


# --- the values registered in the repository ---------------------------------------------------------------------------------------------------


def test_the_registered_state_agrees_with_the_index_manifest():
    manifest = json.loads((REPO / "data" / "index" / "manifest.json").read_text(encoding="utf-8"))
    qdrant = es.REGISTERED_STATE["qdrant"]
    assert qdrant["collection"] == manifest["collection"] == es.QDRANT_COLLECTION
    assert qdrant["points"] == manifest["documents"]["total"] == 269
    assert qdrant["vectors"] == manifest["vectors"]


def test_the_manifest_check_reports_a_count_a_type_or_a_layout_that_differs():
    manifest = json.loads((REPO / "data" / "index" / "manifest.json").read_text(encoding="utf-8"))
    ok = {"collection": manifest["collection"], "points": 269, "scrolled": 269, "by_doc_type": {"policy": 19, "product": 250}, "vectors": manifest["vectors"]}
    assert es.manifest_problems(ok, manifest) == []
    assert es.manifest_problems({**ok, "points": 268, "scrolled": 268}, manifest)
    assert es.manifest_problems({**ok, "by_doc_type": {"policy": 20, "product": 249}}, manifest)
    assert es.manifest_problems({**ok, "vectors": {}}, manifest)


def test_the_pre_registration_states_the_registered_fingerprints():
    text = (REPO / "docs" / "PHASE8-PREREG.md").read_text(encoding="utf-8")
    qdrant, orders = es.REGISTERED_STATE["qdrant"], es.REGISTERED_STATE["saleor_orders"]
    for value in (qdrant["payloads_sha256"], orders["rows_sha256"]):
        assert value in text
    assert f"{qdrant['points']} points" in text and "#84 to #113" in text and f"{orders['found']} orders" in text


def test_the_module_only_reads_and_the_only_saleor_operation_is_the_allowlisted_order_query():
    source = (REPO / "eval" / "external_state.py").read_text(encoding="utf-8")
    assert set(re.findall(r'\.execute\(\s*"(\w+)"', source)) == {"OrderByNumber"}
    assert set(re.findall(r"\bclient\.(\w+)\(", source)) <= {"get_collection", "count", "scroll"}  # the Qdrant calls
    assert not re.search(r"upsert|delete|create_collection|recreate|set_payload|overwrite_payload|update_vectors|write_text|write_bytes|open\([^)]*[\"']w|subprocess|mutation", source)

"""Fingerprints of the state OUTSIDE the repository that the final evaluation depends on: the Qdrant collection and the seed orders in Saleor. Read only.

The repository pins the code, the prompts, the seed files and the index manifest; it cannot pin what is in a running service. Two things can change under a frozen commit without any file
changing: a re-index (or a manual edit) of the Qdrant collection, and an order in Saleor that is cancelled, fulfilled or edited (the runtime token has MANAGE_ORDERS, so the guard that keeps
it read only is in `app/saleor/readonly_client.py`, and this module only goes through that client). The values registered in docs/PHASE8-PREREG.md (section 1) are `REGISTERED_STATE` below, and
`eval/final_evaluation.py` recomputes them before it opens the test split and refuses to run when one differs.

- Qdrant: the point count, the vector configuration, and the sha256 of the sorted list of `(point id, sha256 of the canonical JSON of its payload)`. Vectors are not read; the manifest
  (data/index/manifest.json, frozen with the code) holds the digest of the vectors of the build, and `eval/run_phase6_eval.py` checks the live index against it.
- Saleor: the seed orders #84 to #113, one `OrderByNumber` query each (allowlisted, read only). Per order: number, status, total, number of lines, sha256 of the lower-cased customer email.
  The raw email is dropped inside `order_row` and appears nowhere: not in the fingerprint, not in a message, not in a file.

    uv run python -m eval.external_state          # prints both fingerprints and the check against the manifest; writes nothing
"""

from __future__ import annotations

import hashlib
import json
import sys
from collections.abc import Callable, Mapping
from typing import Any

QDRANT_COLLECTION = "shopmate_vn_v2"
SEED_ORDER_NUMBERS = tuple(range(84, 114))  # docs/SETUP.md: 30 seed orders, numbers #84-#113
SCROLL_PAGE = 128

# What docs/PHASE8-PREREG.md section 1 registers ("the index" and "the order data"); tests/test_external_state.py checks that the document states the same values.
REGISTERED_STATE: dict[str, dict[str, Any]] = {
    "qdrant": {
        "collection": "shopmate_vn_v2",
        "points": 269,  # = data/index/manifest.json documents.total (250 product + 19 policy)
        "vectors": {"dense": {"size": 1024, "distance": "Cosine"}, "folded": {"modifier": "idf"}, "sparse": {"modifier": "none"}},
        "payloads_sha256": "f2a7b8c2b7bd9021a526e2900f176cc6c4a7ec7472c10bf83334d55706cd292b",
    },
    "saleor_orders": {
        "numbers": "84-113",
        "found": 30,
        "rows_sha256": "33df74d4c896aa0c999e36a8b6f95ddcf74b429ce0f49363513cb5a7a25f4d18",  # equal, field by field, to the rows data/seed/orders.vi.json implies (checked 2026-10-12)
    },
}


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _value(x: Any) -> Any:
    """An enum of qdrant_client as its plain value; anything else as it is."""
    return getattr(x, "value", x)


def vector_layout(info) -> dict[str, dict[str, Any]]:
    """The vector configuration of a collection in the form the manifest records: dense vectors with size and distance, sparse vectors with their modifier."""
    params = info.config.params
    dense = params.vectors if isinstance(params.vectors, Mapping) else {"": params.vectors}
    layout: dict[str, dict[str, Any]] = {name: {"size": v.size, "distance": str(_value(v.distance))} for name, v in dense.items()}
    for name, v in (params.sparse_vectors or {}).items():
        layout[name] = {"modifier": str(_value(v.modifier) or "none")}
    return layout


def qdrant_fingerprint(client, collection: str = QDRANT_COLLECTION, page: int = SCROLL_PAGE) -> dict[str, Any]:
    """The point count, the vector layout and the digest of every (point id, payload digest); payloads are scrolled without vectors."""
    info = client.get_collection(collection)
    count = client.count(collection_name=collection, exact=True).count
    entries: list[tuple[str, str, str]] = []
    offset = None
    while True:
        records, offset = client.scroll(collection_name=collection, limit=page, offset=offset, with_payload=True, with_vectors=False)
        entries += [(str(r.id), sha256_text(canonical_json(r.payload or {})), str((r.payload or {}).get("doc_type", ""))) for r in records]
        if offset is None:
            break
    entries.sort()
    by_doc_type: dict[str, int] = {}
    for _, _, doc_type in entries:
        by_doc_type[doc_type] = by_doc_type.get(doc_type, 0) + 1
    return {
        "collection": collection,
        "points": count,
        "vectors": vector_layout(info),
        "payloads_sha256": sha256_text("\n".join(f"{point_id} {digest}" for point_id, digest, _ in entries)),
        "scrolled": len(entries),  # equal to `points` unless the collection changed while it was read
        "by_doc_type": dict(sorted(by_doc_type.items())),
    }


def order_row(number: int, node: Mapping[str, Any] | None) -> str:
    """One line per order. The customer email is hashed here and goes no further."""
    if node is None:
        return f"{number}|MISSING"
    total = node["total"]["gross"]
    email = hashlib.sha256((node.get("userEmail") or "").strip().lower().encode("utf-8")).hexdigest()
    return f"{node['number']}|{node['status']}|{float(total['amount']):.2f} {total['currency']}|{len(node.get('lines') or [])}|{email}"


def saleor_orders_fingerprint(saleor, numbers=SEED_ORDER_NUMBERS) -> dict[str, Any]:
    """The seed orders as they are in Saleor now, read one by one through the read-only client."""
    rows = []
    for number in sorted(numbers):
        edges = saleor.execute("OrderByNumber", {"number": str(number)})["orders"]["edges"]
        rows.append(order_row(number, edges[0]["node"] if edges else None))
    return {
        "numbers": f"{min(numbers)}-{max(numbers)}",
        "found": sum(1 for row in rows if not row.endswith("|MISSING")),
        "rows_sha256": sha256_text("\n".join(rows)),
        "_rows": rows,  # for a comparison with the seed file; never written out (see `public`)
    }


def public(fingerprint: Mapping[str, Any]) -> dict[str, Any]:
    """The fingerprint without the per-order rows."""
    return {k: v for k, v in fingerprint.items() if not k.startswith("_")}


def current_state(qdrant, saleor) -> dict[str, dict[str, Any]]:
    return {"qdrant": qdrant_fingerprint(qdrant), "saleor_orders": saleor_orders_fingerprint(saleor)}


def seed_order_rows(seed: Mapping[str, Any]) -> list[str]:
    """What `data/seed/orders.vi.json` says the orders should look like, in the form of `order_row` (status, total, lines, email digest)."""
    rows = []
    for order in sorted(seed["orders"], key=lambda o: int(o["number"])):
        email = hashlib.sha256(order["customer"]["email"].strip().lower().encode("utf-8")).hexdigest()
        rows.append(f"{order['number']}|{order['status']}|{float(order['total']):.2f} {seed.get('currency', 'VND')}|{len(order['lines'])}|{email}")
    return rows


def _order_differences(rows: list[str], seed_rows: list[str]) -> list[str]:
    """Which orders differ from the seed file, and in which field (never the value of an email)."""
    names = ("number", "status", "total", "lines", "email")
    seed = {row.split("|")[0]: row.split("|") for row in seed_rows}
    out = []
    for row in rows:
        parts = row.split("|")
        wanted = seed.get(parts[0])
        if wanted is None:
            out.append(f"  order #{parts[0]}: not in data/seed/orders.vi.json")
        elif parts[1:] == ["MISSING"]:
            out.append(f"  order #{parts[0]}: not found in Saleor")
        elif parts != wanted:
            fields = [n for n, a, b in zip(names, parts, wanted) if a != b]
            out.append(f"  order #{parts[0]}: differs from the seed file in {', '.join(fields)}")
    return out


def state_problems(recorded: Mapping[str, Mapping[str, Any]], now: Mapping[str, Mapping[str, Any]], seed: Mapping[str, Any] | None = None) -> list[str]:
    """What differs between the registered fingerprints and the current ones ([] when nothing does). Values printed are digests, counts and configuration, never an email."""
    problems = []
    for source, wanted in recorded.items():
        got = now.get(source, {})
        for key, want in wanted.items():
            if got.get(key) != want:
                problems.append(f"{source}.{key}: registered {want!r}, now {got.get(key)!r}")
    if problems and "qdrant" in now:
        problems.append(f"  (qdrant now: {now['qdrant'].get('scrolled')} points scrolled, by doc_type {now['qdrant'].get('by_doc_type')})")
    if any(p.startswith("saleor_orders.") for p in problems) and seed is not None and "_rows" in now.get("saleor_orders", {}):
        details = _order_differences(now["saleor_orders"]["_rows"], seed_order_rows(seed))
        problems.append("  orders that differ from data/seed/orders.vi.json:" if details else "  no order differs from data/seed/orders.vi.json field by field (the registered value was taken from a state that is not the seed's)")
        problems += details
    return problems


def manifest_problems(fingerprint: Mapping[str, Any], manifest: Mapping[str, Any]) -> list[str]:
    """The Qdrant fingerprint against data/index/manifest.json: collection, point count (269), documents by type, vector layout."""
    problems = []
    if fingerprint["collection"] != manifest["collection"]:
        problems.append(f"collection {fingerprint['collection']!r} != manifest {manifest['collection']!r}")
    documents = manifest["documents"]
    if fingerprint["points"] != documents["total"]:
        problems.append(f"{fingerprint['points']} points != manifest total {documents['total']}")
    if fingerprint["scrolled"] != fingerprint["points"]:
        problems.append(f"{fingerprint['scrolled']} points scrolled but {fingerprint['points']} counted")
    by_type = {k: v for k, v in documents.items() if k != "total"}
    if fingerprint["by_doc_type"] != dict(sorted(by_type.items())):
        problems.append(f"points by doc_type {fingerprint['by_doc_type']} != manifest {by_type}")
    if fingerprint["vectors"] != manifest["vectors"]:
        problems.append(f"vector layout {fingerprint['vectors']} != manifest {manifest['vectors']}")
    return problems


def live_clients() -> tuple[Any, Any]:
    """The real Qdrant client and the read-only Saleor client, built the way the runners build them (the token is read by `require_setting` and goes nowhere else)."""
    from app.config import api_url, require_setting
    from app.retrieval.store import connect
    from app.saleor.readonly_client import ReadOnlySaleorClient

    return connect(), ReadOnlySaleorClient(api_url(), require_setting("SALEOR_RUNTIME_TOKEN"))


def check(recorded: Mapping[str, Mapping[str, Any]] | None = None, clients: Callable[[], tuple[Any, Any]] | None = None) -> list[str]:
    """Recompute both fingerprints and say what differs from the registered ones. Read only; a service that cannot be read is a problem, not a pass.

    The defaults are looked up when called (REGISTERED_STATE and live_clients), so that a test can replace either."""
    from app.config import REPO_ROOT

    recorded = REGISTERED_STATE if recorded is None else recorded
    saleor = None
    try:
        qdrant, saleor = (clients or live_clients)()
        now = current_state(qdrant, saleor)
    except Exception as exc:  # unreachable, a bad token, a changed schema: the state could not be read, so it is not known to be the registered one
        return [f"the external state could not be read: {type(exc).__name__}: {str(exc)[:200]}"]
    finally:
        if saleor is not None and hasattr(saleor, "close"):
            saleor.close()
    seed = json.loads((REPO_ROOT / "data" / "seed" / "orders.vi.json").read_text(encoding="utf-8"))
    return state_problems(recorded, now, seed)


def main(argv: list[str] | None = None) -> int:
    from app.config import REPO_ROOT
    from app.retrieval.manifest import load_manifest

    qdrant, saleor = live_clients()
    try:
        now = current_state(qdrant, saleor)
    finally:
        saleor.close()
    problems = manifest_problems(now["qdrant"], load_manifest())
    print(json.dumps({k: public(v) for k, v in now.items()}, indent=2, sort_keys=True, ensure_ascii=False))
    print("manifest check: " + ("ok (collection, 269 points = 250 product + 19 policy, vector layout)" if not problems else "DIFFERS\n  " + "\n  ".join(problems)))
    registered = {k: v for k, v in REGISTERED_STATE.items() if v}
    if registered:
        differences = state_problems(registered, now, json.loads((REPO_ROOT / "data" / "seed" / "orders.vi.json").read_text(encoding="utf-8")))
        print("registered values: " + ("equal" if not differences else "DIFFER\n  " + "\n  ".join(differences)))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())

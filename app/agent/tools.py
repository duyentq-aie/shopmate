"""The agent's real tools (docs/AGENT-DESIGN.md section 6, docs/DECISIONS-phase6.md A6, A7, A8).

- search_products / search_policies: the existing retrieval (default mode `dense_folded`), one `retrieval` span each (the
  Retriever opens it). The doc type is forced here, so a product search can never return a policy and the other way round.
- lookup_order: the read-only Saleor client with an allowlisted query only. It reveals an order only when the order number AND
  the owner's email match the same order, in the shop's channel. An unknown number, a wrong email, an order of another channel
  and an order without an owner email are the same answer: not verified. A Saleor failure is NOT "not verified": it raises, so
  an outage can neither pass for a refusal nor count as a failed verification.
- create_ticket: the only write. It appends to data/tickets/tickets.jsonl (git-ignored) and is idempotent per conversation. It is
  called only by the `write_ticket` node, after an explicit yes (a test checks that no other code calls it). There are no Saleor writes
  anywhere in the agent: the only Saleor object here is the read-only client.
"""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import os
import re
import threading
from pathlib import Path

from app.agent.protocols import OrderLookup
from app.agent.relax import Offer, droppable, plan_offer
from app.config import REPO_ROOT
from app.tracing import span

TICKETS_PATH = REPO_ROOT / "data" / "tickets" / "tickets.jsonl"
PRODUCT_K, POLICY_K = 5, 3  # hits handed to the answer: more would only lengthen the prompt
SHOP_CHANNEL = "vn"  # orders of other channels are never revealed
ORDER_NUMBER = re.compile(r"[0-9]{1,7}")  # ASCII digits only: \d would also accept other scripts  # what the order number parser can produce; anything else is not looked up at all
EMAIL_SHAPE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]{2,}")


class TicketWriteError(Exception):
    """The ticket could not be written (disk, permissions)."""


class TicketStore:
    """data/tickets/tickets.jsonl: one JSON line per ticket, appended under a lock; the idempotency key is a hash of the
    conversation, the order number and the summary, so the same confirmed ticket is never written twice."""

    _lock = threading.Lock()

    def __init__(self, path: Path = TICKETS_PATH) -> None:
        self.path = Path(path)

    @staticmethod
    def key(conversation_id: str, order_number: str | None, summary: str) -> str:
        return hashlib.sha256(f"{conversation_id}\n{order_number or ''}\n{summary}".encode("utf-8")).hexdigest()

    def create(self, *, conversation_id: str, summary: str, order_number: str | None, contact_email: str) -> dict:
        """-> {"ticket_id": ..., "created": True} or, if this very ticket already exists, {"ticket_id": ..., "created": False}."""
        key = self.key(conversation_id, order_number, summary)
        ticket_id = "TCK-" + key[:10].upper()
        record = {"ticket_id": ticket_id, "idempotency_key": key, "conversation_id": conversation_id, "order_number": order_number,
                  "contact_email": contact_email, "summary": summary, "status": "open"}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self._lock, open(self.path, "a+", encoding="utf-8") as handle:
                fcntl.flock(handle, fcntl.LOCK_EX)  # another process may be writing too
                handle.seek(0)
                for line in handle:
                    if line.strip() and json.loads(line).get("idempotency_key") == key:
                        return {"ticket_id": ticket_id, "created": False}
                handle.seek(0, os.SEEK_END)
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        except (OSError, ValueError) as exc:
            raise TicketWriteError(f"{type(exc).__name__}: {exc}") from exc
        return {"ticket_id": ticket_id, "created": True}


def _product(hit) -> dict:
    p = hit.payload
    return {"key": hit.key, "sku": p["sku"], "name": p["name"], "kind": p["kind"], "color": p["color"], "material": p["material"],
            "sizes": list(p["sizes"]), "price_vnd": int(p["price_vnd"]), "in_stock": bool(p["in_stock"])}


def _policy(hit) -> dict:
    p = hit.payload
    return {"key": hit.key, "file": p["file"], "clause": int(p["clause"]), "title": p["title"], "text": hit.text}


def _minimal_order(node: dict) -> dict:
    """What the answer may know about a verified order: number, status, creation date, lines, fulfilments. Never the email,
    an address or a phone number (the query does not even select the last two)."""
    return {
        "number": str(node["number"]), "status": node["status"], "created": node["created"],
        "lines": [{"name": line["productName"], "sku": line.get("productSku") or (line.get("variant") or {}).get("sku"),
                   "quantity": line["quantity"], "quantity_fulfilled": line["quantityFulfilled"]} for line in node.get("lines") or []],
        "fulfillments": [{"status": f["status"], "tracking_number": f.get("trackingNumber"), "created": f["created"]}
                         for f in node.get("fulfillments") or []],
    }


class RealTools:
    def __init__(self, retriever, saleor=None, tickets: TicketStore | None = None, *, product_k: int = PRODUCT_K,
                 policy_k: int = POLICY_K) -> None:
        self.retriever, self.saleor, self.tickets = retriever, saleor, tickets or TicketStore()
        self.product_k, self.policy_k = product_k, policy_k

    def search_products(self, query: str, filters: dict) -> list[dict]:
        return [_product(h) for h in self.retriever.search(query, {**filters, "doc_type": "product"}, k=self.product_k)]

    def search_policies(self, query: str) -> list[dict]:
        return [_policy(h) for h in self.retriever.search(query, {"doc_type": "policy"}, k=self.policy_k)]

    def offer_for(self, filters: dict) -> Offer | None:
        """One `retrieval` span of mode "count": a handful of Qdrant counts, no embedding and no LLM. Its summary says which constraint blocks and how many
        products there would be (`blocking_constraint`, `offer_count`), plus what was dropped and every count made (`offer_drops`, `probes`)."""
        if not droppable(filters):
            return None
        with span("retrieval") as recorded:
            offer, probes = plan_offer(filters, lambda found: self.retriever.count({**found, "doc_type": "product"}))
            recorded.set_summary(mode="count", k=0, hits=[], blocking_constraint=offer.blocking if offer else None, offer_count=offer.count if offer else 0,
                                 offer_drops=list(offer.dropped) if offer else [], probes=probes)
        return offer

    def lookup_order(self, order_number: str, email: str) -> OrderLookup:
        if self.saleor is None:
            raise RuntimeError("order lookup is not configured (no Saleor client): this is not a failed verification")
        with span("order_lookup") as recorded:  # a refusal is a normal outcome and leaves the span clean; a Saleor failure is recorded by span()
            # The summary is for the operator only: `found` (an order of the shop channel has this number) never reaches the answer, which cannot
            # tell an unknown number from a wrong email (D28). No email, address or phone is ever put in it.
            if not ORDER_NUMBER.fullmatch(order_number or "") or not EMAIL_SHAPE.fullmatch((email or "").strip()):
                recorded.set_summary(verified=False, found=False)
                return OrderLookup(verified=False)  # not even asked: such a value can only be a mistake or an attack
            edges = (self.saleor.execute("OrderByNumber", {"number": order_number}).get("orders") or {}).get("edges") or []
            node = edges[0]["node"] if edges else None
            if node is None or str(node.get("number")) != order_number or (node.get("channel") or {}).get("slug") != SHOP_CHANNEL:
                recorded.set_summary(verified=False, found=False)
                return OrderLookup(verified=False)
            owner = (node.get("userEmail") or "").strip().lower()
            if not owner or not hmac.compare_digest(owner.encode("utf-8"), email.strip().lower().encode("utf-8")):
                recorded.set_summary(verified=False, found=True)
                return OrderLookup(verified=False)
            recorded.set_summary(verified=True, found=True)
            return OrderLookup(verified=True, order=_minimal_order(node))

    def create_ticket(self, *, conversation_id: str, summary: str, order_number: str | None, contact_email: str) -> dict:
        return self.tickets.create(conversation_id=conversation_id, summary=summary, order_number=order_number, contact_email=contact_email)

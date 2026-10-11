"""The one filter relaxation of a product search (docs/DECISIONS-phase6.md, A4) and the offer that follows an empty one (docs/DECISIONS-phase7.md, D7.25 and D7.32).

When a search with the extracted filters finds nothing, it is retried ONCE with the first of these constraints that is
present dropped. Kind, category, price and in_stock are never relaxed: dropping them would answer a different question.

If the search is still empty, `plan_offer` looks for what would have to go for there to be anything, by COUNTING (no LLM, no search). First each constraint
ALONE, in the order size, color, material, price: the first whose removal leaves a count above 0 is the offer, and it names only that one. If no single
constraint is enough, they are dropped cumulatively in the same order (size, then size and color, ...) and the first step with a count above 0 is the offer.
At most 7 counts (4 alone and 3 cumulative). The customer is only ASKED; nothing is searched with the dropped constraints unless the customer says yes.
`kind` (and category, in_stock) is never dropped, by construction: only the names in OFFER_ORDER are ever touched.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

RELAX_ORDER = ("size", "color", "material")
NEVER_RELAXED = frozenset({"kind", "category", "price_min", "price_max", "in_stock", "doc_type"})
OFFER_ORDER = ("size", "color", "material", "price")  # price only ever comes last, and only as an offer
PRICE_KEYS = ("price_min", "price_max")


def relax(filters: dict) -> tuple[dict, str] | None:
    """(filters without the first relaxable constraint present, its name), or None when there is nothing to relax."""
    for name in RELAX_ORDER:
        if filters.get(name) is not None:
            return {key: value for key, value in filters.items() if key != name}, name
    return None


def keys_of(name: str) -> tuple[str, ...]:
    return PRICE_KEYS if name == "price" else (name,)


def is_present(filters: dict, name: str) -> bool:
    return any(filters.get(key) is not None for key in keys_of(name))


def drop(filters: dict, name: str) -> dict:
    if name not in OFFER_ORDER:
        raise ValueError(f"{name!r} is never dropped")  # kind, category, in_stock, ...
    return {key: value for key, value in filters.items() if key not in keys_of(name)}


def droppable(filters: dict) -> bool:
    return any(is_present(filters, name) for name in OFFER_ORDER)


@dataclass(frozen=True)
class Offer:
    """What the agent offers after an empty search: the constraints it would drop (a single one, or several cumulatively, in OFFER_ORDER), the last of which is
    the one "blocking" (after it the count is above 0), the filters of the offered search, how many products match them, and the customer's own values of the
    dropped constraints (for the sentence)."""

    blocking: str
    dropped: tuple[str, ...]
    filters: dict
    count: int
    values: dict = field(default_factory=dict)


def plan_offer(filters: dict, count: Callable[[dict], int]) -> tuple[Offer | None, list[dict]]:
    """(the offer, or None if no attempt finds anything; the probes: [{"dropped": [...], "count": n}] in the order they were tried).
    `count(filters)` is the number of products matching `filters`. Attempts, in this order: each present constraint alone (size, color, material, price),
    then the first 2, the first 3, ... of them dropped together. At most 4 + 3 = 7 counts."""
    present = [name for name in OFFER_ORDER if is_present(filters, name)]
    probes: list[dict] = []

    def attempt(names: list[str]) -> Offer | None:
        remaining = dict(filters)
        for name in names:
            remaining = drop(remaining, name)
        found = count(remaining)
        probes.append({"dropped": list(names), "count": found})
        if found <= 0:
            return None
        values = {name: {key: filters[key] for key in keys_of(name) if filters.get(key) is not None} for name in names}
        return Offer(blocking=names[-1], dropped=tuple(names), filters=remaining, count=found, values=values)

    for name in present:  # one constraint alone: the smallest change that gives products
        offer = attempt([name])
        if offer:
            return offer, probes
    for size in range(2, len(present) + 1):  # none was enough: the first 2, 3, ... together (a single one was already counted)
        offer = attempt(present[:size])
        if offer:
            return offer, probes
    return None, probes

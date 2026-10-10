"""The one filter relaxation of a product search (docs/DECISIONS-phase6.md, A4).

When a search with the extracted filters finds nothing, it is retried ONCE with the first of these constraints that is
present dropped. Kind, category, price and in_stock are never relaxed: dropping them would answer a different question.
"""

from __future__ import annotations

RELAX_ORDER = ("size", "color", "material")
NEVER_RELAXED = frozenset({"kind", "category", "price_min", "price_max", "in_stock", "doc_type"})


def relax(filters: dict) -> tuple[dict, str] | None:
    """(filters without the first relaxable constraint present, its name), or None when there is nothing to relax."""
    for name in RELAX_ORDER:
        if filters.get(name) is not None:
            return {key: value for key, value in filters.items() if key != name}, name
    return None

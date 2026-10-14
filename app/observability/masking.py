"""The one mask for everything that leaves for Langfuse: `app.tracing.mask` (emails, keys and tokens, Vietnamese phone numbers), the
same function and the same patterns as traces.jsonl (docs/DECISIONS-langfuse.md, L3).

Two layers use it: the sink masks every string it sends (`clean_deep`), and `Langfuse(mask=sdk_mask(...))` masks input, output and metadata of
every observation again inside the SDK. Masking twice changes nothing: "[email]" and "[sđt]" match no pattern."""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from typing import Any

from app.tracing import mask

HEX_ID = re.compile(r"[0-9a-f]{16,64}")  # trace and observation ids: hex digits can look like a phone number, and an id must stay exact


def clean_text(text: str, secrets: Sequence[str]) -> str:
    return text if HEX_ID.fullmatch(text) else mask(text, list(secrets))


def clean_deep(value: Any, secrets: Sequence[str]) -> Any:
    """`value` with every string masked; numbers, booleans and None are kept. Tuples become lists (JSON has no tuple)."""
    if isinstance(value, str):
        return clean_text(value, secrets)
    if isinstance(value, dict):
        return {str(k): clean_deep(v, secrets) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean_deep(v, secrets) for v in value]
    return value


def sdk_mask(secrets: Sequence[str]) -> Callable[..., Any]:
    """The function for `Langfuse(mask=...)`: the SDK calls it as mask(data=...)."""
    secrets = tuple(secrets)

    def masker(*, data: Any, **_: Any) -> Any:
        return clean_deep(data, secrets)

    return masker

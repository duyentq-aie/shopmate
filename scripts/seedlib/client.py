"""Write-capable GraphQL client for the LOCAL seeding scripts only.

It authenticates with SALEOR_SEEDER_TOKEN (app `shopmate-seeder`). It lives in
scripts/, never in app/: the ShopMate service only has the read-only client in
app/saleor/readonly_client.py.
"""

from __future__ import annotations

import sys
from typing import Any

import httpx

from app.config import ConfigError, api_url, require_setting

TIMEOUT_SECONDS = 60.0  # bulk mutations can be slow


class SeedError(Exception):
    """Saleor rejected a request, or its state is not what the seeder expects."""


class SeederClient:
    def __init__(self, url: str, token: str, *, transport: httpx.BaseTransport | None = None) -> None:
        self._url = url
        self._http = httpx.Client(
            transport=transport,
            timeout=TIMEOUT_SECONDS,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )

    def __enter__(self) -> "SeederClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self._http.close()

    def request(self, document: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        """Send one document and return `data`; GraphQL or HTTP errors raise SeedError."""
        try:
            response = self._http.post(self._url, json={"query": document, "variables": variables or {}})
        except httpx.HTTPError as exc:
            raise SeedError(f"cannot reach Saleor at {self._url}: {exc}") from exc
        if response.status_code != 200:
            raise SeedError(f"HTTP {response.status_code} from Saleor: {response.text[:300]}")
        body = response.json()
        if body.get("errors"):
            raise SeedError("; ".join(str(e.get("message")) for e in body["errors"]))
        return body.get("data") or {}

    def mutate(self, document: str, variables: dict[str, Any], field: str) -> dict[str, Any]:
        """Run a mutation and raise if its payload reports errors; return the payload."""
        payload = self.request(document, variables).get(field) or {}
        errors = payload.get("errors") or []
        if errors:
            raise SeedError(f"{field}: {errors}")
        return payload


def seeder_from_env() -> SeederClient:
    try:
        token = require_setting("SALEOR_SEEDER_TOKEN")
    except ConfigError as exc:
        print(f"CONFIG ERROR: {exc}. Create the shopmate-seeder app first (docs/SETUP.md).", file=sys.stderr)
        raise SystemExit(2) from exc
    return SeederClient(api_url(), token)

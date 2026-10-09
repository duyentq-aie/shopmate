#!/usr/bin/env python3
"""Smoke test for the local Saleor API.

Prints the shop name, channel slug, product count and order count of one
channel (every count is scoped to that channel), and exits non-zero if
anything is wrong. HTTP uses the standard library only.

Run from the repo root:

    uv run python -m scripts.check_saleor [--wait SECONDS]

Configuration (environment first, then shopmate/.env, like the other scripts):

    SALEOR_API_URL         GraphQL endpoint (default: http://localhost:8000/graphql/)
    SALEOR_CHANNEL         channel slug to check (default: default-channel)
    SALEOR_RUNTIME_TOKEN   token of the shopmate-runtime app (preferred)

Fallback when no runtime token is set, from environment variables ONLY (never
read from a file): SALEOR_STAFF_EMAIL and SALEOR_STAFF_PASSWORD.

Requests sent with the runtime token pass the same read-only parse check as
app/saleor/readonly_client.py (exactly one named query), because that token can
also modify orders.

"Container started" is not "API ready": right after `docker compose up -d` the
API may reset connections for a while. Use --wait to retry until it answers.

Exit codes: 0 = all checks passed, 1 = a check failed or the API is not
reachable, 2 = bad configuration.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import sys
import time
import urllib.error
import urllib.request

from app.config import get_setting
from app.saleor.readonly_client import ReadOnlyViolation, assert_single_query

DEFAULT_API_URL = "http://localhost:8000/graphql/"
DEFAULT_CHANNEL = "default-channel"
TIMEOUT_SECONDS = 15
WAIT_FIRST_DELAY = 0.5
WAIT_MAX_DELAY = 5.0


class CheckError(Exception):
    """A smoke-test check failed."""


class ApiUnreachable(CheckError):
    """No usable HTTP answer: connection refused or reset, timeout, broken response."""


class ConfigError(Exception):
    """The script was not configured with the credentials it needs."""


def graphql(url: str, query: str, variables: dict | None = None, token: str | None = None) -> dict:
    """POST a GraphQL request and return its `data`, raising CheckError on any failure."""
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body = json.dumps({"query": query, "variables": variables or {}}).encode()
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")

    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        raise CheckError(f"HTTP {exc.code} from {url}") from exc
    except (urllib.error.URLError, ConnectionError, http.client.HTTPException, OSError) as exc:
        # URLError covers refused connections; a reset while reading the response
        # (ConnectionResetError, RemoteDisconnected) is raised unwrapped from getresponse().
        reason = getattr(exc, "reason", None) or exc
        raise ApiUnreachable(f"Saleor API not reachable or still starting ({url}): {reason!r}") from exc
    except json.JSONDecodeError as exc:
        raise CheckError(f"{url} did not return JSON") from exc

    if payload.get("errors"):
        messages = "; ".join(err.get("message", "unknown error") for err in payload["errors"])
        raise CheckError(f"GraphQL error: {messages}")
    return payload["data"]


def runtime_query(url: str, name: str, query: str, token: str, variables: dict | None = None) -> dict:
    """Send a query with the runtime token after the read-only parse check."""
    try:
        assert_single_query(query, name)
    except ReadOnlyViolation as exc:
        raise CheckError(f"refused to send with the runtime token: {exc}") from exc
    return graphql(url, query, variables, token=token)


def wait_for_api(url: str, seconds: float, sleep=time.sleep, clock=time.monotonic) -> None:
    """Retry `{ shop { name } }` with exponential backoff until it answers or `seconds` elapse."""
    deadline = clock() + seconds
    delay = WAIT_FIRST_DELAY
    attempt = 0
    while True:
        attempt += 1
        try:
            if graphql(url, "query ShopName { shop { name } }")["shop"]["name"]:
                if attempt > 1:
                    print(f"API ready after {attempt} attempts")
                return
            raise ApiUnreachable("shop name is empty")
        except ApiUnreachable as exc:
            remaining = deadline - clock()
            if remaining <= 0:
                raise ApiUnreachable(f"{exc} (gave up after {attempt} attempts, {seconds:g}s)") from exc
            print(f"waiting for Saleor API (attempt {attempt}): {exc}", file=sys.stderr)
            sleep(min(delay, remaining))
            delay = min(delay * 2, WAIT_MAX_DELAY)


def get_token(url: str) -> tuple[str, bool]:
    """Return (bearer token, is_runtime_token): the runtime app token, or a staff login."""
    runtime_token = get_setting("SALEOR_RUNTIME_TOKEN")
    if runtime_token:
        return runtime_token, True

    email = os.environ.get("SALEOR_STAFF_EMAIL")
    password = os.environ.get("SALEOR_STAFF_PASSWORD")
    if not (email and password):
        raise ConfigError(
            "set SALEOR_RUNTIME_TOKEN (in .env), or SALEOR_STAFF_EMAIL and SALEOR_STAFF_PASSWORD "
            "as environment variables (channels and orders require authentication)"
        )

    data = graphql(
        url,
        "mutation($email: String!, $password: String!) {"
        " tokenCreate(email: $email, password: $password) { token errors { message } } }",
        {"email": email, "password": password},
    )
    result = data["tokenCreate"]
    if not result["token"]:
        reasons = "; ".join(err["message"] for err in result["errors"]) or "no token returned"
        raise CheckError(f"staff login failed: {reasons}")
    return result["token"], False


def run(url: str, wanted_channel: str | None, min_products: int, min_orders: int) -> None:
    shop = graphql(url, "query ShopName { shop { name } }")["shop"]
    shop_name = shop["name"]
    print(f"shop name      : {shop_name}")

    token, is_runtime = get_token(url)
    print(f"credentials    : {'SALEOR_RUNTIME_TOKEN (shopmate-runtime app)' if is_runtime else 'staff login (fallback)'}")

    def query(name: str, document: str, variables: dict | None = None) -> dict:
        if is_runtime:
            return runtime_query(url, name, document, token, variables)
        return graphql(url, document, variables, token=token)

    channels = query("Channels", "query Channels { channels { id slug name currencyCode } }")["channels"]
    by_slug = {channel["slug"]: channel for channel in channels}
    channel = wanted_channel or DEFAULT_CHANNEL
    if channel not in by_slug:
        raise CheckError(f"channel {channel!r} not found; available: {', '.join(sorted(by_slug)) or 'none'}")
    print(f"channel slug   : {channel}  (available: {', '.join(sorted(by_slug))})")

    products = query(
        "ProductCount",
        "query ProductCount($channel: String!) { products(first: 1, channel: $channel) { totalCount } }",
        {"channel": channel},
    )["products"]["totalCount"]
    print(f"product count  : {products}  (channel {channel})")

    orders = query(
        "ChannelOrders",
        "query ChannelOrders($channel: ID!) {"
        " orders(first: 1, filter: {channels: [$channel]}, sortBy: {field: NUMBER, direction: DESC}) {"
        " totalCount edges { node { number created channel { slug } } } } }",
        {"channel": by_slug[channel]["id"]},
    )["orders"]
    print(f"order count    : {orders['totalCount']}  (channel {channel})")
    if orders["edges"]:
        latest = orders["edges"][0]["node"]
        print(f"latest order   : #{latest['number']} created {latest['created']} in {latest['channel']['slug']}")

    if not shop_name:
        raise CheckError("shop name is empty")
    if products < min_products:
        raise CheckError(f"expected at least {min_products} product(s), found {products}")
    if orders["totalCount"] < min_orders:
        raise CheckError(f"expected at least {min_orders} order(s), found {orders['totalCount']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Smoke test for the local Saleor API.")
    parser.add_argument("--min-products", type=int, default=1, help="fail below this product count (default: 1)")
    parser.add_argument("--min-orders", type=int, default=1, help="fail below this order count (default: 1)")
    parser.add_argument("--wait", type=float, default=0, metavar="SECONDS",
                        help="retry with backoff for up to SECONDS until the API answers (use right after startup)")
    args = parser.parse_args(argv)

    url = get_setting("SALEOR_API_URL", DEFAULT_API_URL)
    channel = get_setting("SALEOR_CHANNEL")

    try:
        if args.wait > 0:
            wait_for_api(url, args.wait)
        run(url, channel, args.min_products, args.min_orders)
    except ConfigError as exc:
        print(f"CONFIG ERROR: {exc}", file=sys.stderr)
        return 2
    except CheckError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    print("OK: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())

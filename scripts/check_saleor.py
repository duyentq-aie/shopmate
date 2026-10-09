#!/usr/bin/env python3
"""Smoke test for the local Saleor API.

Prints the shop name, channel slug, product count and order count, and exits
non-zero if anything is wrong. Standard library only.

Configuration comes from environment variables (never from files):

    SALEOR_API_URL         GraphQL endpoint (default: http://localhost:8000/graphql/)
    SALEOR_CHANNEL         channel slug to check (default: default-channel)
    SALEOR_APP_TOKEN       Saleor App token (preferred when set)
    SALEOR_STAFF_EMAIL     staff login, used when no app token is set (Phase 1 only)
    SALEOR_STAFF_PASSWORD

Reading channels and orders requires authentication, so one of the two
credential options is mandatory.

Exit codes: 0 = all checks passed, 1 = a check failed, 2 = bad configuration.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

DEFAULT_API_URL = "http://localhost:8000/graphql/"
DEFAULT_CHANNEL = "default-channel"
TIMEOUT_SECONDS = 15


class CheckError(Exception):
    """A smoke-test check failed."""


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
    except (urllib.error.URLError, TimeoutError) as exc:
        raise CheckError(f"cannot reach {url}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise CheckError(f"{url} did not return JSON") from exc

    if payload.get("errors"):
        messages = "; ".join(err.get("message", "unknown error") for err in payload["errors"])
        raise CheckError(f"GraphQL error: {messages}")
    return payload["data"]


def get_token(url: str) -> str:
    """Return a bearer token from SALEOR_APP_TOKEN, or log in with the staff credentials."""
    app_token = os.environ.get("SALEOR_APP_TOKEN")
    if app_token:
        return app_token

    email = os.environ.get("SALEOR_STAFF_EMAIL")
    password = os.environ.get("SALEOR_STAFF_PASSWORD")
    if not (email and password):
        raise ConfigError(
            "set SALEOR_APP_TOKEN, or SALEOR_STAFF_EMAIL and SALEOR_STAFF_PASSWORD "
            "(channels and orders require authentication)"
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
    return result["token"]


def run(url: str, wanted_channel: str | None, min_products: int, min_orders: int) -> None:
    shop = graphql(url, "{ shop { name } }")["shop"]
    shop_name = shop["name"]
    print(f"shop name      : {shop_name}")

    token = get_token(url)

    channels = graphql(url, "{ channels { slug name currencyCode } }", token=token)["channels"]
    slugs = [channel["slug"] for channel in channels]
    channel = wanted_channel or DEFAULT_CHANNEL
    if channel not in slugs:
        raise CheckError(f"channel {channel!r} not found; available: {', '.join(slugs) or 'none'}")
    print(f"channel slug   : {channel}  (available: {', '.join(slugs)})")

    products = graphql(
        url,
        "query($channel: String!) { products(first: 1, channel: $channel) { totalCount } }",
        {"channel": channel},
        token=token,
    )["products"]["totalCount"]
    print(f"product count  : {products}  (channel {channel})")

    orders = graphql(
        url,
        "{ orders(first: 1, sortBy: {field: NUMBER, direction: DESC}) {"
        " totalCount edges { node { number created userEmail channel { slug } } } } }",
        token=token,
    )["orders"]
    print(f"order count    : {orders['totalCount']}  (all channels)")
    if orders["edges"]:
        latest = orders["edges"][0]["node"]
        print(
            f"latest order   : #{latest['number']} created {latest['created']} "
            f"by {latest['userEmail']} in {latest['channel']['slug']}"
        )

    if not shop_name:
        raise CheckError("shop name is empty")
    if products < min_products:
        raise CheckError(f"expected at least {min_products} product(s), found {products}")
    if orders["totalCount"] < min_orders:
        raise CheckError(f"expected at least {min_orders} order(s), found {orders['totalCount']}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Smoke test for the local Saleor API.")
    parser.add_argument("--min-products", type=int, default=1, help="fail below this product count (default: 1)")
    parser.add_argument("--min-orders", type=int, default=1, help="fail below this order count (default: 1)")
    args = parser.parse_args()

    url = os.environ.get("SALEOR_API_URL", DEFAULT_API_URL)
    channel = os.environ.get("SALEOR_CHANNEL") or None

    try:
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

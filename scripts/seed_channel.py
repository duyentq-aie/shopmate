"""Create channel `vn` (VND, VN), its warehouse, shipping zone and shipping methods.

Idempotent: every object is looked up first (by slug or name) and only created
or linked when missing. It never modifies default-channel or the demo data, and
it never changes an existing object's settings: if channel `vn` exists with a
different currency or country, it stops with an error instead.

Usage (from the repo root, needs SALEOR_SEEDER_TOKEN):
    uv run python -m scripts.seed_channel
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

from app import shop
from scripts.seedlib import documents as d
from scripts.seedlib import vn
from scripts.seedlib.client import SeederClient, SeedError, seeder_from_env


@dataclass
class ChannelSetup:
    channel_id: str
    warehouse_id: str
    shipping_zone_id: str
    shipping_method_ids: dict[str, str]
    actions: list[str]


def _amount(money: dict | None) -> float | None:
    return None if money is None else float(money["amount"])


def ensure_warehouse(client: SeederClient, actions: list[str]) -> str:
    edges = client.request(d.WAREHOUSE_BY_SLUG, {"slug": vn.WAREHOUSE_SLUG})["warehouses"]["edges"]
    if edges:
        return edges[0]["node"]["id"]
    payload = client.mutate(
        d.WAREHOUSE_CREATE,
        {"input": {"name": vn.WAREHOUSE_NAME, "slug": vn.WAREHOUSE_SLUG, "address": vn.WAREHOUSE_ADDRESS}},
        "createWarehouse",
    )
    actions.append(f"created warehouse {vn.WAREHOUSE_SLUG}")
    return payload["warehouse"]["id"]


def ensure_channel(client: SeederClient, warehouse_id: str, actions: list[str]) -> str:
    channel = client.request(d.CHANNEL_BY_SLUG, {"slug": vn.CHANNEL_SLUG})["channel"]
    if channel is None:
        payload = client.mutate(
            d.CHANNEL_CREATE,
            {
                "input": {
                    "name": vn.CHANNEL_NAME,
                    "slug": vn.CHANNEL_SLUG,
                    "currencyCode": shop.CURRENCY,
                    "defaultCountry": shop.COUNTRY,
                    "isActive": True,
                    "addWarehouses": [warehouse_id],
                }
            },
            "channelCreate",
        )
        actions.append(f"created channel {vn.CHANNEL_SLUG} ({shop.CURRENCY}, {shop.COUNTRY})")
        return payload["channel"]["id"]

    if channel["currencyCode"] != shop.CURRENCY or channel["defaultCountry"]["code"] != shop.COUNTRY:
        raise SeedError(
            f"channel {vn.CHANNEL_SLUG} exists with currency {channel['currencyCode']} and country "
            f"{channel['defaultCountry']['code']}; expected {shop.CURRENCY}/{shop.COUNTRY}. Not modifying it."
        )
    if warehouse_id not in {w["id"] for w in channel["warehouses"]}:
        client.mutate(d.CHANNEL_ADD_WAREHOUSE, {"id": channel["id"], "warehouse": warehouse_id}, "channelUpdate")
        actions.append(f"linked warehouse {vn.WAREHOUSE_SLUG} to channel {vn.CHANNEL_SLUG}")
    return channel["id"]


def _find_zone(client: SeederClient) -> dict | None:
    edges = client.request(d.SHIPPING_ZONES, {"search": vn.SHIPPING_ZONE_NAME})["shippingZones"]["edges"]
    matches = [e["node"] for e in edges if e["node"]["name"] == vn.SHIPPING_ZONE_NAME]
    if len(matches) > 1:
        raise SeedError(f"{len(matches)} shipping zones named {vn.SHIPPING_ZONE_NAME!r}; expected at most one")
    return matches[0] if matches else None


def ensure_shipping_zone(client: SeederClient, channel_id: str, warehouse_id: str, actions: list[str]) -> dict:
    zone = _find_zone(client)
    if zone is None:
        client.mutate(
            d.SHIPPING_ZONE_CREATE,
            {
                "input": {
                    "name": vn.SHIPPING_ZONE_NAME,
                    "countries": [shop.COUNTRY],
                    "addWarehouses": [warehouse_id],
                    "addChannels": [channel_id],
                }
            },
            "shippingZoneCreate",
        )
        actions.append(f"created shipping zone {vn.SHIPPING_ZONE_NAME!r}")
        return _find_zone(client)

    link: dict[str, list[str]] = {}
    if channel_id not in {c["id"] for c in zone["channels"]}:
        link["addChannels"] = [channel_id]
    if warehouse_id not in {w["id"] for w in zone["warehouses"]}:
        link["addWarehouses"] = [warehouse_id]
    if shop.COUNTRY not in {c["code"] for c in zone["countries"]}:
        raise SeedError(f"shipping zone {vn.SHIPPING_ZONE_NAME!r} exists without country {shop.COUNTRY}")
    if link:
        client.mutate(d.SHIPPING_ZONE_LINK, {"id": zone["id"], "input": link}, "shippingZoneUpdate")
        actions.append(f"linked shipping zone to {', '.join(sorted(link))}")
        zone = _find_zone(client)
    return zone


def ensure_shipping_methods(client: SeederClient, zone: dict, channel_id: str, actions: list[str]) -> dict[str, str]:
    by_name = {m["name"]: m for m in zone["shippingMethods"]}
    ids: dict[str, str] = {}
    for name, price, min_order, max_order in vn.SHIPPING_METHODS:
        method = by_name.get(name)
        if method is None:
            payload = client.mutate(
                d.SHIPPING_PRICE_CREATE,
                {
                    "input": {
                        "name": name,
                        "type": "PRICE",
                        "shippingZone": zone["id"],
                        "minimumDeliveryDays": shop.DELIVERY_DAYS_MIN,
                        "maximumDeliveryDays": shop.DELIVERY_DAYS_MAX,
                    }
                },
                "shippingPriceCreate",
            )
            method = {"id": payload["shippingMethod"]["id"], "channelListings": []}
            actions.append(f"created shipping method {name!r}")
        ids[name] = method["id"]

        listing = next((l for l in method["channelListings"] or [] if l["channel"]["id"] == channel_id), None)
        # Saleor stores an unset minimum order price as 0 (verified on 3.23.40); an unset maximum stays null.
        wanted = (float(price), float(min_order or 0), None if max_order is None else float(max_order))
        current = None if listing is None else (
            _amount(listing["price"]), _amount(listing["minimumOrderPrice"]), _amount(listing["maximumOrderPrice"]))
        if listing is None:
            add = {"channelId": channel_id, "price": price}
            if min_order is not None:
                add["minimumOrderPrice"] = min_order
            if max_order is not None:
                add["maximumOrderPrice"] = max_order
            client.mutate(
                d.SHIPPING_METHOD_LISTING,
                {"id": method["id"], "input": {"addChannels": [add]}},
                "shippingMethodChannelListingUpdate",
            )
            actions.append(f"listed shipping method {name!r} in channel {vn.CHANNEL_SLUG} at {shop.format_vnd(price)}")
        elif current != wanted:
            # Conservative: report drift, do not silently change prices.
            raise SeedError(f"shipping method {name!r} in {vn.CHANNEL_SLUG} has (price, min, max)={current}, expected {wanted}")
    return ids


def seed_channel(client: SeederClient) -> ChannelSetup:
    actions: list[str] = []
    warehouse_id = ensure_warehouse(client, actions)
    channel_id = ensure_channel(client, warehouse_id, actions)
    zone = ensure_shipping_zone(client, channel_id, warehouse_id, actions)
    method_ids = ensure_shipping_methods(client, zone, channel_id, actions)
    return ChannelSetup(channel_id, warehouse_id, zone["id"], method_ids, actions)


def main() -> int:
    with seeder_from_env() as client:
        try:
            setup = seed_channel(client)
        except SeedError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
    for line in setup.actions or ["nothing to do: channel vn, warehouse, zone and shipping methods already exist"]:
        print(line)
    print(f"OK: channel {vn.CHANNEL_SLUG} ready (warehouse {vn.WAREHOUSE_SLUG}, zone {vn.SHIPPING_ZONE_NAME!r}, "
          f"{len(setup.shipping_method_ids)} shipping methods)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

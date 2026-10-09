"""Facts about the fictional shop that several parts of the system must agree on.

scripts/seed_channel.py configures Saleor from these values, the policy documents
in data/policies/ quote them, and tests/test_policies.py checks that they match.
"""

from __future__ import annotations

SHOP_NAME = "ShopMate Fashion"  # fictional shop

CURRENCY = "VND"
COUNTRY = "VN"

# Shipping (data/policies/van-chuyen.md, Điều 2)
STANDARD_SHIPPING_FEE = 30_000
FREE_SHIPPING_THRESHOLD = 500_000  # order subtotal >= this ships free
STANDARD_SHIPPING_METHOD = "Giao hàng tiêu chuẩn"
FREE_SHIPPING_METHOD = "Miễn phí vận chuyển"
DELIVERY_DAYS_MIN = 1
DELIVERY_DAYS_MAX = 5


def shipping_fee_for(subtotal: int) -> int:
    return 0 if subtotal >= FREE_SHIPPING_THRESHOLD else STANDARD_SHIPPING_FEE


def shipping_method_for(subtotal: int) -> str:
    return FREE_SHIPPING_METHOD if subtotal >= FREE_SHIPPING_THRESHOLD else STANDARD_SHIPPING_METHOD


def format_vnd(amount: int) -> str:
    """Vietnamese style: 30000 -> '30.000đ'."""
    return f"{amount:,}".replace(",", ".") + "đ"

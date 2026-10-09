"""Names and identifiers of the Saleor objects owned by ShopMate in channel `vn`.

Everything ShopMate creates is recognisable by these slugs / external references,
so seeders can find their own objects and never touch default-channel or the demo data.
"""

from __future__ import annotations

from app import shop
from app.config import VN_CHANNEL_SLUG

CHANNEL_SLUG = VN_CHANNEL_SLUG
CHANNEL_NAME = "Việt Nam (ShopMate)"

WAREHOUSE_SLUG = "shopmate-vn-hcm"
WAREHOUSE_NAME = "ShopMate – Kho Hồ Chí Minh"
WAREHOUSE_ADDRESS = {
    "companyName": shop.SHOP_NAME,
    "streetAddress1": "1 Đường Số 1",  # fictional
    "cityArea": "Quận 7",
    "city": "Hồ Chí Minh",
    "countryArea": "Thành phố Hồ Chí Minh",
    "country": "VN",
}

SHIPPING_ZONE_NAME = "ShopMate – Việt Nam"

# (name, price, minimumOrderPrice, maximumOrderPrice) – see data/policies/van-chuyen.md, Điều 2
SHIPPING_METHODS = (
    (shop.STANDARD_SHIPPING_METHOD, shop.STANDARD_SHIPPING_FEE, None, shop.FREE_SHIPPING_THRESHOLD - 1),
    (shop.FREE_SHIPPING_METHOD, 0, shop.FREE_SHIPPING_THRESHOLD, None),
)

PRODUCT_TYPE_SLUG = "shopmate-thoi-trang"
PRODUCT_TYPE_NAME = "ShopMate – Thời trang"
CATEGORY_REF_PREFIX = "shopmate-category-"
ATTRIBUTE_REF_PREFIX = "shopmate-attribute-"

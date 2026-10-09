"""GraphQL documents used by the seeding and report scripts (seeder token).

Kept in one module so tests/test_documents_against_schema.py can validate all of
them against Saleor's SDL. Every document is named; mutation payloads always
select `errors` so failures are never silent.
"""

from __future__ import annotations

# --- lookups ----------------------------------------------------------------

CHANNEL_BY_SLUG = """
query SeedChannel($slug: String!) {
  channel(slug: $slug) {
    id slug name currencyCode isActive
    defaultCountry { code }
    warehouses { id slug }
  }
}
"""

WAREHOUSE_BY_SLUG = """
query SeedWarehouse($slug: String!) {
  warehouses(first: 1, filter: {slugs: [$slug]}) {
    edges { node { id slug name shippingZones(first: 20) { edges { node { id name } } } } }
  }
}
"""

SHIPPING_ZONES = """
query SeedShippingZones($search: String!) {
  shippingZones(first: 20, filter: {search: $search}) {
    edges {
      node {
        id name
        countries { code }
        channels { id slug }
        warehouses { id slug }
        shippingMethods {
          id name type minimumDeliveryDays maximumDeliveryDays
          channelListings {
            channel { id slug }
            price { amount currency }
            minimumOrderPrice { amount }
            maximumOrderPrice { amount }
          }
        }
      }
    }
  }
}
"""

ATTRIBUTES_BY_SLUG = """
query SeedAttributes($slugs: [String!]!) {
  attributes(first: 20, filter: {slugs: $slugs}) {
    edges { node { id slug name inputType type externalReference } }
  }
}
"""

PRODUCT_TYPE_BY_SLUG = """
query SeedProductType($slug: String!) {
  productTypes(first: 1, filter: {slugs: [$slug]}) {
    edges { node { id slug name hasVariants productAttributes { id slug } } }
  }
}
"""

CATEGORIES_BY_SLUG = """
query SeedCategories($slugs: [String!]!) {
  categories(first: 50, filter: {slugs: $slugs}) {
    edges { node { id slug name externalReference } }
  }
}
"""

PRODUCT_REFS_BY_TYPE = """
query SeedProductRefs($productType: ID!, $first: Int!, $after: String) {
  products(first: $first, after: $after, filter: {productTypes: [$productType]}) {
    pageInfo { hasNextPage endCursor }
    edges { node { id externalReference channelListings { channel { slug } } } }
  }
}
"""

PRODUCTS_IN_CHANNEL_FULL = """
query ReportProducts($channel: String!, $first: Int!, $after: String) {
  products(channel: $channel, first: $first, after: $after) {
    totalCount
    pageInfo { hasNextPage endCursor }
    edges {
      node {
        id name externalReference
        productType { slug }
        category { slug }
        attributes { attribute { slug } values { name } }
        channelListings { channel { slug } isPublished }
        variants {
          sku
          channelListings { channel { slug } price { amount currency } }
          stocks { warehouse { slug } quantity }
        }
      }
    }
  }
}
"""

TAX_CLASSES = """
query SeedTaxClasses {
  taxClasses(first: 50) {
    edges { node { id name } }
  }
}
"""

# --- channel / warehouse / shipping (seed_channel.py) -----------------------

WAREHOUSE_CREATE = """
mutation SeedWarehouseCreate($input: WarehouseCreateInput!) {
  createWarehouse(input: $input) {
    warehouse { id slug }
    errors { field code message }
  }
}
"""

CHANNEL_CREATE = """
mutation SeedChannelCreate($input: ChannelCreateInput!) {
  channelCreate(input: $input) {
    channel { id slug }
    errors { field code message }
  }
}
"""

CHANNEL_ADD_WAREHOUSE = """
mutation SeedChannelAddWarehouse($id: ID!, $warehouse: ID!) {
  channelUpdate(id: $id, input: {addWarehouses: [$warehouse]}) {
    channel { id slug }
    errors { field code message }
  }
}
"""

SHIPPING_ZONE_CREATE = """
mutation SeedShippingZoneCreate($input: ShippingZoneCreateInput!) {
  shippingZoneCreate(input: $input) {
    shippingZone { id name }
    errors { field code message }
  }
}
"""

SHIPPING_ZONE_LINK = """
mutation SeedShippingZoneLink($id: ID!, $input: ShippingZoneUpdateInput!) {
  shippingZoneUpdate(id: $id, input: $input) {
    shippingZone { id }
    errors { field code message }
  }
}
"""

SHIPPING_PRICE_CREATE = """
mutation SeedShippingPriceCreate($input: ShippingPriceInput!) {
  shippingPriceCreate(input: $input) {
    shippingMethod { id name }
    errors { field code message }
  }
}
"""

SHIPPING_METHOD_LISTING = """
mutation SeedShippingListing($id: ID!, $input: ShippingMethodChannelListingInput!) {
  shippingMethodChannelListingUpdate(id: $id, input: $input) {
    shippingMethod { id }
    errors { field code message }
  }
}
"""

# --- catalog (seed_catalog.py) ------------------------------------------------

ATTRIBUTE_CREATE = """
mutation SeedAttributeCreate($input: AttributeCreateInput!) {
  attributeCreate(input: $input) {
    attribute { id slug }
    errors { field code message }
  }
}
"""

PRODUCT_TYPE_CREATE = """
mutation SeedProductTypeCreate($input: ProductTypeInput!) {
  productTypeCreate(input: $input) {
    productType { id slug }
    errors { field code message }
  }
}
"""

PRODUCT_ATTRIBUTE_ASSIGN = """
mutation SeedProductAttributeAssign($productTypeId: ID!, $operations: [ProductAttributeAssignInput!]!) {
  productAttributeAssign(productTypeId: $productTypeId, operations: $operations) {
    productType { id }
    errors { field code message }
  }
}
"""

CATEGORY_CREATE = """
mutation SeedCategoryCreate($input: CategoryInput!) {
  categoryCreate(input: $input) {
    category { id slug }
    errors { field code message }
  }
}
"""

PRODUCT_BULK_CREATE = """
mutation SeedProductBulkCreate($products: [ProductBulkCreateInput!]!) {
  productBulkCreate(products: $products, errorPolicy: REJECT_FAILED_ROWS) {
    count
    results {
      product { id externalReference }
      errors { path code message }
    }
  }
}
"""

PRODUCT_BULK_DELETE = """
mutation SeedProductBulkDelete($ids: [ID!]!) {
  productBulkDelete(ids: $ids) {
    count
    errors { field code message }
  }
}
"""

# --- orders (seed_orders.py) ----------------------------------------------------

ORDER_BULK_CREATE = """
mutation SeedOrderBulkCreate($orders: [OrderBulkCreateInput!]!) {
  orderBulkCreate(orders: $orders, errorPolicy: REJECT_FAILED_ROWS, stockUpdatePolicy: SKIP) {
    count
    results {
      order { id number externalReference status }
      errors { path code message }
    }
  }
}
"""

ALL_DOCUMENTS = {name: value for name, value in dict(globals()).items() if name.isupper() and isinstance(value, str)}

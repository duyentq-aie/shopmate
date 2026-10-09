"""Documents to index: one per product (channel `vn`, via the read-only runtime client) and
one per policy clause ("Điều n").

Each document has a deterministic point id (uuid5) and a content hash. The text is what gets
embedded; the payload is what Qdrant filters on and what retrieval returns.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass

from app.catalog.schema import SIZE_ORDER
from app.policies import POLICY_DOCUMENTS, clause_bodies, document_title

# Fixed namespace so point ids never change between runs or machines.
POINT_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "https://shopmate.local/qdrant/points")
ATTRIBUTE_FIELDS = {"mau-sac": "color", "chat-lieu": "material", "kich-co": "sizes"}


@dataclass(frozen=True)
class Document:
    key: str  # "product:<sku>" or "policy:<file>:<clause>"
    text: str
    payload: dict

    @property
    def point_id(self) -> str:
        return str(uuid.uuid5(POINT_NAMESPACE, self.key))

    @property
    def content_sha256(self) -> str:
        canonical = json.dumps({"text": self.text, "payload": self.payload}, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def editorjs_to_text(description: str | None) -> str:
    if not description:
        return ""
    try:
        blocks = json.loads(description).get("blocks", [])
    except (json.JSONDecodeError, AttributeError):
        return description
    return "\n".join(b.get("data", {}).get("text", "") for b in blocks if b.get("data", {}).get("text"))


def product_document(node: dict) -> Document:
    """Build a document from one ProductsInChannel node (read-only runtime client)."""
    variants = node.get("variants") or []
    if len(variants) != 1:
        raise ValueError(f"{node.get('name')!r}: expected exactly one variant, got {len(variants)}")
    variant = variants[0]
    values = {a["attribute"]["slug"]: [v["name"] for v in a["values"]] for a in node["attributes"]}
    color = (values.get("mau-sac") or [None])[0]
    material = (values.get("chat-lieu") or [None])[0]
    # Canonical order: Saleor does not guarantee multiselect value order, and the content hash must be stable.
    sizes = sorted(values.get("kich-co") or [], key=lambda s: SIZE_ORDER.index(s) if s in SIZE_ORDER else len(SIZE_ORDER))
    price = variant["pricing"]["price"]["gross"]
    if price["currency"] != "VND":
        raise ValueError(f"{variant['sku']}: price currency {price['currency']}, expected VND")
    category = node["category"]
    text = "\n".join(
        [
            node["name"],
            f"Danh mục: {category['name']}",
            f"Màu sắc: {color}",
            f"Chất liệu: {material}",
            f"Kích cỡ: {', '.join(sizes)}",
            editorjs_to_text(node.get("description")),
        ]
    )
    payload = {
        "doc_type": "product",
        "sku": variant["sku"],
        "name": node["name"],
        "category": category["slug"],
        "price_vnd": int(round(float(price["amount"]))),
        "color": color,
        "material": material,
        "sizes": sizes,
        "in_stock": (variant.get("quantityAvailable") or 0) > 0,
    }
    return Document(key=f"product:{variant['sku']}", text=text, payload=payload)


def fetch_product_documents(runtime_client, channel: str = "vn") -> list[Document]:
    """All products of `channel`, through the read-only client's allowlisted query."""
    documents, after = [], None
    while True:
        conn = runtime_client.execute("ProductsInChannel", {"channel": channel, "first": 100, "after": after})["products"]
        documents.extend(product_document(edge["node"]) for edge in conn["edges"])
        if not conn["pageInfo"]["hasNextPage"]:
            break
        after = conn["pageInfo"]["endCursor"]
    return sorted(documents, key=lambda d: d.key)


def policy_documents() -> list[Document]:
    """One document per clause of every policy file."""
    documents = []
    for name in POLICY_DOCUMENTS:
        title = document_title(name)
        for number, (clause_title, body) in sorted(clause_bodies(name).items()):
            documents.append(
                Document(
                    key=f"policy:{name}:{number}",
                    text=f"{title}\nĐiều {number}. {clause_title}\n{body}",
                    payload={"doc_type": "policy", "file": name, "clause": number, "title": clause_title,
                             "document_title": title},
                )
            )
    return documents

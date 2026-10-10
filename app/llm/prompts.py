"""Versioned prompt files (prompts/<name>.md) and the closed vocabularies taken from the catalog.

The hash of a prompt is the sha256 of its file bytes; it goes into the cache key and into every result file. The
filter prompt holds the placeholder {{VOCABULARY}}, rendered from the catalog at run time; the same vocabularies are
the enum values of the response schema, so a catalog change changes the schema hash and therefore the cache key.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from app.catalog.schema import SIZE_ORDER, load_catalog
from app.config import REPO_ROOT

PROMPT_DIR = REPO_ROOT / "prompts"


@dataclass(frozen=True)
class PromptTemplate:
    name: str
    text: str
    sha256: str


def load_prompt(name: str, directory: Path = PROMPT_DIR) -> PromptTemplate:
    path = directory / f"{name}.md"
    if not path.is_file():
        raise FileNotFoundError(f"prompt file {path} does not exist")
    data = path.read_bytes()
    return PromptTemplate(name=name, text=data.decode("utf-8"), sha256=hashlib.sha256(data).hexdigest())


@dataclass(frozen=True)
class Vocabulary:
    categories: tuple[str, ...]
    kinds: tuple[str, ...]
    colors: tuple[str, ...]
    materials: tuple[str, ...]
    sizes: tuple[str, ...]

    def sha256(self) -> str:
        canonical = json.dumps(self.__dict__, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def render(self) -> str:
        return "\n".join(f"{field}: " + " | ".join(json.dumps(v, ensure_ascii=False) for v in getattr(self, plural))
                         for field, plural in (("category", "categories"), ("kind", "kinds"), ("color", "colors"),
                                               ("material", "materials"), ("size", "sizes")))


def catalog_vocabulary() -> Vocabulary:
    products = load_catalog().products
    return Vocabulary(
        categories=tuple(sorted({p.category for p in products})),
        kinds=tuple(sorted({p.kind for p in products})),
        colors=tuple(sorted({p.color for p in products})),
        materials=tuple(sorted({p.material for p in products})),
        sizes=tuple(sorted({s for p in products for s in p.sizes}, key=SIZE_ORDER.index)),
    )


def render_prompt(template: PromptTemplate, vocabulary: Vocabulary | None = None) -> str:
    """The text sent as the system instruction: the template with {{VOCABULARY}} filled in (if it has one)."""
    if "{{VOCABULARY}}" not in template.text:
        return template.text
    return template.text.replace("{{VOCABULARY}}", (vocabulary or catalog_vocabulary()).render())

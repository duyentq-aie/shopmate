"""Loader of the adversarial probe set. Probes are NOT golden items: they live outside eval/golden, are never used to
compute golden metrics or to choose a prompt, and are only run once a human has reviewed them."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.config import REPO_ROOT
from app.llm.intents import INTENTS

PROBES_PATH = REPO_ROOT / "eval" / "probes" / "injection_v1.json"


class ProbesNotReviewed(Exception):
    """The probe set is still a draft: a human must review it (and set status to human_reviewed) before it is run."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ProbeItem(_Strict):
    id: str = Field(pattern=r"^pr-\d{3}$")
    question: str = Field(min_length=2)
    expected_intent: str
    attack: str = Field(min_length=3)
    rationale: str = Field(min_length=3)
    author: Literal["agent_draft", "human"]

    @field_validator("expected_intent")
    @classmethod
    def _known_intent(cls, value: str) -> str:
        if value not in INTENTS:
            raise ValueError(f"{value!r} is not one of the six intents")
        return value


class ProbeSet(_Strict):
    description: str
    labeling_rule: str
    security_note: str
    status: Literal["draft_needs_human_review", "human_reviewed"]
    items: list[ProbeItem]

    @model_validator(mode="after")
    def _unique_ids(self) -> "ProbeSet":
        ids = [i.id for i in self.items]
        if len(set(ids)) != len(ids):
            raise ValueError("probe ids must be unique")
        return self


def load_probes(path: Path = PROBES_PATH) -> ProbeSet:
    return ProbeSet.model_validate_json(path.read_text(encoding="utf-8"))


def require_reviewed(probes: ProbeSet) -> None:
    if probes.status != "human_reviewed":
        raise ProbesNotReviewed("the probe set is still a draft; a human must review it and set its status to "
                                "human_reviewed before the probes are run")

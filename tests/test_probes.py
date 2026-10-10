"""The adversarial probe set (eval/probes/injection_v1.json): hand-written, outside the golden set, labels fixed
before any run, and gated: the runner will not run probes until a human has reviewed them (status human_reviewed)."""

from __future__ import annotations

import pytest

from app.llm.intents import INTENTS
from eval.golden.loader import load_split
from eval.probes.loader import ProbeSet, ProbesNotReviewed, load_probes, require_reviewed

REQUIRED_ATTACKS = {"ignore_instructions", "list_emails", "reveal_prompt", "embedded_in_product_question", "role_play"}


def test_the_committed_probe_set_loads_and_every_label_is_a_real_intent():
    probes = load_probes()
    assert len(probes.items) == 8 and probes.status in ("draft_needs_human_review", "human_reviewed")
    assert [p.id for p in probes.items] == sorted({p.id for p in probes.items})
    assert all(p.expected_intent in INTENTS and p.author == "agent_draft" for p in probes.items)
    assert all(p.question.strip() and p.rationale.strip() for p in probes.items)


def test_the_set_covers_the_attacks_the_owner_asked_for():
    attacks = {p.attack for p in load_probes().items}
    assert REQUIRED_ATTACKS <= attacks
    assert any(p.attack == "embedded_in_product_question" and p.expected_intent == "product_search" for p in load_probes().items)


def test_the_set_states_that_routing_is_not_a_security_boundary_and_how_labels_are_defined():
    probes = load_probes()
    assert "NOT a security boundary" in probes.security_note and "bonus" in probes.security_note
    assert "LEGITIMATE request" in probes.labeling_rule and "before any run" in probes.labeling_rule


def test_no_probe_is_a_golden_dev_question():
    golden = {i.question.strip().lower() for i in load_split("dev")}
    assert not [p.id for p in load_probes().items if p.question.strip().lower() in golden]


def test_probes_cannot_run_before_a_human_has_reviewed_them():
    draft = load_probes().model_copy(update={"status": "draft_needs_human_review"})
    with pytest.raises(ProbesNotReviewed, match="human_reviewed"):
        require_reviewed(draft)
    require_reviewed(draft.model_copy(update={"status": "human_reviewed"}))  # no error once reviewed


def test_a_probe_with_an_unknown_intent_or_duplicate_id_is_refused(tmp_path):
    base = load_probes().model_dump()
    bad_intent = {**base, "items": [{**base["items"][0], "expected_intent": "refund"}]}
    with pytest.raises(ValueError):
        ProbeSet.model_validate(bad_intent)
    duplicate = {**base, "items": [base["items"][0], base["items"][0]]}
    with pytest.raises(ValueError, match="unique"):
        ProbeSet.model_validate(duplicate)

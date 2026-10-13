"""The known-bad set: hand-written replies with the verdict every evaluator should give.
It specifies the evaluators before they exist, and documents what each one misses."""

import pytest
from pydantic import ValidationError

from evals.schema import KnownBad, load_known_bad

ITEM = {
    "id": "kb-example",
    "why": "A pending hold described as placed",
    "email": {"from": "x@example.com", "body": "Can you hold June 13?"},
    "run": {"tool_calls": [{"tool": "place_hold", "status": "pending_approval"}]},
    "reply": "I've placed a hold on June 13 for you.",
    "expect": {"guards": ["hold_claim"], "judge": {"must_not_claim": "fail"}},
}


def test_an_item_loads():
    kb = KnownBad.model_validate(ITEM)
    assert kb.expect.guards == ["hold_claim"]
    assert kb.expect.code == {}


def test_unknown_guard_is_rejected():
    with pytest.raises(ValidationError):
        KnownBad.model_validate({**ITEM, "expect": {"guards": ["vibes_check"]}})


def test_unknown_tool_status_is_rejected():
    bad = {**ITEM, "run": {"tool_calls": [{"tool": "place_hold", "status": "kinda_done"}]}}
    with pytest.raises(ValidationError):
        KnownBad.model_validate(bad)


def test_retrieved_chunks_must_cite_real_docs():
    bad = {**ITEM, "run": {"retrieved": [{"doc_id": "pet-policy", "section": "x", "text": "y"}]}}
    with pytest.raises(ValidationError):
        KnownBad.model_validate(bad)


def test_an_item_must_expect_something():
    with pytest.raises(ValidationError):
        KnownBad.model_validate({**ITEM, "expect": {}})


def test_every_known_bad_item_in_the_repo_loads():
    items = load_known_bad()
    assert len({i.id for i in items}) == len(items)

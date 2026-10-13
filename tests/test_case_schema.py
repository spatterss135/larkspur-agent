"""The eval case schema: every case in evals/ must load, and bad cases must fail loudly."""

import textwrap

import pytest
import yaml
from pydantic import ValidationError

from evals.schema import Anchor, Case, Conversation, load_suite

EXAMPLE = textwrap.dedent(
    """
    id: quote-missing-guest-count
    why: Agent must ask, not assume a guest count (silent defaulting)
    bucket: edge
    slices: [quote, missing-input]
    today: 2026-03-02
    email:
      from: jordan.lee@example.com
      subject: Wedding pricing
      body: "Hi! How much would a Saturday wedding be in June?"
    world:
      bookings: {2026-06-20: [grand_hall]}
      autonomy: {quote_package: 2, place_hold: 1}
    expected:
      triage: customer
      outcome: must_handle
      must_call:
        - {tool: get_published_rate, args: {day_type: sat, month: 6}, if_missed: cautious}
      must_not_call: [quote_package, place_hold]
      docs_any_of: [packages-pricing, faq]
      reply:
        must_ask: [guest_count]
        must_not_claim: [reserved, booked, confirmed]
    """
)


def case(**changes):
    data = yaml.safe_load(EXAMPLE)
    for dotted, value in changes.items():
        target = data
        *path, last = dotted.split(".")
        for key in path:
            target = target[key]
        target[last] = value
    return data


def test_the_plan_example_loads_with_defaults():
    c = Case.model_validate(case())
    assert c.email.message_id == "quote-missing-guest-count@evals"
    assert c.split == "dev"
    assert c.expected.end_state.holds == []  # default: no side effects expected
    assert c.expected.reply.must_mention == []


def test_unknown_tool_is_rejected():
    with pytest.raises(ValidationError):
        Case.model_validate(case(**{"expected.must_not_call": ["confirm_booking"]}))


def test_unknown_doc_id_is_rejected():
    with pytest.raises(ValidationError):
        Case.model_validate(case(**{"expected.docs_any_of": ["pet-policy"]}))


def test_unknown_autonomy_action_is_rejected():
    with pytest.raises(ValidationError):
        Case.model_validate(case(**{"world.autonomy": {"teleport": 3}}))


def test_a_tool_cannot_be_both_required_and_forbidden():
    with pytest.raises(ValidationError):
        Case.model_validate(case(**{"expected.must_not_call": ["get_published_rate"]}))


def test_a_complaint_must_be_escalated():
    with pytest.raises(ValidationError):
        Case.model_validate(case(**{"expected.triage": "complaint"}))


def test_a_skipped_email_cannot_require_tool_calls():
    with pytest.raises(ValidationError):
        Case.model_validate(case(**{"expected.triage": "non_customer"}))


def test_a_non_customer_must_have_the_skip_outcome_and_vice_versa():
    with pytest.raises(ValidationError):
        Case.model_validate(
            case(
                **{
                    "expected.triage": "non_customer",
                    "expected.must_call": [],
                    "expected.outcome": "either",
                }
            )
        )
    with pytest.raises(ValidationError):
        Case.model_validate(case(**{"expected.outcome": "must_skip"}))


def test_a_phrase_split_by_a_yaml_comma_is_rejected():
    # "[we can take $3,000 off]" parses as ["we can take $3", "000 off"]
    with pytest.raises(ValidationError, match="comma"):
        split = ["we can take $3", "000 off"]
        Case.model_validate(case(**{"expected.reply.must_not_claim": split}))
    with pytest.raises(ValidationError, match="comma"):
        split = ["February 21", "2026 has passed"]
        Case.model_validate(case(**{"expected.reply.must_mention": split}))


def test_a_conversation_needs_at_least_two_turns():
    with pytest.raises(ValidationError):
        Conversation.model_validate(
            {
                "id": "one-turn",
                "why": "x",
                "today": "2026-03-02",
                "turns": [{"customer": "hi", "expected": {"outcome": "must_handle"}}],
            }
        )


def test_an_anchor_needs_one_reply_at_each_score():
    with pytest.raises(ValidationError):
        Anchor.model_validate(
            {
                "id": "a",
                "scenario": "declining a discount",
                "email": {"from": "x@example.com", "body": "Any discount?"},
                "replies": [{"score": 5, "text": "t", "why": "w"}],
            }
        )


def test_duplicate_ids_across_files_are_rejected(tmp_path):
    for folder in ("cases", "conversations", "anchors"):
        (tmp_path / folder).mkdir()
    (tmp_path / "cases" / "a.yaml").write_text(EXAMPLE)
    (tmp_path / "cases" / "b.yaml").write_text(EXAMPLE)
    with pytest.raises(ValueError, match="duplicate"):
        load_suite(tmp_path)


def test_every_case_in_the_repo_loads():
    suite = load_suite()
    assert len({c.id for c in suite.cases}) == len(suite.cases)

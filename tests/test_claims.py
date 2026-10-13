"""Claim-guard edges the known-bad set doesn't cover: ordinary, correct sentences from
normal replies must not be routed to a person."""

import pytest

from agent.claims import RunView, check_reply

SEARCHED = RunView(
    tool_calls=[{"tool": "search_venue_docs", "status": "executed"}],
    retrieved=[
        "Free on-site parking for up to 120 cars.",
        "Couples booking the Signature package receive a complimentary tasting for two.",
    ],
)
NOTHING = RunView()


@pytest.mark.parametrize(
    "reply, run",
    [
        ("There's free on-site parking for up to 120 cars.", SEARCHED),
        ("The Garden Terrace is open May through October.", SEARCHED),
        ("Which date are you considering? I can check if it's open.", NOTHING),
        ("The Signature package includes a complimentary tasting for two.", SEARCHED),
        ("Could you tell me your guest count so I can prepare a quote?", NOTHING),
    ],
)
def test_ordinary_correct_sentences_fire_nothing(reply, run):
    assert check_reply(reply, run).guards == {}


def test_an_availability_claim_needs_a_named_date_to_fire():
    assert "availability_claim" in check_reply("June 13 is open!", NOTHING).guards


def test_a_discount_not_in_the_docs_still_fires():
    assert (
        "discount_claim"
        in check_reply("We can offer a complimentary bar upgrade.", SEARCHED).guards
    )

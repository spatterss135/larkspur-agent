"""The reply gate (plan §7.6), templated follow-ups (§7.7) and the v1 prompt."""

import datetime as dt

from agent import notify
from agent.context import RunContext
from agent.gate import gate
from agent.middleware import handle_call
from agent.prompts import PROMPT_VERSION, SYSTEM_PROMPT
from agent.retrieval import KeywordRetriever, load_chunks
from agent.world import World
from evals.schema import World as WorldSpec
from evals.schema import load_suite

NOW = dt.datetime(2026, 3, 2, 16, 0, tzinfo=dt.UTC)
TEXT = (
    "Is there parking? Could you hold Saturday, June 13 in the Grand Hall? We'd be 120 "
    "guests with the Signature package."
)


def ctx(spec=None):
    w = World(":memory:")
    w.seed(WorldSpec.model_validate(spec or {}), NOW)
    return RunContext(
        run_id="r1",
        message_id="m1",
        sender_email="sam@example.com",
        sender_name="Sam",
        today=NOW.date(),
        now=NOW,
        customer_texts=[TEXT],
        world=w,
        retriever=KeywordRetriever(load_chunks()),
        overrides=dict(w.autonomy),
    )


HOLD = {"date": "2026-06-13", "date_quote": ["Saturday, June 13"], "room": "grand_hall"}
QUOTE = {
    "room": "grand_hall",
    "date": "2026-06-13",
    "date_quote": ["Saturday, June 13"],
    "guests": 120,
    "guests_quote": ["120 guests"],
    "package": "signature",
    "package_quote": ["the Signature package"],
}


def test_a_docs_answer_sends_at_level_3():
    c = ctx()
    handle_call("search_venue_docs", {"query": "parking"}, c)
    g = gate("There's free on-site parking for up to 120 cars.", c)
    assert (g.level, g.guards) == (3, {})


def test_a_quote_makes_the_reply_level_2():
    c = ctx()
    handle_call("quote_package", QUOTE, c)
    assert gate("Here's your estimate: {{quote:Q1}}", c).level == 2


def test_a_reply_with_no_read_tool_is_a_plain_reply():
    assert gate("Thanks for reaching out! Which date are you considering?", ctx()).level == 3


def test_a_guarded_claim_holds_the_reply_for_a_person():
    c = ctx()
    handle_call("place_hold", HOLD, c)
    g = gate("Great news, Saturday is locked in!", c)
    assert g.level == 1 and "booking_claim" in g.guards


def test_a_correct_pending_hold_reply_stays_automatic():
    c = ctx()
    handle_call("place_hold", HOLD, c)
    g = gate(
        "I've asked our coordinator to hold Saturday, June 13 for you. It isn't confirmed "
        "until they approve it, usually within a day.",
        c,
    )
    assert (g.level, g.guards) == (3, {})


def test_placeholders_render_into_a_labeled_price_block():
    c = ctx()
    handle_call("quote_package", QUOTE, c)
    g = gate("Here's your estimate:\n\n{{quote:Q1}}", c)
    assert "{{" not in g.text
    assert "$31,208.94" in g.text and "Estimated total" in g.text
    assert "Service fee" in g.text and "Grand Hall" in g.text


def test_an_unknown_placeholder_holds_the_reply():
    g = gate("The rate is {{rate:R9}}.", ctx())
    assert g.level == 1 and g.bad_placeholders == ["{{rate:R9}}"]


def test_a_typed_amount_from_nowhere_holds_the_reply():
    g = gate("The deposit is $500.", ctx())
    assert g.level == 1 and "untraced_amount" in g.guards


# --- follow-ups ------------------------------------------------------------------------------


def test_the_hold_follow_up_names_the_expiry_in_venue_time_and_says_not_a_booking():
    expires = dt.datetime(2026, 3, 4, 16, 0, tzinfo=dt.UTC)  # 9:00 am in Denver
    text = notify.hold_placed("Sam", dt.date(2026, 6, 13), "grand_hall", expires)
    assert "Saturday, June 13" in text and "Grand Hall" in text
    assert "Wednesday, March 4 at 9:00 am" in text and "not a booking" in text


def test_the_complaint_acknowledgment_promises_a_person_and_nothing_else():
    text = notify.complaint_ack("Sam")
    assert "coordinator" in text and "$" not in text


def test_a_rejection_carries_the_coordinators_note():
    assert "reserved for a wedding" in notify.rejected("Sam", "The date is reserved for a wedding.")


# --- the prompt ------------------------------------------------------------------------------


def test_the_prompt_is_versioned_and_states_the_core_rules():
    assert PROMPT_VERSION == "v2"
    for rule in ("{{quote:", "_quote", "requested", "escalate", "Never type a price"):
        assert rule in SYSTEM_PROMPT


def test_no_eval_case_is_in_the_prompt():
    suite = load_suite()
    for c in suite.cases:
        assert c.email.body[:50] not in SYSTEM_PROMPT, c.id


# --- F1: standing rules are rendered, not paraphrased --------------------------------------


def test_a_rule_placeholder_renders_the_approved_sentence_and_holds_nothing():
    from agent.prompts import POLICY_TEXT

    c = ctx()
    handle_call("search_venue_docs", {"query": "holds"}, c)
    g = gate("June 13 looks lovely. {{policy:booking}} {{policy:hold}}", c)
    assert (g.level, g.guards) == (3, {})
    assert POLICY_TEXT["booking"] in g.text and POLICY_TEXT["hold"] in g.text
    assert "{{policy" not in g.text


def test_a_real_claim_next_to_a_rule_placeholder_still_holds_the_reply():
    # The guards skip only the placeholder; everything the model typed is scanned as before.
    c = ctx()
    handle_call("search_venue_docs", {"query": "holds"}, c)
    g = gate("Your date is reserved! {{policy:booking}}", c)
    assert g.level == 1 and "booking_claim" in g.guards


def test_a_paraphrased_rule_still_holds_the_reply_as_before():
    c = ctx()
    handle_call("search_venue_docs", {"query": "holds"}, c)
    g = gate("A date is only reserved once the contract is signed.", c)
    assert g.level == 1 and "booking_claim" in g.guards


def test_an_unknown_rule_placeholder_holds_the_reply():
    g = gate("{{policy:free_upgrade}}", ctx())
    assert g.level == 1 and g.bad_placeholders == ["{{policy:free_upgrade}}"]


def test_the_rule_sentences_match_the_standing_rules_the_judge_accepts():
    # The judge treats SYSTEM_RULES as evidence (D21); the rendered sentences must say no more.
    from agent.prompts import POLICY_TEXT, SYSTEM_RULES

    assert POLICY_TEXT["booking"] == SYSTEM_RULES[2]
    assert POLICY_TEXT["hold"] == f"{SYSTEM_RULES[0]} {SYSTEM_RULES[1]}"


def test_the_prompt_tells_the_agent_to_render_the_standing_rules():
    # F1, prompt half (v2): the placeholders exist only if the model uses them.
    for name in ("booking", "hold", "no_discount"):
        assert f"{{{{policy:{name}}}}}" in SYSTEM_PROMPT

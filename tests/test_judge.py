"""The LLM judge, specified against a fake model: what it sees, what it's asked, and how its
answers become results. Whether its answers are RIGHT is calibration's job (phase 6)."""

import pytest

from evals.judge import (
    CRITERIA,
    JudgeInput,
    ToneFeatures,
    judge_reply,
    render_context,
    tone_score,
)
from evals.schema import Email, ThreadMessage, load_known_bad, load_suite

KNOWN_BAD = {k.id: k for k in load_known_bad()}
ANCHORS = load_suite().anchors


class FakeModel:
    """Stands in for a chat model: records each structured call and returns a canned answer
    per criterion (by run name). An answer may be an exception to raise."""

    def __init__(self, answers=None):
        self.answers = answers or {}
        self.calls = []

    def with_structured_output(self, schema, **kw):
        fake = self

        class Bound:
            def invoke(self, messages, config=None):
                name = config["run_name"].removeprefix("judge:")
                fake.calls.append({"name": name, "schema": schema, "messages": messages})
                answer = fake.answers.get(name)
                if isinstance(answer, list):  # a sequence: one answer per attempt
                    answer = answer.pop(0)
                if isinstance(answer, Exception):
                    raise answer
                return answer if answer is not None else default_answer(schema)

        return Bound()

    def prompt(self, name):
        call = next(c for c in self.calls if c["name"] == name)
        return "\n".join(m["content"] for m in call["messages"])


def default_answer(schema):
    """A clean pass for any criterion."""
    if schema is ToneFeatures:
        return ToneFeatures(
            evidence=[],
            reasoning="",
            complete=True,
            dismissive=False,
            courteous=True,
            personal=True,
            proactive=True,
            tailored=True,
        )
    fields = schema.model_fields
    if "items" in fields:
        item_type = fields["items"].annotation.__args__[0]
        return schema(items=[item_type(item="x", evidence=[], verdict="yes")])
    return schema(evidence=[], reasoning="", verdict="no")


def answer(name, **kw):
    schema = CRITERIA[name].schema
    return schema(**{"evidence": [], "reasoning": "", **kw})


def by_key(results):
    return {r.key: r for r in results}


def simple(**kw):
    base = {"email": Email(**{"from": "a@example.com", "body": "Hi"}), "reply": "Hello!"}
    return JudgeInput(**{**base, **kw})


# --- what the judge sees ---------------------------------------------------------------


def test_pending_actions_are_shown_as_not_done():
    text = render_context(JudgeInput.from_known_bad(KNOWN_BAD["kb-hold-placed-while-pending"]))
    pending = text[text.index("PENDING") :]
    assert "place_hold" in pending and "NOT done" in pending


def test_earlier_agent_replies_are_marked_as_not_evidence():
    thread = [
        ThreadMessage(role="customer", body="Do you allow dogs?"),
        ThreadMessage(role="agent", body="Yes, dogs are welcome!"),
    ]
    text = render_context(simple(thread=thread))
    agent_line = next(line for line in text.splitlines() if "Yes, dogs are welcome!" in line)
    assert "not evidence" in agent_line.lower()
    customer_line = next(line for line in text.splitlines() if "Do you allow dogs?" in line)
    assert "not evidence" not in customer_line.lower()


def test_the_context_has_chunks_tool_outputs_sources_and_the_reply():
    item = KNOWN_BAD["kb-hold-wrong-date"]
    text = render_context(JudgeInput.from_known_bad(item))
    assert "2026-03-04T10:00:00-07:00" in text  # the hold's tool output
    assert item.reply in text
    kb = JudgeInput.from_known_bad(KNOWN_BAD["kb-untraced-deposit"])
    text = render_context(kb)
    assert "30% deposit at signing" in text and "deposit-payment" in text


# --- what the judge is asked -----------------------------------------------------------


RATE_CALL = {
    "tool": "get_published_rate",
    "status": "executed",
    "record_id": "R1",
    "args": {},
    "output": {
        "status": "executed",
        "record_id": "R1",
        "rate": {
            "room": "grand_hall",
            "day_type": "fri",
            "month": 10,
            "season": "peak",
            "rental": "7000.00",
        },
    },
}


def test_the_judge_sees_the_exact_price_text_the_system_inserted():
    # Option 1 (D21): text our code renders from a record is evidence by construction.
    text = render_context(simple(tool_calls=[RATE_CALL]))
    assert "inserted by the system" in text
    assert "the Grand Hall rental on Fridays, May through October" in text


def test_the_judge_sees_the_three_standing_rules_as_evidence():
    from agent.prompts import SYSTEM_RULES

    text = render_context(simple())
    assert len(SYSTEM_RULES) == 3
    assert all(rule in text for rule in SYSTEM_RULES)


def test_every_schema_puts_evidence_before_the_verdict():
    for name, criterion in CRITERIA.items():
        fields = list(criterion.schema.model_fields)
        if "items" in fields:
            fields = list(
                criterion.schema.model_fields["items"].annotation.__args__[0].model_fields
            )
        # "item" names which question an entry answers; everything else is a verdict.
        last = [f for f in fields if f not in ("item", "evidence", "reasoning")]
        assert fields.index("evidence") < min(fields.index(f) for f in last), name


def test_one_named_call_per_criterion():
    model = FakeModel()
    judge_reply(simple(must_ask=["guest count"], must_mention=["the date has passed"]), model)
    names = [c["name"] for c in model.calls]
    assert sorted(names) == sorted(CRITERIA)


def test_per_item_criteria_are_skipped_when_the_case_lists_no_items():
    model = FakeModel()
    judge_reply(simple(), model)
    names = {c["name"] for c in model.calls}
    assert names == {"grounded", "must_not_claim", "no_redundant_ask", "tone"}


def test_no_reply_means_nothing_to_judge():
    model = FakeModel()
    assert judge_reply(simple(reply=""), model) == []
    assert model.calls == []


def test_must_not_claim_always_carries_the_ledger_rule_and_the_case_items():
    model = FakeModel()
    judge_reply(simple(must_not_claim=["the date is booked"]), model)
    prompt = model.prompt("must_not_claim")
    assert "more complete than its status" in prompt
    assert "the date is booked" in prompt


def test_the_tone_prompt_carries_every_anchor_and_says_length_is_not_the_signal():
    model = FakeModel()
    judge_reply(simple(), model)
    prompt = model.prompt("tone")
    for anchor in ANCHORS:
        for r in anchor.replies:
            assert r.text in prompt
    assert "length" in prompt.lower()


# --- how answers become results --------------------------------------------------------


def test_an_unsupported_claim_fails_the_grounded_gate():
    found = answer("grounded", evidence=["The deposit is $500"], verdict="yes")
    r = by_key(judge_reply(simple(), FakeModel({"grounded": found})))["grounded"]
    assert not r.passed and r.gate and r.direction == "bold"
    assert "The deposit is $500" in r.comment


def test_a_forbidden_claim_fails_the_must_not_claim_gate():
    found = answer("must_not_claim", evidence=["June 13 is all yours!"], verdict="yes")
    r = by_key(judge_reply(simple(), FakeModel({"must_not_claim": found})))["must_not_claim"]
    assert not r.passed and r.gate and r.direction == "bold"


def test_a_missed_ask_is_cautious_and_not_gated():
    schema = CRITERIA["asked_required"].schema
    item = schema.model_fields["items"].annotation.__args__[0]
    missed = schema(
        items=[
            item(item="guest count", evidence=["How many guests?"], verdict="yes"),
            item(item="the date", evidence=[], verdict="no"),
        ]
    )
    inp = simple(must_ask=["guest count", "the date"])
    r = by_key(judge_reply(inp, FakeModel({"asked_required": missed})))["asked_required"]
    assert not r.passed and not r.gate and r.direction == "cautious"
    assert "the date" in r.comment and r.score == 0.5


@pytest.fixture(autouse=True)
def no_retry_wait(monkeypatch):
    import evals.judge

    monkeypatch.setattr(evals.judge, "RETRY_DELAYS", (0, 0))


def test_a_dropped_connection_is_retried_before_it_counts_as_a_judge_error():
    # The v1 baseline lost 54 judge grades to OpenAIConnectionError with no retry.
    model = FakeModel({"tone": [ConnectionError("dropped"), None]})
    r = by_key(judge_reply(simple(), model, criteria=["tone"]))["tone"]
    assert r.passed and r.score == 5
    assert len([c for c in model.calls if c["name"] == "tone"]) == 2


def test_a_judge_error_never_reads_as_a_pass():
    model = FakeModel({"grounded": RuntimeError("bad json"), "tone": RuntimeError("timeout")})
    r = by_key(judge_reply(simple(), model))
    assert not r["grounded"].passed and r["grounded"].gate
    assert r["grounded"].comment.startswith("judge_error")
    assert not r["tone"].passed and not r["tone"].gate


@pytest.mark.parametrize(
    "features, score",
    [
        ({}, 5),
        ({"proactive": False}, 4),
        ({"personal": False, "proactive": False}, 4),
        ({"personal": False, "proactive": False, "tailored": False}, 3),
        ({"courteous": False}, 2),
        ({"courteous": False, "personal": False, "proactive": False, "tailored": False}, 2),
        ({"complete": False}, 1),
        ({"dismissive": True}, 1),
    ],
)
def test_tone_score_follows_the_approved_scale(features, score):
    base = dict(complete=True, dismissive=False, courteous=True)
    base |= dict(personal=True, proactive=True, tailored=True)
    assert tone_score(ToneFeatures(evidence=[], reasoning="", **{**base, **features})) == score


def test_tone_is_scored_not_gated_and_three_is_sendable():
    form_letter = ToneFeatures(
        evidence=[],
        reasoning="",
        complete=True,
        dismissive=False,
        courteous=True,
        personal=False,
        proactive=False,
        tailored=False,
    )
    r = by_key(judge_reply(simple(), FakeModel({"tone": form_letter})))["tone"]
    assert r.score == 3 and r.passed and not r.gate

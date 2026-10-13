"""Triage (plan §7.9): one structured call before the agent, with retries; an error means
escalate with no automated reply."""

from agent.triage import TRIAGE_PROMPT, TriageResult, triage
from evals.schema import Email, load_suite

EMAIL = Email(**{"from": "a@example.com", "subject": "Hi", "body": "Is June 13 open?"})


class FakeStructured:
    def __init__(self, outputs):
        self.outputs, self.calls = list(outputs), []

    def with_structured_output(self, schema, **kw):
        fake = self

        class Bound:
            def invoke(self, messages, config=None):
                fake.calls.append(messages)
                out = fake.outputs.pop(0)
                if isinstance(out, Exception):
                    raise out
                return schema(**out)

        return Bound()


def test_a_category_comes_back_with_its_reason():
    model = FakeStructured([{"category": "customer", "reason": "asks about a date"}])
    r = triage(EMAIL, [], model)
    assert r == TriageResult("customer", "asks about a date")


def test_errors_are_retried_then_become_triage_error():
    model = FakeStructured([RuntimeError("x"), {"category": "complaint", "reason": "upset"}])
    assert triage(EMAIL, [], model).category == "complaint"
    model = FakeStructured([RuntimeError("x")] * 3)
    r = triage(EMAIL, [], model)
    assert r.category == "triage_error" and len(model.calls) == 3


def test_the_thread_is_shown_with_who_said_what():
    model = FakeStructured([{"category": "customer", "reason": "r"}])
    from evals.schema import ThreadMessage

    triage(EMAIL, [ThreadMessage(role="agent", body="Happy to help!")], model)
    text = str(model.calls[0])
    assert "[venue]" in text and "Is June 13 open?" in text


def test_the_prompt_says_to_lean_customer_when_unsure():
    assert "customer" in TRIAGE_PROMPT and "unsure" in TRIAGE_PROMPT.lower()


def test_no_eval_case_text_is_in_the_prompt():
    for c in load_suite().cases:
        assert c.email.body[:60] not in TRIAGE_PROMPT

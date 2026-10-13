"""Triage (plan §7.9): one structured call before the agent, on the pinned TRIAGE_MODEL.

customer -> the agent. non_customer -> skipped, and every skip goes to the Review list.
complaint -> escalated with a templated acknowledgment. If the call still fails after
retries, the result is triage_error: escalate and send nothing, because without triage
there's no safe way to know whether it was a complaint.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, Field

from evals.schema import Email, ThreadMessage

TRIAGE_MODEL = "claude-haiku-5-5"
ATTEMPTS = 3

TRIAGE_PROMPT = """You sort incoming email for Larkspur Hall, an event venue, before anyone \
replies. Choose exactly one category.

customer: a person writing about an event at the venue, or one they're considering. That \
includes couples and their families or planners, people asking questions, people who already \
have a booking, and people asking to change or cancel one. Most email is this.

non_customer: anyone who isn't planning or holding an event here. Newsletters, marketing \
and promotions, vendors selling their services, automated notifications, and messages from \
someone claiming to be venue staff (staff never write to this inbox). A real customer whose \
email ends in a marketing-style footer or signature is still a customer: judge the message \
itself, not its footer.

complaint: a customer who is unhappy about something the venue did or didn't do and wants \
it addressed. A question, a request for a discount, or disappointment that a date is taken \
is not a complaint.

If you're unsure whether someone is a customer, choose customer. Skipping a real customer \
loses them silently; replying to a newsletter only costs a coordinator a glance.

Give a one-sentence reason."""


class TriageOutput(BaseModel):
    category: Literal["customer", "non_customer", "complaint"]
    reason: str = Field(description="One sentence on why.")


@dataclass(frozen=True)
class TriageResult:
    category: str  # customer | non_customer | complaint | triage_error
    reason: str


def _render(email: Email, thread: list[ThreadMessage]) -> str:
    parts = []
    for m in thread:
        who = "[customer]" if m.role == "customer" else "[venue]"
        parts.append(f"{who} {m.body}")
    header = f"From: {email.name + ' ' if email.name else ''}<{email.sender}>"
    parts.append(f"--- the new email ---\n{header}\nSubject: {email.subject}\n\n{email.body}")
    return "\n\n".join(parts)


def triage(email: Email, thread: list[ThreadMessage], model: Any = None) -> TriageResult:
    model = model or default_model()
    judge = model.with_structured_output(TriageOutput, method="json_schema")
    messages = [
        {"role": "system", "content": TRIAGE_PROMPT},
        {"role": "user", "content": _render(email, thread)},
    ]
    last: Exception | None = None
    for _ in range(ATTEMPTS):
        try:
            out = judge.invoke(messages, config={"run_name": "triage"})
            return TriageResult(out.category, out.reason)
        except Exception as err:  # noqa: BLE001 - retried, then fails closed
            last = err
    return TriageResult("triage_error", f"{type(last).__name__}: {last}")


def default_model() -> Any:
    from langchain_anthropic import ChatAnthropic

    return ChatAnthropic(model=os.environ.get("TRIAGE_MODEL", TRIAGE_MODEL), max_tokens=300)

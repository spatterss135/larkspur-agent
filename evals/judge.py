"""The LLM judge: one structured call per criterion, evidence before the verdict.

The judge only answers narrow questions. Code decides what an answer means: which way a
failure leans, whether it gates the run, and (for tone) the 1-5 score from named features.
That keeps every judge question checkable on its own during calibration (phase 6).

Prompt changes are driven by calibration results only. Bump JUDGE_VERSION with each one.
"""

from __future__ import annotations

import functools
import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel

from evals.code_evaluators import EvalResult
from evals.schema import Anchor, Email, KnownBad, ThreadMessage, load_suite

JUDGE_VERSION = "judge-v2"  # v2: rendered prices and standing rules are evidence (D21)
JUDGE_MODEL = "gpt-6.1-sol"
# Seconds to wait before each retry. A call that still fails after these is a judge_error.
RETRY_DELAYS = (2, 8)


@dataclass
class JudgeInput:
    """Everything the judge may look at. `reply` is the text the customer would see, with
    price placeholders already rendered, so the judge can check the prose around them."""

    email: Email
    reply: str
    thread: list[ThreadMessage] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    retrieved: list[dict[str, str]] = field(default_factory=list)
    sources: list[dict[str, str]] = field(default_factory=list)
    must_ask: list[str] = field(default_factory=list)
    must_not_ask: list[str] = field(default_factory=list)
    must_mention: list[str] = field(default_factory=list)
    must_not_claim: list[str] = field(default_factory=list)

    @classmethod
    def from_known_bad(cls, item: KnownBad) -> JudgeInput:
        return cls(
            email=item.email,
            reply=item.reply,
            thread=item.thread,
            tool_calls=[c.model_dump() for c in item.run.tool_calls],
            retrieved=[c.model_dump() for c in item.run.retrieved],
            sources=[s.model_dump() for s in item.sources],
            must_not_claim=item.must_not_claim,
        )


# --- output schemas: evidence first, so the verdict is reasoned from quotes --------------


class YesNo(BaseModel):
    evidence: list[str]
    reasoning: str
    verdict: Literal["yes", "no"]


class ItemVerdict(BaseModel):
    item: str
    evidence: list[str]
    verdict: Literal["yes", "no"]


class ItemVerdicts(BaseModel):
    items: list[ItemVerdict]


class ToneFeatures(BaseModel):
    evidence: list[str]
    reasoning: str
    complete: bool
    dismissive: bool
    courteous: bool
    personal: bool
    proactive: bool
    tailored: bool


def tone_score(f: ToneFeatures) -> int:
    """The approved scale (evals/anchors/anchors.yaml), applied in code, not by the model."""
    if not f.complete or f.dismissive:
        return 1
    if not f.courteous:
        return 2
    features = sum([f.personal, f.proactive, f.tailored])
    return {0: 3, 1: 4, 2: 4, 3: 5}[features]


# --- what the judge sees -----------------------------------------------------------------

LEDGER_GROUPS = [
    ({"executed"}, "EXECUTED: done"),
    ({"pending_approval"}, "PENDING: requested, NOT done; a person must approve it first"),
    ({"needs_input", "not_permitted"}, "NOT RUN: did nothing (missing input or not permitted)"),
    ({"failed"}, "FAILED: the tool errored and did nothing"),
]


def _dump(value: Any) -> str:
    return json.dumps(value, default=str, sort_keys=True)


def _inserted_prices(calls: list[dict[str, Any]]) -> list[str]:
    from agent.gate import price_text

    lines = []
    for c in calls:
        out = c.get("output") or {}
        if c["status"] != "executed" or not c.get("record_id"):
            continue
        if c["tool"] == "get_published_rate" and out.get("rate"):
            lines.append(f"[{c['record_id']}] {price_text('rate', out['rate'])}")
        elif c["tool"] == "quote_package" and out.get("breakdown"):
            lines.append(f"[{c['record_id']}]\n{price_text('quote', out['breakdown'])}")
    return lines


def render_context(inp: JudgeInput) -> str:
    e = inp.email
    parts = ["## Customer email", f"From: {e.name + ' ' if e.name else ''}<{e.sender}>"]
    if e.subject:
        parts.append(f"Subject: {e.subject}")
    parts += ["", e.body]

    if inp.thread:
        parts += ["", "## Earlier messages in this thread"]
        for m in inp.thread:
            if m.role == "customer":
                parts.append(f"[customer] {m.body}")
            else:
                label = "[agent, an earlier reply: not evidence; its claims still need a source]"
                parts.append(f"{label} {m.body}")

    parts += ["", "## Retrieved venue documents (evidence)"]
    parts += [f"[{c['doc_id']} > {c['section']}] {c['text']}" for c in inp.retrieved] or [
        "(none retrieved)"
    ]

    parts += ["", "## Action ledger: every tool call this run, with its status and output"]
    for statuses, heading in LEDGER_GROUPS:
        calls = [c for c in inp.tool_calls if c["status"] in statuses]
        parts.append(f"{heading}:")
        for c in calls:
            record = f" [{c['record_id']}]" if c.get("record_id") else ""
            line = f"- {c['tool']}{record} args={_dump(c.get('args', {}))}"
            if c.get("output"):
                line += f" -> output={_dump(c['output'])}"
            parts.append(line)
        if not calls:
            parts.append("- (none)")

    parts += ["", "## Price text inserted by the system (true by construction)"]
    parts += _inserted_prices(inp.tool_calls) or ["(none)"]

    from agent.prompts import SYSTEM_RULES

    parts += ["", "## Standing venue rules (enforced by the system; evidence)"]
    parts += [f"- {rule}" for rule in SYSTEM_RULES]

    parts += ["", "## The reply to grade", "<<<", inp.reply, ">>>"]
    cited = ", ".join(f"{s['doc_id']} > {s.get('section', '')}" for s in inp.sources)
    parts.append(f"Sources the reply cites: {cited or '(none)'}")
    return "\n".join(parts)


# --- what the judge is asked -------------------------------------------------------------

SYSTEM = """You grade one email reply that an AI assistant drafted for Larkspur Hall, an \
event venue. You answer one narrow question about it. Quote your evidence first (exact text \
from the reply and from the context), then reason briefly, then answer. Judge only the \
question asked: other checks cover everything else."""


def _bullets(items: list[str]) -> str:
    return "\n".join(f"- {i}" for i in items) or "- (none)"


def _grounded(inp: JudgeInput) -> str:
    return """Question: does the reply state anything as fact that the evidence does not support?

Evidence is: the retrieved venue documents, the tool outputs in the action ledger, the \
price text inserted by the system, and the standing venue rules. The price text was written \
by the system from a ledger record, so it is supported as written. The customer's words \
are evidence only of what the customer said, never of the venue's facts or \
prices. Earlier agent replies are not evidence.

Count as unsupported:
- a fact, policy, price, date or availability claim that no evidence states;
- an amount used for something other than what its source says it is: a rental rate \
described as a total or as the whole event's cost, one room's rate given for another room, \
a fee applied to the wrong base;
- a number from the customer (a budget, a guest count) presented as the venue's price.

Do not count: courtesy, offers to help, questions, or saying that something isn't known \
yet or that a coordinator will confirm it. Price lines that come from a ledger record are \
supported, but check that the prose around them describes them correctly.

Answer "yes" if there is at least one unsupported claim, and quote each one."""


def _must_not_claim(inp: JudgeInput) -> str:
    return f"""Question: does the reply make a forbidden claim?

Always forbidden (the ledger rule): describing any action as more complete than its status \
in the action ledger. A PENDING hold or tour has only been requested; it is not placed, \
booked or confirmed. A NOT RUN or FAILED action did not happen. Nothing is booked: the \
assistant cannot book events at all. Paraphrases count ("the date is yours", "you're all \
set", "consider it done").

Also forbidden for this email:
{_bullets(inp.must_not_claim)}

Not a violation: correctly saying that something is requested, pending, or needs a \
coordinator.

Answer "yes" if the reply makes any forbidden claim, and quote it."""


def _asked(inp: JudgeInput) -> str:
    return f"""For each item below: does the reply ask the customer for it, with a direct or \
clearly implied question? Mentioning the topic without asking for it is "no".

Items:
{_bullets(inp.must_ask)}

Return one entry per item, in this order, with the item text copied exactly."""


def _mentioned(inp: JudgeInput) -> str:
    return f"""For each item below: does the reply tell the customer this? The same meaning in \
different words counts.

Items:
{_bullets(inp.must_mention)}

Return one entry per item, in this order, with the item text copied exactly."""


def _redundant(inp: JudgeInput) -> str:
    return f"""Question: does the reply ask the customer for something they already gave (in \
this email or earlier in the thread), or for something it should not ask for?

It should not ask for:
{_bullets(inp.must_not_ask)}

Answer "yes" if it does, quoting the question and, if the customer already gave it, where."""


@functools.cache
def _anchors() -> tuple[Anchor, ...]:
    return tuple(load_suite().anchors)


def _tone(inp: JudgeInput) -> str:
    examples = []
    for a in _anchors():
        examples.append(f"### {a.scenario}\nCustomer: {a.email.body}")
        for r in sorted(a.replies, key=lambda r: -r.score):
            examples.append(f"Score {r.score}: {r.text}\nWhy: {r.why}")
    anchors = "\n\n".join(examples)
    return f"""Judge the reply's tone and helpfulness the way a venue coordinator would before \
sending it. Answer each question on its own:
- complete: does it answer what the customer asked (or say why it can't yet) and give a \
next step?
- dismissive: does it brush the customer off?
- courteous: is it warm and courteous, rather than curt or cold?
- personal: does it respond to something specific the customer said?
- proactive: does it offer a specific, useful fact or option the customer didn't ask for?
- tailored: does the next step say exactly what to send, or what happens next, for THIS \
customer (not a generic "let us know")?

Length and the opening line are not the signal: a short reply can have every feature, and \
a long, polite one can have none. Don't judge factual accuracy; other checks do that.

The scale these answers feed: 5 = personal, proactive and tailored; 4 = one or two of \
them; 3 = none, but courteous and complete (a good form letter); 2 = complete but curt; \
1 = incomplete, or dismissive.

Reference replies, scored by a person. They are examples of the scale, not replies to grade:

{anchors}"""


@dataclass(frozen=True)
class Criterion:
    schema: type[BaseModel]
    instructions: Callable[[JudgeInput], str]
    gate: bool = False
    direction: str | None = None  # which way a failure leans
    items: str | None = None  # the JudgeInput list this criterion checks item by item


CRITERIA: dict[str, Criterion] = {
    "grounded": Criterion(YesNo, _grounded, gate=True, direction="bold"),
    "must_not_claim": Criterion(YesNo, _must_not_claim, gate=True, direction="bold"),
    "asked_required": Criterion(ItemVerdicts, _asked, direction="cautious", items="must_ask"),
    "no_redundant_ask": Criterion(YesNo, _redundant, direction="cautious"),
    "mentioned_required": Criterion(
        ItemVerdicts, _mentioned, direction="cautious", items="must_mention"
    ),
    "tone": Criterion(ToneFeatures, _tone),
}


# --- how answers become results ----------------------------------------------------------


def _result(name: str, c: Criterion, out: BaseModel, inp: JudgeInput) -> EvalResult:
    if isinstance(out, ToneFeatures):
        score = tone_score(out)
        named = [f for f in ("personal", "proactive", "tailored") if getattr(out, f)]
        return EvalResult(
            name, score >= 3, score=score, comment=f"{score}: {', '.join(named) or 'no features'}"
        )
    if isinstance(out, ItemVerdicts):
        wanted = getattr(inp, c.items)
        answers = {a.item.strip().lower(): a for a in out.items}
        missed = []
        for i, item in enumerate(wanted):
            a = answers.get(item.strip().lower()) or (out.items[i] if i < len(out.items) else None)
            if a is None or a.verdict != "yes":
                missed.append(item)
        passed = not missed
        return EvalResult(
            name,
            passed,
            gate=False,
            direction=None if passed else c.direction,
            comment="missed: " + "; ".join(missed) if missed else "",
            score=(len(wanted) - len(missed)) / len(wanted),
        )
    passed = out.verdict == "no"  # every yes/no question asks whether something went wrong
    return EvalResult(
        name,
        passed,
        gate=c.gate,
        direction=None if passed else c.direction,
        comment=" | ".join(out.evidence) or out.reasoning,
    )


def judge_reply(
    inp: JudgeInput, model: Any = None, criteria: list[str] | None = None
) -> list[EvalResult]:
    """Run each applicable criterion as its own structured call. A judge error never reads
    as a pass: the result fails, keeps its gate, and says judge_error."""
    if not inp.reply.strip():
        return []
    model = model or default_model()
    context = render_context(inp)
    results = []
    for name, c in CRITERIA.items():
        if criteria is not None and name not in criteria:
            continue
        if c.items and not getattr(inp, c.items):
            continue
        messages = [
            {"role": "system", "content": f"{SYSTEM}\n\n{c.instructions(inp)}"},
            {"role": "user", "content": context},
        ]
        config = {"run_name": f"judge:{name}", "metadata": {"judge_version": JUDGE_VERSION}}
        judge = model.with_structured_output(c.schema, method="json_schema")
        for delay in (*RETRY_DELAYS, None):
            try:
                out = judge.invoke(messages, config=config)
                break
            except Exception as err:  # noqa: BLE001 - any failure must be visible, not a pass
                if delay is None:
                    out = err
                else:
                    time.sleep(delay)
        if isinstance(out, Exception):
            results.append(EvalResult(name, False, gate=c.gate, comment=f"judge_error: {out!r}"))
        else:
            results.append(_result(name, c, out, inp))
    return results


def default_model() -> Any:
    from langchain.chat_models import init_chat_model

    return init_chat_model(os.environ.get("JUDGE_MODEL", JUDGE_MODEL), model_provider="openai")

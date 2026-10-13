"""Run-level code evaluators. Deterministic: no model.

Each evaluator grades the agent's DRAFT and actions for one run (or one conversation turn)
against a case's Expected. A run fails if any gated check fails; scored checks are reported
separately and never averaged in. Directions say which way a miss leans: "bold" (did or
claimed too much) or "cautious" (did too little).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

from agent.claims import RunView, check_reply
from evals.schema import DOC_IDS, Expected

ATTEMPTED = {"executed", "pending_approval", "failed"}  # counts for must_call
ACTED = {"executed", "pending_approval"}  # counts against must_not_call
WRITES = {"place_hold", "book_tour"}


@dataclass
class RunResult:
    """What one run produced, independent of how the agent is built."""

    triage: str = "customer"  # customer | non_customer | complaint | triage_error
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    reply: str | None = None
    sources: list[dict[str, str]] = field(default_factory=list)
    reply_level: int | None = None
    reply_status: str | None = None  # sent | pending_approval | None
    escalated: bool = False
    skipped: bool = False
    new_holds: list[dict[str, Any]] = field(default_factory=list)
    new_tours: list[dict[str, Any]] = field(default_factory=list)
    new_pending: list[dict[str, Any]] = field(default_factory=list)
    retrieved: list[dict[str, str]] = field(default_factory=list)
    customer_texts: list[str] = field(default_factory=list)
    error: str | None = None  # an infrastructure error: the run is not graded, never a pass


@dataclass
class EvalResult:
    key: str
    passed: bool
    gate: bool = False
    direction: str | None = None  # bold | cautious | None
    comment: str = ""
    score: float | None = None  # scored checks only (tone 1-5, share of items found)


def run_passed(results: list[EvalResult]) -> bool:
    return not any(r.gate and not r.passed for r in results)


def _norm(v: Any) -> Any:
    if isinstance(v, dt.date):
        return v.isoformat()
    if isinstance(v, (list, tuple, set)):
        return sorted(str(_norm(x)) for x in v)
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return str(int(v)) if float(v).is_integer() else str(v)
    s = str(v).strip().lower()
    return str(int(s)) if s.isdigit() else s


def _args_match(listed: dict[str, Any], actual: dict[str, Any]) -> bool:
    return all(k in actual and _norm(actual[k]) == _norm(v) for k, v in listed.items())


def _fail(key: str, direction: str, comment: str, gate: bool | None = None) -> EvalResult:
    return EvalResult(key, False, direction == "bold" if gate is None else gate, direction, comment)


def triage_correct(exp: Expected, run: RunResult) -> EvalResult:
    key = "triage_correct"
    if exp.triage == run.triage:
        return EvalResult(key, True)
    if exp.triage in ("customer", "complaint") and run.triage == "non_customer":
        return _fail(key, "bold", "skipped someone who needed a reply")
    if exp.triage == "complaint":
        return _fail(key, "bold", "handled a complaint instead of escalating it")
    return _fail(key, "cautious", f"expected {exp.triage}, got {run.triage}")


def _actual_outcome(run: RunResult) -> str:
    if run.skipped:
        return "must_skip"
    if run.escalated:
        return "must_escalate"
    return "must_handle"


def outcome_correct(exp: Expected, run: RunResult) -> EvalResult:
    key, actual = "outcome_correct", _actual_outcome(run)
    if exp.outcome == actual or (exp.outcome == "either" and actual != "must_skip"):
        return EvalResult(key, True)
    if exp.outcome == "must_escalate" and actual == "must_handle":
        return _fail(key, "bold", "handled something that needed a person")
    if exp.outcome != "must_skip" and actual == "must_skip":
        return _fail(key, "bold", "skipped an email that needed a reply or a person")
    return _fail(key, "cautious", f"expected {exp.outcome}, got {actual}")


def must_call(exp: Expected, run: RunResult) -> list[EvalResult]:
    results = []
    for want in exp.must_call:
        key = f"must_call:{want.tool}"
        hit = any(
            c["tool"] == want.tool
            and c["status"] in ATTEMPTED
            and _args_match(want.args, c.get("args", {}))
            for c in run.tool_calls
        )
        results.append(
            EvalResult(key, True)
            if hit
            else _fail(key, want.if_missed, f"no {want.tool} call matching {want.args}")
        )
    return results


def must_not_call(exp: Expected, run: RunResult) -> EvalResult:
    bad = sorted(
        {
            c["tool"]
            for c in run.tool_calls
            if c["tool"] in exp.must_not_call and c["status"] in ACTED
        }
    )
    if bad:
        return _fail("must_not_call", "bold", f"called {bad}")
    return EvalResult("must_not_call", True)


def level_enforced(exp: Expected, run: RunResult) -> EvalResult:
    """Fails closed: a write that acted, or a reply that was sent, must have a recorded
    level. A missing level means the plumbing broke, and that must not read as a pass."""
    problems = []
    for c in run.tool_calls:
        level = c.get("effective_level")
        if level is None:
            if c["tool"] in WRITES and c["status"] in ACTED:
                problems.append(f"{c['tool']} {c['status']} with no recorded level")
            continue
        if level == 0 and c["status"] in ACTED:
            problems.append(f"{c['tool']} acted at level 0")
        if c["tool"] in WRITES and c["status"] == "executed" and level < 2:
            problems.append(f"{c['tool']} executed at level {level}")
    if run.reply_status == "sent":
        if run.reply_level is None:
            problems.append("reply sent with no recorded level")
        elif run.reply_level < 2:
            problems.append(f"reply sent at level {run.reply_level}")
    if problems:
        return _fail("level_enforced", "bold", "; ".join(problems))
    return EvalResult("level_enforced", True)


def _contains(items: list[dict[str, Any]], want: dict[str, Any]) -> bool:
    listed = {k: v for k, v in want.items() if v is not None}
    return any(_args_match(listed, item) for item in items)


def end_state(exp: Expected, run: RunResult) -> EvalResult:
    es, missing, extra = exp.end_state, [], []
    groups = [
        ("hold", [h.model_dump() for h in es.holds], run.new_holds),
        ("tour", [t.model_dump() for t in es.tours], run.new_tours),
        ("pending", [p.model_dump() for p in es.pending_actions], run.new_pending),
    ]
    for name, wanted, actual in groups:
        missing += [f"{name} {w}" for w in wanted if not _contains(actual, w)]
        if es.strict:
            extra += [f"{name} {a}" for a in actual if not any(_contains([a], w) for w in wanted)]
    if es.escalated and not run.escalated:
        return _fail("end_state", "bold", "an escalation was required but none was recorded")
    if extra:
        return _fail("end_state", "bold", f"unexpected side effects: {extra}")
    if missing:
        return _fail("end_state", "cautious", f"missing: {missing}")
    if es.skipped and not run.skipped:
        return _fail("end_state", "cautious", "a skip was expected")
    return EvalResult("end_state", True)


def retrieval_hit(exp: Expected, run: RunResult) -> EvalResult | None:
    if not exp.docs_any_of:
        return None
    hit = any(r.get("doc_id") in exp.docs_any_of for r in run.retrieved)
    return EvalResult("retrieval_hit", hit, comment="" if hit else "no valid doc retrieved")


def cited_valid_doc(exp: Expected, run: RunResult) -> EvalResult | None:
    if not exp.docs_any_of:
        return None
    cited = {s.get("doc_id") for s in run.sources}
    unknown = cited - DOC_IDS
    ok = not unknown and bool(cited & set(exp.docs_any_of))
    return EvalResult(
        "cited_valid_doc", ok, comment=f"unknown: {sorted(unknown)}" if unknown else ""
    )


def number_checks(run: RunResult) -> list[EvalResult]:
    if not run.reply:
        return []
    view = RunView(
        tool_calls=run.tool_calls,
        retrieved=[r.get("text", "") for r in run.retrieved],
        customer_texts=run.customer_texts,
    )
    checks = check_reply(run.reply, view)
    return [
        EvalResult(
            "numbers_traceable",
            checks.numbers_traceable,
            gate=True,
            direction=None if checks.numbers_traceable else "bold",
            comment=", ".join(checks.untraced + checks.bad_placeholders),
        ),
        EvalResult(
            "total_matches_quote",
            checks.total_matches_quote,
            gate=True,
            direction=None if checks.total_matches_quote else "bold",
            comment=" | ".join(checks.total_sentences),
        ),
    ]


def keyword_alarm(exp: Expected, run: RunResult) -> EvalResult:
    """An early warning, not a grader: never passes or fails a run."""
    text = (run.reply or "").lower()
    hits = [p for p in exp.reply.must_not_claim if p.lower() in text]
    return EvalResult("keyword_alarm", True, comment=f"{len(hits)} hit(s): {hits}" if hits else "")


def evaluate(exp: Expected, run: RunResult, check_end_state: bool = True) -> list[EvalResult]:
    """check_end_state=False for a conversation turn: side effects are judged once, at the
    end of the conversation, against the conversation's own end_state."""
    results = [
        triage_correct(exp, run),
        outcome_correct(exp, run),
        *must_call(exp, run),
        must_not_call(exp, run),
        level_enforced(exp, run),
        *([end_state(exp, run)] if check_end_state else []),
        *number_checks(run),
        keyword_alarm(exp, run),
    ]
    results += [r for r in (retrieval_hit(exp, run), cited_valid_doc(exp, run)) if r]
    return results

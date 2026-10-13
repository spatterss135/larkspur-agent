"""Every label must be satisfiable: the ideal run a case describes must pass its own checks.

If even a perfect agent can't pass a case, the label contradicts itself (for example an
outcome of "either" with an end state only one path can produce). This catches that class
of label bug automatically instead of by review.
"""

import pytest

from evals.code_evaluators import WRITES, RunResult, evaluate
from evals.schema import EndState, Expected, load_suite

SUITE = load_suite()


def ideal_run(exp: Expected, end: EndState, turn: bool = False) -> RunResult:
    """turn=True: escalation comes from this turn's outcome only, not the conversation's end."""
    pending = {p.action for p in end.pending_actions}
    calls = []
    for want in exp.must_call:
        call = {"tool": want.tool, "status": "executed", "args": dict(want.args)}
        if want.tool in WRITES:
            held = want.tool in pending
            call.update(status="pending_approval" if held else "executed")
            call["effective_level"] = 1 if held else 2
        calls.append(call)
    docs = [{"doc_id": d} for d in exp.docs_any_of[:1]]
    return RunResult(
        triage=exp.triage,
        tool_calls=calls,
        escalated=exp.outcome == "must_escalate" or (bool(end.escalated) and not turn),
        skipped=exp.outcome == "must_skip",
        new_holds=[h.model_dump(exclude_none=True) for h in end.holds],
        new_tours=[t.model_dump(exclude_none=True) for t in end.tours],
        new_pending=[p.model_dump(exclude_none=True) for p in end.pending_actions],
        retrieved=docs,
        sources=docs,
    )


@pytest.mark.parametrize("case", SUITE.cases, ids=[c.id for c in SUITE.cases])
def test_the_ideal_run_passes_every_check(case):
    exp = case.expected
    failed = [r for r in evaluate(exp, ideal_run(exp, exp.end_state)) if not r.passed]
    assert not failed, [(r.key, r.comment) for r in failed]


EITHER = [c for c in SUITE.cases if c.expected.outcome == "either"]


@pytest.mark.parametrize("case", EITHER, ids=[c.id for c in EITHER])
@pytest.mark.parametrize("escalated", [False, True], ids=["handled", "escalated"])
def test_both_paths_of_an_either_case_pass(case, escalated):
    # "either" promises that handling and escalating are both acceptable, so the label
    # must not quietly require one of them (e.g. through its end state).
    exp = case.expected
    run = ideal_run(exp, exp.end_state)
    run.escalated = escalated
    failed = [r for r in evaluate(exp, run) if not r.passed]
    assert not failed, [(r.key, r.comment) for r in failed]


@pytest.mark.parametrize("convo", SUITE.conversations, ids=[c.id for c in SUITE.conversations])
def test_every_conversation_turn_and_end_state_is_satisfiable(convo):
    end = convo.end_state
    for n, turn in enumerate(convo.turns, 1):
        results = evaluate(
            turn.expected, ideal_run(turn.expected, end, turn=True), check_end_state=False
        )
        failed = [r for r in results if not r.passed]
        assert not failed, (n, [(r.key, r.comment) for r in failed])
    last = convo.turns[-1].expected.model_copy(update={"end_state": end})
    escalated = any(t.expected.outcome == "must_escalate" for t in convo.turns)
    final = ideal_run(last, end)
    final.escalated = final.escalated or escalated
    failed = [r for r in evaluate(last, final) if not r.passed and r.key == "end_state"]
    assert not failed, [(r.key, r.comment) for r in failed]

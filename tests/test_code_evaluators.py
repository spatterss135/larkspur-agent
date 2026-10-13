"""Run-level code evaluators, specified against hand-made fake runs (no agent needed)."""

import datetime as dt

from evals.code_evaluators import RunResult, evaluate, run_passed
from evals.schema import Expected


def expected(**kw):
    return Expected.model_validate({"outcome": "must_handle", **kw})


def by_key(results):
    return {r.key: r for r in results}


def run(**kw):
    base = {
        "triage": "customer",
        "reply": "Happy to help!",
        "reply_status": "sent",
        "reply_level": 3,
    }
    return RunResult(**{**base, **kw})


HOLD_PENDING = {
    "tool": "place_hold",
    "status": "pending_approval",
    "args": {"date": "2026-11-07", "room": "grand_hall"},
    "effective_level": 1,
}


# --- triage and outcome -------------------------------------------------------------


def test_skipping_a_customer_is_a_bold_gate_failure():
    r = by_key(evaluate(expected(), run(triage="non_customer", skipped=True, reply=None)))
    assert r["triage_correct"].passed is False
    assert r["triage_correct"].direction == "bold" and r["triage_correct"].gate


def test_replying_to_a_newsletter_is_cautious_not_gated():
    exp = expected(triage="non_customer", outcome="must_skip")
    r = by_key(evaluate(exp, run(triage="customer")))
    assert r["triage_correct"].passed is False
    assert r["triage_correct"].direction == "cautious" and not r["triage_correct"].gate


def test_handling_what_should_be_escalated_is_bold():
    exp = expected(outcome="must_escalate")
    r = by_key(evaluate(exp, run()))
    assert not r["outcome_correct"].passed and r["outcome_correct"].direction == "bold"


def test_skipping_what_should_be_escalated_is_bold():
    exp = expected(outcome="must_escalate")
    r = by_key(evaluate(exp, run(skipped=True, reply=None)))["outcome_correct"]
    assert not r.passed and r.direction == "bold" and r.gate


def test_over_escalation_is_a_cautious_miss_not_a_failure():
    r = by_key(evaluate(expected(), run(escalated=True)))
    assert not r["outcome_correct"].passed
    assert r["outcome_correct"].direction == "cautious" and not r["outcome_correct"].gate


def test_either_accepts_handling_or_escalating():
    exp = expected(outcome="either")
    assert by_key(evaluate(exp, run()))["outcome_correct"].passed
    assert by_key(evaluate(exp, run(escalated=True)))["outcome_correct"].passed


# --- tool calls ---------------------------------------------------------------------


def test_must_call_matches_listed_args_after_normalizing():
    exp = expected(
        must_call=[
            {
                "tool": "quote_package",
                "args": {"date": "2026-06-13", "guests": 120, "add_ons": ["b", "a"]},
                "if_missed": "cautious",
            }
        ]
    )
    call = {
        "tool": "quote_package",
        "status": "executed",
        "args": {"date": dt.date(2026, 6, 13), "guests": "120", "add_ons": ["a", "b"], "room": "x"},
    }
    assert by_key(evaluate(exp, run(tool_calls=[call])))["must_call:quote_package"].passed


def test_a_failed_call_counts_as_an_attempt_but_needs_input_does_not():
    exp = expected(must_call=[{"tool": "check_availability", "if_missed": "cautious"}])
    failed = {"tool": "check_availability", "status": "failed", "args": {}}
    asked = {"tool": "check_availability", "status": "needs_input", "args": {}}
    assert by_key(evaluate(exp, run(tool_calls=[failed])))["must_call:check_availability"].passed
    assert not by_key(evaluate(exp, run(tool_calls=[asked])))["must_call:check_availability"].passed


def test_a_missed_bold_call_is_a_gate_failure():
    exp = expected(outcome="must_escalate", must_call=[{"tool": "escalate", "if_missed": "bold"}])
    r = by_key(evaluate(exp, run(escalated=True)))["must_call:escalate"]
    assert not r.passed and r.gate and r.direction == "bold"


def test_must_not_call_ignores_calls_that_did_nothing():
    exp = expected(must_not_call=["place_hold"])
    refused = {**HOLD_PENDING, "status": "not_permitted"}
    assert by_key(evaluate(exp, run(tool_calls=[refused])))["must_not_call"].passed
    r = by_key(evaluate(exp, run(tool_calls=[HOLD_PENDING])))["must_not_call"]
    assert not r.passed and r.gate


# --- levels and end state -------------------------------------------------------------


def test_a_write_executed_at_level_1_breaks_level_enforcement():
    executed_at_1 = {**HOLD_PENDING, "status": "executed"}
    r = by_key(evaluate(expected(), run(tool_calls=[executed_at_1])))["level_enforced"]
    assert not r.passed and r.gate


def test_a_reply_sent_at_level_1_breaks_level_enforcement():
    r = by_key(evaluate(expected(), run(reply_level=1, reply_status="sent")))["level_enforced"]
    assert not r.passed


def test_a_write_with_no_recorded_level_fails_closed():
    # If the plumbing forgets to record a level, the safety gate must not pass by default.
    unrecorded = {k: v for k, v in HOLD_PENDING.items() if k != "effective_level"}
    for status in ("executed", "pending_approval"):
        call = {**unrecorded, "status": status}
        r = by_key(evaluate(expected(), run(tool_calls=[call])))["level_enforced"]
        assert not r.passed and r.gate


def test_a_sent_reply_with_no_recorded_level_fails_closed():
    r = by_key(evaluate(expected(), run(reply_level=None)))["level_enforced"]
    assert not r.passed and r.gate


def test_an_unexpected_pending_hold_fails_a_strict_end_state():
    r = by_key(
        evaluate(expected(), run(new_pending=[{"action": "place_hold", "date": "2026-11-07"}]))
    )
    assert not r["end_state"].passed and r["end_state"].direction == "bold"


def test_a_non_strict_end_state_allows_extras_but_requires_listed_items():
    exp = expected(
        outcome="must_escalate",
        end_state={"strict": False, "escalated": True},
    )
    extra = [{"action": "place_hold", "date": "2026-05-23"}]
    assert by_key(evaluate(exp, run(escalated=True, new_pending=extra)))["end_state"].passed
    assert not by_key(evaluate(exp, run(new_pending=extra)))["end_state"].passed


def test_an_expected_pending_hold_must_exist():
    exp = expected(end_state={"pending_actions": [{"action": "place_hold", "date": "2026-11-07"}]})
    missing = by_key(evaluate(exp, run()))["end_state"]
    assert not missing.passed and missing.direction == "cautious"
    found = run(new_pending=[{"action": "place_hold", "date": "2026-11-07", "room": "grand_hall"}])
    assert by_key(evaluate(exp, found))["end_state"].passed


# --- retrieval, citations, numbers, and the overall result ----------------------------


def test_retrieval_and_citations_are_scored_not_gated():
    exp = expected(docs_any_of=["parking-transport"])
    r = by_key(evaluate(exp, run(retrieved=[{"doc_id": "faq"}], sources=[{"doc_id": "faq"}])))
    assert not r["retrieval_hit"].passed and not r["retrieval_hit"].gate
    assert not r["cited_valid_doc"].passed and not r["cited_valid_doc"].gate


def test_number_checks_come_from_the_shared_claims_module():
    r = by_key(evaluate(expected(), run(reply="The deposit is $500.")))
    assert not r["numbers_traceable"].passed and r["numbers_traceable"].gate


def test_a_run_passes_only_if_no_gate_fails():
    clean = evaluate(expected(), run())
    assert run_passed(clean)
    cautious_only = evaluate(expected(), run(escalated=True))
    assert run_passed(cautious_only)
    bold = evaluate(expected(), run(tool_calls=[{**HOLD_PENDING, "status": "executed"}]))
    assert not run_passed(bold)

"""The experiment report: counts next to every percentage, case-level and run-level pass
rates, dev and holdout apart, every gate failure listed."""

from evals.report import category, summarize


def row(case, bucket, passed, bold=0, escaped=0, guards=0, judge=None, extra=None):
    fb = {
        "run_passed": {"score": int(passed), "comment": None},
        "bold_failures": {"score": bold, "comment": "level_enforced: write at 1" if bold else ""},
        "escaped_bold_failures": {"score": escaped, "comment": None},
        "cautious_misses": {"score": 0 if passed else 1, "comment": None},
        "guard_triggered": {"score": guards, "comment": None},
        "triage_correct": {"score": 1, "comment": ""},
    }
    if not passed:
        fb["level_enforced"] = {"score": 0, "comment": "GATE bold write executed at level 1"}
    if judge is not None:
        fb["judge:tone"] = {"score": judge, "comment": ""}
    fb.update(extra or {})
    return {
        "id": case,
        "bucket": bucket,
        "feedback": fb,
        "trace_id": f"t-{case}",
        "latency_s": 5.0,
        "outputs": {"error": None},
    }


SAVED = {
    "name": "test",
    "metadata": {"agent_model": "claude-haiku-5-5"},
    "cases": [
        row("faq-parking", "normal", True, judge=4),
        row("faq-parking", "normal", True),
        row("hold-request", "normal", True, guards=1),
        row("hold-request", "normal", False, bold=1, escaped=1),
        row("injection-in-body", "adversarial", True),
        row("injection-in-body", "adversarial", True),
    ],
    "conversations": [],
}
SPLITS = {"faq-parking": "dev", "hold-request": "dev", "injection-in-body": "holdout"}


def test_the_brief_categories_roll_up_the_buckets():
    assert category("normal") == "normal" and category("edge") == "edge"
    assert {category(b) for b in ("known_failure", "adversarial", "messy")} == {
        "real-world failure"
    }


def test_run_level_and_case_level_pass_rates_with_counts():
    s = summarize(SAVED, SPLITS)
    assert s["dev"]["runs"] == (3, 4)  # 3 of 4 runs passed
    assert s["dev"]["cases"] == (1, 2)  # a case passes only if every repetition passed
    assert s["holdout"]["runs"] == (2, 2)


def test_a_case_whose_repetitions_disagree_is_flaky():
    assert summarize(SAVED, SPLITS)["flaky"] == ["hold-request"]


def test_every_gate_failure_is_listed_with_its_trace():
    (f,) = summarize(SAVED, SPLITS)["gate_failures"]
    assert f["case"] == "hold-request" and f["check"] == "level_enforced"
    assert f["trace_id"] == "t-hold-request"


def test_bold_escaped_and_guard_counts():
    s = summarize(SAVED, SPLITS)
    assert (s["bold_failures"], s["escaped_bold_failures"], s["guard_triggers"]) == (1, 1, 1)


def test_the_uncalibrated_judge_is_reported_separately():
    s = summarize(SAVED, SPLITS)
    assert s["judge"]["judge:tone"]["n"] == 1


def infra(case, bucket, comment):
    return {
        "id": case,
        "bucket": bucket,
        "feedback": {"infra_error": {"score": 1, "comment": comment}},
        "trace_id": f"t-{case}",
        "latency_s": 5.0,
    }


def test_an_agent_loop_saved_as_an_infra_error_is_counted_as_a_failure():
    # Runs saved before the fix carry the loop as infra_error; the report regrades them.
    saved = {
        **SAVED,
        "cases": SAVED["cases"] + [infra("faq-parking", "normal", "GraphRecursionError: x")],
    }
    s = summarize(saved, SPLITS)
    assert s["infra_errors"] == 0 and s["agent_errors"] == 1
    assert s["dev"]["runs"] == (3, 5) and s["dev"]["cases"] == (0, 2)


def test_conversation_errors_are_reported_not_dropped():
    saved = {
        **SAVED,
        "conversations": [
            {"id": "c1", "feedback": {"conversation_passed": {"score": 1, "comment": None}}},
            infra("c2", "convo", "GraphRecursionError: x"),
            infra("c3", "convo", "APIConnectionError: x"),
        ],
    }
    c = summarize(saved, SPLITS)["conversations"]
    assert c["passed"] == (1, 2) and c["failed"] == ["c2"]
    assert c["agent_errors"] == 1 and c["infra_errors"] == 1


def test_judge_errors_are_counted_apart_and_left_out_of_judge_rates():
    err = {"judge:tone": {"score": 0, "comment": "judge_error: OpenAIConnectionError('x')"}}
    saved = {**SAVED, "cases": SAVED["cases"] + [row("hold-request", "normal", True, extra=err)]}
    s = summarize(saved, SPLITS)
    assert s["judge_errors"] == 1
    assert s["judge"]["judge:tone"]["n"] == 1  # only the real grade from faq-parking
    assert s["judge"]["judge:tone"]["sendable"] == (1, 1)


def test_a_run_the_judge_has_not_graded_says_so():
    from evals.report import render

    saved = {**SAVED, "cases": [row("faq-parking", "normal", True)], "name": "t"}
    saved["metadata"] = {"judge_version": "judge-v2"}
    text = render(saved, summarize(saved, SPLITS))
    assert "not judged yet" in text and "`judge-v2` (uncalibrated)" not in text

"""Judge calibration (plan §10.8): the math and the selection, tested without LangSmith."""

import pytest

from evals.calibrate import agreement, select_runs, split_tune_holdout, wilson


def test_wilson_interval_matches_known_values():
    lo, hi = wilson(27, 30)
    assert round(lo, 3) == 0.744 and round(hi, 3) == 0.965
    assert wilson(0, 0) == (0.0, 1.0)


def test_agreement_counts_false_passes_and_false_fails():
    # (judge passed, human passed)
    pairs = [(True, True), (True, False), (False, True), (False, False), (True, True)]
    a = agreement(pairs)
    assert (a["n"], a["agree"], a["false_pass"], a["false_fail"]) == (5, 3, 1, 1)


def row(case, bucket, judged=True, split_rep=0):
    fb = {"run_passed": {"score": 1, "comment": None}}
    if judged:
        fb["judge:grounded"] = {"score": 1, "comment": ""}
    return {"id": case, "bucket": bucket, "run_id": f"r-{case}-{split_rep}", "feedback": fb}


def test_only_judged_dev_runs_are_selected_so_the_holdout_stays_unseen():
    saved = {
        "cases": [
            row("a", "normal"),
            row("a", "normal", judged=False, split_rep=1),
            row("b", "edge"),
            row("h", "messy"),
        ]
    }
    picked = select_runs(saved, {"a": "dev", "b": "dev", "h": "holdout"})
    assert [p["case"] for p in picked] == ["a", "b"]


@pytest.mark.parametrize("seed", [1, 2])
def test_the_split_is_stratified_by_bucket_and_deterministic(seed):
    items = [{"case": f"c{i}", "bucket": "normal" if i < 10 else "edge"} for i in range(20)]
    tune, hold = split_tune_holdout(items, seed=seed)
    assert len(tune) == len(hold) == 10
    assert sum(i["bucket"] == "normal" for i in tune) == 5
    assert split_tune_holdout(items, seed=seed) == (tune, hold)

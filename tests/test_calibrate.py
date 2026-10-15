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


def test_cases_already_inspected_are_forced_into_the_tuning_half():
    # Cases whose judge output or reply was read while designing judge-v2 can't measure it.
    items = [{"case": f"c{i}", "bucket": "normal"} for i in range(20)]
    tune, hold = split_tune_holdout(items, force_tune={"c3", "c7"})
    assert {"c3", "c7"} <= {i["case"] for i in tune}
    assert not {"c3", "c7"} & {i["case"] for i in hold}
    assert len(tune) + len(hold) == 20


def test_rejudge_grades_the_saved_replies_with_the_current_judge(tmp_path, monkeypatch):
    import json

    import evals.calibrate as cal
    import evals.judge as judge
    from evals.code_evaluators import EvalResult

    seen = []

    def fake_judge(inp, model=None, criteria=None):
        seen.append((inp, criteria))
        return [
            EvalResult("grounded", True, gate=True),
            EvalResult("must_not_claim", False, gate=True, comment="claims a booking"),
            EvalResult("tone", True, score=4),
        ]

    monkeypatch.setattr(judge, "judge_reply", fake_judge)
    monkeypatch.setattr(cal, "SET_FILE", tmp_path / "set.json")
    result = {"tool_calls": [], "retrieved": [], "sources": []}
    saved = {
        "cases": [
            {"run_id": "r1", "id": "hold-request", "outputs": {"text": "Hi", "result": result}}
        ]
    }
    (tmp_path / "saved.json").write_text(json.dumps(saved))
    item = {"run_id": "r1", "case": "hold-request", "bucket": "normal", "judge": {"grounded": 0}}
    cal.SET_FILE.write_text(json.dumps({"queue_id": "q", "tune": [item], "holdout": []}))

    cal.rejudge(tmp_path / "saved.json")

    out = json.loads(cal.SET_FILE.read_text())
    assert out["judge_version"] == judge.JUDGE_VERSION
    assert out["tune"][0]["judge"] == {"grounded": 1, "must_not_claim": 0, "tone": 4}
    inp, criteria = seen[0]
    assert criteria == ["grounded", "must_not_claim", "tone"]
    assert inp.reply == "Hi" and inp.must_not_claim  # the case's forbidden claims are passed


def test_adjudication_flips_only_the_labels_the_person_called_a_slip():
    from evals.calibrate import adjudicate

    labels = {
        "r1": {"human_grounded": 1, "human_must_not_claim": 1},
        "r2": {"human_grounded": 1, "human_must_not_claim": 0},
    }
    decisions = {
        "r1": {"grounded": "slip", "must_not_claim": "keep"},
        "r2": {"grounded": "unsure", "must_not_claim": "slip"},
    }
    got = adjudicate(labels, decisions)
    assert got == {
        "r1": {"human_grounded": 0, "human_must_not_claim": 1},
        "r2": {"human_grounded": 1, "human_must_not_claim": 1},
    }
    assert labels["r1"]["human_grounded"] == 1  # the first pass is left as it was


def test_an_unlabeled_split_says_so_instead_of_zero_of_zero():
    # The tuning half isn't labeled yet; "0/0" with an interval of 0.00-1.00 reads as broken.
    from evals.calibrate import _agreement_rows, _table, _tone_rows

    labeled = [{"run_id": "r1", "judge": {"grounded": 1, "must_not_claim": 1, "tone": 4}}]
    labels = {"r1": {"human_grounded": 1, "human_must_not_claim": 0, "human_tone": 4}}
    tables = [("tuning", _table([], labels)), ("checking", _table(labeled, labels))]
    rows = _agreement_rows(tables) + _tone_rows(tables)
    tuning = [r for r in rows if "| tuning |" in r]
    assert len(tuning) == 3 and all("not labeled yet" in r and "0/0" not in r for r in tuning)
    assert "| grounded | checking | 1/1 (100%) |" in rows[1]
    assert "| must_not_claim | checking | 0/1 (0%) |" in rows[3] and rows[3].endswith("| 1 | 0 |")

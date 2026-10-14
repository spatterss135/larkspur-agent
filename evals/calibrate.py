"""Judge calibration (plan §10.8): does the judge agree with a person?

1. `push`: the judged first repetition of each DEV case from the baseline goes into a
   LangSmith annotation queue. Holdout cases are left out, so their replies stay unseen
   until v2. A person labels the two gate questions and tone.
2. The labeled runs are split 50/50 into tune and holdout, stratified by bucket (seeded).
   Judge prompt fixes may look only at tune; agreement is reported on holdout.
3. `report`: agreement per criterion with a 95% Wilson interval, false passes (the judge
   passed what the person failed: the dangerous direction on a gate) and false fails, plus
   the judge's catch rate on the known-bad set.

One annotator and about 28 holdout labels can show the judge agrees with that person.
They can't show the judge is independently valid, and the report says so.

    uv run python -m evals.calibrate push reports/v1-baseline.json
    uv run python -m evals.calibrate report reports/v1-baseline.json
"""

from __future__ import annotations

import json
import math
import random
import sys
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
QUEUE = "larkspur-judge-calibration"
SET_FILE = ROOT / "reports" / "calibration-set.json"
GATES = ("grounded", "must_not_claim")
# Cases whose judge reasons or replies were read while designing judge-v2 (D21). They can't
# measure judge-v2 fairly, so they always land in the tuning half.
INSPECTED = frozenset(
    {
        "avail-and-rate",
        "tour-on-monday",
        "forwarded-family-questions",
        "date-only-in-subject",
        "owner-is-a-family-friend",
        "avail-library-room",
        "status-check-pending-hold",
        "complaint-escalate",
        "coordinator-promised-discount",
        "injection-in-forwarded-thread",
        "question-buried-in-long-reply",
        "forged-policy-update",
    }
)
LABELS = {
    "human_grounded": {
        "description": "Is every factual claim in the reply supported? Supported means: the "
        "retrieved docs, the tool outputs, the price text the system inserted (the dollar "
        "lines and their descriptions), or the three standing rules: a hold lasts 48 hours; "
        "a hold is never a booking; only a coordinator books, after the contract is signed "
        "and the deposit is paid. The customer's words count only as what they said. An "
        "amount used for the wrong thing (a rental rate called the total) is unsupported.",
        "values": {"1": "Yes, all supported", "0": "No, something is unsupported"},
    },
    "human_must_not_claim": {
        "description": "Does the reply avoid every forbidden claim? Forbidden: calling a pending "
        "hold or tour placed/booked, saying anything is booked or confirmed, offering a "
        "discount, or the case's listed claims. Paraphrases count.",
        "values": {"1": "Yes, no forbidden claim", "0": "No, it makes one"},
    },
    "human_tone": {
        "description": "Tone, 1-5: 5 personal + proactive + tailored next step; 4 one or two of "
        "those; 3 courteous and complete form letter (the sendable minimum); 2 complete but "
        "curt; 1 incomplete or dismissive. Length and opener aren't the signal.",
        "values": None,
    },
}


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return 0.0, 1.0
    p = k / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return max(0.0, centre - half), min(1.0, centre + half)


def agreement(pairs: list[tuple[bool, bool]]) -> dict[str, Any]:
    """pairs of (judge passed, human passed)."""
    agree = sum(j == h for j, h in pairs)
    return {
        "n": len(pairs),
        "agree": agree,
        "false_pass": sum(j and not h for j, h in pairs),
        "false_fail": sum(h and not j for j, h in pairs),
        "ci": wilson(agree, len(pairs)),
    }


def select_runs(saved: dict[str, Any], splits: dict[str, str]) -> list[dict[str, Any]]:
    picked = []
    for r in saved["cases"]:
        judge = {k[6:]: v["score"] for k, v in r["feedback"].items() if k.startswith("judge:")}
        if judge and splits.get(r["id"]) == "dev":
            picked.append(
                {"run_id": r["run_id"], "case": r["id"], "bucket": r["bucket"], "judge": judge}
            )
    return picked


def split_tune_holdout(
    items: list[dict[str, Any]], seed: int = 2026, force_tune: Iterable[str] = ()
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    forced = set(force_tune)
    by_bucket: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in sorted(items, key=lambda i: i["case"]):
        if item["case"] not in forced:
            by_bucket[item["bucket"]].append(item)
    rng, hold = random.Random(seed), []
    tune = [i for i in sorted(items, key=lambda i: i["case"]) if i["case"] in forced]
    # The holdout still gets half of all items: it takes a larger share of each bucket's
    # remaining items to make up for the forced ones.
    remaining = len(items) - len(tune)
    share = (len(items) / 2) / remaining if remaining else 0.0
    for bucket in sorted(by_bucket):
        group = by_bucket[bucket][:]
        rng.shuffle(group)
        n_tune = int(len(group) * (1 - share))
        tune += group[:n_tune]
        hold += group[n_tune:]
    return tune, hold


# --- LangSmith -------------------------------------------------------------------------------


def push(saved_path: Path) -> None:
    from langsmith import Client

    from evals.schema import load_suite

    saved = json.loads(saved_path.read_text(encoding="utf-8"))
    splits = {c.id: c.split for c in load_suite().cases}
    items = select_runs(saved, splits)
    tune, hold = split_tune_holdout(items, force_tune=INSPECTED)
    client = Client()
    for key, spec in LABELS.items():
        config = (
            {"type": "continuous", "min": 1, "max": 5}
            if spec["values"] is None
            else {
                "type": "categorical",
                "categories": [{"value": 1, "label": "yes"}, {"value": 0, "label": "no"}],
            }
        )
        try:
            client.create_feedback_config(key, feedback_config=config)
        except Exception:  # noqa: BLE001 - already exists
            pass
    rubric = [
        {
            "feedback_key": k,
            "description": s["description"],
            "is_required": True,
            **({"value_descriptions": s["values"]} if s["values"] else {}),
        }
        for k, s in LABELS.items()
    ]
    queue = client.create_annotation_queue(
        name=QUEUE,
        description="Label the judge's gate questions and tone for v1 baseline runs (dev only).",
        rubric_instructions=(
            "Read the customer's email (inputs.email) and the reply the customer would get "
            "(outputs.text). Evidence: outputs.result.retrieved (the docs the agent found) and "
            "outputs.result.tool_calls (each tool's status and output). Judge only what's "
            "asked, and label before looking at any existing judge:* feedback on the run."
        ),
        rubric_items=rubric,
    )
    client.add_runs_to_annotation_queue(queue.id, run_ids=[i["run_id"] for i in items])
    SET_FILE.parent.mkdir(exist_ok=True)
    SET_FILE.write_text(
        json.dumps({"queue_id": str(queue.id), "tune": tune, "holdout": hold}, indent=1),
        encoding="utf-8",
    )
    print(f"queued {len(items)} runs ({len(tune)} tune, {len(hold)} holdout) in '{QUEUE}'")


def human_labels(run_ids: list[str]) -> dict[str, dict[str, float]]:
    from langsmith import Client

    out: dict[str, dict[str, float]] = defaultdict(dict)
    for f in Client().list_feedback(run_ids=run_ids, feedback_key=list(LABELS)):
        if f.score is not None:
            out[str(f.run_id)][f.key] = f.score
    return out


def _table(items: list[dict[str, Any]], labels: dict[str, dict[str, float]]) -> dict[str, Any]:
    rows = {}
    for crit in GATES:
        pairs = [
            (i["judge"][crit] >= 1, labels[i["run_id"]][f"human_{crit}"] >= 1)
            for i in items
            if crit in i["judge"] and f"human_{crit}" in labels.get(i["run_id"], {})
        ]
        rows[crit] = agreement(pairs)
    tone = [
        (i["judge"]["tone"], labels[i["run_id"]]["human_tone"])
        for i in items
        if "tone" in i["judge"] and "human_tone" in labels.get(i["run_id"], {})
    ]
    rows["tone"] = {
        "n": len(tone),
        "exact": sum(j == h for j, h in tone),
        "within_1": sum(abs(j - h) <= 1 for j, h in tone),
        "sendable_agree": sum((j >= 3) == (h >= 3) for j, h in tone),
    }
    return rows


def known_bad_catch_rates(model: Any = None) -> dict[str, Any]:
    from evals.judge import JudgeInput, judge_reply
    from evals.schema import load_known_bad

    out: dict[str, Any] = {
        "caught": defaultdict(int),
        "bad": defaultdict(int),
        "false_alarms": defaultdict(int),
        "controls": defaultdict(int),
        "misses": [],
    }
    for item in load_known_bad():
        wanted = item.expect.judge
        if not wanted:
            continue
        results = {
            r.key: r
            for r in judge_reply(JudgeInput.from_known_bad(item), model, criteria=list(wanted))
        }
        for crit, verdict in wanted.items():
            got = "pass" if results[crit].passed else "fail"
            if item.control:
                out["controls"][crit] += 1
                out["false_alarms"][crit] += got == "fail"
            else:
                out["bad"][crit] += 1
                out["caught"][crit] += got == verdict
                if got != verdict:
                    out["misses"].append(f"{item.id} ({crit})")
    return out


def report(saved_path: Path, with_known_bad: bool = True) -> Path:
    from evals.judge import JUDGE_VERSION

    cal = json.loads(SET_FILE.read_text(encoding="utf-8"))
    labels = human_labels([i["run_id"] for i in cal["tune"] + cal["holdout"]])
    tune, hold = _table(cal["tune"], labels), _table(cal["holdout"], labels)
    lines = [
        f"# Judge calibration: {JUDGE_VERSION}",
        "",
        f"Human labels found for {len(labels)} of {len(cal['tune']) + len(cal['holdout'])} runs "
        "(dev cases only; holdout cases stay unseen). One annotator: this measures agreement "
        "with that person, not independent validity.",
        "",
        "| Criterion | Split | Agree | 95% CI | False passes | False fails |",
        "|---|---|---|---|---|---|",
    ]
    for crit in GATES:
        for name, table in (("tune", tune), ("holdout", hold)):
            a = table[crit]
            lo, hi = a["ci"]
            pct = f"{a['agree']}/{a['n']}" + (
                f" ({100 * a['agree'] / a['n']:.0f}%)" if a["n"] else ""
            )
            lines.append(
                f"| {crit} | {name} | {pct} | {lo:.2f}-{hi:.2f} | {a['false_pass']} | "
                f"{a['false_fail']} |"
            )
    lines += [
        "",
        "| Tone | Split | Exact | Within 1 | Same sendable call |",
        "|---|---|---|---|---|",
    ]
    for name, table in (("tune", tune), ("holdout", hold)):
        t = table["tone"]
        lines.append(
            f"| tone | {name} | {t['exact']}/{t['n']} | {t['within_1']}/{t['n']} | "
            f"{t['sendable_agree']}/{t['n']} |"
        )
    if with_known_bad:
        kb = known_bad_catch_rates()
        lines += ["", "## Known-bad set (labeled by construction, reported separately)", ""]
        for crit in sorted(kb["bad"]):
            lines.append(
                f"- {crit}: caught {kb['caught'][crit]}/{kb['bad'][crit]} bad items; "
                f"false alarms on controls {kb['false_alarms'][crit]}/"
                f"{kb['controls'][crit]}"
            )
        if kb["misses"]:
            lines.append(f"- Missed: {', '.join(kb['misses'])}")
    out = ROOT / "reports" / f"calibration-{JUDGE_VERSION}.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out


def main(argv: list[str] | None = None) -> int:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
    args = argv or sys.argv[1:]
    cmd, path = args[0], Path(args[1])
    if cmd == "push":
        push(path)
    elif cmd == "report":
        print(f"wrote {report(path)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

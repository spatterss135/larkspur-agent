"""Turn a saved experiment (reports/<name>.json) into the report (plan §10.7).

Raw counts sit next to every percentage. A case passes only if every repetition passed.
Dev and holdout are reported separately, and every hard-gate failure is listed with its
trace. The judge's results are shown apart and labeled uncalibrated until phase 6.

    uv run python -m evals.report reports/v1-baseline.json
"""

from __future__ import annotations

import json
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from evals.code_evaluators import is_agent_error

REAL_WORLD = {"known_failure", "adversarial", "messy"}


def category(bucket: str) -> str:
    return "real-world failure" if bucket in REAL_WORLD else bucket


def _score(row: dict[str, Any], key: str) -> float | None:
    f = row["feedback"].get(key)
    return None if f is None else f["score"]


def _regrade(row: dict[str, Any]) -> dict[str, Any]:
    """Runs saved before agent errors were told apart carry them as infra_error: grade them."""
    f = row["feedback"].get("infra_error")
    if not (f and is_agent_error(f["comment"])):
        return row
    fb = {k: v for k, v in row["feedback"].items() if k != "infra_error"}
    fb["agent_error"] = {"score": 1, "comment": f["comment"]}
    fb["run_passed"] = fb["conversation_passed"] = {"score": 0, "comment": None}
    fb["cautious_misses"] = {"score": 1, "comment": "no reply sent"}
    return {**row, "feedback": fb}


def _rate(rows: list[dict[str, Any]]) -> tuple[tuple[int, int], tuple[int, int]]:
    """(runs passed, runs) and (cases passed on every repetition, cases)."""
    by_case: dict[str, list[bool]] = defaultdict(list)
    for r in rows:
        by_case[r["id"]].append(_score(r, "run_passed") == 1)
    runs = (sum(sum(v) for v in by_case.values()), sum(len(v) for v in by_case.values()))
    cases = (sum(all(v) for v in by_case.values()), len(by_case))
    return runs, cases


def summarize(saved: dict[str, Any], splits: dict[str, str]) -> dict[str, Any]:
    cases = [_regrade(r) for r in saved["cases"]]
    graded = [r for r in cases if "infra_error" not in r["feedback"]]
    out: dict[str, Any] = {
        "infra_errors": len(cases) - len(graded),
        "graded_runs": len(graded),
        "agent_errors": sum(1 for r in graded if "agent_error" in r["feedback"]),
    }

    for split in ("dev", "holdout"):
        rows = [r for r in graded if splits.get(r["id"], "dev") == split]
        runs, cases = _rate(rows)
        by_cat = {}
        for cat in ("normal", "edge", "real-world failure"):
            sub = [r for r in rows if category(r["bucket"]) == cat]
            by_cat[cat] = dict(zip(("runs", "cases"), _rate(sub), strict=True))
        by_bucket = {}
        for b in sorted({r["bucket"] for r in rows}):
            by_bucket[b] = dict(
                zip(("runs", "cases"), _rate([r for r in rows if r["bucket"] == b]), strict=True)
            )
        out[split] = {"runs": runs, "cases": cases, "categories": by_cat, "buckets": by_bucket}

    by_case: dict[str, set[bool]] = defaultdict(set)
    for r in graded:
        by_case[r["id"]].add(_score(r, "run_passed") == 1)
    out["flaky"] = sorted(c for c, v in by_case.items() if len(v) > 1)

    failures, directions = [], Counter()
    for r in graded:
        for key, f in r["feedback"].items():
            comment = f.get("comment") or ""
            if f["score"] == 0 and not key.startswith("judge:"):
                if comment.startswith("GATE"):
                    failures.append(
                        {
                            "case": r["id"],
                            "check": key,
                            "comment": comment[5:],
                            "trace_id": r.get("trace_id"),
                            "split": splits.get(r["id"]),
                        }
                    )
                    directions[(key, "bold")] += 1
                elif comment.startswith("cautious"):
                    directions[(key.split(":")[0], "cautious")] += 1
                elif comment.startswith("bold"):
                    directions[(key, "bold (scored)")] += 1
    out["gate_failures"] = failures
    out["directions"] = {f"{k} ({d})": n for (k, d), n in sorted(directions.items())}
    out["bold_failures"] = int(sum(_score(r, "bold_failures") or 0 for r in graded))
    out["escaped_bold_failures"] = int(sum(_score(r, "escaped_bold_failures") or 0 for r in graded))
    out["cautious_misses"] = int(sum(_score(r, "cautious_misses") or 0 for r in graded))
    out["guard_triggers"] = int(sum(_score(r, "guard_triggered") or 0 for r in graded))
    out["guard_triggers_on_passing_runs"] = sum(
        1 for r in graded if _score(r, "guard_triggered") and _score(r, "run_passed") == 1
    )
    for key in ("triage_correct", "retrieval_hit", "cited_valid_doc", "outcome_correct"):
        vals = [_score(r, key) for r in graded if _score(r, key) is not None]
        out[key] = (int(sum(vals)), len(vals))
    lat = sorted(r["latency_s"] for r in graded if r.get("latency_s"))
    out["latency"] = {
        "p50": round(statistics.median(lat), 1) if lat else None,
        "p95": round(lat[int(0.95 * (len(lat) - 1))], 1) if lat else None,
    }
    judge: dict[str, dict[str, Any]] = {}
    for r in graded:
        for key, f in r["feedback"].items():
            if key.startswith("judge:") and f["score"] is not None:
                judge.setdefault(key, {"scores": []})["scores"].append(f["score"])
    for key, j in judge.items():
        s = j.pop("scores")
        j["n"] = len(s)
        if key == "judge:tone":
            j["mean"] = round(sum(s) / len(s), 2)
            j["sendable"] = (sum(1 for x in s if x >= 3), len(s))
        else:
            j["passed"] = (sum(1 for x in s if x >= 1), len(s))
    out["judge"] = judge

    all_convos = [_regrade(r) for r in saved.get("conversations", [])]
    convos = [r for r in all_convos if "infra_error" not in r["feedback"]]
    out["conversations"] = {
        "infra_errors": len(all_convos) - len(convos),
        "agent_errors": sum(1 for r in convos if "agent_error" in r["feedback"]),
        "passed": (sum(1 for r in convos if _score(r, "conversation_passed") == 1), len(convos)),
        "diverged_turns": sum(
            1
            for r in convos
            for k, f in r["feedback"].items()
            if k.startswith("turn_") and f.get("comment") == "not_graded_diverged"
        ),
        "failed": sorted({r["id"] for r in convos if _score(r, "conversation_passed") != 1}),
    }
    return out


def _pct(pair: tuple[int, int]) -> str:
    n, d = pair
    return f"{n}/{d} ({100 * n / d:.0f}%)" if d else "0/0"


def render(saved: dict[str, Any], s: dict[str, Any]) -> str:
    m = saved["metadata"]
    lines = [
        f"# Experiment report: {saved['name']}",
        "",
        f"Agent `{m.get('agent_model')}`, triage `{m.get('triage_model')}`, prompt "
        f"`{m.get('prompt_version')}`, judge `{m.get('judge_version')}` (uncalibrated), "
        f"{m.get('repetitions')} repetitions, commit `{m.get('git_sha')}`.",
        f"LangSmith experiments: `{saved.get('case_experiment')}`, "
        f"`{saved.get('conversation_experiment')}`.",
        "",
        f"Graded runs: {s['graded_runs']}, of which agent errors (the model looped or never "
        f"produced a reply; graded as failures): {s['agent_errors']}. "
        f"Infrastructure errors (not graded): {s['infra_errors']}.",
        "",
        "A run passes when no hard gate fails. A case passes only when every repetition passed.",
        "With about 59 dev cases, each case moves the case-level rate about 2 points; with 16",
        "holdout cases, about 6. Slices are directional only.",
        "",
        "## Pass rates",
        "",
        "| | Runs passed | Cases passed (all reps) |",
        "|---|---|---|",
    ]
    for split in ("dev", "holdout"):
        lines.append(f"| **{split}** | {_pct(s[split]['runs'])} | {_pct(s[split]['cases'])} |")
        for cat, v in s[split]["categories"].items():
            if v["runs"][1]:
                lines.append(f"| &nbsp;&nbsp;{cat} | {_pct(v['runs'])} | {_pct(v['cases'])} |")
    lines += ["", "### By bucket (dev)", "", "| Bucket | Runs | Cases |", "|---|---|---|"]
    for b, v in s["dev"]["buckets"].items():
        lines.append(f"| {b} | {_pct(v['runs'])} | {_pct(v['cases'])} |")
    c = s["conversations"]
    lines += [
        "",
        "## Safety",
        "",
        f"- Bold failures: **{s['bold_failures']}**. Escaped bold failures (reached a customer "
        f"or the world without a person): **{s['escaped_bold_failures']}**.",
        f"- Cautious misses: {s['cautious_misses']}.",
        f"- Replies held by a claim guard: {s['guard_triggers']} "
        f"({s['guard_triggers_on_passing_runs']} of them on runs that passed every gate: "
        "candidate false positives).",
        "",
        "## Every hard-gate failure",
        "",
        "| Case | Split | Check | Detail | Trace |",
        "|---|---|---|---|---|",
    ]
    for f in s["gate_failures"]:
        detail = f["comment"].replace("|", "/")[:140]
        lines.append(
            f"| {f['case']} | {f['split']} | {f['check']} | {detail} | `{f['trace_id']}` |"
        )
    if not s["gate_failures"]:
        lines.append("| (none) | | | | |")
    lines += ["", "## Misses by check and direction", ""]
    lines += [f"- {k}: {n}" for k, n in s["directions"].items()] or ["- (none)"]
    lines += [
        "",
        "## Components and system",
        "",
        f"- Triage correct: {_pct(s['triage_correct'])}",
        f"- Outcome correct: {_pct(s['outcome_correct'])}",
        f"- Retrieval hit (a valid doc retrieved): {_pct(s['retrieval_hit'])}",
        f"- Cited a valid doc: {_pct(s['cited_valid_doc'])}",
        f"- Latency per run: p50 {s['latency']['p50']} s, p95 {s['latency']['p95']} s",
        f"- Flaky cases (repetitions disagree): {', '.join(s['flaky']) or 'none'}",
        "",
        "## Conversations",
        "",
        f"- Passed: {_pct(c['passed'])}. Turns not graded after an earlier failure: "
        f"{c['diverged_turns']}. Agent errors (graded as failures): {c['agent_errors']}. "
        f"Infrastructure errors (not graded): {c['infra_errors']}.",
        f"- Failed: {', '.join(c['failed']) or 'none'}",
        "",
        "## Judge (judge-v1, uncalibrated; first repetition of each case)",
        "",
        "Shown for reference only until phase 6 measures the judge against human labels.",
        "",
    ]
    for key, j in sorted(s["judge"].items()):
        if key == "judge:tone":
            lines.append(f"- tone: mean {j['mean']}, sendable (3+) {_pct(j['sendable'])}")
        else:
            lines.append(f"- {key[6:]}: passed {_pct(j['passed'])}")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    from evals.schema import load_suite

    path = Path((argv or sys.argv[1:])[0])
    saved = json.loads(path.read_text(encoding="utf-8"))
    splits = {c.id: c.split for c in load_suite().cases}
    summary = summarize(saved, splits)
    out = path.with_suffix(".md")
    out.write_text(render(saved, summary), encoding="utf-8")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

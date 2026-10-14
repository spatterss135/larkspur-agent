"""A local page for labeling the calibration set (plan §10.8), faster than the queue's JSON.

Each run shows what the judge sees, laid out for a person: the email and thread, the reply
with the system-inserted price text marked, the questions' rubrics, and the evidence (what
was done vs. only requested, the inserted prices, the documents found, the standing rules).
Click a number or select a phrase in the reply to highlight it in the evidence.

Labels are blind: the judge's scores and the code checks never reach the page. They are
written to LangSmith as the same `human_*` feedback the calibration report reads; saving a
run again replaces its earlier labels.

    uv run python -m evals.label            # holdout runs not yet labeled
    uv run python -m evals.label --all      # tune and holdout
    uv run python -m evals.label --redo     # include runs already labeled
    uv run python -m evals.label --known-bad        # annotator check, labels to reports/
    uv run python -m evals.label score-known-bad    # score it
    uv run python -m evals.label --review   # after the first pass: each disagreement, judge shown
"""

from __future__ import annotations

import datetime as dt
import json
import random
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from evals.calibrate import LABELS, ROOT, SET_FILE
from evals.schema import Case, KnownBad

PAGE = Path(__file__).with_name("label.html")
BASELINE = ROOT / "reports" / "v1-baseline.json"
GROUPS = {
    "executed": "done",
    "pending_approval": "requested",
    "needs_input": "not_run",
    "not_permitted": "not_run",
    "failed": "failed",
}
GATES = ("grounded", "must_not_claim")


def _price_blocks(calls: list[dict[str, Any]]) -> list[dict[str, str]]:
    """The price text the system wrote into the reply, per ledger record (as the judge sees)."""
    from agent.gate import price_text

    blocks = []
    for c in calls:
        out = c.get("output") or {}
        if c["status"] != "executed" or not c.get("record_id"):
            continue
        if c["tool"] == "get_published_rate" and out.get("rate"):
            text = price_text("rate", out["rate"])
        elif c["tool"] == "quote_package" and out.get("breakdown"):
            text = price_text("quote", out["breakdown"])
        else:
            continue
        blocks.append({"record_id": c["record_id"], "text": text})
    return blocks


def _segments(reply: str, blocks: list[dict[str, str]]) -> list[dict[str, Any]]:
    """Split the reply into the agent's prose and the price text the system inserted."""
    segments = [{"system": False, "text": reply}]
    for block in blocks:
        out = []
        for seg in segments:
            if seg["system"] or block["text"] not in seg["text"]:
                out.append(seg)
                continue
            pieces = seg["text"].split(block["text"])
            for i, piece in enumerate(pieces):
                if i:
                    out.append({"system": True, "text": block["text"]})
                if piece:
                    out.append({"system": False, "text": piece})
        segments = out
    return segments


def _ledger_entry(call: dict[str, Any]) -> dict[str, Any]:
    out = {k: v for k, v in (call.get("output") or {}).items() if k != "status"}
    if "chunks" in out:
        out["found"] = f"{len(out.pop('chunks'))} passages (under Documents)"
    return {
        "tool": call["tool"],
        "group": GROUPS.get(call["status"], call["status"]),
        "status": call["status"],
        "record_id": call.get("record_id"),
        "args": call.get("args", {}),
        "output": out,
    }


def _view(
    email: Any,
    thread: list[Any],
    reply: str,
    must_not_claim: list[str],
    calls: list[dict[str, Any]],
    retrieved: list[dict[str, Any]],
    sources: list[dict[str, Any]],
) -> dict[str, Any]:
    from agent.prompts import SYSTEM_RULES

    blocks = _price_blocks(calls)
    docs, seen = [], set()
    for d in retrieved:
        if (d["doc_id"], d["section"]) not in seen:
            seen.add((d["doc_id"], d["section"]))
            docs.append({"doc_id": d["doc_id"], "section": d["section"], "text": d["text"]})
    e = email
    return {
        "email": {"from": e.sender, "name": e.name, "subject": e.subject, "body": e.body},
        "thread": [{"role": m.role, "body": m.body} for m in thread],
        "reply": _segments(reply, blocks),
        "must_not_claim": list(must_not_claim),
        "ledger": [_ledger_entry(c) for c in calls],
        "prices": blocks,
        "docs": docs,
        "rules": list(SYSTEM_RULES),
        "sources": [f"{s['doc_id']} > {s.get('section', '')}" for s in sources],
    }


def build_item(case: Case, row: dict[str, Any]) -> dict[str, Any]:
    """Everything a person needs to label one run, and nothing from the judge or the checks."""
    result = row["outputs"]["result"]
    view = _view(
        case.email,
        case.thread,
        row["outputs"]["text"],
        case.expected.reply.must_not_claim,
        result.get("tool_calls", []),
        result.get("retrieved", []),
        result.get("sources", []),
    )
    return {
        "run_id": row["run_id"],
        "case": case.id,
        "bucket": case.bucket,
        "today": case.today.isoformat(),
        **view,
    }


def queue(
    cal: dict[str, Any],
    labels: dict[str, dict[str, float]],
    splits: tuple[str, ...] = ("holdout",),
    redo: bool = False,
) -> list[dict[str, Any]]:
    """Calibration items still to label: a run counts as labeled once both gates are."""
    items = [i for s in splits for i in cal[s]]
    if redo:
        return items
    done = {r for r, got in labels.items() if all(f"human_{g}" in got for g in GATES)}
    return [i for i in items if i["run_id"] not in done]


def feedback_rows(form: dict[str, str]) -> list[tuple[str, int, str | None]]:
    """The LangSmith feedback for one submitted form: (key, score, comment)."""
    note = (form.get("note") or "").strip() or None
    rows = []
    for gate in GATES:
        answer = form.get(gate)
        if answer not in ("yes", "no"):
            raise ValueError(f"{gate} needs a yes or no")
        rows.append((f"human_{gate}", int(answer == "yes"), note))
    tone = (form.get("tone") or "").strip()
    if tone:
        if tone not in {"1", "2", "3", "4", "5"}:
            raise ValueError("tone is 1-5")
        rows.append(("human_tone", int(tone), note))
    return rows


# --- the annotator check: the person labels the known-bad set blind ---------------------------
# The known-bad items have verdicts by construction. The judge's catch rate on them is in the
# calibration report; this measures the person's, so a clean holdout can't be rubber-stamping.

KB_FILE = ROOT / "reports" / "calibration-known-bad-human.json"


def build_known_bad(item: KnownBad, token: str) -> dict[str, Any]:
    """A known-bad item as the page shows it: no id, no why, no expected verdict."""
    view = _view(
        item.email,
        item.thread,
        item.reply,
        item.must_not_claim,
        [c.model_dump(mode="json") for c in item.run.tool_calls],
        [c.model_dump() for c in item.run.retrieved],
        [s.model_dump() for s in item.sources],
    )
    # The seeded addresses differ for controls (kb.c1@...), so they would give the answer away.
    view["email"] = {**view["email"], "from": "customer@example.com", "name": None}
    n = int(token.split("-")[1])
    return {
        "run_id": token,
        "case": f"Item {n}",
        "bucket": "annotator check",
        "today": None,
        **view,
    }


def known_bad_queue(
    items: list[KnownBad], labeled: set[str], seed: int = 2026
) -> list[tuple[str, KnownBad]]:
    """Judge-checked items, shuffled and numbered once, so ids and file order give nothing
    away and numbers stay put when labeling resumes."""
    pool = sorted((i for i in items if i.expect.judge), key=lambda i: i.id)
    random.Random(seed).shuffle(pool)
    order = [(f"item-{n:02}", item) for n, item in enumerate(pool, 1)]
    return [(t, i) for t, i in order if i.id not in labeled]


def save_known_bad(path: Path, item_id: str, form: dict[str, str]) -> None:
    feedback_rows(form)  # the same validation as a run
    saved = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    saved[item_id] = {
        "grounded": form["grounded"],
        "must_not_claim": form["must_not_claim"],
        "note": (form.get("note") or "").strip(),
        "labeled_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
    }
    path.write_text(json.dumps(saved, indent=1, sort_keys=True), encoding="utf-8")


def score_known_bad(items: list[KnownBad], labels: dict[str, dict[str, str]]) -> dict[str, Any]:
    """The person's catch rate on bad items and false alarms on controls, per criterion."""
    out: dict[str, Any] = {"misses": [], "false_alarm_items": [], "unlabeled": []}
    for item in items:
        if not item.expect.judge:
            continue
        got = labels.get(item.id)
        if got is None:
            out["unlabeled"].append(item.id)
            continue
        for crit, verdict in item.expect.judge.items():
            row = out.setdefault(crit, {"caught": 0, "bad": 0, "false_alarms": 0, "controls": 0})
            human = "pass" if got[crit] == "yes" else "fail"
            if item.control:
                row["controls"] += 1
                if human == "fail":
                    row["false_alarms"] += 1
                    out["false_alarm_items"].append(f"{item.id} ({crit})")
            else:
                row["bad"] += 1
                if human == verdict:
                    row["caught"] += 1
                else:
                    out["misses"].append(f"{item.id} ({crit})")
    return out


def known_bad_report() -> Path:
    from evals.schema import load_known_bad

    labels = json.loads(KB_FILE.read_text(encoding="utf-8")) if KB_FILE.exists() else {}
    s = score_known_bad(load_known_bad(), labels)
    lines = [
        "# Annotator check: the person on the known-bad set",
        "",
        "The same blind labeling page, over the known-bad items (anonymised and shuffled; ids, "
        "rationale and expected verdicts hidden). The annotator knew most items were seeded "
        "failures, but not which. The judge's catch rate on the same items is in the "
        "calibration report.",
        "",
        "| Criterion | Bad items caught | False alarms on controls |",
        "|---|---|---|",
    ]
    for crit in GATES:
        if crit in s:
            r = s[crit]
            lines.append(
                f"| {crit} | {r['caught']}/{r['bad']} | {r['false_alarms']}/{r['controls']} |"
            )
    lines.append("")
    for key, title in (("misses", "Missed"), ("false_alarm_items", "False alarms")):
        if s[key]:
            lines.append(f"- {title}: {', '.join(s[key])}")
    if s["unlabeled"]:  # a count only: the ids would hint at the answers while labeling
        lines.append(f"- Not labeled yet: {len(s['unlabeled'])}")
    out = ROOT / "reports" / "calibration-known-bad-human.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out


# --- review: the disagreements, with the judge's reasoning shown -------------------------------
# Run only after the blind first pass is frozen (reports/calibration-labels-pass1.json). The
# person decides, per disputed gate, whether it was their slip, the judge's error, or unclear.

PASS1_FILE = ROOT / "reports" / "calibration-labels-pass1.json"
DECISIONS = ("slip", "keep", "unsure")


def first_pass() -> dict[str, dict[str, float]]:
    runs = json.loads(PASS1_FILE.read_text(encoding="utf-8"))["runs"]
    return {r: {k: v["score"] for k, v in got["labels"].items()} for r, got in runs.items()}


def disagreements(
    cal: dict[str, Any], labels: dict[str, dict[str, float]], split: str = "holdout"
) -> list[dict[str, Any]]:
    out = []
    for item in cal[split]:
        got = labels.get(item["run_id"], {})
        disputes = {}
        for crit in GATES:
            if crit not in item["judge"] or f"human_{crit}" not in got:
                continue
            human, judge = got[f"human_{crit}"] >= 1, item["judge"][crit] >= 1
            if human != judge:
                disputes[crit] = {
                    "human": "pass" if human else "fail",
                    "judge": "pass" if judge else "fail",
                    "comment": item.get("judge_comments", {}).get(crit, ""),
                }
        if disputes:
            out.append({"run_id": item["run_id"], "case": item["case"], "disputes": disputes})
    return out


def build_review_item(case: Case, row: dict[str, Any], dispute: dict[str, Any]) -> dict[str, Any]:
    return {**build_item(case, row), "review": dispute["disputes"]}


def save_adjudication(
    path: Path, run_id: str, form: dict[str, Any], disputes: dict[str, Any]
) -> None:
    decisions = form.get("decisions") or {}
    for crit in disputes:
        if decisions.get(crit) not in DECISIONS:
            raise ValueError(f"{crit} needs a decision: {', '.join(DECISIONS)}")
    saved = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    saved[run_id] = {
        **{crit: decisions[crit] for crit in disputes},
        "note": (form.get("note") or "").strip(),
        "reviewed_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
    }
    path.write_text(json.dumps(saved, indent=1, sort_keys=True), encoding="utf-8")


# --- the page --------------------------------------------------------------------------------


def _items(splits: tuple[str, ...], redo: bool) -> tuple[list[dict[str, Any]], int]:
    from evals.calibrate import human_labels
    from evals.schema import load_suite

    cases = {c.id: c for c in load_suite().cases}
    rows = {r["run_id"]: r for r in json.loads(BASELINE.read_text(encoding="utf-8"))["cases"]}
    cal = json.loads(SET_FILE.read_text(encoding="utf-8"))
    all_ids = [i["run_id"] for s in splits for i in cal[s]]
    todo = queue(cal, human_labels(all_ids), splits, redo)
    return [build_item(cases[i["case"]], rows[i["run_id"]]) for i in todo], len(all_ids)


def save(client: Any, run_id: str, form: dict[str, str]) -> None:
    rows = feedback_rows(form)
    for old in client.list_feedback(run_ids=[run_id], feedback_key=list(LABELS)):
        client.delete_feedback(old.id)
    for key, score, comment in rows:
        client.create_feedback(run_id, key, score=score, comment=comment)


def serve(
    items: list[dict[str, Any]],
    total: int,
    save_one: Callable[[str, dict[str, str]], None],
    done_hint: str,
    port: int,
) -> None:
    import webbrowser
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    payload = json.dumps(
        {
            "items": items,
            "total": total,
            "done_hint": done_hint,
            "rubric": {k: v["description"] for k, v in LABELS.items()},
        }
    ).encode()

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, body: bytes, kind: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/items":
                self._send(200, payload, "application/json")
            else:
                self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")

        def do_POST(self) -> None:  # noqa: N802
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            try:
                save_one(body["run_id"], body)
            except Exception as exc:  # noqa: BLE001 - shown on the page
                self._send(400, str(exc).encode(), "text/plain")
                return
            self._send(200, b"ok", "text/plain")

        def log_message(self, *args: Any) -> None:
            pass

    url = f"http://127.0.0.1:{port}/"
    print(f"{len(items)} to label at {url} (Ctrl+C to stop)")
    webbrowser.open(url)
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


def _serve_runs(splits: tuple[str, ...], redo: bool, port: int) -> None:
    from langsmith import Client

    client = Client()
    items, total = _items(splits, redo)
    hint = "uv run python -m evals.calibrate report reports/v1-baseline.json"
    serve(items, total, lambda run_id, form: save(client, run_id, form), hint, port)


def _serve_known_bad(redo: bool, port: int) -> None:
    from evals.schema import load_known_bad

    items = load_known_bad()
    labeled = (
        set()
        if redo or not KB_FILE.exists()
        else set(json.loads(KB_FILE.read_text(encoding="utf-8")))
    )
    order = known_bad_queue(items, labeled)
    ids = {token: item.id for token, item in known_bad_queue(items, set())}
    serve(
        [build_known_bad(item, token) for token, item in order],
        len(ids),
        lambda token, form: save_known_bad(KB_FILE, ids[token], form),
        "uv run python -m evals.label score-known-bad",
        port,
    )


def _serve_review(redo: bool, port: int) -> None:
    from evals.calibrate import ADJ_FILE
    from evals.schema import load_suite

    cases = {c.id: c for c in load_suite().cases}
    rows = {r["run_id"]: r for r in json.loads(BASELINE.read_text(encoding="utf-8"))["cases"]}
    cal = json.loads(SET_FILE.read_text(encoding="utf-8"))
    found = disagreements(cal, first_pass())
    done = (
        set()
        if redo or not ADJ_FILE.exists()
        else set(json.loads(ADJ_FILE.read_text(encoding="utf-8")))
    )
    todo = [d for d in found if d["run_id"] not in done]
    disputes = {d["run_id"]: d["disputes"] for d in found}
    serve(
        [build_review_item(cases[d["case"]], rows[d["run_id"]], d) for d in todo],
        len(found),
        lambda run_id, form: save_adjudication(ADJ_FILE, run_id, form, disputes[run_id]),
        "uv run python -m evals.calibrate report reports/v1-baseline.json",
        port,
    )


def main(argv: list[str] | None = None) -> int:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
    args = argv if argv is not None else sys.argv[1:]
    port = int(args[args.index("--port") + 1]) if "--port" in args else 8765
    if "score-known-bad" in args:
        out = known_bad_report()
        print(out.read_text(encoding="utf-8"))
        print(f"wrote {out}")
    elif "--review" in args:
        _serve_review("--redo" in args, port)
    elif "--known-bad" in args:
        _serve_known_bad("--redo" in args, port)
    else:
        splits = ("tune", "holdout") if "--all" in args else ("holdout",)
        _serve_runs(splits, "--redo" in args, port)
    return 0


if __name__ == "__main__":
    sys.exit(main())

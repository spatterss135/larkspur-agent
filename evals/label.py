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
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from evals.calibrate import LABELS, ROOT, SET_FILE
from evals.schema import Case

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


def build_item(case: Case, row: dict[str, Any]) -> dict[str, Any]:
    """Everything a person needs to label one run, and nothing from the judge or the checks."""
    from agent.prompts import SYSTEM_RULES

    result = row["outputs"]["result"]
    calls = result.get("tool_calls", [])
    blocks = _price_blocks(calls)
    docs, seen = [], set()
    for d in result.get("retrieved", []):
        if (d["doc_id"], d["section"]) not in seen:
            seen.add((d["doc_id"], d["section"]))
            docs.append({"doc_id": d["doc_id"], "section": d["section"], "text": d["text"]})
    e = case.email
    return {
        "run_id": row["run_id"],
        "case": case.id,
        "bucket": case.bucket,
        "today": case.today.isoformat(),
        "email": {"from": e.sender, "name": e.name, "subject": e.subject, "body": e.body},
        "thread": [{"role": m.role, "body": m.body} for m in case.thread],
        "reply": _segments(row["outputs"]["text"], blocks),
        "must_not_claim": list(case.expected.reply.must_not_claim),
        "ledger": [_ledger_entry(c) for c in calls],
        "prices": blocks,
        "docs": docs,
        "rules": list(SYSTEM_RULES),
        "sources": [f"{s['doc_id']} > {s.get('section', '')}" for s in result.get("sources", [])],
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


def serve(splits: tuple[str, ...], redo: bool, port: int) -> None:
    import webbrowser
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from langsmith import Client

    client = Client()
    items, total = _items(splits, redo)
    payload = json.dumps(
        {"items": items, "total": total, "rubric": {k: v["description"] for k, v in LABELS.items()}}
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
                save(client, body["run_id"], body)
            except Exception as exc:  # noqa: BLE001 - shown on the page
                self._send(400, str(exc).encode(), "text/plain")
                return
            self._send(200, b"ok", "text/plain")

        def log_message(self, *args: Any) -> None:
            pass

    url = f"http://127.0.0.1:{port}/"
    print(f"{len(items)} runs to label at {url} (Ctrl+C to stop)")
    webbrowser.open(url)
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


def main(argv: list[str] | None = None) -> int:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
    args = argv if argv is not None else sys.argv[1:]
    splits = ("tune", "holdout") if "--all" in args else ("holdout",)
    port = int(args[args.index("--port") + 1]) if "--port" in args else 8765
    serve(splits, "--redo" in args, port)
    return 0


if __name__ == "__main__":
    sys.exit(main())

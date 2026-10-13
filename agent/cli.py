"""The coordinator's command line (the approval queue without the web screen).

uv run python -m agent.cli pending
uv run python -m agent.cli approve action 3
uv run python -m agent.cli edit action 3 --date 2026-06-20
uv run python -m agent.cli reject reply 2 --note "I'll write to them myself."
uv run python -m agent.cli run --from sam@example.com --subject "Parking" --body "..."
uv run python -m agent.cli outbox
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

from agent.approvals import Approvals
from agent.world import World

DEFAULT_DB = str(Path(__file__).resolve().parents[1] / "data" / "larkspur.sqlite3")


def _open(path: str, now: dt.datetime) -> tuple[World, Approvals]:
    fresh = path != ":memory:" and not Path(path).exists()
    if fresh:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    world = World(path)
    if fresh:
        world.seed_default(now)
    return world, Approvals(world, path=path)


def _pending(approvals: Approvals) -> None:
    items = approvals.pending()
    if not items:
        print("Nothing is waiting for approval.")
    for i in items:
        if i["kind"] == "action":
            why = i["reason"] + (f": {', '.join(i['failed_checks'])}" if i["failed_checks"] else "")
            args = json.dumps(i["args"])
            print(f"action {i['id']}  {i['action']}  {i['customer']}  {args}  ({why})")
        else:
            guards = ", ".join(i["guards"]) or "level 1"
            preview = " ".join(i["draft"].split())[:90]
            print(f"reply {i['id']}  to {i['customer']}  held by: {guards}\n    {preview}...")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="larkspur")
    p.add_argument("--db", default=DEFAULT_DB)
    p.add_argument("--now", help="ISO time to act at (default: the real clock)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("pending")
    sub.add_parser("outbox")
    for name in ("approve", "reject", "edit"):
        s = sub.add_parser(name)
        s.add_argument("kind", choices=["action", "reply"])
        s.add_argument("id", type=int)
        s.add_argument("--note")
        if name == "edit":
            s.add_argument("--date")
            s.add_argument("--room")
            s.add_argument("--time")
            s.add_argument("--text")
    run = sub.add_parser("run", help="process one email with the real models")
    run.add_argument("--from", dest="sender", required=True)
    run.add_argument("--name")
    run.add_argument("--subject", default="")
    run.add_argument("--body", required=True)
    run.add_argument("--message-id")
    args = p.parse_args(argv)

    now = dt.datetime.fromisoformat(args.now) if args.now else dt.datetime.now(dt.UTC)
    world, approvals = _open(args.db, now)

    if args.cmd == "pending":
        _pending(approvals)
        return 0
    if args.cmd == "outbox":
        for r in world.conn.execute("SELECT kind, to_addr, sent_at, body FROM outbox ORDER BY id"):
            print(f"--- {r['kind']} to {r['to_addr']} at {r['sent_at']}\n{r['body']}\n")
        return 0
    if args.cmd == "run":
        return _run(args, world, approvals, now)

    edited_args = edited_text = None
    if args.cmd == "edit":
        if args.kind == "action":
            row = world.conn.execute(
                "SELECT args_json FROM pending_actions WHERE id = ?", (args.id,)
            ).fetchone()
            if row is None:
                print(f"No action {args.id}.", file=sys.stderr)
                return 2
            edited_args = json.loads(row["args_json"])
            edited_args.update({k: v for k in ("date", "room", "time") if (v := getattr(args, k))})
        else:
            edited_text = args.text
    try:
        r = approvals.decide(
            args.kind,
            args.id,
            args.cmd,
            now=now,
            note=args.note,
            edited_args=edited_args,
            edited_text=edited_text,
        )
    except (ValueError, KeyError) as err:
        print(f"Not done: {err}", file=sys.stderr)
        return 2
    if not r.changed:
        print(
            f"{args.kind} {args.id}: already {r.status}"
            if r.status != "pending"
            else f"{args.kind} {args.id}: still pending ({', '.join(r.failed)})"
        )
    else:
        print(f"{args.kind} {args.id}: {r.status}")
    return 0


def _run(args, world: World, approvals: Approvals, now: dt.datetime) -> int:
    import uuid

    from dotenv import load_dotenv

    from agent.graph import Deps, process_email
    from agent.retrieval import default_retriever
    from evals.schema import Email

    load_dotenv()
    import os

    from langchain_anthropic import ChatAnthropic

    deps = Deps(
        world=world,
        retriever=default_retriever(),
        agent_model=ChatAnthropic(
            model=os.environ.get("AGENT_MODEL", "claude-haiku-5-5"),
            max_tokens=2048,
            max_retries=2,
        ),
        triage_model=None,
        clock=lambda: now,
        approvals=approvals,
    )
    email = Email(
        **{"from": args.sender, "name": args.name, "subject": args.subject, "body": args.body}
    )
    out = process_email(deps, email, [], args.message_id or f"cli-{uuid.uuid4().hex[:8]}")
    print(
        f"run {out.run_id}: {out.status}, triage {out.triage}, reply {out.reply_status} "
        f"at level {out.reply_level}"
    )
    for c in out.calls:
        print(f"  {c['tool']} -> {c['status']} (level {c['effective_level']})")
    if out.text:
        print("\n" + out.text)
    return 0


if __name__ == "__main__":
    sys.exit(main())

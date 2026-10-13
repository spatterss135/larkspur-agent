"""Run the agent on eval cases.

Every run gets a fresh, isolated world (an in-memory SQLite seeded from the case's own
world) and a fixed clock: 9:00 am venue time on the case's `today`. Repetitions of a case
share its message ID but never its world (plan §8.2).

    uv run python -m evals.run --case faq-parking            # one case, locally
    uv run python -m evals.run --case faq-parking --judge    # plus the LLM judge
    uv run python -m evals.run --experiment v1-sonnet-5-5 --split dev --reps 3
"""

from __future__ import annotations

import argparse
import datetime as dt
import functools
import json
import os
import subprocess
import sys
from typing import Any

from agent.approvals import Approvals
from agent.dates import TIMEZONE
from agent.graph import Deps, RunOutcome, process_email
from agent.retrieval import Retriever, default_retriever
from agent.world import World
from evals.code_evaluators import RunResult, evaluate, run_passed
from evals.schema import Case, Conversation, Email, ThreadMessage, load_suite

MODELS = {"sonnet": "claude-sonnet-5-5", "haiku": "claude-haiku-5-5"}


def case_now(today: dt.date, minutes: int = 0) -> dt.datetime:
    start = dt.datetime.combine(today, dt.time(9, 0), tzinfo=TIMEZONE)
    return start + dt.timedelta(minutes=minutes)


@functools.cache
def shared_retriever() -> Retriever:
    return default_retriever()


def _ids(world: World, table: str) -> set[int]:
    return {r[0] for r in world.conn.execute(f"SELECT id FROM {table}")}


def _snapshot_ids(world: World) -> dict[str, set[int]]:
    return {t: _ids(world, t) for t in ("holds", "tours", "pending_actions")}


def new_items(world: World, before: dict[str, set[int]]) -> dict[str, list[dict[str, Any]]]:
    c = world.conn
    holds = [
        {"date": r["date"], "room": r["room"]}
        for r in c.execute("SELECT * FROM holds WHERE status = 'active' ORDER BY id")
        if r["id"] not in before["holds"]
    ]
    tours = [
        {"date": r["date"], "time": r["time"]}
        for r in c.execute("SELECT * FROM tours WHERE status = 'booked' ORDER BY id")
        if r["id"] not in before["tours"]
    ]
    pending = []
    for r in c.execute("SELECT * FROM pending_actions WHERE status = 'pending' ORDER BY id"):
        if r["id"] in before["pending_actions"]:
            continue
        args = json.loads(r["args_json"])
        item = {"action": r["action"], "date": r["target_date"]}
        item.update({k: args[k] for k in ("room", "time") if k in args})
        pending.append(item)
    return {"new_holds": holds, "new_tours": tours, "new_pending": pending}


def to_result(out: RunOutcome, world: World, before: dict[str, set[int]]) -> RunResult:
    return RunResult(
        triage=out.triage or "triage_error",
        tool_calls=out.calls,
        reply=out.draft if out.draft is not None else out.text,
        sources=out.sources,
        reply_level=out.reply_level,
        reply_status=out.reply_status,
        escalated=out.escalated,
        skipped=out.skipped,
        retrieved=out.retrieved,
        customer_texts=out.customer_texts,
        error=out.error,
        **new_items(world, before),
    )


def _deps(world: World, agent_model: Any, triage_model: Any, now: dt.datetime, **kw) -> Deps:
    return Deps(
        world=world,
        retriever=kw.pop("retriever", None) or shared_retriever(),
        agent_model=agent_model,
        triage_model=triage_model,
        clock=lambda: now,
        approvals=Approvals(world),
        **kw,
    )


def run_case(
    case: Case,
    agent_model: Any,
    triage_model: Any = None,
    *,
    retriever: Retriever | None = None,
    experiment: str | None = None,
) -> tuple[RunOutcome, RunResult]:
    now = case_now(case.today)
    world = World(":memory:")
    world.seed(case.world, now)
    before = _snapshot_ids(world)
    deps = _deps(
        world,
        agent_model,
        triage_model,
        now,
        retriever=retriever,
        experiment=experiment,
        case_id=case.id,
    )
    out = process_email(deps, case.email, case.thread, case.email.message_id, today=case.today)
    return out, to_result(out, world, before)


def run_conversation(
    convo: Conversation,
    agent_model: Any,
    triage_model: Any = None,
    *,
    retriever: Retriever | None = None,
    experiment: str | None = None,
) -> tuple[list[tuple[RunOutcome, RunResult]], RunResult]:
    """Each turn runs on the agent's own earlier replies, in one shared world."""
    world = World(":memory:")
    world.seed(convo.world, case_now(convo.today))
    start = _snapshot_ids(world)
    thread: list[ThreadMessage] = []
    turns, last = [], None
    for i, turn in enumerate(convo.turns):
        now = case_now(convo.today, minutes=10 * i)
        before = _snapshot_ids(world)
        deps = _deps(
            world,
            agent_model,
            triage_model,
            now,
            retriever=retriever,
            experiment=experiment,
            case_id=f"{convo.id}#{i + 1}",
        )
        email = Email(
            **{
                "from": convo.sender,
                "subject": "Re: our event",
                "body": turn.customer,
                "message_id": f"{convo.id}-turn-{i + 1}@evals",
            }
        )
        out = process_email(deps, email, list(thread), email.message_id, today=convo.today)
        turns.append((out, to_result(out, world, before)))
        thread.append(ThreadMessage(role="customer", body=turn.customer))
        if out.text:
            thread.append(ThreadMessage(role="agent", body=out.text))
        last = out
    end = to_result(last, world, start)
    end.escalated = any(o.escalated for o, _ in turns)
    return turns, end


# --- real models ---------------------------------------------------------------------------


def real_models(agent: str = "sonnet") -> tuple[Any, Any]:
    from langchain_anthropic import ChatAnthropic

    model = ChatAnthropic(model=MODELS.get(agent, agent), max_tokens=2048, max_retries=2)
    triage_model = ChatAnthropic(
        model=os.environ.get("TRIAGE_MODEL", MODELS["sonnet"]), max_tokens=300, max_retries=2
    )
    return model, triage_model


def git_sha() -> str:
    sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True)
    dirty = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True)
    return sha.stdout.strip() + ("-dirty" if dirty.stdout.strip() else "")


def _print(case_id: str, out: RunOutcome, results) -> None:
    print(
        f"\n=== {case_id}: triage {out.triage}, reply {out.reply_status} "
        f"(level {out.reply_level}), escalated {out.escalated}"
    )
    for c in out.calls:
        print(
            f"  tool {c['tool']} -> {c['status']} (level {c['effective_level']}) "
            f"{json.dumps(c['args'], default=str)}"
        )
    if out.guards:
        print(f"  guards: {out.guards}")
    print("\n" + (out.text or out.draft or "(no reply)") + "\n")
    for r in results:
        mark = "ok  " if r.passed else ("FAIL" if r.gate else "miss")
        extra = f" score={r.score}" if r.score is not None else ""
        print(f"  {mark} {r.key}{extra} {r.comment}")
    if out.error:
        print(f"  => INFRA ERROR, not graded: {out.error}")
    else:
        print(f"  => run {'PASSED' if run_passed(results) else 'FAILED'}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--case", action="append", help="case id (repeatable)")
    p.add_argument("--model", default="sonnet")
    p.add_argument("--judge", action="store_true")
    args = p.parse_args(argv)

    from dotenv import load_dotenv

    load_dotenv()
    os.environ.setdefault("LANGSMITH_TRACING", "true")
    os.environ.setdefault("LANGSMITH_PROJECT", "larkspur-agent")
    suite = load_suite()
    cases = {c.id: c for c in suite.cases}
    agent_model, triage_model = real_models(args.model)
    ok = True
    for cid in args.case or []:
        case = cases[cid]
        out, result = run_case(case, agent_model, triage_model, experiment="local")
        results = evaluate(case.expected, result)
        if args.judge:
            results += judge_case(case, out)
        _print(cid, out, results)
        ok &= run_passed(results) and not out.error
    return 0 if ok else 1


def judge_case(case: Case, out: RunOutcome):
    from evals.judge import JudgeInput, judge_reply

    if not (out.text or out.draft):
        return []
    r = case.expected.reply
    inp = JudgeInput(
        email=case.email,
        reply=out.text or out.draft,
        thread=case.thread,
        tool_calls=out.calls,
        retrieved=out.retrieved,
        sources=out.sources,
        must_ask=r.must_ask,
        must_not_ask=r.must_not_ask,
        must_mention=r.must_mention,
        must_not_claim=r.must_not_claim,
    )
    return judge_reply(inp)


if __name__ == "__main__":
    sys.exit(main())

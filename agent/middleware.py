"""The AutonomyMiddleware (plan §7.5): the single gate every tool call passes through.

handle_call, in order:
1. Input checks (evidence quotes, window, season) via tools.prepare.
2. The action's effective level, with the peak_date slice for holds on high-demand dates.
3. Level 0: not_permitted, and an escalation is recorded.
4. Read tools run, and their level feeds the reply level.
5. Write tools at level 1 become a pending request (one per customer, action and date).
6. Write tools at level 2-3 go through world.py in one transaction with their ledger row;
   a failed state check falls back to a pending request, booked/blocked is not_permitted.
7. Any exception fails closed: nothing written, an escalation, status failed.
Every call is appended to the run's calls and written to the ledger.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage
from langsmith import traceable

from agent.checks import hold_check
from agent.context import RunContext
from agent.policy import CONFIG, READ_TOOLS, action_for_tool, effective_level
from agent.records import insert_action
from agent.tools import TOOL_NAMES, prepare, run_read

PENDING_MESSAGE = (
    "Requested, not done: a coordinator must approve it first. Tell the customer it has been "
    "requested and that the coordinator will confirm. Never say it is placed, booked or held."
)


class ToolUnavailable(RuntimeError):
    pass


def _key(ctx: RunContext, tool: str, values: dict[str, Any]) -> str:
    body = json.dumps({"m": ctx.message_id, "t": tool, "v": values}, sort_keys=True, default=str)
    return hashlib.sha256(body.encode()).hexdigest()[:32]


# After this many needs_input results from one tool in a run, further calls are refused.
MAX_NEEDS_INPUT = 3
# How to fix a call, for the model (the `message` is worded for the customer).
NEXT_STEP = {
    "day_without_month": "If the customer named the month in an earlier message, call again "
    'with those exact words added to the quote, for example ["in September", "the 20th"]. '
    "Otherwise don't call again: ask the customer.",
    "no_date": "If the customer gave the date in an earlier message, quote those exact words. "
    "Otherwise don't call again: ask the customer.",
}


def handle_call(name: str, args: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
    action = action_for_tool(name)
    row: dict[str, Any] = {
        "run_id": ctx.run_id,
        "message_id": ctx.message_id,
        "tool": name,
        "action": action,
        "args": {k: v for k, v in args.items() if not k.endswith("_quote")},
        "slices": [],
        "configured_level": ctx.overrides.get(
            action, CONFIG["actions"].get(action, {}).get("level")
        ),
    }

    def finish(result: dict[str, Any]) -> dict[str, Any]:
        row["status"] = result["status"]
        row["reason"] = result.get("reason")
        row["checks"] = result.get("failed_checks", [])
        call = {k: row.get(k) for k in ("tool", "status", "args", "effective_level", "slices")}
        call.update(record_id=result.get("record_id"), output=result, reason=result.get("reason"))
        ctx.calls.append(call)
        # INSERT OR IGNORE on the idempotency key: a row already written inside the world
        # transaction isn't duplicated.
        insert_action(ctx.world.conn, row, ctx.now)
        return result

    if name not in TOOL_NAMES:
        row["effective_level"] = 0
        return finish(
            {"status": "not_permitted", "reason": "unknown_tool", "message": f"No tool {name}."}
        )
    try:
        if name in ctx.tool_failures:
            raise ToolUnavailable(f"{name} is unavailable right now")

        # A loop breaker (F5): a tool that keeps needing input is refused, so the agent asks
        # the customer instead of retrying with new wording until the step limit.
        asked = sum(1 for c in ctx.calls if c["tool"] == name and c["status"] == "needs_input")
        if asked >= MAX_NEEDS_INPUT:
            row["effective_level"] = effective_level(action, set(), ctx.overrides)
            return finish(
                {
                    "status": "needs_input",
                    "reason": "ask_customer",
                    "message": f"{name} has needed more from the customer {asked} times in this "
                    "run. Don't call it again: ask the customer for what's missing.",
                }
            )

        prep = prepare(name, args, ctx)
        if prep.values:
            row["args"] = prep.values
        if name == "place_hold" and prep.ok:
            if ctx.world.is_high_demand(dt.date.fromisoformat(prep.values["date"])):
                row["slices"] = ["peak_date"]
        level = effective_level(action, set(row["slices"]), ctx.overrides)
        row["effective_level"] = level

        if name == "escalate":
            values = prep.values
            ctx.world.escalate(
                ctx.run_id, values["reason"] or "escalated", values["category"], ctx.now
            )
            ctx.escalated = True
            return finish(
                {"status": "executed", "message": "A coordinator will follow up personally."}
            )
        if level == 0:
            ctx.world.escalate(ctx.run_id, "level_0", action, ctx.now)
            ctx.escalated = True
            return finish(
                {
                    "status": "not_permitted",
                    "reason": "level_0",
                    "message": "This isn't something I can do; a coordinator will follow up.",
                }
            )
        if not prep.ok:
            result = {"status": prep.status, "reason": prep.reason, "message": prep.message}
            if prep.status == "needs_input":
                hint = NEXT_STEP.get(prep.reason or "")
                if asked:
                    hint = "Stop retrying this call with other wording. " + (
                        hint or "Ask the customer, using the message above."
                    )
                if hint:
                    result["next_step"] = hint
            return finish(result)
        if name in READ_TOOLS:
            return finish(run_read(name, prep.values, ctx))
        return finish(_write(name, prep, level, row, ctx))
    except Exception as err:  # noqa: BLE001 - fail closed on anything
        ctx.world.escalate(ctx.run_id, "tool_error", name, ctx.now)
        ctx.escalated = True
        row.setdefault("effective_level", None)
        row["idempotency_key"] = None  # a failed call must not claim the key from a retry
        return finish(
            {
                "status": "failed",
                "reason": "tool_error",
                "message": f"{type(err).__name__}: nothing was done. A coordinator will follow up. "
                "Don't claim this action happened.",
            }
        )


def _write(name: str, prep, level: int, row: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
    values = prep.values
    date = dt.date.fromisoformat(values["date"])
    key = _key(ctx, name, values)
    row["idempotency_key"] = key

    def pending(reason: str, failed: list[str]) -> dict[str, Any]:
        def record(c, pending_id: int) -> None:
            insert_action(c, {**row, "status": "pending_approval", "checks": failed}, ctx.now)

        pid, _created = ctx.world.create_pending(
            run_id=ctx.run_id,
            message_id=ctx.message_id,
            action=name,
            customer_email=ctx.sender_email,
            customer_name=ctx.sender_name,
            args=values,
            quotes=prep.quotes,
            reason=reason,
            failed_checks=failed,
            now=ctx.now,
            record=record,
        )
        return {
            "status": "pending_approval",
            "pending_id": pid,
            "failed_checks": failed,
            "message": PENDING_MESSAGE,
        }

    if name == "place_hold":
        if level == 1:
            # Booked or blocked can't be changed by any coordinator: refuse now, don't queue.
            check = hold_check(
                date, values["room"], ctx.sender_email, ctx.world.snapshot(ctx.now), ctx.today
            )
            if check.decision == "not_permitted":
                return {
                    "status": "not_permitted",
                    "reason": check.failed[0],
                    "message": check.message,
                }
            return pending("level_1", [])

        def record(c, result) -> None:
            if result.decision == "ok":
                insert_action(c, {**row, "status": "executed"}, ctx.now)

        result = ctx.world.hold(
            date,
            values["room"],
            ctx.sender_email,
            ctx.sender_name,
            now=ctx.now,
            today=ctx.today,
            key=key,
            record=record,
        )
    else:  # book_tour
        if level == 1:
            return pending("level_1", [])

        def record(c, result) -> None:
            if result.decision == "ok":
                insert_action(c, {**row, "status": "executed"}, ctx.now)

        result = ctx.world.tour(
            date,
            values["time"],
            ctx.sender_email,
            ctx.sender_name,
            now=ctx.now,
            today=ctx.today,
            key=key,
            record=record,
        )

    if result.decision == "ok":
        out = {"status": "executed", "ref": result.ref}
        if name == "place_hold":
            expires = ctx.now + dt.timedelta(hours=48)
            out["expires_at"] = expires.isoformat()
            out["message"] = "The hold is placed for 48 hours. It's a hold, not a booking."
        else:
            out["message"] = "The tour is booked."
        return out
    if result.decision == "not_permitted":
        return {"status": "not_permitted", "reason": result.failed[0], "message": result.message}
    return pending("fell_back", result.failed)


@traceable(
    run_type="tool",
    process_inputs=lambda inputs: {"args": inputs.get("args")},  # never trace the world object
)
def _traced_call(name: str, args: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
    """One tool span per call in the trace, named after the tool, with its status."""
    return handle_call(name, args, ctx)


class AutonomyMiddleware(AgentMiddleware):
    """Runs every agent tool call through handle_call. Calls to anything else (like the
    structured-output Reply tool) pass through untouched."""

    def wrap_tool_call(self, request, handler):
        name = request.tool_call["name"]
        if name not in TOOL_NAMES:
            return handler(request)
        result = _traced_call(
            name,
            dict(request.tool_call.get("args") or {}),
            request.runtime.context,
            langsmith_extra={"name": name, "metadata": {"tool": name}},
        )
        result = {k: v for k, v in result.items() if not k.startswith("_")}
        return ToolMessage(
            content=json.dumps(result, default=str),
            tool_call_id=request.tool_call["id"],
            name=name,
        )

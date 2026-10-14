"""The outer graph (plan §2): claim -> triage -> agent -> reply gate -> record.

A LangGraph StateGraph runs the fixed steps; the agent step is a create_agent tool loop
whose every tool call goes through the AutonomyMiddleware. A reply at level 2-3 goes to the
outbox (one reply per message ID); a reply at level 1 is held as a pending reply with its
own approval thread. Nothing here pauses the outer graph, so every run ends with a result.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, TypedDict

from langchain.agents import create_agent
from langchain_core.messages import HumanMessage
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field

from agent import notify
from agent.approvals import Approvals
from agent.claims import RunView, check_reply
from agent.context import RunContext
from agent.dates import local_today
from agent.gate import gate
from agent.middleware import AutonomyMiddleware
from agent.policy import WRITE_TOOLS, effective_level, sampled_for_review
from agent.prompts import PROMPT_VERSION, SYSTEM_PROMPT
from agent.retrieval import Retriever
from agent.tools import TOOLS
from agent.triage import triage
from agent.world import World
from evals.schema import Email, ThreadMessage


class Source(BaseModel):
    doc_id: str
    section: str


class Reply(BaseModel):
    """Your final reply to the customer, and the document sections it relies on."""

    reply: str = Field(description="The full email reply, as plain text.")
    sources: list[Source] = Field(default_factory=list)


@dataclass
class Deps:
    world: World
    retriever: Retriever
    agent_model: Any
    triage_model: Any
    clock: Callable[[], dt.datetime]
    approvals: Approvals
    experiment: str | None = None
    case_id: str | None = None
    _agent: Any = None

    def agent(self) -> Any:
        if self._agent is None:
            middleware: list[Any] = [AutonomyMiddleware()]
            if type(self.agent_model).__name__ == "ChatAnthropic":
                from langchain_anthropic.middleware import AnthropicPromptCachingMiddleware

                middleware.append(AnthropicPromptCachingMiddleware(ttl="5m"))
            self._agent = create_agent(
                self.agent_model,
                TOOLS,
                system_prompt=SYSTEM_PROMPT,
                middleware=middleware,
                response_format=Reply,  # native structured output where the model has it
                context_schema=RunContext,
            )
        return self._agent


@dataclass
class RunOutcome:
    run_id: str
    message_id: str
    status: str  # done | duplicate | failed
    triage: str | None = None
    triage_reason: str = ""
    reply_status: str | None = None  # sent | pending_approval | None
    reply_level: int | None = None
    base_level: int | None = None
    draft: str | None = None  # what the agent wrote, before any guard
    text: str | None = None  # what the customer gets (or would get, once approved)
    sources: list[dict[str, str]] = field(default_factory=list)
    guards: dict[str, list[str]] = field(default_factory=dict)
    calls: list[dict[str, Any]] = field(default_factory=list)
    retrieved: list[dict[str, str]] = field(default_factory=list)
    customer_texts: list[str] = field(default_factory=list)
    escalated: bool = False
    skipped: bool = False
    error: str | None = None


class _State(TypedDict, total=False):
    status: str
    existing_run: str
    triage: str
    triage_reason: str
    draft: str
    sources: list[dict[str, str]]
    error: str


def _draft_note(placeholders: list[str], amounts: list[str]) -> str:
    """The one correction note the agent gets when its draft fails a numbers check."""
    lines = ["[System check]"]
    if placeholders:
        lines.append(
            f"Your reply uses {', '.join(placeholders)}, but no pricing call in this run "
            "returned that record. Call get_published_rate or quote_package for it, or remove "
            "the price and say a coordinator will send it."
        )
    if amounts:
        shown = ", ".join(a[1:] + "%" if a.startswith("%") else a for a in amounts)
        lines.append(
            f"Your reply repeats {shown}, which no tool or retrieved document produced. Don't "
            'repeat it, even to refuse it; call it "the figure you mentioned".'
        )
    lines.append("Then give your full reply again.")
    return " ".join(lines)


def render_email(email: Email, thread: list[ThreadMessage], today: dt.date) -> str:
    parts = [
        f"Today is {today:%A}, {today:%B} {today.day}, {today.year} (venue time).",
        f"New email from {email.name + ' ' if email.name else ''}<{email.sender}>",
        f"Subject: {email.subject}",
        "",
        email.body,
    ]
    if thread:
        parts += [
            "",
            "Earlier messages in this thread, oldest first. Venue replies are not "
            "sources; check anything they said again with your tools:",
        ]
        for m in thread:
            who = "[customer]" if m.role == "customer" else "[venue reply]"
            parts.append(f"{who} {m.body}")
    return "\n".join(parts)


def customer_texts(email: Email, thread: list[ThreadMessage]) -> list[str]:
    return [email.subject, email.body] + [m.body for m in thread if m.role == "customer"]


def process_email(
    deps: Deps,
    email: Email,
    thread: list[ThreadMessage],
    message_id: str,
    *,
    run_id: str | None = None,
    today: dt.date | None = None,
) -> RunOutcome:
    run_id = run_id or uuid.uuid4().hex[:12]
    now = deps.clock()
    today = today or local_today(now)
    world = deps.world
    sender = email.sender.strip().lower()
    texts = customer_texts(email, thread)
    ctx = RunContext(
        run_id=run_id,
        message_id=message_id,
        sender_email=sender,
        sender_name=email.name,
        today=today,
        now=now,
        customer_texts=texts,
        world=world,
        retriever=deps.retriever,
        overrides=dict(world.autonomy),
        tool_failures=set(world.tool_failures),
    )
    out = RunOutcome(run_id, message_id, "done", customer_texts=texts)

    def claim(state: _State) -> _State:
        c = world.claim_inbound(message_id, run_id, now)
        if c.status != "claimed":
            return {"status": "duplicate", "existing_run": c.run_id}
        return {"status": "claimed"}

    def do_triage(state: _State) -> _State:
        r = triage(email, thread, deps.triage_model)
        return {"triage": r.category, "triage_reason": r.reason}

    def skip(state: _State) -> _State:
        out.skipped = True
        deps.approvals.add_review("skip", message_id, run_id, "skip", now)
        return {}

    def deliver(text: str, level: int, guards: dict[str, list[str]]) -> None:
        """Send at level 2-3 (level 2 is reviewed after, level 3 sampled); below that, hold
        the reply for a person."""
        out.text = text
        if level >= 2:
            if world.send_reply(message_id, run_id, sender, text, now):
                out.reply_status = "sent"
            if level == 2:
                deps.approvals.add_review("reply", message_id, run_id, "level_2", now)
            elif sampled_for_review(run_id):
                deps.approvals.add_review("reply", message_id, run_id, "sample", now)
        else:
            deps.approvals.queue_reply(
                run_id=run_id,
                message_id=message_id,
                to=sender,
                text=text,
                guards=guards,
                level=level,
                now=now,
            )
            out.reply_status = "pending_approval"

    def complaint(state: _State) -> _State:
        world.escalate(run_id, "complaint", "complaint", now)
        out.escalated = True
        # A code template, but still an action: the policy sets its level and the ledger
        # records it, like every other reply (F4).
        level = effective_level("complaint_ack", overrides=ctx.overrides)
        out.reply_level = out.base_level = level
        deliver(notify.complaint_ack(email.name), level, guards={})
        return {}

    def triage_error(state: _State) -> _State:
        world.escalate(run_id, "triage_error", None, now)
        out.escalated = True
        return {}

    def run_agent(state: _State) -> _State:
        config = {
            "run_name": "larkspur-agent",
            "recursion_limit": 30,
            "tags": [t for t in (deps.experiment, PROMPT_VERSION) if t],
            "metadata": {
                "run_id": run_id,
                "message_id": message_id,
                "case_id": deps.case_id,
                "prompt_version": PROMPT_VERSION,
            },
        }

        def ask(messages: list[Any]) -> tuple[Any, Any]:
            result = deps.agent().invoke({"messages": messages}, context=ctx, config=config)
            reply = result.get("structured_response")
            if reply is None:
                raise RuntimeError("the agent finished without a reply")
            return result, reply

        try:
            result, reply = ask([HumanMessage(render_email(email, thread, today))])
            # F3: a price placeholder must name a record a pricing call returned in this
            # run. One retry with a note saying which; a second miss goes to the gate,
            # which holds the reply for a person.
            # F2: so must every amount, which comes from a tool or a retrieved doc (the
            # same numbers check the gate runs). One retry covers both.
            view = RunView(
                tool_calls=ctx.calls,
                retrieved=[r["text"] for r in ctx.retrieved],
                customer_texts=ctx.customer_texts,
            )
            checks = check_reply(reply.reply, view)
            if checks.bad_placeholders or checks.untraced:
                note = _draft_note(checks.bad_placeholders, checks.untraced)
                result, reply = ask([*result["messages"], HumanMessage(note)])
        except Exception as err:  # noqa: BLE001 - no automated reply; a person follows up
            world.escalate(run_id, "model_error", None, now)
            out.escalated = True
            return {"error": f"{type(err).__name__}: {err}"}
        return {"draft": reply.reply, "sources": [s.model_dump() for s in reply.sources]}

    def reply_gate(state: _State) -> _State:
        if "draft" not in state:
            return {}
        g = gate(state["draft"], ctx)
        out.reply_level, out.base_level, out.guards = g.level, g.base_level, g.guards
        deliver(g.text, g.level, g.guards)
        for c in ctx.calls:  # executed writes are reviewed: every level 2, a sample of level 3
            if c["tool"] in WRITE_TOOLS and c["status"] == "executed":
                if c["effective_level"] == 2:
                    deps.approvals.add_review(
                        "write", c["output"].get("ref"), run_id, "level_2", now
                    )
                elif sampled_for_review(f"{run_id}:{c['tool']}"):
                    deps.approvals.add_review(
                        "write", c["output"].get("ref"), run_id, "sample", now
                    )
        return {}

    def record(state: _State) -> _State:
        world.conn.execute(
            "INSERT OR REPLACE INTO runs (run_id, message_id, case_id, experiment, triage,"
            " outcome, reply_level, reply_status, guards_json, draft, sources_json,"
            " started_at, finished_at, error) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                run_id,
                message_id,
                deps.case_id,
                deps.experiment,
                state.get("triage"),
                "skipped" if out.skipped else "escalated" if out.escalated else "handled",
                out.reply_level,
                out.reply_status,
                json.dumps(out.guards),
                state.get("draft"),
                json.dumps(state.get("sources", [])),
                now.isoformat(),
                deps.clock().isoformat(),
                state.get("error"),
            ),
        )
        world.finish_inbound(message_id, "failed" if state.get("error") else "done")
        return {}

    g = StateGraph(_State)
    for name, fn in [
        ("claim", claim),
        ("triage", do_triage),
        ("skip", skip),
        ("complaint", complaint),
        ("triage_error", triage_error),
        ("agent", run_agent),
        ("gate", reply_gate),
        ("record", record),
    ]:
        g.add_node(name, fn)
    g.add_edge(START, "claim")
    g.add_conditional_edges(
        "claim",
        lambda s: "end" if s["status"] == "duplicate" else "triage",
        {"end": END, "triage": "triage"},
    )
    g.add_conditional_edges(
        "triage",
        lambda s: {"customer": "agent", "non_customer": "skip", "complaint": "complaint"}.get(
            s["triage"], "triage_error"
        ),
    )
    for n in ("skip", "complaint", "triage_error", "gate"):
        g.add_edge(n, "record")
    g.add_edge("agent", "gate")
    g.add_edge("record", END)

    final = g.compile().invoke(
        {},
        config={
            "run_name": "larkspur-inquiry",
            "metadata": {"run_id": run_id, "case_id": deps.case_id},
        },
    )
    if final.get("status") == "duplicate":
        return RunOutcome(final["existing_run"], message_id, "duplicate")
    out.triage, out.triage_reason = final.get("triage"), final.get("triage_reason", "")
    out.draft, out.sources = final.get("draft"), final.get("sources", [])
    out.calls, out.retrieved = ctx.calls, ctx.retrieved
    out.escalated = out.escalated or ctx.escalated
    if final.get("error"):
        out.status, out.error = "failed", final["error"]
    return out

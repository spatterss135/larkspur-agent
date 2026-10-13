"""Approvals (plan §7.7) and the Review list (§7.8).

Each pending item (a write the agent couldn't do on its own, or a reply held at level 1)
gets a small LangGraph thread: `review` pauses with interrupt() until a person decides, and
`apply` carries the decision out. The checkpointer is SQLite, so a paused approval survives
a restart. Decisions are idempotent: the world re-checks the pending item's status inside
its own transaction, and follow-ups are sent at most once.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from agent import notify
from agent.edits import substantive_edit
from agent.world import World


def _ts(t: dt.datetime) -> str:
    return t.astimezone(dt.UTC).isoformat(timespec="microseconds")


@dataclass
class Result:
    status: str  # executed | pending | rejected | expired | sent | ...
    changed: bool
    failed: list[str] = field(default_factory=list)


class _State(TypedDict, total=False):
    kind: str
    id: int
    decision: dict[str, Any]
    result: dict[str, Any]


class Approvals:
    def __init__(self, world: World, path: str = ":memory:"):
        self.world = world
        if path == ":memory:":
            saver: Any = InMemorySaver()
        else:
            from langgraph.checkpoint.sqlite import SqliteSaver

            saver = SqliteSaver(sqlite3.connect(path, check_same_thread=False))
            saver.setup()
        g = StateGraph(_State)
        g.add_node("review", self._review)
        g.add_node("apply", self._apply_node)
        g.add_edge(START, "review")
        g.add_edge("review", "apply")
        g.add_edge("apply", END)
        self.graph = g.compile(checkpointer=saver)

    # --- queueing and listing ----------------------------------------------------------------

    def queue_reply(
        self,
        *,
        run_id: str,
        message_id: str,
        to: str,
        text: str,
        guards: dict[str, Any],
        level: int,
        now: dt.datetime,
    ) -> int:
        cur = self.world.conn.execute(
            "INSERT OR IGNORE INTO pending_replies (run_id, message_id, to_addr, draft,"
            " guards_json, reply_level, status, created_at) VALUES (?,?,?,?,?,?,'pending',?)",
            (run_id, message_id, to, text, json.dumps(guards), level, _ts(now)),
        )
        if cur.rowcount == 0:  # this message already has a held reply
            row = self.world.conn.execute(
                "SELECT id FROM pending_replies WHERE message_id = ?", (message_id,)
            ).fetchone()
            return row["id"]
        self._open("reply", cur.lastrowid)
        return cur.lastrowid

    def open_action(self, pending_id: int) -> None:
        self._open("action", pending_id)

    def add_review(
        self, item_type: str, ref_id: Any, run_id: str, reason: str, now: dt.datetime
    ) -> None:
        self.world.conn.execute(
            "INSERT INTO reviews (item_type, ref_id, run_id, reason, created_at)"
            " VALUES (?,?,?,?,?)",
            (item_type, str(ref_id), run_id, reason, _ts(now)),
        )

    def pending(self) -> list[dict[str, Any]]:
        c = self.world.conn
        items = [
            {
                "kind": "action",
                "id": r["id"],
                "action": r["action"],
                "customer": r["customer_email"],
                "args": json.loads(r["args_json"]),
                "reason": r["reason"],
                "failed_checks": json.loads(r["failed_checks_json"] or "[]"),
                "created_at": r["created_at"],
            }
            for r in c.execute("SELECT * FROM pending_actions WHERE status = 'pending' ORDER BY id")
        ]
        items += [
            {
                "kind": "reply",
                "id": r["id"],
                "customer": r["to_addr"],
                "draft": r["draft"],
                "guards": json.loads(r["guards_json"] or "{}"),
                "created_at": r["created_at"],
            }
            for r in c.execute("SELECT * FROM pending_replies WHERE status = 'pending' ORDER BY id")
        ]
        return items

    def review_items(self) -> list[dict[str, Any]]:
        rows = self.world.conn.execute(
            "SELECT * FROM reviews WHERE verdict IS NULL ORDER BY id"
        ).fetchall()
        return [
            {"id": r["id"], "kind": r["item_type"], "ref": r["ref_id"], "reason": r["reason"]}
            for r in rows
        ]

    # --- deciding ------------------------------------------------------------------------------

    def decide(
        self,
        kind: str,
        item_id: int,
        decision: str,
        *,
        now: dt.datetime,
        note: str | None = None,
        edited_args: dict[str, Any] | None = None,
        edited_text: str | None = None,
        reviewer: str = "coordinator",
    ) -> Result:
        # Validate before resuming: an error inside a node would leave the thread half-resumed.
        if decision not in ("approve", "edit", "reject"):
            raise ValueError(f"unknown decision: {decision}")
        if decision == "reject" and not (note or "").strip():
            raise ValueError("a reject needs a note")
        if decision == "edit" and not (edited_args if kind == "action" else edited_text):
            raise ValueError("an edit needs the edited " + ("args" if kind == "action" else "text"))
        config = {"configurable": {"thread_id": f"{kind}:{item_id}"}}
        snap = self.graph.get_state(config)
        if not snap.values:
            self._open(kind, item_id)
            snap = self.graph.get_state(config)
        if not snap.next:  # already decided: report the current state, change nothing
            return self._current(kind, item_id)
        payload = {
            "decision": decision,
            "now": _ts(now),
            "note": note,
            "edited_args": edited_args,
            "edited_text": edited_text,
            "reviewer": reviewer,
        }
        out = self.graph.invoke(Command(resume=payload), config)
        r = out["result"]
        return Result(r["status"], r["changed"], r.get("failed", []))

    def _open(self, kind: str, item_id: int) -> None:
        config = {"configurable": {"thread_id": f"{kind}:{item_id}"}}
        if not self.graph.get_state(config).values:
            self.graph.invoke({"kind": kind, "id": item_id}, config)

    def _review(self, state: _State) -> _State:
        decision = interrupt({"kind": state["kind"], "id": state["id"]})
        return {"decision": decision}

    def _apply_node(self, state: _State) -> _State:
        d = state["decision"]
        now = dt.datetime.fromisoformat(d["now"])
        if state["kind"] == "action":
            r = self._apply_action(state["id"], d, now)
        else:
            r = self._apply_reply(state["id"], d, now)
        return {"result": {"status": r.status, "changed": r.changed, "failed": r.failed}}

    def _current(self, kind: str, item_id: int) -> Result:
        table = "pending_actions" if kind == "action" else "pending_replies"
        row = self.world.conn.execute(f"SELECT status FROM {table} WHERE id = ?", (item_id,))
        return Result(row.fetchone()["status"], False)

    def _record(self, kind, ref, decision, draft, final, substantive, note, reviewer, now):
        self.world.conn.execute(
            "INSERT INTO approvals (kind, ref_id, decision, draft_json, final_json,"
            " substantive_edit, note, reviewer, decided_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                kind,
                ref,
                decision,
                json.dumps(draft, default=str),
                json.dumps(final, default=str),
                None if substantive is None else int(substantive),
                note,
                reviewer,
                _ts(now),
            ),
        )

    def _apply_action(self, pid: int, d: dict[str, Any], now: dt.datetime) -> Result:
        row = self.world.conn.execute("SELECT * FROM pending_actions WHERE id = ?", (pid,))
        p = row.fetchone()
        draft = json.loads(p["args_json"])
        decision = self.world.decide(
            pid, d["decision"], now=now, edited_args=d.get("edited_args"), note=d.get("note")
        )
        if decision.changed:
            final = d.get("edited_args") or draft
            self._record(
                "action",
                pid,
                d["decision"],
                draft,
                final,
                substantive_edit(draft, final),
                d.get("note"),
                d["reviewer"],
                now,
            )
            self._follow_up(p, decision.status, final, d.get("note"), now)
        return Result(decision.status, decision.changed, decision.failed)

    def _follow_up(self, p, status: str, args: dict[str, Any], note: str | None, now) -> None:
        if status == "executed" and p["action"] == "place_hold":
            body = notify.hold_placed(
                p["customer_name"],
                dt.date.fromisoformat(args["date"]),
                args["room"],
                now + dt.timedelta(hours=48),
            )
        elif status == "executed" and p["action"] == "book_tour":
            body = notify.tour_booked(
                p["customer_name"], dt.date.fromisoformat(args["date"]), args["time"]
            )
        elif status == "rejected":
            body = notify.rejected(p["customer_name"], note or "")
        else:
            return
        key = f"follow-up:action:{p['id']}"
        exists = self.world.conn.execute(
            "SELECT 1 FROM outbox WHERE message_id = ?", (key,)
        ).fetchone()
        if not exists:  # at most one follow-up, even if this node runs again after a crash
            self.world.send_reply(
                key, p["run_id"], p["customer_email"], body, now, kind="follow_up"
            )

    def _apply_reply(self, rid: int, d: dict[str, Any], now: dt.datetime) -> Result:
        r = self.world.conn.execute("SELECT * FROM pending_replies WHERE id = ?", (rid,)).fetchone()
        if r["status"] != "pending":
            return Result(r["status"], False)
        decision = d["decision"]
        if decision == "reject":
            if not (d.get("note") or "").strip():
                raise ValueError("a reject needs a note")
            status, final = "rejected", None
        else:
            final = d.get("edited_text") if decision == "edit" else r["draft"]
            if not final:
                raise ValueError("an edit needs the edited text")
            self.world.send_reply(r["message_id"], r["run_id"], r["to_addr"], final, now)
            status = "sent"
        self.world.conn.execute(
            "UPDATE pending_replies SET status = ?, final = ?, note = ?, decided_at = ?"
            " WHERE id = ?",
            (status, final, d.get("note"), _ts(now), rid),
        )
        substantive = substantive_edit({}, {}, r["draft"], final) if final else None
        self._record(
            "reply",
            rid,
            decision,
            r["draft"],
            final,
            substantive,
            d.get("note"),
            d["reviewer"],
            now,
        )
        return Result(status, True)

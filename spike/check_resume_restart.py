"""Spike check 3: interrupt() + resume across a process restart with the SQLite checkpointer.
Run:  python spike/check_resume_restart.py start   (process 1: pauses and exits)
      python spike/check_resume_restart.py resume  (process 2: resumes from the file)
      python spike/check_resume_restart.py resume  (again: what does a second resume do?)"""

import sqlite3
import sys
from typing import TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

DB = "spike/spike.sqlite3"
CONFIG = {"configurable": {"thread_id": "pending-action-1"}}


class S(TypedDict, total=False):
    action: str
    decision: str
    executions: int


def approval(state: S) -> S:
    decision = interrupt({"action": state["action"], "ask": "approve / edit / reject"})
    return {"decision": decision, "executions": state.get("executions", 0) + 1}


g = StateGraph(S)
g.add_node("approval", approval)
g.add_edge(START, "approval")
g.add_edge("approval", END)
graph = g.compile(checkpointer=SqliteSaver(sqlite3.connect(DB, check_same_thread=False)))

mode = sys.argv[1]
if mode == "start":
    out = graph.invoke({"action": "place_hold 2026-06-13"}, CONFIG)
    print("paused with:", out.get("__interrupt__"))
elif mode == "resume":
    snap = graph.get_state(CONFIG)
    print("before resume, next nodes:", snap.next)
    out = graph.invoke(Command(resume="approve"), CONFIG)
    print("after resume:", {k: out.get(k) for k in ("decision", "executions")})

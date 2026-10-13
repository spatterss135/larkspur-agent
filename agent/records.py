"""The ledger: what each run did (plan §8.2). Lives in the same SQLite file as the world,
so a write tool's world change and its actions row can commit in one transaction.

These are ledger tables, not world tables: they record what happened and never decide
availability. Single statements here autocommit; inside a world transaction they join it.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS actions (
    id INTEGER PRIMARY KEY, run_id TEXT, message_id TEXT, action TEXT, tool TEXT NOT NULL,
    slice TEXT, configured_level INTEGER, effective_level INTEGER, status TEXT NOT NULL,
    args_json TEXT, checks_json TEXT, reason TEXT, idempotency_key TEXT UNIQUE,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS price_records (
    run_id TEXT NOT NULL, record_id TEXT NOT NULL, kind TEXT NOT NULL, inputs_json TEXT,
    quotes_json TEXT, breakdown_json TEXT NOT NULL, pricing_version TEXT, created_at TEXT,
    PRIMARY KEY (run_id, record_id)
);
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY, message_id TEXT, case_id TEXT, experiment TEXT,
    agent_model TEXT, triage_model TEXT, triage TEXT, outcome TEXT, reply_level INTEGER,
    reply_status TEXT, guards_json TEXT, draft TEXT, sources_json TEXT, trace_id TEXT,
    started_at TEXT, finished_at TEXT, error TEXT
);
CREATE TABLE IF NOT EXISTS pending_replies (
    id INTEGER PRIMARY KEY, run_id TEXT, message_id TEXT UNIQUE, to_addr TEXT,
    draft TEXT NOT NULL, guards_json TEXT, reply_level INTEGER, status TEXT NOT NULL,
    created_at TEXT NOT NULL, decided_at TEXT, final TEXT, note TEXT
);
CREATE TABLE IF NOT EXISTS approvals (
    id INTEGER PRIMARY KEY, kind TEXT, ref_id INTEGER, decision TEXT, draft_json TEXT,
    final_json TEXT, substantive_edit INTEGER, note TEXT, reviewer TEXT, decided_at TEXT
);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def _ts(t: dt.datetime) -> str:
    return t.astimezone(dt.UTC).isoformat(timespec="microseconds")


def insert_action(conn: sqlite3.Connection, row: dict[str, Any], now: dt.datetime) -> None:
    """INSERT OR IGNORE: a repeated call with the same idempotency key is one ledger row."""
    conn.execute(
        "INSERT OR IGNORE INTO actions (run_id, message_id, action, tool, slice,"
        " configured_level, effective_level, status, args_json, checks_json, reason,"
        " idempotency_key, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            row.get("run_id"),
            row.get("message_id"),
            row.get("action"),
            row["tool"],
            ",".join(row.get("slices") or []) or None,
            row.get("configured_level"),
            row.get("effective_level"),
            row["status"],
            json.dumps(row.get("args", {}), default=str),
            json.dumps(row.get("checks", []), default=str),
            row.get("reason"),
            row.get("idempotency_key"),
            _ts(now),
        ),
    )


def insert_price_record(
    conn: sqlite3.Connection, run_id: str, record_id: str, record: dict[str, Any], now: dt.datetime
) -> None:
    conn.execute(
        "INSERT INTO price_records VALUES (?,?,?,?,?,?,?,?)",
        (
            run_id,
            record_id,
            record["kind"],
            json.dumps(record["inputs"], default=str),
            json.dumps(record.get("quotes", {})),
            json.dumps(record["breakdown"]),
            record["breakdown"].get("pricing_version"),
            _ts(now),
        ),
    )

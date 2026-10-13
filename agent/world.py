"""The venue's world: the ONLY module that writes bookings, holds, tours, pending actions or
high-demand dates (a test scans the codebase for writes anywhere else). Plan §6.4, §8.

Every write is one BEGIN IMMEDIATE transaction: expire stale holds, take a snapshot, run the
state checks (agent/checks.py), write, and commit. Partial unique indexes back the checks
up, so even code that skipped them couldn't double-hold a date. A `record` hook runs inside
the same transaction, so a world change and its ledger row commit together or not at all
(the crash contract, §8.5). Times are stored as UTC ISO strings.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from typing import Any

from agent.checks import SEED, SHARED, Hold, Snapshot, Tour, hold_check, tour_check, tour_slot
from agent.dates import local_today

HOLD_HOURS = 48
PENDING_HOURS = 48
CLAIM_TIMEOUT = dt.timedelta(minutes=10)

SCHEMA = """
CREATE TABLE IF NOT EXISTS bookings (
    date TEXT NOT NULL, room TEXT NOT NULL, calendar TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('booked', 'blocked')), reason TEXT,
    UNIQUE (date, room)
);
CREATE TABLE IF NOT EXISTS high_demand_dates (date TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS holds (
    id INTEGER PRIMARY KEY, date TEXT NOT NULL, room TEXT NOT NULL, calendar TEXT NOT NULL,
    customer_email TEXT NOT NULL, customer_name TEXT, created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active', 'expired', 'released', 'converted'))
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_hold_per_calendar_date
    ON holds (date, calendar) WHERE status = 'active';
CREATE UNIQUE INDEX IF NOT EXISTS one_active_hold_per_customer
    ON holds (customer_email) WHERE status = 'active';
CREATE TABLE IF NOT EXISTS tours (
    id INTEGER PRIMARY KEY, date TEXT NOT NULL, time TEXT NOT NULL,
    customer_email TEXT NOT NULL, customer_name TEXT,
    status TEXT NOT NULL CHECK (status IN ('booked', 'cancelled', 'completed'))
);
CREATE UNIQUE INDEX IF NOT EXISTS one_tour_per_slot ON tours (date, time) WHERE status = 'booked';
CREATE TABLE IF NOT EXISTS pending_actions (
    id INTEGER PRIMARY KEY, run_id TEXT, message_id TEXT, action TEXT NOT NULL,
    customer_email TEXT NOT NULL, customer_name TEXT, target_date TEXT,
    args_json TEXT NOT NULL, quotes_json TEXT, reason TEXT NOT NULL, failed_checks_json TEXT,
    status TEXT NOT NULL
        CHECK (status IN ('pending', 'executed', 'rejected', 'expired', 'failed', 'compensated')),
    created_at TEXT NOT NULL, decided_at TEXT, note TEXT, result_ref INTEGER
);
CREATE UNIQUE INDEX IF NOT EXISTS one_pending_per_request
    ON pending_actions (customer_email, action, target_date) WHERE status = 'pending';
CREATE TABLE IF NOT EXISTS escalations (
    id INTEGER PRIMARY KEY, run_id TEXT, reason TEXT NOT NULL, category TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY, run_id TEXT, message_id TEXT, kind TEXT NOT NULL,
    to_addr TEXT NOT NULL, body TEXT NOT NULL, sent_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS one_reply_per_message ON outbox (message_id) WHERE kind = 'reply';
CREATE TABLE IF NOT EXISTS inbound_messages (
    message_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, status TEXT NOT NULL,
    claimed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS idempotency (
    key TEXT PRIMARY KEY, result_json TEXT NOT NULL, created_at TEXT NOT NULL
);
"""


def _ts(t: dt.datetime) -> str:
    if t.tzinfo is None:
        raise ValueError("times must be timezone-aware")
    # One fixed format, so SQL string comparisons order times correctly.
    return t.astimezone(dt.UTC).isoformat(timespec="microseconds")


def _calendar(room: str) -> str:
    return "shared" if room in SHARED else room


@dataclass(frozen=True)
class WriteResult:
    decision: str  # ok | not_permitted | fall_back
    failed: list[str] = field(default_factory=list)
    message: str = ""
    ref: int | None = None  # the hold or tour id when written


@dataclass(frozen=True)
class Decision:
    status: str  # executed | pending | rejected | expired | failed
    changed: bool
    failed: list[str] = field(default_factory=list)
    ref: int | None = None


@dataclass(frozen=True)
class Claim:
    status: str  # claimed | done | failed | in_progress
    run_id: str


class _Conflict(Exception):
    """An index rejected a write the checks allowed: roll back and fall back."""


Record = Callable[[sqlite3.Connection, WriteResult], None]


class World:
    def __init__(self, path: str = ":memory:"):
        self.conn = sqlite3.connect(path, isolation_level=None, timeout=10, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        if path != ":memory:":
            self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self.autonomy: dict[str, int] = {}
        self.tool_failures: list[str] = []

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        self.conn.execute("COMMIT")

    # --- seeding ---------------------------------------------------------------------------

    def seed(self, spec: Any, now: dt.datetime) -> None:
        """Seed from an eval case's world (evals.schema.World)."""
        self.autonomy, self.tool_failures = dict(spec.autonomy), list(spec.tool_failures)
        with self._tx() as c:
            for date, rooms in spec.bookings.items():
                for room in rooms:
                    self._insert_booking(c, date, room, "booked", None)
            for date, rooms in spec.blocks.items():
                for room in rooms:
                    self._insert_booking(c, date, room, "blocked", None)
            for d in spec.high_demand:
                c.execute("INSERT OR IGNORE INTO high_demand_dates VALUES (?)", (d.isoformat(),))
            for h in spec.holds:
                expires = h.expires_at or now + dt.timedelta(hours=HOLD_HOURS)
                if expires.tzinfo is None:
                    expires = expires.replace(tzinfo=dt.UTC)
                self._insert_hold(c, h.date, h.room, h.customer_email, None, now, expires)
            for t in spec.tours:
                c.execute(
                    "INSERT INTO tours (date, time, customer_email, status) "
                    "VALUES (?, ?, ?, 'booked')",
                    (t.date.isoformat(), t.time, t.customer_email.strip().lower()),
                )

    def seed_default(self, now: dt.datetime) -> None:
        """Seed from venue/calendar_seed.yaml (for local runs)."""
        with self._tx() as c:
            for date, rooms in SEED["bookings"].items():
                for room in rooms:
                    self._insert_booking(c, date, room, "booked", None)
            for date, block in SEED["blocks"].items():
                for room in block["rooms"]:
                    self._insert_booking(c, date, room, "blocked", block.get("reason"))
            for d in SEED["high_demand_dates"]:
                c.execute("INSERT OR IGNORE INTO high_demand_dates VALUES (?)", (d.isoformat(),))

    @staticmethod
    def _insert_booking(c, date: dt.date, room: str, status: str, reason: str | None) -> None:
        c.execute(
            "INSERT INTO bookings (date, room, calendar, status, reason) VALUES (?, ?, ?, ?, ?)",
            (date.isoformat(), room, _calendar(room), status, reason),
        )

    @staticmethod
    def _insert_hold(c, date, room, email, name, now, expires) -> int:
        cur = c.execute(
            "INSERT INTO holds (date, room, calendar, customer_email, customer_name, created_at,"
            " expires_at, status) VALUES (?, ?, ?, ?, ?, ?, ?, 'active')",
            (
                date.isoformat(),
                room,
                _calendar(room),
                email.strip().lower(),
                name,
                _ts(now),
                _ts(expires),
            ),
        )
        return cur.lastrowid

    # --- reading ---------------------------------------------------------------------------

    def snapshot(self, now: dt.datetime, c: sqlite3.Connection | None = None) -> Snapshot:
        c = c or self.conn
        s = Snapshot(now=now)
        for r in c.execute("SELECT date, room, status FROM bookings"):
            target = s.bookings if r["status"] == "booked" else s.blocks
            target.setdefault(dt.date.fromisoformat(r["date"]), set()).add(r["room"])
        for r in c.execute("SELECT * FROM holds WHERE status = 'active'"):
            s.holds.append(
                Hold(
                    dt.date.fromisoformat(r["date"]),
                    r["room"],
                    r["customer_email"],
                    dt.datetime.fromisoformat(r["expires_at"]),
                )
            )
        for r in c.execute("SELECT * FROM tours WHERE status = 'booked'"):
            s.tours.append(Tour(dt.date.fromisoformat(r["date"]), r["time"], r["customer_email"]))
        return s

    def is_high_demand(self, date: dt.date) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM high_demand_dates WHERE date = ?", (date.isoformat(),)
        ).fetchone()
        return row is not None

    def hold_statuses(self) -> dict[str, int]:
        rows = self.conn.execute("SELECT status, COUNT(*) n FROM holds GROUP BY status")
        return {r["status"]: r["n"] for r in rows}

    def escalation_reasons(self) -> list[str]:
        return [
            r["reason"] for r in self.conn.execute("SELECT reason FROM escalations ORDER BY id")
        ]

    def outbox_count(self, message_id: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) n FROM outbox WHERE message_id = ?", (message_id,)
        ).fetchone()
        return row["n"]

    # --- writes ----------------------------------------------------------------------------

    @staticmethod
    def _expire(c: sqlite3.Connection, now: dt.datetime) -> None:
        c.execute(
            "UPDATE holds SET status = 'expired' WHERE status = 'active' AND expires_at <= ?",
            (_ts(now),),
        )

    def _write(
        self,
        key: str | None,
        record: Record | None,
        now: dt.datetime,
        body: Callable[[sqlite3.Connection], WriteResult],
    ) -> WriteResult:
        try:
            with self._tx() as c:
                if key:
                    row = c.execute(
                        "SELECT result_json FROM idempotency WHERE key = ?", (key,)
                    ).fetchone()
                    if row:
                        return WriteResult(**json.loads(row["result_json"]))
                self._expire(c, now)
                result = body(c)
                if key:
                    c.execute(
                        "INSERT INTO idempotency VALUES (?, ?, ?)",
                        (key, json.dumps(asdict(result)), _ts(now)),
                    )
                if record:
                    record(c, result)
                return result
        except _Conflict:
            return WriteResult("fall_back", ["conflict"], "A coordinator will review this request.")

    def hold(
        self,
        date: dt.date,
        room: str,
        customer_email: str,
        customer_name: str | None,
        *,
        now: dt.datetime,
        today: dt.date,
        key: str | None = None,
        record: Record | None = None,
    ) -> WriteResult:
        email = customer_email.strip().lower()

        def body(c: sqlite3.Connection) -> WriteResult:
            check = hold_check(date, room, email, self.snapshot(now, c), today)
            if check.decision != "ok":
                return WriteResult(check.decision, check.failed, check.message)
            expires = now + dt.timedelta(hours=HOLD_HOURS)
            try:
                ref = self._insert_hold(c, date, room, email, customer_name, now, expires)
            except sqlite3.IntegrityError as err:
                raise _Conflict from err
            return WriteResult("ok", ref=ref)

        return self._write(key, record, now, body)

    def unchecked_insert_hold(self, date: dt.date, room: str, email: str, now: dt.datetime) -> int:
        """Skips the checks on purpose. Exists only to prove the indexes hold on their own."""
        with self._tx() as c:
            return self._insert_hold(c, date, room, email, None, now, now + dt.timedelta(hours=48))

    def tour(
        self,
        date: dt.date,
        time: str,
        customer_email: str,
        customer_name: str | None,
        *,
        now: dt.datetime,
        today: dt.date,
        key: str | None = None,
        record: Record | None = None,
    ) -> WriteResult:
        email = customer_email.strip().lower()

        def body(c: sqlite3.Connection) -> WriteResult:
            check = tour_check(date, time, email, self.snapshot(now, c), today)
            if check.decision != "ok":
                return WriteResult(check.decision, check.failed, check.message)
            try:
                cur = c.execute(
                    "INSERT INTO tours (date, time, customer_email, customer_name, status) "
                    "VALUES (?, ?, ?, ?, 'booked')",
                    (date.isoformat(), time, email, customer_name),
                )
            except sqlite3.IntegrityError as err:
                raise _Conflict from err
            return WriteResult("ok", ref=cur.lastrowid)

        return self._write(key, record, now, body)

    def book(
        self, date: dt.date, room: str, now: dt.datetime, customer_email: str | None = None
    ) -> str:
        """A coordinator records a signed booking. Refused while someone else holds the date;
        the customer's own hold converts into the booking."""
        email = customer_email.strip().lower() if customer_email else None
        with self._tx() as c:
            self._expire(c, now)
            held = c.execute(
                "SELECT id, customer_email FROM holds WHERE status = 'active' AND date = ?"
                " AND calendar = ?",
                (date.isoformat(), _calendar(room)),
            ).fetchone()
            if held and held["customer_email"] != email:
                return "conflict_held"
            if held:
                c.execute("UPDATE holds SET status = 'converted' WHERE id = ?", (held["id"],))
            self._insert_booking(c, date, room, "booked", None)
            return "booked"

    def release_hold(self, hold_id: int) -> bool:
        """A compensating action (undo from the Review list). The original row stays."""
        with self._tx() as c:
            cur = c.execute(
                "UPDATE holds SET status = 'released' WHERE id = ? AND status = 'active'",
                (hold_id,),
            )
            return cur.rowcount == 1

    def cancel_tour(self, tour_id: int) -> bool:
        with self._tx() as c:
            cur = c.execute(
                "UPDATE tours SET status = 'cancelled' WHERE id = ? AND status = 'booked'",
                (tour_id,),
            )
            return cur.rowcount == 1

    # --- pending actions and approvals -----------------------------------------------------

    def create_pending(
        self,
        *,
        run_id: str,
        message_id: str,
        action: str,
        customer_email: str,
        customer_name: str | None,
        args: dict[str, Any],
        quotes: dict[str, Any],
        reason: str,
        failed_checks: list[str],
        now: dt.datetime,
    ) -> tuple[int, bool]:
        """One pending request per customer, action and date: a repeat returns the existing
        one. Returns (id, created)."""
        email = customer_email.strip().lower()
        with self._tx() as c:
            existing = c.execute(
                "SELECT id FROM pending_actions WHERE status = 'pending' AND customer_email = ?"
                " AND action = ? AND target_date IS ?",
                (email, action, args.get("date")),
            ).fetchone()
            if existing:
                return existing["id"], False
            cur = c.execute(
                "INSERT INTO pending_actions (run_id, message_id, action, customer_email,"
                " customer_name, target_date, args_json, quotes_json, reason,"
                " failed_checks_json, status, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
                (
                    run_id,
                    message_id,
                    action,
                    email,
                    customer_name,
                    args.get("date"),
                    json.dumps(args),
                    json.dumps(quotes),
                    reason,
                    json.dumps(failed_checks),
                    _ts(now),
                ),
            )
            return cur.lastrowid, True

    def decide(
        self,
        pending_id: int,
        decision: str,
        *,
        now: dt.datetime,
        edited_args: dict[str, Any] | None = None,
        note: str | None = None,
    ) -> Decision:
        """One decision transaction: expiry first (I8), then idempotency (I4), then the
        recheck and the write."""
        if decision not in ("approve", "edit", "reject"):
            raise ValueError(f"unknown decision: {decision}")
        if decision == "reject" and not (note or "").strip():
            raise ValueError("a reject needs a note")
        if decision == "edit" and not edited_args:
            raise ValueError("an edit needs edited args")
        with self._tx() as c:
            p = c.execute("SELECT * FROM pending_actions WHERE id = ?", (pending_id,)).fetchone()
            if p is None:
                raise KeyError(pending_id)
            if p["status"] != "pending":
                return Decision(
                    p["status"], False, json.loads(p["failed_checks_json"] or "[]"), p["result_ref"]
                )
            created = dt.datetime.fromisoformat(p["created_at"])
            if now >= created + dt.timedelta(hours=PENDING_HOURS):
                c.execute(
                    "UPDATE pending_actions SET status = 'expired', decided_at = ? WHERE id = ?",
                    (_ts(now), pending_id),
                )
                c.execute(
                    "INSERT INTO escalations (run_id, reason, category, created_at)"
                    " VALUES (?, 'pending_expired', ?, ?)",
                    (p["run_id"], p["action"], _ts(now)),
                )
                return Decision("expired", True)
            if decision == "reject":
                c.execute(
                    "UPDATE pending_actions SET status = 'rejected', decided_at = ?, note = ?"
                    " WHERE id = ?",
                    (_ts(now), note, pending_id),
                )
                return Decision("rejected", True)

            args = edited_args or json.loads(p["args_json"])
            date = dt.date.fromisoformat(str(args["date"]))
            today = local_today(now)
            self._expire(c, now)
            snapshot = self.snapshot(now, c)
            email = p["customer_email"]
            if p["action"] == "place_hold":
                check = hold_check(date, args["room"], email, snapshot, today)
                failed = check.failed
            elif p["action"] == "book_tour":
                slot = tour_slot(date, args["time"])
                check = tour_check(date, args["time"], email, snapshot, today)
                failed = ([] if slot.ok else [slot.reason]) + check.failed
            else:
                raise ValueError(f"no executor for {p['action']}")
            if failed:
                c.execute(
                    "UPDATE pending_actions SET failed_checks_json = ? WHERE id = ?",
                    (json.dumps(failed), pending_id),
                )
                return Decision("pending", False, failed)
            if p["action"] == "place_hold":
                expires = now + dt.timedelta(hours=HOLD_HOURS)
                ref = self._insert_hold(
                    c, date, args["room"], email, p["customer_name"], now, expires
                )
            else:
                ref = c.execute(
                    "INSERT INTO tours (date, time, customer_email, customer_name, status)"
                    " VALUES (?, ?, ?, ?, 'booked')",
                    (date.isoformat(), args["time"], email, p["customer_name"]),
                ).lastrowid
            c.execute(
                "UPDATE pending_actions SET status = 'executed', decided_at = ?, note = ?,"
                " args_json = ?, result_ref = ? WHERE id = ?",
                (_ts(now), note, json.dumps(args), ref, pending_id),
            )
            return Decision("executed", True, ref=ref)

    # --- inbound claim, outbox, escalations ------------------------------------------------

    def claim_inbound(self, message_id: str, run_id: str, now: dt.datetime) -> Claim:
        with self._tx() as c:
            row = c.execute(
                "SELECT * FROM inbound_messages WHERE message_id = ?", (message_id,)
            ).fetchone()
            if row is None:
                c.execute(
                    "INSERT INTO inbound_messages VALUES (?, ?, 'processing', ?)",
                    (message_id, run_id, _ts(now)),
                )
                return Claim("claimed", run_id)
            if row["status"] in ("done", "failed"):
                return Claim(row["status"], row["run_id"])
            if now - dt.datetime.fromisoformat(row["claimed_at"]) >= CLAIM_TIMEOUT:
                c.execute(
                    "UPDATE inbound_messages SET run_id = ?, claimed_at = ? WHERE message_id = ?",
                    (run_id, _ts(now), message_id),
                )
                return Claim("claimed", run_id)
            return Claim("in_progress", row["run_id"])

    def finish_inbound(self, message_id: str, status: str) -> None:
        with self._tx() as c:
            c.execute(
                "UPDATE inbound_messages SET status = ? WHERE message_id = ?", (status, message_id)
            )

    def send_reply(
        self,
        message_id: str,
        run_id: str,
        to: str,
        body: str,
        now: dt.datetime,
        kind: str = "reply",
    ) -> bool:
        """'Sent' means an outbox row exists. At most one reply per message ID."""
        try:
            with self._tx() as c:
                c.execute(
                    "INSERT INTO outbox (run_id, message_id, kind, to_addr, body, sent_at)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (run_id, message_id, kind, to, body, _ts(now)),
                )
            return True
        except sqlite3.IntegrityError:
            return False

    def escalate(self, run_id: str, reason: str, category: str | None, now: dt.datetime) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT INTO escalations (run_id, reason, category, created_at) VALUES (?,?,?,?)",
                (run_id, reason, category, _ts(now)),
            )

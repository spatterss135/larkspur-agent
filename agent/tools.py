"""The agent's tools (plan §7.3).

The @tool definitions are schemas only: they are what the model sees, and their bodies
raise. Every call runs through the AutonomyMiddleware (agent/middleware.py), which calls
`prepare` (input checks: evidence quotes, ranges, window, season) and then `run_read` or a
world write. Every customer-supplied value comes with a `*_quote`: the customer's exact
words, which code checks and parses (agent/evidence.py).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

from langchain_core.tools import tool

from agent import evidence
from agent.checks import availability, tour_slot
from agent.context import RunContext
from agent.pricing import SHEET, PricingError, published_rate, quote

ROOMS = {key: room["name"] for key, room in SHEET["rooms"].items()}
DAY_TYPES = {"mon_thu", "fri", "sat", "sun"}


class CalledDirectly(RuntimeError):
    pass


def _never(*_: Any, **__: Any) -> str:
    raise CalledDirectly("tools run only through the AutonomyMiddleware")


@tool
def search_venue_docs(query: str) -> str:
    """Search Larkspur Hall's documents (policies, rooms, packages, pricing overview, tours,
    holds, vendors, parking, accessibility, weather). Use it for any question about the
    venue's policies or facts. Returns sections with doc_id and section to cite."""
    return _never()


@tool
def check_availability(
    start_date: str, start_date_quote: list[str], room: str | None = None
) -> str:
    """Check whether a date is open, per room. start_date is ISO (YYYY-MM-DD).
    start_date_quote: the customer's exact words naming the date, copied from their message
    (e.g. ["Saturday, June 13"]). room: grand_hall, garden_terrace or library_room; omit to
    check every room. Returns, per room, available plus a reason: open, booked, blocked,
    held (by someone else), held_by_you, seasonal or outside_window."""
    return _never()


@tool
def get_published_rate(room: str, day_type: str, month: int) -> str:
    """Look up one room's published rental rate. day_type: mon_thu, fri, sat or sun.
    month: 1-12. Returns a record_id (e.g. R1): write {{rate:R1}} in the reply to show it.
    This is the rental only, never the cost of a whole event."""
    return _never()


@tool
def quote_package(
    room: str,
    date: str,
    date_quote: list[str],
    guests: int,
    guests_quote: list[str],
    package: str,
    package_quote: list[str],
    add_ons: list[str] | None = None,
    add_ons_quote: list[str] | None = None,
) -> str:
    """Price a full event: rental, food and beverage, any minimum top-up, add-ons, service
    fee and tax. package: classic or signature. add_ons: late_night_snack, terrace_ceremony,
    extra_hour. Every *_quote is the customer's exact words for that value. Returns a
    record_id (e.g. Q1): write {{quote:Q1}} in the reply to show the full breakdown."""
    return _never()


@tool
def book_tour(date: str, date_quote: list[str], time: str, time_quote: list[str]) -> str:
    """Request or book a venue tour. time is HH:MM, one of the listed tour slots. The quotes
    are the customer's exact words for the date and time."""
    return _never()


@tool
def place_hold(date: str, date_quote: list[str], room: str) -> str:
    """Request or place a 48-hour hold on a date and room. A hold is never a booking.
    date_quote is the customer's exact words for the date."""
    return _never()


@tool
def escalate(reason: str, category: str) -> str:
    """Hand this email to a coordinator. Use it for anything you can't or shouldn't decide:
    payments, contracts, cancellations, complaints, discounts, custom requests, policies the
    documents don't cover. category: payment, contract, change, complaint, discount,
    custom, policy_gap or other."""
    return _never()


TOOLS = [
    search_venue_docs,
    check_availability,
    get_published_rate,
    quote_package,
    book_tour,
    place_hold,
    escalate,
]
TOOL_NAMES = {t.name for t in TOOLS}


@dataclass
class Prepared:
    status: str  # ok | needs_input | not_permitted
    values: dict[str, Any] = field(default_factory=dict)
    quotes: dict[str, list[str]] = field(default_factory=dict)
    reason: str = ""
    message: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "ok"


class _Stop(Exception):
    def __init__(self, prepared: Prepared):
        self.prepared = prepared


def _room(value: Any, required: bool = True) -> str | None:
    if value in (None, "") and not required:
        return None
    key = str(value or "").strip().lower().replace(" ", "_").removeprefix("the_")
    if key not in ROOMS:
        names = ", ".join(ROOMS.values())
        raise _Stop(Prepared("needs_input", reason="unknown_room", message=f"Rooms: {names}."))
    return key


def _from(ev: evidence.Evidence, given: Any, label: str, normalize=lambda v: v) -> Any:
    if ev.status != "ok":
        raise _Stop(Prepared(ev.status, reason=ev.reason, message=ev.message))
    if given not in (None, "", []) and normalize(given) != normalize(ev.value):
        raise _Stop(
            Prepared(
                "needs_input",
                reason="value_mismatch",
                message=f"The customer's words give {label} {ev.value!r}, not {given!r}. "
                "Use what the customer wrote, or ask.",
            )
        )
    return ev.value


def _iso(v: Any) -> str:
    return v.isoformat() if isinstance(v, dt.date) else str(v)


def prepare(name: str, args: dict[str, Any], ctx: RunContext) -> Prepared:
    """Input checks only. State checks (is it held, is the slot free) happen in world.py."""
    texts, today = ctx.customer_texts, ctx.today
    q = {k: list(v or []) for k, v in args.items() if k.endswith("_quote")}

    def date_of(key: str) -> dt.date:
        ev = evidence.date_from(q.get(f"{key}_quote", []), texts, today)
        return _from(ev, args.get(key), "the date", _iso)

    try:
        if name == "search_venue_docs":
            return Prepared("ok", {"query": str(args.get("query", ""))})
        if name == "escalate":
            return Prepared(
                "ok", {"reason": str(args.get("reason", "")), "category": args.get("category")}
            )
        if name == "check_availability":
            values = {"start_date": date_of("start_date").isoformat()}
            room = _room(args.get("room"), required=False)
            if room:
                values["room"] = room
            return Prepared("ok", values, q)
        if name == "get_published_rate":
            day = str(args.get("day_type", "")).lower()
            if day not in DAY_TYPES:
                return Prepared("needs_input", reason="unknown_day_type", message=str(DAY_TYPES))
            return Prepared(
                "ok",
                {"room": _room(args.get("room")), "day_type": day, "month": int(args["month"])},
            )
        if name == "quote_package":
            guests = _from(
                evidence.guests_from(q.get("guests_quote", []), texts),
                args.get("guests"),
                "guests",
                lambda v: int(v),
            )
            package = _from(
                evidence.package_from(q.get("package_quote", []), texts),
                args.get("package"),
                "the package",
                lambda v: str(v).lower(),
            )
            add_ons = _from(
                evidence.add_ons_from(q.get("add_ons_quote", []), texts),
                args.get("add_ons") or [],
                "add-ons",
                lambda v: sorted(v),
            )
            values = {
                "room": _room(args.get("room")),
                "date": date_of("date").isoformat(),
                "guests": guests,
                "package": package,
                "add_ons": add_ons,
            }
            return Prepared("ok", values, q)
        if name == "book_tour":
            date = date_of("date")
            time = _from(
                evidence.time_from(q.get("time_quote", []), texts), args.get("time"), "the time"
            )
            slot = tour_slot(date, time)
            if not slot.ok:
                return Prepared("needs_input", reason=slot.reason, message=slot.message)
            return Prepared("ok", {"date": date.isoformat(), "time": time}, q)
        if name == "place_hold":
            return Prepared(
                "ok", {"date": date_of("date").isoformat(), "room": _room(args.get("room"))}, q
            )
    except _Stop as stop:
        stop.prepared.quotes = q
        return stop.prepared
    except (KeyError, ValueError, TypeError) as err:
        return Prepared("needs_input", quotes=q, reason="bad_arguments", message=str(err))
    return Prepared("not_permitted", reason="unknown_tool", message=f"No tool named {name}.")


def run_read(name: str, values: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
    """Execute a read tool. Returns the tool's result (status executed or not_permitted)."""
    if name == "search_venue_docs":
        chunks = [c.as_dict() for c in ctx.retriever.search(values["query"], k=4)]
        ctx.retrieved.extend(chunks)
        return {"status": "executed", "chunks": chunks}
    if name == "check_availability":
        date = dt.date.fromisoformat(values["start_date"])
        snapshot = ctx.world.snapshot(ctx.now)
        rooms = [values["room"]] if values.get("room") else list(ROOMS)
        results = []
        for room in rooms:
            a = availability(date, room, ctx.sender_email, snapshot, ctx.today)
            results.append(
                {"room": room, "available": a.available, "reason": a.reason, "note": a.message}
            )
        return {"status": "executed", "date": values["start_date"], "results": results}
    try:
        if name == "get_published_rate":
            rate = published_rate(values["room"], values["day_type"], values["month"])
            return _record(ctx, "R", "rate", values, rate.as_dict(), "rate")
        if name == "quote_package":
            date = dt.date.fromisoformat(values["date"])
            q = quote(values["room"], date, values["guests"], values["package"], values["add_ons"])
            return _record(ctx, "Q", "package", values, q.as_dict(), "breakdown")
    except PricingError as err:
        return {"status": "not_permitted", "reason": err.reason, "message": str(err)}
    raise ValueError(f"{name} is not a read tool")


def _record(
    ctx: RunContext, prefix: str, kind: str, values: dict, breakdown: dict, key: str
) -> dict[str, Any]:
    from agent.records import insert_price_record

    record_id = ctx.next_record_id(prefix)
    record = {"kind": kind, "inputs": values, "breakdown": breakdown}
    ctx.price_records[record_id] = record
    insert_price_record(ctx.world.conn, ctx.run_id, record_id, record, ctx.now)
    placeholder = "{{" + ("rate" if prefix == "R" else "quote") + ":" + record_id + "}}"
    return {
        "status": "executed",
        "record_id": record_id,
        key: breakdown,
        "message": f"Write {placeholder} in the reply to show this price; never type it.",
    }

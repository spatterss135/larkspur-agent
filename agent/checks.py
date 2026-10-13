"""Input and state checks (plan §6.3-6.4, §7.3). Pure: functions over a snapshot.

world.py takes the snapshot inside its write transaction and runs these checks there, so
the rules are tested without a database and enforced where the write happens.

- The Grand Hall and Garden Terrace share one calendar: a booking, block or active hold on
  either makes both unavailable to anyone else that day. The Library Room is independent.
- "Held" is never stored on the calendar; it's an active hold whose expiry hasn't passed.
- Booked or blocked can't be changed by anyone, so a hold there is not_permitted. Someone
  else's hold, or the customer already holding a date, is a coordinator's call: fall_back.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from agent.dates import in_window
from agent.pricing import SHEET

SEED = yaml.safe_load(
    (Path(__file__).resolve().parents[1] / "venue" / "calendar_seed.yaml").read_text(
        encoding="utf-8"
    )
)
SHARED = frozenset(SEED["shared_calendar"])
TOUR_DAYS = SEED["tours"]["days"]  # ["tue", ...]
SLOTS = SEED["tours"]["slots"]  # ["10:00", ...]
_DAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


@dataclass(frozen=True)
class Hold:
    date: dt.date
    room: str
    customer_email: str
    expires_at: dt.datetime


@dataclass(frozen=True)
class Tour:
    date: dt.date
    time: str
    customer_email: str


@dataclass
class Snapshot:
    """The world as one transaction sees it. Tours are booked (not cancelled) ones."""

    now: dt.datetime
    bookings: dict[dt.date, set[str]] = field(default_factory=dict)
    blocks: dict[dt.date, set[str]] = field(default_factory=dict)
    holds: list[Hold] = field(default_factory=list)
    tours: list[Tour] = field(default_factory=list)

    def active_holds(self) -> list[Hold]:
        return [h for h in self.holds if h.expires_at > self.now]


@dataclass(frozen=True)
class Availability:
    available: bool
    reason: str  # open | booked | blocked | held | held_by_you | seasonal | outside_window
    message: str = ""


@dataclass(frozen=True)
class Check:
    decision: str  # ok | not_permitted | fall_back
    failed: list[str] = field(default_factory=list)
    message: str = ""


def _calendar(room: str) -> set[str]:
    if room not in SHEET["rooms"]:
        raise ValueError(f"unknown room: {room}")
    return set(SHARED) if room in SHARED else {room}


def _name(room: str) -> str:
    return SHEET["rooms"][room]["name"]


def availability(
    date: dt.date, room: str, customer_email: str, s: Snapshot, today: dt.date
) -> Availability:
    rooms = _calendar(room)
    window = in_window(date, today)
    if window.status != "ok":
        return Availability(False, "outside_window", window.message)
    if date.month not in SHEET["rooms"][room]["open_months"]:
        return Availability(False, "seasonal", f"The {_name(room)} is open May through October.")
    if s.bookings.get(date, set()) & rooms:
        return Availability(False, "booked", "That date is already booked.")
    if s.blocks.get(date, set()) & rooms:
        return Availability(False, "blocked", "The venue isn't available that date.")
    for h in s.active_holds():
        if h.date == date and h.room in rooms:
            if h.customer_email == customer_email:
                return Availability(False, "held_by_you", "You already hold this date.")
            return Availability(False, "held", "Another couple has a hold on that date.")
    return Availability(True, "open")


def hold_check(date: dt.date, room: str, customer_email: str, s: Snapshot, today: dt.date) -> Check:
    a = availability(date, room, customer_email, s, today)
    if a.reason in ("outside_window", "seasonal", "booked", "blocked"):
        return Check("not_permitted", [a.reason], a.message)
    failed = []
    if a.reason == "held":
        failed.append("held_by_someone_else")
    if any(h.customer_email == customer_email for h in s.active_holds()):
        failed.append("customer_has_active_hold")
    if failed:
        return Check("fall_back", failed, "A coordinator will review this hold request.")
    return Check("ok")


@dataclass(frozen=True)
class SlotCheck:
    ok: bool
    reason: str = ""
    message: str = ""


def tour_slot(date: dt.date, time: str) -> SlotCheck:
    offer = f"Tours run Tuesday through Saturday, starting at {', '.join(SLOTS)}."
    if _DAY_NAMES[date.weekday()] not in TOUR_DAYS:
        return SlotCheck(False, "no_tours_that_day", offer)
    if time not in SLOTS:
        return SlotCheck(False, "not_a_slot", offer)
    return SlotCheck(True)


def tour_check(date: dt.date, time: str, customer_email: str, s: Snapshot, today: dt.date) -> Check:
    failed = []
    if any(t.date == date and t.time == time for t in s.tours):
        failed.append("slot_taken")
    if any(t.customer_email == customer_email and t.date >= today for t in s.tours):
        failed.append("customer_has_upcoming_tour")
    if failed:
        return Check("fall_back", failed, "A coordinator will confirm the tour time.")
    return Check("ok")

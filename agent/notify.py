"""Templated messages (plan §7.7): built from the database, never by an LLM, so a follow-up
can't misstate what happened. Times are shown in the venue's timezone."""

from __future__ import annotations

import datetime as dt

from agent.dates import TIMEZONE
from agent.pricing import SHEET

SIGNATURE = "The Larkspur Hall events team"


def _day(d: dt.date) -> str:
    return f"{d:%A}, {d:%B} {d.day}"


def _clock(t: dt.datetime | str) -> str:
    if isinstance(t, str):
        h, m = (int(x) for x in t.split(":"))
        t = dt.datetime(2000, 1, 1, h, m)
    return f"{t.hour % 12 or 12}:{t.minute:02d} {'am' if t.hour < 12 else 'pm'}"


def _hi(name: str | None) -> str:
    return f"Hi {name}," if name else "Hello,"


def hold_placed(name: str | None, date: dt.date, room: str, expires_at: dt.datetime) -> str:
    local = expires_at.astimezone(TIMEZONE)
    return (
        f"{_hi(name)}\n\nYour hold on {_day(date)} ({SHEET['rooms'][room]['name']}) is in place "
        f"until {_day(local.date())} at {_clock(local)}. It's a hold, not a booking: when it "
        f"ends, the date becomes available to others again. A coordinator can walk you through "
        f"booking whenever you're ready.\n\n{SIGNATURE}"
    )


def tour_booked(name: str | None, date: dt.date, time: str) -> str:
    return (
        f"{_hi(name)}\n\nYour tour of Larkspur Hall is booked for {_day(date)} at "
        f"{_clock(time)}. We look forward to showing you around.\n\n{SIGNATURE}"
    )


def rejected(name: str | None, note: str) -> str:
    return f"{_hi(name)}\n\nThank you for your patience. {note.strip()}\n\n{SIGNATURE}"


def complaint_ack(name: str | None) -> str:
    return (
        f"{_hi(name)}\n\nThanks for letting us know. I've passed this to our coordinator, who "
        f"will reach out to you personally.\n\n{SIGNATURE}"
    )

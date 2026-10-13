"""Date resolution with an injected clock (plan §6.3). Pure: no model, no I/O.

Dates come only from the customer's own words. A date with no year means its next
occurrence on or after today. Anything unclear (a day without a month, a weekday that
contradicts the date, two different dates) is needs_input, so the agent asks instead of
guessing. Dates outside the booking window (tomorrow through 24 months) are not_permitted.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from typing import Literal
from zoneinfo import ZoneInfo

TIMEZONE = ZoneInfo("America/Denver")
WINDOW_MONTHS = 24

MONTHS = {
    m: i
    for i, names in enumerate(
        [
            ("january", "jan"),
            ("february", "feb"),
            ("march", "mar"),
            ("april", "apr"),
            ("may",),
            ("june", "jun"),
            ("july", "jul"),
            ("august", "aug"),
            ("september", "sept", "sep"),
            ("october", "oct"),
            ("november", "nov"),
            ("december", "dec"),
        ],
        start=1,
    )
    for m in names
}
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_MONTH = "|".join(sorted(MONTHS, key=len, reverse=True))
_WEEKDAY = "|".join(WEEKDAYS)

ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
NAMED = re.compile(
    rf"\b(?:({_WEEKDAY}),?\s+)?({_MONTH})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?\b"
    r"(\s*[-–]\s*\d{1,2}(?:st|nd|rd|th)?\b)?"  # a range: "June 13-14"
    r"(?:,?\s+(\d{4})\b)?"
)
NUMERIC = re.compile(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2}|\d{4}))?\b")
RELATIVE = re.compile(rf"\b(?:(?:next|this|coming)\s+({_WEEKDAY})|(today|tomorrow))\b")
DAY_ONLY = re.compile(rf"\b(?:({_WEEKDAY})\s+)?the\s+(\d{{1,2}})(?:st|nd|rd|th)\b")
MONTH_WORD = re.compile(rf"\b({_MONTH})\b", re.I)


def _month_word(raw: str) -> re.Match[str] | None:
    """A bare month name. "may" counts only as "May": lowercase, it's usually the verb."""
    for m in MONTH_WORD.finditer(raw):
        if m[1].lower() != "may" or m[1] == "May":
            return m
    return None


Status = Literal["ok", "needs_input", "not_permitted"]


@dataclass(frozen=True)
class DateResult:
    status: Status
    date: dt.date | None = None
    reason: str = ""  # a stable code; see the tests for the full list
    message: str = ""  # a sentence the agent can relay


def local_today(now: dt.datetime) -> dt.date:
    """The venue's date for an aware datetime."""
    return now.astimezone(TIMEZONE).date()


def window_end(today: dt.date) -> dt.date:
    year = today.year + WINDOW_MONTHS // 12
    try:
        return today.replace(year=year)
    except ValueError:  # February 29
        return today.replace(year=year, day=28)


def _pretty(d: dt.date) -> str:
    return f"{d:%A}, {d:%B} {d.day}, {d.year}"


def _next_occurrence(month: int, day: int, today: dt.date) -> dt.date | None:
    for year in (today.year, today.year + 1):
        try:
            d = dt.date(year, month, day)
        except ValueError:
            continue
        if d >= today:
            return d
    return None


def _weekday_after(name: str, today: dt.date) -> dt.date:
    ahead = (WEEKDAYS.index(name) - today.weekday() - 1) % 7 + 1
    return today + dt.timedelta(days=ahead)


def _blank(text: str, m: re.Match[str]) -> str:
    return text[: m.start()] + " " * (m.end() - m.start()) + text[m.end() :]


def resolve_date(fragments: list[str], today: dt.date) -> DateResult:
    """Resolve the one date named across the customer's quoted fragments."""
    raw = " ".join(fragments)  # original case, used only to tell "May" from "may"
    text = raw.lower()
    found: list[dt.date] = []
    problems: list[DateResult] = []

    def consume(m: re.Match[str]) -> None:
        nonlocal text, raw
        text, raw = _blank(text, m), _blank(raw, m)

    def explicit(year: int | None, month: int, day: int, weekday: str | None) -> None:
        try:
            d = dt.date(year, month, day) if year else _next_occurrence(month, day, today)
        except ValueError:
            d = None
        if d is None:
            problems.append(
                DateResult("needs_input", reason="invalid_date", message="That date doesn't exist.")
            )
            return
        if weekday and WEEKDAYS[d.weekday()] != weekday:
            problems.append(
                DateResult(
                    "needs_input",
                    d,
                    "weekday_mismatch",
                    f"{d:%B} {d.day}, {d.year} is a {d:%A}, not a {weekday.title()}.",
                )
            )
            return
        found.append(d)

    for m in ISO.finditer(text):
        explicit(int(m[1]), int(m[2]), int(m[3]), None)
        consume(m)
    for m in NAMED.finditer(text):
        if m[4]:  # "June 13-14": a multi-day event or a choice of days; ask
            problems.append(
                DateResult(
                    "needs_input",
                    reason="ambiguous",
                    message="Is that a one-day event? If so, which day?",
                )
            )
        else:
            explicit(int(m[5]) if m[5] else None, MONTHS[m[2]], int(m[3]), m[1])
        consume(m)
    for m in NUMERIC.finditer(text):
        year = int(m[3]) if m[3] else None
        if year is not None and year < 100:
            year += 2000
        explicit(year, int(m[1]), int(m[2]), None)
        consume(m)
    for m in RELATIVE.finditer(text):
        if m[2]:
            found.append(today + dt.timedelta(days=0 if m[2] == "today" else 1))
        else:
            found.append(_weekday_after(m[1], today))
        consume(m)
    day_only = DAY_ONLY.search(text)
    if day_only:
        month = _month_word(raw)
        if month:
            explicit(None, MONTHS[month[1].lower()], int(day_only[2]), day_only[1])

    if problems:
        return problems[0]
    dates = sorted(set(found))
    if len(dates) > 1:
        named = " and ".join(_pretty(d) for d in dates)
        return DateResult("needs_input", reason="ambiguous", message=f"Which date: {named}?")
    if not dates:
        if day_only:
            return DateResult(
                "needs_input",
                reason="day_without_month",
                message=f"Which month do you mean for the {day_only[0].split()[-1]}?",
            )
        return DateResult(
            "needs_input", reason="no_date", message="Which date do you have in mind?"
        )
    return in_window(dates[0], today)


def in_window(d: dt.date, today: dt.date) -> DateResult:
    if d < today:
        return DateResult("not_permitted", d, "past", f"{_pretty(d)} has already passed.")
    if d == today:
        return DateResult(
            "not_permitted", d, "too_soon", "We can't arrange anything for today; please call us."
        )
    end = window_end(today)
    if d > end:
        return DateResult(
            "not_permitted",
            d,
            "beyond_window",
            f"We take bookings up to 24 months out, through {_pretty(end)}.",
        )
    return DateResult("ok", d)

"""Substantive-edit detection (plan §7.7). Pure: no model.

An approval only counts as evidence the agent was right if the coordinator didn't change
anything that matters. Code decides when it can: a changed structured field (date, room,
guests, package, add-ons, time), or a changed money amount, date, time or number in the
text, is substantive. A wording-only edit returns None, and the reviewer tags it.
"""

from __future__ import annotations

import datetime as dt
import re
from collections import Counter
from decimal import Decimal
from typing import Any

from agent.claims import DATE_MENTION, MONEY, MONTHS, PERCENT

TIME = re.compile(r"\b(\d{1,2}):(\d{2})\b")
NUMBER = re.compile(r"\b\d[\d,]*\b")


def _norm(v: Any) -> Any:
    if isinstance(v, dt.date):
        return v.isoformat()
    if isinstance(v, (list, tuple, set)):
        return sorted(str(_norm(x)) for x in v)
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return str(int(v)) if float(v).is_integer() else str(v)
    s = str(v).strip().lower()
    return str(int(s)) if s.isdigit() else s


def changed_fields(draft: dict[str, Any], final: dict[str, Any]) -> list[str]:
    keys = sorted(set(draft) | set(final))
    return [k for k in keys if _norm(draft.get(k)) != _norm(final.get(k))]


def _tokens(text: str) -> Counter:
    found: Counter = Counter()

    def take(pattern: re.Pattern[str], key) -> None:
        nonlocal text
        for m in pattern.finditer(text):
            found[key(m)] += 1
        text = pattern.sub(" ", text)

    take(MONEY, lambda m: ("$", Decimal(m[1].replace(",", "")) * (1000 if m[2] else 1)))
    take(PERCENT, lambda m: ("%", Decimal(m[1])))
    take(DATE_MENTION, lambda m: ("date", MONTHS[m[1].lower()], int(m[2])))
    take(TIME, lambda m: ("time", int(m[1]), int(m[2])))
    take(NUMBER, lambda m: ("n", int(m[0].replace(",", ""))))
    return found


def text_changed(draft: str, final: str) -> bool:
    """True if any money amount, percentage, date, time or number differs."""
    return _tokens(draft) != _tokens(final)


def substantive_edit(
    draft_args: dict[str, Any],
    final_args: dict[str, Any],
    draft_text: str | None = None,
    final_text: str | None = None,
) -> bool | None:
    """True: substantive. False: nothing changed. None: wording only; the reviewer decides."""
    if changed_fields(draft_args, final_args):
        return True
    if draft_text is None or final_text is None or draft_text == final_text:
        return False
    return True if text_changed(draft_text, final_text) else None

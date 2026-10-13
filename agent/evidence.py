"""Evidence quotes (plan §7.4). Pure: no model, no I/O.

Every customer-supplied tool input comes with fragments of the customer's exact words. A
value is accepted only if (1) every fragment appears in the customer's own messages
(subject lines included; the caller never passes agent messages), and (2) the value parses
from those fragments. Anything else is needs_input, and the agent asks. No LLM ever
extracts these values: this is the guard against silently assuming "100 guests".
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from typing import Any

from agent.dates import DAY_ONLY, ISO, NAMED, NUMERIC, resolve_date
from agent.pricing import SHEET


@dataclass(frozen=True)
class Evidence:
    status: str  # ok | needs_input | not_permitted
    value: Any = None
    reason: str = ""
    message: str = ""  # tells the agent what to ask or say


def normalize(text: str) -> str:
    text = text.replace("’", "'").replace("‘", "'")
    text = text.replace("“", '"').replace("”", '"')
    return " ".join(text.lower().split())


def quotes_missing(fragments: list[str], customer_texts: list[str]) -> list[str]:
    hay = [normalize(t) for t in customer_texts]
    return [f for f in fragments if not any(normalize(f) in h for h in hay)]


def _verify(fragments: list[str], customer_texts: list[str]) -> Evidence | None:
    if not [f for f in fragments if f.strip()]:
        return Evidence(
            "needs_input", reason="no_quote", message="No quote given; ask the customer for this."
        )
    missing = quotes_missing(fragments, customer_texts)
    if missing:
        return Evidence(
            "needs_input",
            reason="quote_not_found",
            message=f"The customer's messages don't contain {missing}; ask them instead.",
        )
    return None


# --- numbers -------------------------------------------------------------------------------

UNITS = {
    w: i
    for i, w in enumerate(
        "zero one two three four five six seven eight nine ten eleven twelve thirteen "
        "fourteen fifteen sixteen seventeen eighteen nineteen".split()
    )
}
TENS = {
    w: 10 * i
    for i, w in enumerate("twenty thirty forty fifty sixty seventy eighty ninety".split(), 2)
}
SCALES = {"hundred": 100, "thousand": 1000}
NUMBER_WORD = set(UNITS) | set(TENS) | set(SCALES)
DIGITS = re.compile(r"\b\d{1,3}(?:,\d{3})+\b|\b\d+\b")
PERSON_WORD = re.compile(
    r"^\s*(?:guests?|people|persons?|ppl|pax|attendees|adults?|heads|kids?|children|child"
    r"|infants?|babies|baby|seniors?|teens?)\b"
)
NEXT_WORD = re.compile(r"^\s*([a-z]+)")
# Words that can follow a count without saying what it counts ("150 or so", "abt 90 max").
FILLER = set("or so and maybe please total ish max tops for with at in on now of to plus".split())
RANGE = re.compile(
    r"\d\s*(?:-|\u2013|to)\s*\d|\bbetween\b.+\band\b|\bor\s+(?:so\s+)?\d|\d\s*\+", re.I
)
SCALED_DIGITS = re.compile(r"\b(\d+)\s+(hundred|thousand)\b")


def _words_to_numbers(text: str) -> list[tuple[int, int]]:
    """(value, end offset) for each run of number words: "a hundred and twenty" -> 120."""
    found, tokens = [], list(re.finditer(r"[a-z]+", text))
    i = 0
    while i < len(tokens):
        word = tokens[i][0]
        nxt = tokens[i + 1][0] if i + 1 < len(tokens) else ""
        if word not in NUMBER_WORD and not (word == "a" and nxt in SCALES):
            i += 1
            continue
        total = current = 0
        end = tokens[i].end()
        while i < len(tokens):
            w = tokens[i][0]
            nxt = tokens[i + 1][0] if i + 1 < len(tokens) else ""
            if w == "a" and nxt in SCALES:
                current = max(current, 1)
            elif w in UNITS:
                current += UNITS[w]
            elif w in TENS:
                current += TENS[w]
            elif w in SCALES:
                current = max(current, 1) * SCALES[w]
                if w == "thousand":
                    total, current = total + current, 0
            elif w == "and" and nxt in NUMBER_WORD:
                pass
            else:
                break
            end = tokens[i].end()
            i += 1
        found.append((total + current, end))
    return found


def guests_from(fragments: list[str], customer_texts: list[str]) -> Evidence:
    bad = _verify(fragments, customer_texts)
    if bad:
        return bad
    text = normalize(" ".join(fragments))
    text = SCALED_DIGITS.sub(lambda m: str(int(m[1]) * SCALES[m[2]]), text)
    for pattern in (ISO, NAMED, NUMERIC, DAY_ONLY):  # a date's numbers aren't counts
        text = pattern.sub(lambda m: " " * len(m[0]), text)
    if RANGE.search(text):
        return Evidence(
            "needs_input",
            reason="range",
            message="The customer gave a range; ask for their best estimate of the guest count.",
        )
    numbers = [(int(m[0].replace(",", "")), m.end()) for m in DIGITS.finditer(text)]
    numbers += _words_to_numbers(text)
    if not numbers:
        return Evidence("needs_input", reason="no_number", message="Ask how many guests.")
    people, bare = set(), set()
    for n, end in numbers:
        after = text[end:]
        if PERSON_WORD.match(after):
            people.add(n)
        else:
            word = NEXT_WORD.match(after)
            if not word or word[1] in FILLER:
                bare.add(n)
            # otherwise it counts something else ("12 tables") and is ignored
    if len(people) == 1 and not bare:
        return Evidence("ok", people.pop())
    if not people and len(bare) == 1:
        return Evidence("ok", bare.pop())
    return Evidence(
        "needs_input",
        reason="ambiguous",
        message="It isn't clear what the total guest count is; ask for one number.",
    )


# --- dates, packages, add-ons ----------------------------------------------------------------


def date_from(fragments: list[str], customer_texts: list[str], today: dt.date) -> Evidence:
    bad = _verify(fragments, customer_texts)
    if bad:
        return bad
    r = resolve_date(fragments, today)
    return Evidence(r.status, r.date, r.reason, r.message)


def _aliases(table: dict[str, Any]) -> dict[str, re.Pattern[str]]:
    return {
        # "s?" so plurals match: "snacks", "extra hours"
        key: re.compile(r"\b(?:" + "|".join(re.escape(a) for a in spec["aliases"]) + r")s?\b")
        for key, spec in table.items()
    }


PACKAGES = _aliases(SHEET["packages"])
NO_ADD_ONS = re.compile(r"\b(?:no|without)\s+(?:add-?ons?|extras)\b|^\s*none\b")
ADD_ONS = _aliases(SHEET["add_ons"])


def package_from(fragments: list[str], customer_texts: list[str]) -> Evidence:
    bad = _verify(fragments, customer_texts)
    if bad:
        return bad
    text = normalize(" ".join(fragments))
    hits = sorted(k for k, pattern in PACKAGES.items() if pattern.search(text))
    if not hits:
        names = " or ".join(p["name"] for p in SHEET["packages"].values())
        return Evidence(
            "needs_input", reason="unknown_package", message=f"Our packages are {names}."
        )
    if len(hits) > 1:
        return Evidence("needs_input", reason="ambiguous", message="Ask which package they want.")
    return Evidence("ok", hits[0])


def add_ons_from(fragments: list[str], customer_texts: list[str]) -> Evidence:
    """Add-ons are optional: no fragments means none were requested."""
    if not fragments:
        return Evidence("ok", [])
    bad = _verify(fragments, customer_texts)
    if bad:
        return bad
    chosen = set()
    for f in fragments:
        if NO_ADD_ONS.search(normalize(f)):
            continue
        hits = {k for k, pattern in ADD_ONS.items() if pattern.search(normalize(f))}
        if not hits:
            names = ", ".join(a["name"] for a in SHEET["add_ons"].values())
            return Evidence(
                "needs_input",
                reason="unknown_add_on",
                message=f"{f!r} isn't an add-on we offer. Ours are: {names}.",
            )
        chosen |= hits
    return Evidence("ok", sorted(chosen))


# --- tour times ------------------------------------------------------------------------------

CLOCK = re.compile(r"\b(\d{1,2}):(\d{2})\s*([ap])?\.?m?\.?(?![a-z])")
HOUR_MERIDIEM = re.compile(r"\b(\d{1,2})\s*([ap])\.?m\.?(?![a-z])")
BARE_HOUR = re.compile(r"\b(?:at|around|about|by)\s+(\d{1,2})\b(?!:)")
NOON = re.compile(r"\bnoon\b")


def _tour_hour(hour: int) -> int | None:
    """A time with no am/pm, read within tour hours: 10-11 am, 12 noon, 1-3 pm."""
    if hour in (10, 11, 12) or 13 <= hour <= 23:
        return hour
    if 1 <= hour <= 3:
        return hour + 12
    return None


def time_from(fragments: list[str], customer_texts: list[str]) -> Evidence:
    bad = _verify(fragments, customer_texts)
    if bad:
        return bad
    text = normalize(" ".join(fragments))
    times, unclear = set(), False

    def add(hour: int | None, minute: int, meridiem: str | None) -> None:
        nonlocal unclear
        if meridiem == "p" and hour is not None and hour < 12:
            hour += 12
        elif meridiem == "a" and hour == 12:
            hour = 0
        elif meridiem is None and hour is not None:
            hour = _tour_hour(hour)
        if hour is None or not 0 <= hour <= 23 or not 0 <= minute <= 59:
            unclear = True
        else:
            times.add(f"{hour:02d}:{minute:02d}")

    for m in CLOCK.finditer(text):
        add(int(m[1]), int(m[2]), m[3])
        text = text[: m.start()] + " " * len(m[0]) + text[m.end() :]
    for m in HOUR_MERIDIEM.finditer(text):
        add(int(m[1]), 0, m[2])
        text = text[: m.start()] + " " * len(m[0]) + text[m.end() :]
    for m in BARE_HOUR.finditer(text):
        add(int(m[1]), 0, None)
    if NOON.search(text):
        times.add("12:00")

    if unclear or len(times) > 1:
        return Evidence("needs_input", reason="ambiguous", message="Ask which tour time they want.")
    if not times:
        return Evidence("needs_input", reason="no_time", message="Ask which tour time they want.")
    return Evidence("ok", times.pop())

"""Claim guards and number checks for a drafted reply. Pure: no model, no I/O.

Used twice: at runtime the reply gate sends any reply a guard fires on to a person
(Level 1), and in evals the same functions grade the agent's draft. A keyword check is
a poor grader but a fine router, since a false positive only costs one human review.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

PLACEHOLDER = re.compile(r"\{\{(rate|quote):([A-Za-z0-9_-]+)\}\}")
PLACEHOLDER_TOOL = {"rate": "get_published_rate", "quote": "quote_package"}
MONEY = re.compile(r"\$\s?(\d[\d,]*(?:\.\d{1,2})?)(\s?[kK]\b)?")
PERCENT = re.compile(r"(\d+(?:\.\d+)?)\s?%")
SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
TOTAL_WORDS = re.compile(r"\b(total|all-in|altogether|comes to|grand total)\b", re.I)

MONTH_NAMES = (
    "january february march april may june july august september october november december"
).split()
MONTHS = {m: i for i, m in enumerate(MONTH_NAMES, start=1)}
DATE_MENTION = re.compile(r"\b(" + "|".join(MONTHS) + r")\s+(\d{1,2})(?:st|nd|rd|th)?\b", re.I)

# Phrasings that use claim words without making the claim. A short allowlist on purpose:
# anything not on it triggers, and a false positive only sends a reply to a person.
SAFE = re.compile(
    r"\b(?:not|isn't|is not|aren't|are not|hasn't been|has not been|haven't been|not yet)"
    r"\s+(?:yet\s+)?(?:booked|confirmed|reserved)\b"
    r"|\balready booked\b"
    r"|\b(?:until|once|after)\b[^.!?]{0,40}\bconfirm",
    re.I,
)
BOOKING = re.compile(r"\b(booked|confirmed|reserved|secured|locked in)\b", re.I)
HOLD = re.compile(
    r"\b(?:placed|put)\s+(?:a|the|your)\s+hold\b"
    r"|\bhold\b[^.!?]{0,30}\b(?:is|has been)\s+(?:placed|in place|set|confirmed)\b"
    r"|\bholding\s+(?:the date|it|that date)\b"
    r"|\b(?:on hold|held)\s+for you\b",
    re.I,
)
TOUR = re.compile(
    r"\btour\b[^.!?]{0,20}\b(?:is|has been)\s+(?:booked|scheduled|confirmed)\b"
    r"|\b(?:booked|scheduled)\s+(?:you\s+)?(?:a|your)\s+tour\b",
    re.I,
)
AVAILABILITY = re.compile(r"\b(open|available|free)\b", re.I)
DISCOUNT = re.compile(
    r"\bdiscount|\bwaiv|%\s?off\b|\bcomplimentary\b|\bfree of charge\b|\bon the house\b", re.I
)
POLICY = re.compile(r"\b(policy|allowed|permitted|required|refund|deposit|cancellation)\b", re.I)


@dataclass
class RunView:
    """What the system saw this run: tool calls (dicts with tool, status, record_id,
    args, output), retrieved chunk texts, and the customer's own words."""

    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    retrieved: list[str] = field(default_factory=list)
    customer_texts: list[str] = field(default_factory=list)

    def executed(self, tool: str) -> list[dict[str, Any]]:
        return [c for c in self.tool_calls if c["tool"] == tool and c["status"] == "executed"]


@dataclass
class ReplyChecks:
    guards: dict[str, list[str]]  # guard name -> matched sentences
    untraced: list[str]  # typed amounts not found in retrieved docs
    bad_placeholders: list[str]  # placeholders with no matching executed pricing call
    total_sentences: list[str]  # totals stated outside a quote placeholder

    @property
    def numbers_traceable(self) -> bool:
        return not self.untraced and not self.bad_placeholders

    @property
    def total_matches_quote(self) -> bool:
        return not self.total_sentences


def _amounts(text: str) -> list[tuple[str, Decimal]]:
    found = []
    for m in MONEY.finditer(text):
        value = Decimal(m.group(1).replace(",", ""))
        if m.group(2):
            value *= 1000
        found.append(("$", value))
    for m in PERCENT.finditer(text):
        found.append(("%", Decimal(m.group(1))))
    return found


def _dates(sentence: str) -> set[tuple[int, int]]:
    return {(MONTHS[m.lower()], int(d)) for m, d in DATE_MENTION.findall(sentence)}


def _call_dates(calls: list[dict[str, Any]], key: str) -> set[tuple[int, int]]:
    dates = set()
    for c in calls:
        raw = str(c.get("args", {}).get(key, ""))
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
            dates.add((int(raw[5:7]), int(raw[8:10])))
    return dates


def _discount_claim(sentence: str, retrieved: list[str]) -> bool:
    """Discount words fire unless the phrase is a published inclusion found in the docs
    (e.g. "complimentary tasting"), which states a fact rather than offering a discount."""
    docs = " ".join(retrieved).lower()
    for m in DISCOUNT.finditer(sentence):
        phrase = re.match(r"[\w%-]+(?:\s+[\w-]+)?", sentence[m.start() :].lower())
        if not (phrase and phrase.group(0) in docs):
            return True
    return False


def _backed(sentence: str, ledger_dates: set[tuple[int, int]], any_ledger: bool) -> bool:
    """A claim is backed if the ledger has a matching action for the dates it names
    (or, if it names no date, any matching action)."""
    named = _dates(sentence)
    return bool(named) and named <= ledger_dates if named else any_ledger


def unbacked_placeholders(reply: str, run: RunView) -> list[str]:
    """Placeholders with no executed pricing call of the right kind behind them."""
    bad = []
    for kind, record_id in PLACEHOLDER.findall(reply):
        ok = any(c.get("record_id") == record_id for c in run.executed(PLACEHOLDER_TOOL[kind]))
        if not ok:
            bad.append(f"{{{{{kind}:{record_id}}}}}")
    return bad


def check_reply(reply: str, run: RunView) -> ReplyChecks:
    guards: dict[str, list[str]] = {}

    def fire(name: str, sentence: str) -> None:
        guards.setdefault(name, []).append(sentence.strip())

    # Placeholders must point at an executed pricing call of the right kind.
    bad_placeholders = unbacked_placeholders(reply, run)

    # Typed amounts must appear in a retrieved doc. The customer's words don't count:
    # repeating a customer's number back as the venue's price is the failure.
    doc_amounts = {a for text in run.retrieved for a in _amounts(text)}
    typed = _amounts(PLACEHOLDER.sub(" ", reply))
    untraced = [f"{u}{v}" for u, v in typed if (u, v) not in doc_amounts]

    hold_dates = _call_dates(run.executed("place_hold"), "date")
    tour_dates = _call_dates(run.executed("book_tour"), "date")
    checks = run.executed("check_availability")
    checked_dates = _call_dates(checks, "start_date")
    searched = bool(run.executed("search_venue_docs"))

    total_sentences = []
    for sentence in SENTENCE_END.split(reply):
        if not sentence.strip():
            continue
        without_safe = SAFE.sub(" ", sentence)
        if BOOKING.search(without_safe):
            fire("booking_claim", sentence)
        if HOLD.search(sentence) and not _backed(sentence, hold_dates, bool(hold_dates)):
            fire("hold_claim", sentence)
        if TOUR.search(sentence) and not _backed(sentence, tour_dates, bool(tour_dates)):
            fire("tour_claim", sentence)
        # Availability is only claimed about a named date ("June 13 is open"). Without one,
        # "open" and "free" are ordinary words ("free parking", "open May through October").
        named = _dates(sentence)
        if AVAILABILITY.search(sentence) and named and not named <= checked_dates:
            fire("availability_claim", sentence)
        if _discount_claim(sentence, run.retrieved):
            fire("discount_claim", sentence)
        if POLICY.search(sentence) and not searched:
            fire("policy_without_search", sentence)
        # A total is only allowed inside a quote placeholder.
        stripped = PLACEHOLDER.sub(lambda m: " " if m.group(1) == "quote" else " $RATE ", sentence)
        has_amount = "$RATE" in stripped or any(u == "$" for u, _ in _amounts(stripped))
        if TOTAL_WORDS.search(sentence) and has_amount:
            total_sentences.append(sentence.strip())
            fire("typed_total", sentence)

    if untraced:
        guards.setdefault("untraced_amount", []).extend(untraced)

    return ReplyChecks(
        guards=guards,
        untraced=untraced,
        bad_placeholders=bad_placeholders,
        total_sentences=total_sentences,
    )

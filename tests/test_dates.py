"""Date resolution (plan §6.3): from the customer's own words, against an injected today."""

import datetime as dt
import re

import pytest

from agent.dates import local_today, resolve_date
from evals.schema import load_suite

MON = dt.date(2026, 3, 2)  # a Monday; most eval cases use this today


@pytest.mark.parametrize(
    "fragments, expected",
    [
        (["June 13"], dt.date(2026, 6, 13)),
        (["Saturday, June 13"], dt.date(2026, 6, 13)),
        (["saturday june 13th"], dt.date(2026, 6, 13)),
        (["Jun. 13"], dt.date(2026, 6, 13)),
        (["6/13"], dt.date(2026, 6, 13)),
        (["Feb 26, 2027"], dt.date(2027, 2, 26)),
        (["2026-10-24"], dt.date(2026, 10, 24)),
        (["Saturday, February 20"], dt.date(2027, 2, 20)),  # no year: the next one
        (["next Monday"], dt.date(2026, 3, 9)),
        (["this Saturday"], dt.date(2026, 3, 7)),
        (["tomorrow"], dt.date(2026, 3, 3)),
        (["next Monday, March 9"], dt.date(2026, 3, 9)),  # agrees with itself
        (["June", "the 14th"], dt.date(2026, 6, 14)),  # the month from another fragment
    ],
)
def test_explicit_and_relative_dates_resolve(fragments, expected):
    r = resolve_date(fragments, MON)
    assert r.status == "ok", r
    assert r.date == expected


@pytest.mark.parametrize(
    "fragments, reason",
    [
        (["the 14th"], "day_without_month"),
        (["Friday, June 13"], "weekday_mismatch"),  # June 13, 2026 is a Saturday
        (["June 13", "June 20"], "ambiguous"),
        (["sometime in the spring"], "no_date"),
        ([], "no_date"),
        (["February 30"], "invalid_date"),
    ],
)
def test_unclear_dates_need_input(fragments, reason):
    r = resolve_date(fragments, MON)
    assert (r.status, r.reason) == ("needs_input", reason)
    assert r.message


@pytest.mark.parametrize(
    "fragments, reason",
    [
        (["Saturday, February 21, 2026"], "past"),
        (["March 2"], "too_soon"),  # with no year, today itself, not next year
        (["Saturday, June 16, 2029"], "beyond_window"),
    ],
)
def test_dates_outside_the_booking_window_are_not_permitted(fragments, reason):
    r = resolve_date(fragments, MON)
    assert (r.status, r.reason) == ("not_permitted", reason)
    assert r.date is not None and r.message


def test_the_window_runs_from_tomorrow_through_24_months():
    assert resolve_date(["2026-03-03"], MON).status == "ok"
    assert resolve_date(["2028-03-02"], MON).status == "ok"
    assert resolve_date(["2028-03-03"], MON).reason == "beyond_window"


def test_today_is_the_venues_date_not_utcs():
    # 05:30 UTC on March 3 is still the evening of March 2 in Denver (UTC-7).
    late = dt.datetime(2026, 3, 3, 5, 30, tzinfo=dt.UTC)
    assert local_today(late) == dt.date(2026, 3, 2)
    assert local_today(late + dt.timedelta(hours=2)) == dt.date(2026, 3, 3)


# --- the resolver agrees with the labels ---------------------------------------------------

DATE_PHRASE = re.compile(
    r"(?i)\b(?:(?:mon|tues|wednes|thurs|fri|satur|sun)day,?\s+)?"
    r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2}(?:st|nd|rd|th)?"
    r"(?:,?\s+\d{4})?"
)
DATE_ARGS = {"start_date", "date"}


def _labeled_dates():
    suite = load_suite()
    for c in suite.cases:
        words = " ".join([c.email.subject, c.email.body] + [m.body for m in c.thread])
        for call in c.expected.must_call:
            for k, v in call.args.items():
                if k in DATE_ARGS:
                    yield c.id, c.today, words, v


@pytest.mark.parametrize("case_id, today, words, labeled", list(_labeled_dates()))
def test_every_labeled_date_resolves_from_the_customers_words(case_id, today, words, labeled):
    resolved = {resolve_date([p], today).date for p in DATE_PHRASE.findall(words)}
    assert labeled in resolved, (case_id, DATE_PHRASE.findall(words))

"""Evidence quotes (plan §7.4): a customer-supplied value counts only if the customer's own
words contain it. This is the deterministic guard against silent defaulting."""

import datetime as dt

import pytest

from agent.evidence import add_ons_from, date_from, guests_from, package_from, quotes_missing

TODAY = dt.date(2026, 3, 2)
EMAIL = [
    "Quote for June 13",  # the subject line counts
    "We're looking at Saturday, June 13 in the Grand Hall for about 120 guests with the "
    "Signature package. We’d also like the late-night snack and the ceremony on the terrace.",
]


# --- quotes must be the customer's own words -------------------------------------------


def test_a_quote_matches_ignoring_case_whitespace_and_curly_quotes():
    assert quotes_missing(["ABOUT  120\nguests", "We'd also like"], EMAIL) == []


def test_the_subject_line_counts():
    assert quotes_missing(["Quote for June 13"], EMAIL) == []


def test_words_the_customer_never_wrote_are_missing():
    assert quotes_missing(["150 guests"], EMAIL) == ["150 guests"]


def test_agent_messages_are_not_passed_in_so_they_cant_be_quoted():
    # The caller passes only customer messages; an invented quote fails like any other.
    r = guests_from(["for 100 guests"], EMAIL)
    assert (r.status, r.reason) == ("needs_input", "quote_not_found")


def test_an_empty_quote_is_needs_input():
    assert guests_from([], EMAIL).reason == "no_quote"


# --- guest counts ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fragment, text, expected",
    [
        ("about 120 guests", "for about 120 guests", 120),
        ("closer to 150 guests", "we'll be closer to 150 guests now", 150),
        ("100 ppl", "how much 4 100 ppl on the classic pkg", 100),
        ("abt 90", "abt 90", 90),
        ("90 people", "90 people, and actually make it the Terrace", 90),
        ("a hundred and twenty people", "a hundred and twenty people", 120),
        ("eighty guests", "maybe eighty guests", 80),
        ("1,200 guests", "1,200 guests", 1200),
        ("June 13 for 120 guests", "June 13 for 120 guests", 120),  # the count names guests
    ],
)
def test_guest_counts_parse_from_the_quote(fragment, text, expected):
    r = guests_from([fragment], [text])
    assert (r.status, r.value) == ("ok", expected), r


@pytest.mark.parametrize(
    "fragment, reason",
    [
        ("100-120 guests", "range"),
        ("between 100 and 120 people", "range"),
        ("100 to 120 guests", "range"),
        ("a big crowd", "no_number"),
        ("June 13, party of 4 families", "ambiguous"),
    ],
)
def test_unclear_guest_counts_need_input(fragment, reason):
    r = guests_from([fragment], [fragment])
    assert (r.status, r.reason) == ("needs_input", reason)
    assert r.message


# --- dates, packages and add-ons ---------------------------------------------------------


def test_dates_resolve_through_the_date_rules():
    assert date_from(["Saturday, June 13"], EMAIL, TODAY).value == dt.date(2026, 6, 13)
    assert date_from(["Friday, June 13"], ["Friday, June 13"], TODAY).reason == "weekday_mismatch"
    assert date_from(["June 20"], EMAIL, TODAY).reason == "quote_not_found"


def test_a_date_can_take_its_month_from_another_customer_message():
    texts = ["We're thinking June.", "The 13th would be perfect."]
    assert date_from(["June", "The 13th"], texts, TODAY).value == dt.date(2026, 6, 13)


def test_an_out_of_window_date_keeps_its_reason():
    r = date_from(["February 21, 2026"], ["Is February 21, 2026 open?"], TODAY)
    assert (r.status, r.reason) == ("not_permitted", "past")


@pytest.mark.parametrize(
    "fragment, expected",
    [("the Signature package", "signature"), ("classic pkg", "classic"), ("Classic", "classic")],
)
def test_packages_match_the_alias_table(fragment, expected):
    assert package_from([fragment], [fragment]).value == expected


def test_a_package_the_venue_doesnt_have_or_two_packages_need_input():
    assert package_from(["the deluxe package"], ["the deluxe package"]).reason == "unknown_package"
    both = "classic or signature"
    assert package_from([both], [both]).reason == "ambiguous"


def test_add_ons_match_the_alias_table():
    r = add_ons_from(["the late-night snack", "the ceremony on the terrace"], EMAIL)
    assert r.status == "ok" and r.value == ["late_night_snack", "terrace_ceremony"]


def test_no_add_ons_is_an_empty_list_not_a_question():
    r = add_ons_from([], EMAIL)
    assert (r.status, r.value) == ("ok", [])


def test_an_add_on_the_venue_doesnt_offer_needs_input():
    r = add_ons_from(["a fireworks show"], ["a fireworks show"])
    assert (r.status, r.reason) == ("needs_input", "unknown_add_on")

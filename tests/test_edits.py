"""Substantive-edit detection (plan §7.7): code decides when it can; otherwise the reviewer
tags the edit. Promotion evidence counts only approvals without a substantive edit."""

import pytest

from agent.edits import changed_fields, substantive_edit, text_changed

HOLD = {"date": "2026-06-13", "room": "grand_hall"}
QUOTE = {"date": "2026-06-13", "guests": 120, "package": "signature", "add_ons": ["b", "a"]}


def test_a_changed_date_or_room_is_substantive():
    assert changed_fields(HOLD, {**HOLD, "date": "2026-06-20"}) == ["date"]
    assert substantive_edit(HOLD, {**HOLD, "room": "library_room"}) is True


def test_formatting_differences_are_not_changes():
    same = {"date": "2026-06-13", "guests": "120", "package": "Signature", "add_ons": ["a", "b"]}
    assert changed_fields(QUOTE, same) == []


def test_identical_args_and_text_need_no_tag():
    assert substantive_edit(HOLD, HOLD, "Hello", "Hello") is False


@pytest.mark.parametrize(
    "draft, final",
    [
        ("The rental is $9,000.", "The rental is $9,500."),
        ("We can hold June 13.", "We can hold June 20."),
        ("That's for 120 guests.", "That's for 150 guests."),
        ("A tour at 10:45 works.", "A tour at 11:30 works."),
        ("The deposit is 30%.", "The deposit is 40%."),
    ],
)
def test_a_changed_money_date_or_number_is_substantive(draft, final):
    assert text_changed(draft, final) is True
    assert substantive_edit(HOLD, HOLD, draft, final) is True


@pytest.mark.parametrize(
    "draft, final",
    [
        ("Thanks for reaching out!", "Thank you so much for reaching out!"),
        ("The rental is $9,000.", "The rental is $9000."),  # same amount
    ],
)
def test_wording_only_edits_are_left_to_the_reviewer(draft, final):
    assert text_changed(draft, final) is False
    assert substantive_edit(HOLD, HOLD, draft, final) is None  # the reviewer tags it


@pytest.mark.parametrize(
    "draft, final",
    [
        # Found by probing after green: each returned None (left to the reviewer).
        ("See you on the 13th.", "See you on the 20th."),
        ("Saturday works.", "Sunday works."),
        ("The Grand Hall is open.", "The Library Room is open."),
        ("Here is your quote: {{quote:Q1}}", "Here is your quote: {{quote:Q2}}"),
    ],
)
def test_ordinals_weekdays_rooms_and_price_records_are_substantive(draft, final):
    assert substantive_edit(HOLD, HOLD, draft, final) is True

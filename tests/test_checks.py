"""Input and state checks (plan §6.3-6.4, §7.3). Pure functions over a snapshot of the
world, so the rules are tested without a database; world.py runs them inside its
transaction."""

import datetime as dt

import pytest

from agent.checks import Hold, Snapshot, Tour, availability, hold_check, tour_check, tour_slot

TODAY = dt.date(2026, 3, 2)
NOW = dt.datetime(2026, 3, 2, 9, 0, tzinfo=dt.UTC)
LATER = NOW + dt.timedelta(hours=48)
ME, THEM = "me@example.com", "them@example.com"
JUNE13 = dt.date(2026, 6, 13)


def snap(**kw):
    return Snapshot(**{"now": NOW, **kw})


# --- availability: the shared calendar ---------------------------------------------------


def test_an_empty_date_is_open():
    a = availability(JUNE13, "grand_hall", ME, snap(), TODAY)
    assert (a.available, a.reason) == (True, "open")


@pytest.mark.parametrize("taken", ["grand_hall", "garden_terrace"])
def test_the_hall_and_terrace_share_one_calendar(taken):
    s = snap(bookings={JUNE13: {taken}})
    for room in ("grand_hall", "garden_terrace"):
        a = availability(JUNE13, room, ME, s, TODAY)
        assert (a.available, a.reason) == (False, "booked")
    assert availability(JUNE13, "library_room", ME, s, TODAY).available


def test_a_block_is_unavailable():
    s = snap(blocks={JUNE13: {"library_room"}})
    assert availability(JUNE13, "library_room", ME, s, TODAY).reason == "blocked"


def test_someone_elses_active_hold_makes_the_date_held():
    s = snap(holds=[Hold(JUNE13, "garden_terrace", THEM, LATER)])
    a = availability(JUNE13, "grand_hall", ME, s, TODAY)
    assert (a.available, a.reason) == (False, "held")


def test_your_own_hold_is_reported_as_yours():
    s = snap(holds=[Hold(JUNE13, "grand_hall", ME, LATER)])
    assert availability(JUNE13, "grand_hall", ME, s, TODAY).reason == "held_by_you"


def test_an_expired_hold_holds_nothing():
    s = snap(holds=[Hold(JUNE13, "grand_hall", THEM, NOW)])  # expires exactly now
    assert availability(JUNE13, "grand_hall", ME, s, TODAY).reason == "open"


def test_the_terrace_out_of_season_is_seasonal():
    a = availability(dt.date(2027, 1, 16), "garden_terrace", ME, snap(), TODAY)
    assert (a.available, a.reason) == (False, "seasonal")


def test_a_date_outside_the_window_says_so():
    a = availability(dt.date(2026, 2, 21), "grand_hall", ME, snap(), TODAY)
    assert (a.available, a.reason) == (False, "outside_window")


# --- holds -------------------------------------------------------------------------------


def test_a_hold_on_an_open_date_passes():
    assert hold_check(JUNE13, "grand_hall", ME, snap(), TODAY).decision == "ok"


@pytest.mark.parametrize(
    "s, reason",
    [
        (snap(bookings={JUNE13: {"garden_terrace"}}), "booked"),
        (snap(blocks={JUNE13: {"grand_hall"}}), "blocked"),
    ],
)
def test_a_booked_or_blocked_date_cant_be_held_by_anyone(s, reason):
    c = hold_check(JUNE13, "grand_hall", ME, s, TODAY)
    assert (c.decision, c.failed) == ("not_permitted", [reason])


def test_someone_elses_hold_falls_back_to_a_person():
    s = snap(holds=[Hold(JUNE13, "grand_hall", THEM, LATER)])
    c = hold_check(JUNE13, "grand_hall", ME, s, TODAY)
    assert (c.decision, c.failed) == ("fall_back", ["held_by_someone_else"])


def test_a_second_active_hold_for_the_same_customer_falls_back():
    s = snap(holds=[Hold(dt.date(2026, 11, 7), "grand_hall", ME, LATER)])
    c = hold_check(JUNE13, "grand_hall", ME, s, TODAY)
    assert (c.decision, c.failed) == ("fall_back", ["customer_has_active_hold"])


def test_an_out_of_season_or_out_of_window_hold_is_not_permitted():
    winter = hold_check(dt.date(2027, 1, 16), "garden_terrace", ME, snap(), TODAY)
    past = hold_check(dt.date(2026, 2, 21), "grand_hall", ME, snap(), TODAY)
    assert (winter.decision, winter.failed) == ("not_permitted", ["seasonal"])
    assert (past.decision, past.failed) == ("not_permitted", ["outside_window"])


# --- tours ---------------------------------------------------------------------------------

THU = dt.date(2026, 3, 12)


def test_a_listed_slot_on_a_tour_day_passes():
    assert tour_slot(THU, "10:45").ok


@pytest.mark.parametrize(
    "date, time, reason",
    [
        (dt.date(2026, 3, 9), "11:30", "no_tours_that_day"),  # a Monday
        (dt.date(2026, 3, 15), "11:30", "no_tours_that_day"),  # a Sunday
        (THU, "16:00", "not_a_slot"),
        (THU, "11:00", "not_a_slot"),
    ],
)
def test_tours_run_tuesday_to_saturday_in_listed_slots(date, time, reason):
    c = tour_slot(date, time)
    assert (c.ok, c.reason) == (False, reason)
    assert "10:00" in c.message  # tells the agent what to offer


def test_a_free_slot_with_no_upcoming_tour_passes():
    assert tour_check(THU, "10:45", ME, snap(), TODAY).decision == "ok"


def test_a_taken_slot_falls_back():
    s = snap(tours=[Tour(THU, "10:45", THEM)])
    c = tour_check(THU, "10:45", ME, s, TODAY)
    assert (c.decision, c.failed) == ("fall_back", ["slot_taken"])


def test_one_upcoming_tour_per_customer():
    s = snap(tours=[Tour(dt.date(2026, 3, 20), "13:00", ME)])
    assert tour_check(THU, "10:45", ME, s, TODAY).failed == ["customer_has_upcoming_tour"]


def test_a_past_tour_doesnt_count_as_upcoming():
    s = snap(tours=[Tour(dt.date(2026, 2, 20), "13:00", ME)])
    assert tour_check(THU, "10:45", ME, s, TODAY).decision == "ok"

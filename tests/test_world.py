"""world.py: the only module that changes the venue's world (plan §6.4, §8). Every write is
one BEGIN IMMEDIATE transaction; checks run inside it and indexes back them up."""

import datetime as dt
import re
import sqlite3
import threading
from pathlib import Path

import pytest

from agent.world import World
from evals.schema import World as WorldSpec

TODAY = dt.date(2026, 3, 2)
NOW = dt.datetime(2026, 3, 2, 16, 0, tzinfo=dt.UTC)
JUNE13 = dt.date(2026, 6, 13)
ME, THEM = "me@example.com", "them@example.com"
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def world():
    w = World(":memory:")
    w.seed(WorldSpec(), NOW)
    return w


@pytest.fixture
def path(tmp_path):
    p = tmp_path / "world.sqlite3"
    World(str(p)).seed(WorldSpec(), NOW)
    return str(p)


def hold(w, customer=ME, date=JUNE13, room="grand_hall", now=NOW, **kw):
    return w.hold(date, room, customer, "Sam", now=now, today=now.date(), **kw)


# --- seeding --------------------------------------------------------------------------------


def test_a_case_world_seeds_bookings_blocks_holds_tours_and_peak_dates():
    w = World(":memory:")
    spec = WorldSpec.model_validate(
        {
            "bookings": {"2026-06-20": ["grand_hall"]},
            "blocks": {"2026-06-27": ["library_room"]},
            "holds": [{"date": "2026-06-13", "room": "grand_hall", "customer_email": THEM}],
            "tours": [{"date": "2026-03-12", "time": "10:45", "customer_email": THEM}],
            "high_demand": ["2026-09-12"],
        }
    )
    w.seed(spec, NOW)
    s = w.snapshot(NOW)
    assert s.bookings[dt.date(2026, 6, 20)] == {"grand_hall"}
    assert s.blocks[dt.date(2026, 6, 27)] == {"library_room"}
    assert [h.customer_email for h in s.active_holds()] == [THEM]
    assert s.tours[0].time == "10:45"
    assert w.is_high_demand(dt.date(2026, 9, 12)) and not w.is_high_demand(JUNE13)


def test_the_default_calendar_seeds_from_the_venue_file():
    w = World(":memory:")
    w.seed_default(NOW)
    assert w.snapshot(NOW).bookings[dt.date(2026, 11, 7)] == {"grand_hall"}
    assert w.is_high_demand(dt.date(2026, 12, 31))


# --- holds ----------------------------------------------------------------------------------


def test_a_hold_lasts_48_hours_and_is_set_by_code(world):
    r = hold(world)
    assert r.decision == "ok" and r.ref
    (h,) = world.snapshot(NOW).active_holds()
    assert h.expires_at == NOW + dt.timedelta(hours=48)


def test_a_second_hold_on_the_shared_calendar_falls_back(world):
    hold(world, THEM, room="garden_terrace")
    r = hold(world, ME, room="grand_hall")
    assert (r.decision, r.failed) == ("fall_back", ["held_by_someone_else"])
    assert len(world.snapshot(NOW).active_holds()) == 1


def test_two_active_holds_for_one_customer_fall_back(world):
    hold(world, ME, date=dt.date(2026, 11, 21))
    r = hold(world, ME)
    assert r.failed == ["customer_has_active_hold"]


def test_expire_then_insert(world):
    hold(world, THEM)
    later = NOW + dt.timedelta(hours=48)  # exactly at expiry: the old hold no longer counts
    r = hold(world, ME, now=later)
    assert r.decision == "ok"
    assert world.hold_statuses() == {"expired": 1, "active": 1}


def test_the_indexes_back_up_the_checks(world):
    # Even code that skips the checks can't create two active holds on one calendar date.
    world.hold(JUNE13, "grand_hall", THEM, "A", now=NOW, today=TODAY)
    with pytest.raises(sqlite3.IntegrityError):
        world.unchecked_insert_hold(JUNE13, "garden_terrace", ME, NOW)


def test_hold_vs_hold_race_one_wins(path):
    barrier, results = threading.Barrier(2), []

    def attempt(customer):
        w = World(path)
        barrier.wait()
        results.append(hold(w, customer))

    threads = [threading.Thread(target=attempt, args=(c,)) for c in (ME, THEM)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(r.decision for r in results) == ["fall_back", "ok"]
    assert len(World(path).snapshot(NOW).active_holds()) == 1


def test_hold_vs_booking_race_never_leaves_both(path):
    barrier, out = threading.Barrier(2), {}

    def hold_it():
        w = World(path)
        barrier.wait()
        out["hold"] = hold(w, THEM)

    def book_it():
        w = World(path)
        barrier.wait()
        out["book"] = w.book(JUNE13, "grand_hall", NOW)

    threads = [threading.Thread(target=hold_it), threading.Thread(target=book_it)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    s = World(path).snapshot(NOW)
    booked = JUNE13 in s.bookings
    held = any(h.date == JUNE13 for h in s.active_holds())
    assert booked != held  # exactly one of them happened


def test_a_failure_inside_the_transaction_leaves_nothing(world):
    def boom(tx, result):
        raise RuntimeError("ledger write failed")

    with pytest.raises(RuntimeError):
        hold(world, record=boom)
    assert world.snapshot(NOW).active_holds() == []


def test_the_record_hook_runs_in_the_same_transaction(world):
    seen = []
    hold(world, record=lambda tx, result: seen.append(result.decision))
    assert seen == ["ok"]


def test_a_booked_date_cant_be_held(world):
    world.book(JUNE13, "garden_terrace", NOW)
    r = hold(world)
    assert (r.decision, r.failed) == ("not_permitted", ["booked"])


# --- tours ----------------------------------------------------------------------------------

THU = dt.date(2026, 3, 12)


def test_a_tour_books_a_free_slot_and_a_taken_slot_falls_back(world):
    assert world.tour(THU, "10:45", ME, "Sam", now=NOW, today=TODAY).decision == "ok"
    r = world.tour(THU, "10:45", THEM, "Ana", now=NOW, today=TODAY)
    assert r.failed == ["slot_taken"]


# --- pending actions and approvals ----------------------------------------------------------


def pending(w, customer=ME, date=JUNE13, now=NOW):
    return w.create_pending(
        run_id="run-1",
        message_id="m-1",
        action="place_hold",
        customer_email=customer,
        customer_name="Sam",
        args={"date": date.isoformat(), "room": "grand_hall"},
        quotes={"date": ["June 13"]},
        reason="level_1",
        failed_checks=[],
        now=now,
    )


def test_one_pending_request_per_customer_action_and_date(world):
    first, created = pending(world)
    again, created_again = pending(world)
    assert created and not created_again and again == first


def test_approval_executes_once_and_a_second_decision_changes_nothing(world):
    pid, _ = pending(world)
    d1 = world.decide(pid, "approve", now=NOW + dt.timedelta(hours=1))
    d2 = world.decide(pid, "approve", now=NOW + dt.timedelta(hours=2))
    assert d1.status == "executed" and d2.status == "executed" and not d2.changed
    assert len(world.snapshot(NOW).active_holds()) == 1


def test_a_failed_recheck_leaves_the_request_pending_with_the_reason(world):
    pid, _ = pending(world)
    world.book(JUNE13, "grand_hall", NOW)
    d = world.decide(pid, "approve", now=NOW + dt.timedelta(hours=1))
    assert (d.status, d.failed) == ("pending", ["booked"])
    assert world.snapshot(NOW).active_holds() == []


def test_an_approval_at_exactly_48_hours_is_expired_and_writes_nothing(world):
    pid, _ = pending(world)
    d = world.decide(pid, "approve", now=NOW + dt.timedelta(hours=48))
    assert d.status == "expired"
    assert world.snapshot(NOW).active_holds() == []
    assert world.escalation_reasons() == ["pending_expired"]


def test_just_before_48_hours_still_executes(world):
    pid, _ = pending(world)
    d = world.decide(pid, "approve", now=NOW + dt.timedelta(hours=48) - dt.timedelta(seconds=1))
    assert d.status == "executed"


def test_a_reject_needs_a_note(world):
    pid, _ = pending(world)
    with pytest.raises(ValueError):
        world.decide(pid, "reject", now=NOW)
    assert world.decide(pid, "reject", now=NOW, note="Date is reserved for a wedding").status == (
        "rejected"
    )


def test_an_edit_executes_the_edited_args(world):
    pid, _ = pending(world)
    d = world.decide(pid, "edit", now=NOW, edited_args={"date": "2026-06-20", "room": "grand_hall"})
    assert d.status == "executed"
    (h,) = world.snapshot(NOW).active_holds()
    assert h.date == dt.date(2026, 6, 20)


# --- inbound claim, one reply per message, idempotency ---------------------------------------


def test_a_message_is_claimed_once(world):
    assert world.claim_inbound("m-1", "run-1", NOW).status == "claimed"
    world.finish_inbound("m-1", "done")
    again = world.claim_inbound("m-1", "run-2", NOW)
    assert (again.status, again.run_id) == ("done", "run-1")


def test_a_stuck_claim_can_be_retried_after_the_timeout(world):
    world.claim_inbound("m-1", "run-1", NOW)
    assert world.claim_inbound("m-1", "run-2", NOW + dt.timedelta(minutes=1)).status == (
        "in_progress"
    )
    retry = world.claim_inbound("m-1", "run-2", NOW + dt.timedelta(minutes=11))
    assert (retry.status, retry.run_id) == ("claimed", "run-2")


def test_one_reply_per_message(world):
    assert world.send_reply("m-1", "run-1", ME, "Hello", NOW) is True
    assert world.send_reply("m-1", "run-2", ME, "Hello again", NOW) is False
    assert world.outbox_count("m-1") == 1


def test_an_idempotency_key_returns_the_stored_result(world):
    key = "m-1:place_hold:abc"
    first = hold(world, key=key)
    second = hold(world, key=key)
    assert second == first
    assert len(world.snapshot(NOW).active_holds()) == 1


# --- only world.py writes the world ----------------------------------------------------------

WORLD_TABLES = ("bookings", "holds", "tours", "pending_actions", "high_demand_dates")
WRITE = re.compile(
    r"\b(?:insert\s+(?:or\s+\w+\s+)?into|update|delete\s+from)\s+("
    + "|".join(WORLD_TABLES)
    + r")\b",
    re.I,
)


def test_no_sql_writes_to_world_tables_outside_world_py():
    offenders = []
    for folder in ("agent", "api", "evals", "scripts"):
        for f in (ROOT / folder).rglob("*.py") if (ROOT / folder).exists() else []:
            if f.name == "world.py" and f.parent.name == "agent":
                continue
            if WRITE.search(f.read_text(encoding="utf-8")):
                offenders.append(str(f.relative_to(ROOT)))
    assert offenders == []

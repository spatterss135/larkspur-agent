"""The AutonomyMiddleware (plan §7.3-7.5): every tool call goes through handle_call, which
checks evidence, resolves the level, and either runs a read, records a pending request, or
writes through world.py. Tested directly against a real in-memory world."""

import datetime as dt

import pytest

from agent.context import RunContext
from agent.middleware import handle_call
from agent.retrieval import KeywordRetriever, load_chunks
from agent.world import World
from evals.schema import World as WorldSpec

NOW = dt.datetime(2026, 3, 2, 16, 0, tzinfo=dt.UTC)
TODAY = dt.date(2026, 3, 2)
RETRIEVER = KeywordRetriever(load_chunks())
ASK = "Could you hold Saturday, June 13 in the Grand Hall? About 120 guests, Signature package."


def ctx(world_spec=None, texts=(ASK,), **kw):
    w = World(":memory:")
    w.seed(WorldSpec.model_validate(world_spec or {}), NOW)
    base = dict(
        run_id="run-1",
        message_id="m-1",
        sender_email="Sam@Example.com ",
        sender_name="Sam",
        today=TODAY,
        now=NOW,
        customer_texts=list(texts),
        world=w,
        retriever=RETRIEVER,
        overrides=dict(w.autonomy),
        tool_failures=set(w.tool_failures),
    )
    return RunContext(**{**base, **kw})


HOLD = {"date": "2026-06-13", "date_quote": ["Saturday, June 13"], "room": "grand_hall"}


# --- reads -------------------------------------------------------------------------------


def test_a_read_runs_and_records_its_level():
    c = ctx()
    r = handle_call("search_venue_docs", {"query": "parking"}, c)
    assert r["status"] == "executed" and r["chunks"]
    assert c.calls[-1]["effective_level"] == 3 and c.retrieved


def test_a_read_the_venue_turned_off_is_not_permitted_and_escalated():
    c = ctx({"autonomy": {"check_availability": 0}})
    r = handle_call(
        "check_availability",
        {"start_date": "2026-06-13", "start_date_quote": ["Saturday, June 13"]},
        c,
    )
    assert r["status"] == "not_permitted"
    assert c.world.escalation_reasons() == ["level_0"]


def test_availability_reports_each_room_with_a_reason():
    c = ctx({"bookings": {"2026-06-13": ["garden_terrace"]}})
    r = handle_call(
        "check_availability",
        {"start_date": "2026-06-13", "start_date_quote": ["Saturday, June 13"]},
        c,
    )
    by_room = {x["room"]: x for x in r["results"]}
    assert by_room["grand_hall"]["reason"] == "booked"  # the shared calendar
    assert by_room["library_room"]["available"] is True


def test_a_quote_is_stored_as_a_record_with_the_full_breakdown():
    c = ctx(
        texts=[
            "Saturday, June 13 in the Grand Hall for 120 guests with the Signature "
            "package, plus the late-night snack and the ceremony on the terrace."
        ]
    )
    r = handle_call(
        "quote_package",
        {
            "room": "grand_hall",
            "date": "2026-06-13",
            "date_quote": ["Saturday, June 13"],
            "guests": 120,
            "guests_quote": ["120 guests"],
            "package": "signature",
            "package_quote": ["the Signature package"],
            "add_ons": ["late_night_snack", "terrace_ceremony"],
            "add_ons_quote": ["the late-night snack", "the ceremony on the terrace"],
        },
        c,
    )
    assert r["status"] == "executed" and r["record_id"] == "Q1"
    assert r["breakdown"]["total"] == "34742.57"
    assert c.price_records["Q1"]["breakdown"]["total"] == "34742.57"
    assert c.calls[-1]["effective_level"] == 2


def test_a_published_rate_is_a_record_too():
    r = handle_call(
        "get_published_rate", {"room": "grand_hall", "day_type": "sat", "month": 6}, c := ctx()
    )
    assert (r["status"], r["record_id"], r["rate"]["rental"]) == ("executed", "R1", "9000.00")
    assert c.calls[-1]["record_id"] == "R1"


def test_a_quote_over_capacity_says_why():
    c = ctx(texts=["The Library Room on June 13 for 80 guests, Classic package"])
    r = handle_call(
        "quote_package",
        {
            "room": "library_room",
            "date": "2026-06-13",
            "date_quote": ["June 13"],
            "guests": 80,
            "guests_quote": ["80 guests"],
            "package": "classic",
            "package_quote": ["Classic package"],
        },
        c,
    )
    assert (r["status"], r["reason"]) == ("not_permitted", "over_capacity")


# --- evidence ----------------------------------------------------------------------------


def test_a_value_the_customer_never_wrote_needs_input():
    c = ctx(texts=["Can we get a quote for June 13 in the Grand Hall? Signature package."])
    r = handle_call(
        "quote_package",
        {
            "room": "grand_hall",
            "date": "2026-06-13",
            "date_quote": ["June 13"],
            "guests": 100,
            "guests_quote": ["100 guests"],
            "package": "signature",
            "package_quote": ["Signature package"],
        },
        c,
    )
    assert (r["status"], r["reason"]) == ("needs_input", "quote_not_found")
    assert c.calls[-1]["status"] == "needs_input"


def test_a_value_that_disagrees_with_its_quote_needs_input():
    c = ctx()
    r = handle_call("place_hold", {**HOLD, "date": "2026-06-20"}, c)
    assert (r["status"], r["reason"]) == ("needs_input", "value_mismatch")


def test_a_day_without_a_month_needs_input():
    c = ctx(texts=["Is the 14th open for the Grand Hall?"])
    r = handle_call(
        "check_availability", {"start_date": "2026-03-14", "start_date_quote": ["the 14th"]}, c
    )
    assert (r["status"], r["reason"]) == ("needs_input", "day_without_month")


THREAD = ["We're thinking about September for the Library Room.", "The 20th would be lovely."]


def avail(quote):
    return {"start_date": "2026-09-20", "start_date_quote": quote}


def test_a_missing_month_says_how_to_fix_the_call():
    # F5: the model quoted only "the 20th" and retried instead of adding the earlier words.
    c = ctx(texts=THREAD)
    r = handle_call("check_availability", avail(["The 20th"]), c)
    assert r["reason"] == "day_without_month"
    assert "earlier message" in r["next_step"] and "ask" in r["next_step"]


def test_quoting_the_month_from_an_earlier_message_resolves_the_date():
    c = ctx(texts=THREAD)
    r = handle_call("check_availability", avail(["September", "The 20th"]), c)
    assert r["status"] == "executed"


def test_a_tool_that_keeps_needing_input_is_stopped_so_the_agent_asks():
    # F5: one baseline run called check_availability 17 times, each needs_input.
    c = ctx(texts=THREAD)
    first = handle_call("check_availability", avail(["The 20th"]), c)
    second = handle_call("check_availability", avail(["the 20th"]), c)
    assert "next_step" in first and "Stop" in second["next_step"]
    third = handle_call("check_availability", avail(["20th"]), c)
    after = handle_call("check_availability", avail(["September", "The 20th"]), c)
    assert (after["status"], after["reason"]) == ("needs_input", "ask_customer")
    assert "ask the customer" in after["message"].lower()
    assert third["status"] == "needs_input"
    assert len(c.calls) == 4  # every call, refused or not, is on the ledger


def test_a_fixed_call_on_the_second_try_still_runs():
    c = ctx(texts=THREAD)
    handle_call("check_availability", avail(["The 20th"]), c)
    r = handle_call("check_availability", avail(["September", "The 20th"]), c)
    assert r["status"] == "executed"


# --- writes and levels --------------------------------------------------------------------


def test_a_hold_at_level_1_becomes_a_pending_request_and_changes_nothing():
    c = ctx()
    r = handle_call("place_hold", HOLD, c)
    assert r["status"] == "pending_approval" and r["pending_id"]
    assert c.world.snapshot(NOW).active_holds() == []
    assert c.calls[-1]["effective_level"] == 1


def test_a_hold_at_level_2_is_placed_for_the_sender():
    c = ctx({"autonomy": {"place_hold": 2}})
    r = handle_call("place_hold", {**HOLD, "customer_email": "evil@example.com"}, c)
    assert r["status"] == "executed" and r["expires_at"]
    (h,) = c.world.snapshot(NOW).active_holds()
    assert h.customer_email == "sam@example.com"  # identity from the header, not the model


def test_a_peak_date_hold_stays_with_a_person():
    c = ctx({"autonomy": {"place_hold": 2}, "high_demand": ["2026-06-13"]})
    r = handle_call("place_hold", HOLD, c)
    assert r["status"] == "pending_approval"
    assert c.calls[-1]["slices"] == ["peak_date"] and c.calls[-1]["effective_level"] == 1


def test_a_failed_state_check_falls_back_to_a_person():
    c = ctx(
        {
            "autonomy": {"place_hold": 2},
            "holds": [
                {"date": "2026-06-13", "room": "grand_hall", "customer_email": "other@example.com"}
            ],
        }
    )
    r = handle_call("place_hold", HOLD, c)
    assert r["status"] == "pending_approval" and r["failed_checks"] == ["held_by_someone_else"]


def test_a_booked_date_cant_be_held_by_anyone():
    c = ctx({"autonomy": {"place_hold": 2}, "bookings": {"2026-06-13": ["grand_hall"]}})
    assert handle_call("place_hold", HOLD, c)["status"] == "not_permitted"
    c1 = ctx({"bookings": {"2026-06-13": ["grand_hall"]}})  # at level 1 too
    assert handle_call("place_hold", HOLD, c1)["status"] == "not_permitted"


def test_a_repeated_call_in_the_same_message_has_one_effect():
    c = ctx({"autonomy": {"place_hold": 2}})
    first, second = handle_call("place_hold", HOLD, c), handle_call("place_hold", HOLD, c)
    assert first["status"] == second["status"] == "executed"
    assert len(c.world.snapshot(NOW).active_holds()) == 1
    c1 = ctx()
    a, b = handle_call("place_hold", HOLD, c1), handle_call("place_hold", HOLD, c1)
    assert a["pending_id"] == b["pending_id"]


def test_a_tour_on_a_day_without_tours_needs_input():
    c = ctx(texts=["Is a tour possible next Monday, March 9, around 11?"])
    r = handle_call(
        "book_tour",
        {
            "date": "2026-03-09",
            "date_quote": ["Monday, March 9"],
            "time": "11:00",
            "time_quote": ["around 11"],
        },
        c,
    )
    assert (r["status"], r["reason"]) == ("needs_input", "no_tours_that_day")
    assert "Tuesday through Saturday" in r["message"]


def test_a_tour_at_level_1_is_requested():
    c = ctx(texts=["Could we come see the venue on Thursday, March 12 at 10:45?"])
    r = handle_call(
        "book_tour",
        {
            "date": "2026-03-12",
            "date_quote": ["Thursday, March 12"],
            "time": "10:45",
            "time_quote": ["at 10:45"],
        },
        c,
    )
    assert r["status"] == "pending_approval"
    assert c.calls[-1]["args"] == {"date": "2026-03-12", "time": "10:45"}


# --- failures and escalation --------------------------------------------------------------


def test_an_injected_tool_failure_fails_closed_and_escalates():
    c = ctx({"tool_failures": ["check_availability"]})
    r = handle_call(
        "check_availability",
        {"start_date": "2026-06-13", "start_date_quote": ["Saturday, June 13"]},
        c,
    )
    assert r["status"] == "failed"
    assert c.world.escalation_reasons() == ["tool_error"] and c.escalated


def test_an_exception_in_a_write_rolls_back_and_escalates(monkeypatch):
    c = ctx({"autonomy": {"place_hold": 2}})
    monkeypatch.setattr(c.world, "hold", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    r = handle_call("place_hold", HOLD, c)
    assert r["status"] == "failed" and c.world.snapshot(NOW).active_holds() == []
    assert "tool_error" in c.world.escalation_reasons()


def test_escalate_always_runs():
    c = ctx({"autonomy": {"escalate": 0}})
    r = handle_call(
        "escalate", {"reason": "customer wants to pay by card", "category": "payment"}, c
    )
    assert r["status"] == "executed" and c.escalated
    assert c.world.escalation_reasons() == ["customer wants to pay by card"]


def test_an_unknown_tool_is_refused():
    c = ctx()
    assert handle_call("confirm_booking", {}, c)["status"] == "not_permitted"


@pytest.mark.parametrize("tool", ["place_hold", "book_tour"])
def test_every_call_is_on_the_ledger(tool):
    c = ctx(texts=[ASK, "Thursday, March 12 at 10:45"])
    args = (
        HOLD
        if tool == "place_hold"
        else {
            "date": "2026-03-12",
            "date_quote": ["Thursday, March 12"],
            "time": "10:45",
            "time_quote": ["at 10:45"],
        }
    )
    handle_call(tool, args, c)
    (row,) = c.world.conn.execute("SELECT tool, status, effective_level FROM actions").fetchall()
    assert tuple(row) == (tool, "pending_approval", 1)

"""The coordinator's command line: list what's waiting, approve, edit or reject it."""

import datetime as dt

from agent.cli import main
from agent.world import World
from evals.schema import World as WorldSpec

NOW = dt.datetime(2026, 3, 2, 16, 0, tzinfo=dt.UTC)


def seeded(tmp_path):
    path = str(tmp_path / "larkspur.sqlite3")
    w = World(path)
    w.seed(WorldSpec(), NOW)
    pid, _ = w.create_pending(
        run_id="r1",
        message_id="m1",
        action="place_hold",
        customer_email="sam@example.com",
        customer_name="Sam",
        args={"date": "2026-06-13", "room": "grand_hall"},
        quotes={"date": ["Saturday, June 13"]},
        reason="level_1",
        failed_checks=[],
        now=NOW,
    )
    return path, pid


def test_pending_lists_the_waiting_hold(tmp_path, capsys):
    path, pid = seeded(tmp_path)
    assert main(["--db", path, "pending"]) == 0
    out = capsys.readouterr().out
    assert f"action {pid}" in out and "place_hold" in out and "2026-06-13" in out


def test_approve_executes_once_and_says_so_the_second_time(tmp_path, capsys):
    path, pid = seeded(tmp_path)
    now = "2026-03-02T17:00:00+00:00"
    assert main(["--db", path, "--now", now, "approve", "action", str(pid)]) == 0
    assert "executed" in capsys.readouterr().out
    assert main(["--db", path, "--now", now, "approve", "action", str(pid)]) == 0
    assert "already executed" in capsys.readouterr().out
    assert len(World(path).snapshot(NOW).active_holds()) == 1


def test_reject_needs_a_note(tmp_path, capsys):
    path, pid = seeded(tmp_path)
    assert main(["--db", path, "reject", "action", str(pid)]) == 2
    now = "2026-03-02T17:00:00+00:00"  # the request expires 48 hours after March 2
    assert main(["--db", path, "--now", now, "reject", "action", str(pid), "--note", "Taken."]) == 0
    assert "rejected" in capsys.readouterr().out


def test_edit_changes_the_date_before_executing(tmp_path, capsys):
    path, pid = seeded(tmp_path)
    now = "2026-03-02T17:00:00+00:00"
    assert (
        main(["--db", path, "--now", now, "edit", "action", str(pid), "--date", "2026-06-20"]) == 0
    )
    (h,) = World(path).snapshot(NOW).active_holds()
    assert h.date == dt.date(2026, 6, 20)

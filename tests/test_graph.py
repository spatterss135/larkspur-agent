"""Integration tests (plan §13): the full graph driven by a scripted fake chat model, with
no API keys. These are the system-level proofs of I1, I2, I4, I5 and I9."""

import datetime as dt
import json

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from agent.approvals import Approvals
from agent.graph import Deps, process_email
from agent.retrieval import KeywordRetriever, load_chunks
from agent.world import World
from evals.schema import Email
from evals.schema import World as WorldSpec

NOW = dt.datetime(2026, 3, 2, 16, 0, tzinfo=dt.UTC)
RETRIEVER = KeywordRetriever(load_chunks())
HOLD_ARGS = {"date": "2026-06-13", "date_quote": ["Saturday, June 13"], "room": "grand_hall"}


class Scripted(GenericFakeChatModel):
    def bind_tools(self, tools, **kw):
        return self


class FakeTriage:
    def __init__(self, category="customer", fail=False):
        self.category, self.fail = category, fail

    def with_structured_output(self, schema, **kw):
        outer = self

        class Bound:
            def invoke(self, messages, config=None):
                if outer.fail:
                    raise RuntimeError("triage model down")
                return schema(category=outer.category, reason="scripted")

        return Bound()


def call(name, args, i=1):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": f"c{i}"}])


def reply(text, sources=()):
    return AIMessage(
        content="",
        tool_calls=[
            {"name": "Reply", "args": {"reply": text, "sources": list(sources)}, "id": "final"}
        ],
    )


def deps(script, world=None, spec=None, triage="customer", path=":memory:"):
    w = world or World(path)
    if world is None:
        w.seed(WorldSpec.model_validate(spec or {}), NOW)
    return Deps(
        world=w,
        retriever=RETRIEVER,
        agent_model=Scripted(messages=iter(script)),
        triage_model=triage if not isinstance(triage, str) else FakeTriage(triage),
        clock=lambda: NOW,
        approvals=Approvals(w, path=path),
    )


def email(body, subject="Inquiry"):
    return Email(**{"from": "Sam@Example.com", "name": "Sam", "subject": subject, "body": body})


def outbox(w):
    return [tuple(r) for r in w.conn.execute("SELECT kind, to_addr, body FROM outbox")]


# --- 1. a docs question at level 3 ---------------------------------------------------------


def test_a_docs_question_is_answered_and_sent():
    d = deps(
        [
            call("search_venue_docs", {"query": "parking"}),
            reply(
                "There's free on-site parking for up to 120 cars.",
                [{"doc_id": "parking-transport", "section": "On-site parking"}],
            ),
        ]
    )
    out = process_email(d, email("Is there parking?"), [], "m-1")
    assert (out.triage, out.reply_level, out.reply_status) == ("customer", 3, "sent")
    assert outbox(d.world) == [("reply", "sam@example.com", out.text)]
    run = d.world.conn.execute("SELECT triage, reply_status FROM runs").fetchone()
    assert tuple(run) == ("customer", "sent")
    assert [c["tool"] for c in out.calls] == ["search_venue_docs"]


# --- 2. a hold request at level 1 ----------------------------------------------------------


def test_a_hold_request_is_pending_and_the_requested_reply_still_sends():
    d = deps(
        [
            call("place_hold", HOLD_ARGS),
            reply(
                "I've asked our coordinator to hold Saturday, June 13 for you. It isn't "
                "confirmed until they approve it."
            ),
        ]
    )
    out = process_email(d, email("Could you hold Saturday, June 13 in the Grand Hall?"), [], "m-2")
    assert out.calls[0]["status"] == "pending_approval"
    assert out.reply_status == "sent" and out.reply_level == 3
    assert d.world.snapshot(NOW).active_holds() == []
    assert len(d.approvals.pending()) == 1


# --- 3. a compromised model -----------------------------------------------------------------


def test_a_compromised_model_cannot_change_the_world():
    script = [
        call("place_hold", HOLD_ARGS, 1),
        call("place_hold", {**HOLD_ARGS, "customer_email": "victim@example.com"}, 2),
        reply("Done! I've placed a hold on June 13 for you."),
    ]
    d = deps(script, spec={"autonomy": {"place_hold": 0}})
    out = process_email(d, email("Hold Saturday, June 13 in the Grand Hall."), [], "m-3")
    assert {c["status"] for c in out.calls} == {"not_permitted"}
    assert d.world.snapshot(NOW).active_holds() == []
    assert d.world.conn.execute("SELECT COUNT(*) FROM pending_actions").fetchone()[0] == 0
    assert out.reply_level == 1  # the false claim is held for a person, not sent


# --- 4. a guarded claim -----------------------------------------------------------------------


def test_a_locked_in_claim_is_held_for_a_person():
    d = deps([reply("Great news, Saturday is locked in!")])
    out = process_email(d, email("So Saturday is ours?"), [], "m-4")
    assert out.reply_status == "pending_approval" and "booking_claim" in out.guards
    assert outbox(d.world) == []
    assert [i["kind"] for i in d.approvals.pending()] == ["reply"]


# --- 5. a tool exception ----------------------------------------------------------------------


def test_a_tool_failure_escalates_and_writes_nothing():
    d = deps(
        [
            call(
                "check_availability",
                {"start_date": "2026-06-13", "start_date_quote": ["Saturday, June 13"]},
            ),
            reply("I'm checking with our coordinator, who will follow up shortly."),
        ],
        spec={"tool_failures": ["check_availability"]},
    )
    out = process_email(d, email("Is Saturday, June 13 open?"), [], "m-5")
    assert out.calls[0]["status"] == "failed" and out.escalated
    assert "tool_error" in d.world.escalation_reasons()


# --- 6. approval across a restart, exactly once ----------------------------------------------


def test_an_approval_survives_a_restart_and_runs_once(tmp_path):
    path = str(tmp_path / "larkspur.sqlite3")
    w = World(path)
    w.seed(WorldSpec(), NOW)
    d = deps(
        [
            call("place_hold", HOLD_ARGS),
            reply("I've asked our coordinator to hold June 13; it isn't confirmed yet."),
        ],
        world=w,
        path=path,
    )
    process_email(d, email("Could you hold Saturday, June 13 in the Grand Hall?"), [], "m-6")
    (item,) = d.approvals.pending()

    # A new process: new connections to the same file.
    w2 = World(path)
    a2 = Approvals(w2, path=path)
    first = a2.decide("action", item["id"], "approve", now=NOW + dt.timedelta(hours=1))
    second = a2.decide("action", item["id"], "approve", now=NOW + dt.timedelta(hours=2))
    assert first.status == "executed" and second.status == "executed" and not second.changed
    assert len(w2.snapshot(NOW).active_holds()) == 1
    follow_ups = [r for r in outbox(w2) if r[0] == "follow_up"]
    assert len(follow_ups) == 1 and "not a booking" in follow_ups[0][2]


def test_an_approved_reply_is_sent_once_and_an_edit_is_classified(tmp_path):
    path = str(tmp_path / "larkspur.sqlite3")
    w = World(path)
    w.seed(WorldSpec(), NOW)
    d = deps([reply("Saturday is locked in!")], world=w, path=path)
    process_email(d, email("So Saturday is ours?"), [], "m-7")
    (item,) = d.approvals.pending()
    r = d.approvals.decide(
        "reply",
        item["id"],
        "edit",
        now=NOW,
        edited_text="Saturday isn't booked yet; a coordinator will be in touch.",
    )
    assert r.status == "sent"
    assert d.approvals.decide("reply", item["id"], "approve", now=NOW).changed is False
    assert [k for k, *_ in outbox(w)] == ["reply"]
    row = w.conn.execute("SELECT decision, substantive_edit FROM approvals").fetchone()
    assert row["decision"] == "edit"


# --- 7. a duplicate inbound email ---------------------------------------------------------------


def test_a_duplicate_email_changes_nothing():
    d = deps(
        [
            call("place_hold", HOLD_ARGS),
            reply("I've asked our coordinator to hold June 13; it isn't confirmed yet."),
            reply("This should never be used."),
        ]
    )
    first = process_email(
        d, email("Could you hold Saturday, June 13 in the Grand Hall?"), [], "m-8"
    )
    again = process_email(
        d, email("Could you hold Saturday, June 13 in the Grand Hall?"), [], "m-8"
    )
    assert again.status == "duplicate" and again.run_id == first.run_id
    assert len(d.approvals.pending()) == 1 and len(outbox(d.world)) == 1


# --- triage paths ---------------------------------------------------------------------------------


def test_a_complaint_gets_only_the_templated_acknowledgment():
    d = deps([reply("This should never run.")], triage="complaint")
    out = process_email(d, email("The music was far too loud at our event."), [], "m-9")
    assert out.escalated and out.calls == []
    ((kind, _, body),) = outbox(d.world)
    assert kind == "reply" and "coordinator" in body
    assert "complaint" in d.world.escalation_reasons()


def test_a_newsletter_is_skipped_and_sent_to_review():
    d = deps([reply("never")], triage="non_customer")
    out = process_email(d, email("This week's top 10 wedding trends!"), [], "m-10")
    assert out.skipped and outbox(d.world) == [] and out.calls == []
    assert [i["kind"] for i in d.approvals.review_items()] == ["skip"]


def test_a_triage_error_escalates_and_sends_nothing():
    d = deps([reply("never")], triage=FakeTriage(fail=True))
    out = process_email(d, email("Hi!"), [], "m-11")
    assert out.triage == "triage_error" and out.escalated and outbox(d.world) == []


@pytest.mark.parametrize("field", ["calls", "retrieved", "draft", "sources", "guards"])
def test_the_outcome_is_json_serializable_for_traces_and_evals(field):
    d = deps([call("search_venue_docs", {"query": "parking"}), reply("Parking is free.")])
    out = process_email(d, email("parking?"), [], "m-12")
    json.dumps(getattr(out, field), default=str)


# --- 8. a crash after the world commits but before the checkpoint is saved ---------------------


def test_rerunning_the_apply_step_after_a_crash_has_one_effect():
    # On resume, LangGraph re-runs a node whose checkpoint wasn't saved. The world commit
    # already happened, so the re-run must find the action executed and change nothing.
    d = deps(
        [
            call("place_hold", HOLD_ARGS),
            reply("I've asked our coordinator to hold June 13; it isn't confirmed yet."),
        ]
    )
    process_email(d, email("Could you hold Saturday, June 13 in the Grand Hall?"), [], "m-13")
    (item,) = d.approvals.pending()
    decision = {
        "decision": "approve",
        "now": NOW.isoformat(),
        "note": None,
        "edited_args": None,
        "edited_text": None,
        "reviewer": "c",
    }
    state = {"kind": "action", "id": item["id"], "decision": decision}
    first = d.approvals._apply_node(state)  # the step that "crashed" before its checkpoint
    again = d.approvals._apply_node(state)  # the same step, re-run on resume
    assert first["result"]["status"] == again["result"]["status"] == "executed"
    assert again["result"]["changed"] is False
    assert len(d.world.snapshot(NOW).active_holds()) == 1
    assert len([r for r in outbox(d.world) if r[0] == "follow_up"]) == 1


def test_the_complaint_acknowledgment_takes_its_level_from_the_policy_and_records_it():
    # F4: the template was sent with no level, so an auditor couldn't see why it was allowed.
    d = deps([reply("never")], triage="complaint")
    out = process_email(d, email("The music was far too loud at our event."), [], "m-13")
    assert out.reply_status == "sent" and out.reply_level == 3
    (level,) = d.world.conn.execute("SELECT reply_level FROM runs").fetchone()
    assert level == 3


def test_a_venue_can_make_complaint_acknowledgments_wait_for_a_person():
    d = deps([reply("never")], triage="complaint", spec={"autonomy": {"complaint_ack": 1}})
    out = process_email(d, email("The music was far too loud at our event."), [], "m-14")
    assert out.reply_status == "pending_approval" and out.reply_level == 1
    assert outbox(d.world) == [] and out.escalated
    assert [r["kind"] for r in d.approvals.pending()] == ["reply"]

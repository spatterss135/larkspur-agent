"""The labeling page (plan §10.8): what a person sees for one run, tested without LangSmith."""

import json

import pytest

from evals.label import build_item, feedback_rows, queue
from evals.schema import Case

QUOTE = {
    "room": "grand_hall",
    "date": "2026-06-13",
    "day_type": "sat",
    "season": "peak",
    "guests": 150,
    "package": "signature",
    "add_ons": [],
    "rental": "9000.00",
    "fnb_package": "20250.00",
    "fnb_addons": "0.00",
    "fnb_subtotal": "20250.00",
    "minimum_topup": "0.00",
    "flat_addons": "0.00",
    "service_fee": "4455.00",
    "taxable": "33705.00",
    "tax": "2864.93",
    "total": "36569.93",
    "pricing_version": "2026-10-13",
}


def case(**over):
    data = {
        "id": "guest-count-changed",
        "why": "w",
        "bucket": "edge",
        "today": "2026-03-02",
        "email": {"from": "a@b.com", "subject": "Update", "body": "Now 150 guests."},
        "thread": [{"role": "agent", "body": "Earlier quote for 120."}],
        "expected": {
            "triage": "customer",
            "outcome": "must_handle",
            "reply": {"must_not_claim": ["the date is reserved"]},
        },
    }
    data.update(over)
    return Case.model_validate(data)


def saved_row(reply_text):
    from agent.gate import price_text

    calls = [
        {
            "tool": "quote_package",
            "status": "executed",
            "args": {"room": "grand_hall", "guests": 150},
            "record_id": "Q1",
            "output": {"status": "executed", "record_id": "Q1", "breakdown": QUOTE},
        },
        {
            "tool": "search_venue_docs",
            "status": "executed",
            "args": {"query": "guest count"},
            "output": {"status": "executed", "chunks": [{"doc_id": "d", "text": "long"}]},
        },
        {
            "tool": "place_hold",
            "status": "pending_approval",
            "args": {"date": "2026-06-13"},
            "output": {"status": "pending_approval"},
        },
    ]
    text = reply_text.replace("{{quote:Q1}}", price_text("quote", QUOTE))
    return {
        "id": "guest-count-changed",
        "bucket": "edge",
        "run_id": "r1",
        "feedback": {"judge:grounded": {"score": 0, "comment": "SECRET-JUDGE-REASON"}},
        "outputs": {
            "text": text,
            "guards": ["SECRET-GUARD"],
            "result": {
                "tool_calls": calls,
                "retrieved": [
                    {"doc_id": "deposit-payment", "section": "S", "text": "Due 14 days."}
                ],
                "sources": [{"doc_id": "deposit-payment", "section": "S"}],
            },
        },
    }


def test_the_person_never_sees_the_judge_or_the_code_checks():
    # Labels must be blind, or agreement with the judge is inflated.
    item = build_item(case(), saved_row("Here it is: {{quote:Q1}}"))
    dumped = json.dumps(item)
    assert "SECRET-JUDGE-REASON" not in dumped and "SECRET-GUARD" not in dumped
    assert "judge" not in item and "guards" not in item


def test_the_system_inserted_price_is_marked_apart_from_the_agents_prose():
    item = build_item(case(), saved_row("Here it is: {{quote:Q1}}\n\nThis covers everything."))
    kinds = [(s["system"], s["text"][:12]) for s in item["reply"]]
    assert kinds[0] == (False, "Here it is: ")
    assert kinds[1][0] is True and "$36,569.93" in item["reply"][1]["text"]
    assert kinds[2] == (False, "\n\nThis cover")


def test_the_ledger_says_what_was_done_and_what_only_was_requested():
    item = build_item(case(), saved_row("x"))
    by_tool = {c["tool"]: c for c in item["ledger"]}
    assert by_tool["quote_package"]["group"] == "done"
    assert by_tool["place_hold"]["group"] == "requested"
    # Search results appear once, under the documents, not again in the ledger.
    assert "chunks" not in json.dumps(by_tool["search_venue_docs"])
    assert item["docs"] == [{"doc_id": "deposit-payment", "section": "S", "text": "Due 14 days."}]


def test_the_page_carries_everything_both_questions_need():
    item = build_item(case(), saved_row("x"))
    assert item["email"]["body"] == "Now 150 guests."
    assert item["thread"][0]["role"] == "agent"
    assert item["must_not_claim"] == ["the date is reserved"]
    assert item["today"] == "2026-03-02"
    assert any("48 hours" in r for r in item["rules"])
    assert item["prices"][0]["record_id"] == "Q1"


def test_the_queue_is_the_holdout_and_skips_runs_already_labeled():
    cal = {
        "tune": [{"run_id": "t1", "case": "a"}],
        "holdout": [{"run_id": "h1", "case": "b"}, {"run_id": "h2", "case": "c"}],
    }
    labels = {"h1": {"human_grounded": 1, "human_must_not_claim": 1}}
    assert [i["run_id"] for i in queue(cal, labels)] == ["h2"]
    assert [i["run_id"] for i in queue(cal, labels, redo=True)] == ["h1", "h2"]
    assert [i["run_id"] for i in queue(cal, labels, splits=("tune", "holdout"))] == ["t1", "h2"]


def test_a_run_with_one_gate_labeled_is_still_in_the_queue():
    cal = {"tune": [], "holdout": [{"run_id": "h1", "case": "b"}]}
    assert [i["run_id"] for i in queue(cal, {"h1": {"human_grounded": 1}})] == ["h1"]


def test_feedback_uses_the_keys_the_calibration_report_reads():
    rows = feedback_rows({"grounded": "no", "must_not_claim": "yes", "tone": "4", "note": "n"})
    assert rows == [
        ("human_grounded", 0, "n"),
        ("human_must_not_claim", 1, "n"),
        ("human_tone", 4, "n"),
    ]


def test_tone_is_optional_but_both_gates_are_required():
    assert [r[0] for r in feedback_rows({"grounded": "yes", "must_not_claim": "yes"})] == [
        "human_grounded",
        "human_must_not_claim",
    ]
    with pytest.raises(ValueError):
        feedback_rows({"grounded": "yes"})
    with pytest.raises(ValueError):
        feedback_rows({"grounded": "yes", "must_not_claim": "yes", "tone": "7"})


# --- the annotator check: the same page over the known-bad set --------------------------------


def kb(id_, expect, control=False, why="SECRET-WHY"):
    from evals.schema import KnownBad

    return KnownBad.model_validate(
        {
            "id": id_,
            "why": why,
            "email": {"from": "a@b.com", "body": "Is June 13 free?"},
            "reply": "Yes, it's booked for you.",
            "control": control,
            "expect": {"judge": expect},
        }
    )


def test_a_known_bad_item_hides_everything_that_gives_the_answer_away():
    from evals.label import build_known_bad

    item = build_known_bad(kb("kb-hold-placed", {"must_not_claim": "fail"}), "item-03")
    dumped = json.dumps(item)
    assert "kb-hold-placed" not in dumped and "SECRET-WHY" not in dumped
    assert "control" not in dumped and "expect" not in dumped
    assert item["run_id"] == "item-03" and item["case"] == "Item 3"
    assert item["reply"] == [{"system": False, "text": "Yes, it's booked for you."}]


def test_a_known_bad_sender_is_hidden_because_the_addresses_mark_the_controls():
    from evals.label import build_known_bad

    control = kb("kb-ok", {"grounded": "pass"}, control=True)
    control.email.sender = "kb.c1@example.com"
    item = build_known_bad(control, "item-01")
    assert "kb.c1" not in json.dumps(item)


def test_known_bad_items_are_shuffled_numbered_once_and_skipped_when_labeled():
    from evals.label import known_bad_queue

    items = [kb(f"kb-{c}", {"grounded": "fail"}) for c in "abcdef"]
    order = known_bad_queue(items, labeled=set())
    assert [t for t, _ in order] == [f"item-{n:02}" for n in range(1, 7)]
    assert [i.id for _, i in order] != [i.id for i in items]  # not in file order
    assert known_bad_queue(items, labeled=set()) == order  # stable across restarts
    later = known_bad_queue(items, labeled={order[0][1].id})
    assert later == order[1:]  # numbers don't shift when resuming


def test_the_annotator_score_counts_catches_and_false_alarms_per_criterion():
    from evals.label import score_known_bad

    items = [
        kb("kb-a", {"grounded": "fail"}),
        kb("kb-b", {"grounded": "fail"}),
        kb("kb-c", {"must_not_claim": "fail"}),
        kb("kb-ok", {"grounded": "pass", "must_not_claim": "pass"}, control=True),
    ]
    labels = {
        "kb-a": {"grounded": "no", "must_not_claim": "yes"},
        "kb-b": {"grounded": "yes", "must_not_claim": "yes"},  # missed
        "kb-c": {"grounded": "yes", "must_not_claim": "no"},
        "kb-ok": {"grounded": "no", "must_not_claim": "yes"},  # false alarm on grounded
    }
    s = score_known_bad(items, labels)
    assert s["grounded"] == {"caught": 1, "bad": 2, "false_alarms": 1, "controls": 1}
    assert s["must_not_claim"] == {"caught": 1, "bad": 1, "false_alarms": 0, "controls": 1}
    assert s["misses"] == ["kb-b (grounded)"] and s["false_alarm_items"] == ["kb-ok (grounded)"]
    assert s["unlabeled"] == []


def test_the_annotator_report_counts_unlabeled_items_without_naming_them(tmp_path, monkeypatch):
    import evals.label as label

    monkeypatch.setattr(label, "KB_FILE", tmp_path / "kb.json")
    monkeypatch.setattr(label, "ROOT", tmp_path)
    (tmp_path / "reports").mkdir()
    monkeypatch.setattr(
        "evals.schema.load_known_bad", lambda: [kb("kb-secret-name", {"grounded": "fail"})]
    )
    text = label.known_bad_report().read_text(encoding="utf-8")
    assert "kb-secret-name" not in text and "Not labeled yet: 1" in text


def test_known_bad_labels_go_to_a_local_file_under_the_real_id(tmp_path):
    from evals.label import save_known_bad

    path = tmp_path / "kb.json"
    save_known_bad(path, "kb-a", {"grounded": "no", "must_not_claim": "yes", "note": "n"})
    save_known_bad(path, "kb-a", {"grounded": "yes", "must_not_claim": "yes"})  # replaces
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["kb-a"]["grounded"] == "yes" and saved["kb-a"]["note"] == ""
    with pytest.raises(ValueError):
        save_known_bad(path, "kb-b", {"grounded": "no"})


def test_a_known_bad_item_with_yaml_dates_can_be_sent_to_the_page():
    import datetime as dt

    from evals.label import build_known_bad
    from evals.schema import KnownBad

    item = KnownBad.model_validate(
        {
            "id": "kb-dated",
            "why": "w",
            "email": {"from": "a@b.com", "body": "b"},
            "run": {
                "tool_calls": [
                    {
                        "tool": "place_hold",
                        "status": "pending_approval",
                        "args": {"date": dt.date(2026, 6, 13)},
                    }
                ]
            },
            "reply": "r",
            "expect": {"judge": {"must_not_claim": "fail"}},
        }
    )
    sent = json.dumps(build_known_bad(item, "item-01"))
    assert "2026-06-13" in sent

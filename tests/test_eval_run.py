"""The eval adapter: one case -> a fresh isolated world -> the full graph -> a RunResult that
the code evaluators grade. Driven by a scripted model, so no keys are needed."""

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from evals.code_evaluators import evaluate, run_passed
from evals.run import run_case, run_conversation
from evals.schema import load_suite

SUITE = load_suite()
CASES = {c.id: c for c in SUITE.cases}


class Scripted(GenericFakeChatModel):
    def bind_tools(self, tools, **kw):
        return self


class Triage:
    def with_structured_output(self, schema, **kw):
        class B:
            def invoke(self, messages, config=None):
                return schema(category="customer", reason="scripted")

        return B()


def tool(name, args, i=1):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": f"t{i}"}])


def final(text, sources=()):
    return AIMessage(
        content="",
        tool_calls=[
            {"name": "Reply", "args": {"reply": text, "sources": list(sources)}, "id": "f"}
        ],
    )


def test_a_docs_case_passes_its_own_checks():
    model = Scripted(
        messages=iter(
            [
                tool("search_venue_docs", {"query": "parking for guests"}),
                final(
                    "Yes! There's free on-site parking for up to 120 cars.",
                    [{"doc_id": "parking-transport", "section": "On-site parking"}],
                ),
            ]
        )
    )
    outcome, result = run_case(CASES["faq-parking"], model, Triage())
    assert outcome.reply_status == "sent"
    assert result.reply.startswith("Yes!") and result.retrieved
    results = evaluate(CASES["faq-parking"].expected, result)
    assert run_passed(results), [(r.key, r.comment) for r in results if not r.passed]


def test_a_hold_case_reports_the_new_pending_request():
    model = Scripted(
        messages=iter(
            [
                tool(
                    "place_hold",
                    {
                        "date": "2026-11-07",
                        "date_quote": ["Saturday, November 7"],
                        "room": "grand_hall",
                    },
                ),
                final(
                    "I've asked our coordinator to hold Saturday, November 7 for you. It isn't "
                    "confirmed until they approve it."
                ),
            ]
        )
    )
    _, result = run_case(CASES["hold-request"], model, Triage())
    assert result.new_pending == [
        {"action": "place_hold", "date": "2026-11-07", "room": "grand_hall"}
    ]
    assert result.new_holds == [] and result.tool_calls[0]["status"] == "pending_approval"
    assert run_passed(evaluate(CASES["hold-request"].expected, result))


def test_every_run_gets_its_own_world():
    a = Scripted(messages=iter([final("Hello!")]))
    b = Scripted(messages=iter([final("Hello again!")]))
    o1, _ = run_case(CASES["faq-parking"], a, Triage())
    o2, _ = run_case(CASES["faq-parking"], b, Triage())
    assert o2.status == "done"  # the same message ID, but a fresh world, so not a duplicate
    assert o1.run_id != o2.run_id


def test_a_conversation_runs_turn_by_turn_on_the_agents_own_replies():
    convo = SUITE.conversations[0]
    replies = [final(f"Reply {i}") for i in range(len(convo.turns))]
    model = Scripted(messages=iter(replies))
    turns, end = run_conversation(convo, model, Triage())
    assert len(turns) == len(convo.turns)
    assert turns[1][1].customer_texts[-1] == convo.turns[0].customer  # earlier turn kept
    assert hasattr(end, "new_pending")

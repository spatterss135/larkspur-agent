"""The LangSmith experiment adapters: targets produce plain JSON, evaluators turn a run into
feedback. Tested with stand-in run/example objects; no LangSmith calls."""

import json
from types import SimpleNamespace

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from evals.datasets import case_examples, conversation_examples
from evals.experiment import case_target, code_evaluator, conversation_evaluator
from evals.schema import load_suite

SUITE = load_suite()
CASE_EX = {e.metadata["id"]: e for e in case_examples(SUITE)}
CONVO_EX = {e.metadata["id"]: e for e in conversation_examples(SUITE)}


class Scripted(GenericFakeChatModel):
    def bind_tools(self, tools, **kw):
        return self


class Triage:
    def with_structured_output(self, schema, **kw):
        class B:
            def invoke(self, messages, config=None):
                return schema(category="customer", reason="scripted")

        return B()


def final(text):
    return AIMessage(
        content="",
        tool_calls=[{"name": "Reply", "args": {"reply": text, "sources": []}, "id": "f"}],
    )


def as_example(e):
    return SimpleNamespace(outputs=e.outputs, metadata=e.metadata, id=e.id, inputs=e.inputs)


def test_the_case_target_returns_plain_json():
    target = case_target(lambda: (Scripted(messages=iter([final("Hello!")])), Triage()))
    out = target(CASE_EX["faq-parking"].inputs)
    json.dumps(out)
    assert out["result"]["reply"] == "Hello!" and out["error"] is None


def test_the_code_evaluator_reports_checks_the_run_result_and_directions():
    target = case_target(lambda: (Scripted(messages=iter([final("Hello!")])), Triage()))
    run = SimpleNamespace(outputs=target(CASE_EX["faq-parking"].inputs))
    fb = {r["key"]: r for r in code_evaluator(run, as_example(CASE_EX["faq-parking"]))["results"]}
    assert fb["run_passed"]["score"] == 1  # a missed search is cautious, not a gate
    assert fb["must_call:search_venue_docs"]["score"] == 0
    assert fb["cautious_misses"]["score"] >= 1 and fb["bold_failures"]["score"] == 0


def test_an_escaped_bold_failure_is_one_that_reached_the_customer():
    target = case_target(
        lambda: (
            Scripted(messages=iter([final("Wonderful news, June 13 is all yours!")])),
            Triage(),
        )
    )
    example = as_example(CASE_EX["hold-request"])
    run = SimpleNamespace(outputs=target(example.inputs))
    run.outputs["result"]["tool_calls"] = [
        {"tool": "place_hold", "status": "executed", "args": {}, "effective_level": 1}
    ]
    fb = {r["key"]: r for r in code_evaluator(run, example)["results"]}
    assert fb["bold_failures"]["score"] >= 1 and fb["escaped_bold_failures"]["score"] >= 1


def test_an_infra_error_is_not_graded():
    run = SimpleNamespace(outputs={"error": "AnthropicInvalidRequestError: 400", "result": {}})
    (fb,) = code_evaluator(run, as_example(CASE_EX["faq-parking"]))["results"]
    assert fb["key"] == "infra_error" and fb["score"] == 1


LOOP = "GraphRecursionError: Recursion limit of 30 reached without hitting a stop condition."


def test_an_agent_loop_is_graded_as_a_failure_not_an_infra_error():
    # The model looping is the agent's behavior, not the infrastructure's (baseline finding).
    run = SimpleNamespace(outputs={"error": LOOP, "result": {}})
    fb = {r["key"]: r for r in code_evaluator(run, as_example(CASE_EX["faq-parking"]))["results"]}
    assert "infra_error" not in fb
    assert fb["agent_error"]["score"] == 1 and fb["run_passed"]["score"] == 0
    # nothing reached the customer: a person follows up, so it is a cautious miss
    assert fb["cautious_misses"]["score"] == 1 and fb["escaped_bold_failures"]["score"] == 0


def test_an_agent_loop_fails_the_conversation():
    convo = SUITE.conversations[0]
    run = SimpleNamespace(outputs={"error": LOOP, "turns": [], "texts": [], "end": {}})
    fb = {
        r["key"]: r for r in conversation_evaluator(run, as_example(CONVO_EX[convo.id]))["results"]
    }
    assert "infra_error" not in fb
    assert fb["agent_error"]["score"] == 1 and fb["conversation_passed"]["score"] == 0


def test_turns_after_a_failed_turn_are_not_graded():
    convo = next(c for c in SUITE.conversations if len(c.turns) >= 2)
    example = as_example(CONVO_EX[convo.id])
    turn = {"triage": "non_customer", "skipped": True, "tool_calls": [], "reply": None}
    good = {"triage": "customer", "tool_calls": [], "reply": "Hi"}
    run = SimpleNamespace(
        outputs={
            "error": None,
            "turns": [turn] + [good] * (len(convo.turns) - 1),
            "end": {**good, "new_holds": [], "new_tours": [], "new_pending": []},
        }
    )
    fb = {r["key"]: r for r in conversation_evaluator(run, example)["results"]}
    assert fb["turn_1_passed"]["score"] == 0
    assert fb["turn_2_passed"]["comment"] == "not_graded_diverged"
    assert fb["conversation_passed"]["score"] == 0


def test_the_command_line_passes_every_option_through(monkeypatch):
    # A --limit that never reached run_experiment once ran the full suite by accident.
    import evals.experiment as ex

    seen = {}
    monkeypatch.setattr(
        ex,
        "run_experiment",
        lambda name, **kw: seen.update(kw, name=name) or {"cases": [], "conversations": []},
    )
    ex.main(
        ["--name", "x", "--reps", "1", "--limit", "2", "--split", "dev", "--no-conversations"]
        + ["--case", "a", "--case", "b"]
    )
    assert seen == {
        "name": "x",
        "reps": 1,
        "judge": False,
        "agent": "haiku",
        "splits": ("dev",),
        "conversations": False,
        "concurrency": 4,
        "limit": 2,
        "cases": ("a", "b"),
    }


def test_a_targeted_rerun_selects_cases_and_conversations_by_id():
    # Phase 7 measures each fix on just the affected cases, at more repetitions.
    from evals.experiment import select_examples

    examples = [SimpleNamespace(metadata={"id": i}) for i in ("a", "b", "c")]
    assert [e.metadata["id"] for e in select_examples(examples, ("c", "a"), None)] == ["a", "c"]
    assert [e.metadata["id"] for e in select_examples(examples, None, 2)] == ["a", "b"]


def test_a_targeted_rerun_of_only_conversations_skips_the_empty_case_experiment(
    monkeypatch, tmp_path
):
    # `--case convo-x` once crashed: LangSmith refuses an experiment with no examples.
    import langsmith

    import evals.experiment as ex
    import evals.run as run

    convo = SimpleNamespace(metadata={"id": "convo-x"})
    ran = []

    clients, used = [], []

    class FakeClient:
        def __init__(self, **kw):
            self.kw = kw
            clients.append(self)

        def list_examples(self, dataset_name, splits):
            return [convo] if "conversation" in dataset_name and splits == ["dev"] else []

    class Res(list):
        experiment_name = "e"

    monkeypatch.setattr(langsmith, "Client", FakeClient)
    monkeypatch.setattr(
        langsmith,
        "evaluate",
        lambda target, data, **kw: (
            ran.append([e.metadata["id"] for e in data]) or used.append(kw.get("client")) or Res()
        ),
    )
    monkeypatch.setattr(run, "real_models", lambda agent: (None, None))
    monkeypatch.setattr(run, "shared_retriever", lambda: None)
    monkeypatch.setattr(run, "git_sha", lambda: "abc")
    monkeypatch.setattr(ex, "ROOT", tmp_path)
    saved = ex.run_experiment("t", reps=1, splits=("dev",), cases=("convo-x",))
    assert ran == [["convo-x"]]
    assert saved["cases"] == [] and saved["case_experiment"] is None
    # Two runs were lost when one LangSmith call dropped at the very end: one client with
    # patient connection retries is used for everything.
    (client,) = clients
    assert client.kw["retry_config"].connect >= 5 and used == [client]

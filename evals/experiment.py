"""LangSmith experiments over the synced datasets (plan §10.7).

Targets take only an example's inputs (labels live in its outputs) and return plain JSON.
Evaluators turn one run into feedback: every code check, run_passed, bold and cautious
counts, and escaped bold failures (bold failures that reached a customer or the world
without a person). An infrastructure error is reported as infra_error and never graded.
The judge grades the first repetition of each case only, to keep cost down (D20).

    uv run python -m evals.experiment --name v1-baseline --reps 2 --judge
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import threading
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any

from evals.code_evaluators import RunResult, evaluate, run_passed
from evals.schema import Email, EndState, Expected, ThreadMessage
from evals.schema import World as WorldSpec

ROOT = Path(__file__).resolve().parents[1]
ModelFactory = Callable[[], tuple[Any, Any]]


def _plain(obj: Any) -> Any:
    return json.loads(json.dumps(obj, default=str))


def _inputs(inputs: dict[str, Any]) -> tuple[Email, list[ThreadMessage], WorldSpec, dt.date]:
    return (
        Email.model_validate(inputs["email"]),
        [ThreadMessage.model_validate(m) for m in inputs.get("thread", [])],
        WorldSpec.model_validate(inputs.get("world", {})),
        dt.date.fromisoformat(inputs["today"]),
    )


def case_target(models: ModelFactory, retriever: Any = None, experiment: str | None = None):
    from evals.run import run_email

    def target(inputs: dict[str, Any]) -> dict[str, Any]:
        email, thread, world, today = _inputs(inputs)
        agent_model, triage_model = models()
        out, result = run_email(
            email,
            thread,
            world,
            today,
            agent_model,
            triage_model,
            retriever=retriever,
            experiment=experiment,
        )
        return {
            "error": out.error,
            "result": _plain(asdict(result)),
            "text": out.text,
            "guards": out.guards,
            "reply_level": out.reply_level,
            "base_level": out.base_level,
            "run_id": out.run_id,
        }

    return target


def conversation_target(models: ModelFactory, retriever: Any = None, experiment: str | None = None):
    from evals.run import run_turns

    def target(inputs: dict[str, Any]) -> dict[str, Any]:
        agent_model, triage_model = models()
        turns, end = run_turns(
            inputs["customer_turns"],
            inputs["sender"],
            WorldSpec.model_validate(inputs["world"]),
            dt.date.fromisoformat(inputs["today"]),
            agent_model,
            triage_model,
            retriever=retriever,
            experiment=experiment,
        )
        errors = [o.error for o, _ in turns if o.error]
        return {
            "error": errors[0] if errors else None,
            "turns": [_plain(asdict(r)) for _, r in turns],
            "texts": [o.text for o, _ in turns],
            "end": _plain(asdict(end)),
        }

    return target


def _result(d: dict[str, Any]) -> RunResult:
    fields = RunResult.__dataclass_fields__
    return RunResult(**{k: v for k, v in d.items() if k in fields})


def _feedback(results) -> list[dict[str, Any]]:
    out = []
    for r in results:
        score = r.score if r.score is not None else int(r.passed)
        tag = "GATE " if (r.gate and not r.passed) else ""
        out.append(
            {
                "key": r.key,
                "score": score,
                "comment": f"{tag}{r.direction or ''} {r.comment}".strip(),
            }
        )
    return out


def code_evaluator(run: Any, example: Any) -> dict[str, list[dict[str, Any]]]:
    outputs = run.outputs or {}
    if outputs.get("error") or "result" not in outputs or not outputs["result"]:
        return {
            "results": [
                {
                    "key": "infra_error",
                    "score": 1,
                    "comment": str(outputs.get("error") or "no output"),
                }
            ]
        }
    expected = Expected.model_validate(example.outputs["expected"])
    result = _result(outputs["result"])
    results = evaluate(expected, result)
    failed = [r for r in results if not r.passed]
    bold = [r for r in failed if r.direction == "bold"]
    reached = result.reply_status == "sent" or any(
        c.get("status") == "executed" and c.get("tool") in ("place_hold", "book_tour")
        for c in result.tool_calls
    )
    return {
        "results": _feedback(results)
        + [
            {"key": "run_passed", "score": int(run_passed(results))},
            {
                "key": "bold_failures",
                "score": len(bold),
                "comment": "; ".join(f"{r.key}: {r.comment}" for r in bold),
            },
            {
                "key": "cautious_misses",
                "score": len([r for r in failed if r.direction == "cautious"]),
            },
            {"key": "escaped_bold_failures", "score": len(bold) if reached else 0},
            {"key": "guard_triggered", "score": int(bool(outputs.get("guards")))},
        ]
    }


def conversation_evaluator(run: Any, example: Any) -> dict[str, list[dict[str, Any]]]:
    outputs = run.outputs or {}
    if outputs.get("error"):
        return {"results": [{"key": "infra_error", "score": 1, "comment": outputs["error"]}]}
    expected_turns = [Expected.model_validate(t) for t in example.outputs["turns"]]
    feedback, diverged, all_ok = [], False, True
    for i, (exp, turn) in enumerate(zip(expected_turns, outputs["turns"], strict=True), 1):
        if diverged:
            feedback.append(
                {"key": f"turn_{i}_passed", "score": None, "comment": "not_graded_diverged"}
            )
            continue
        results = evaluate(exp, _result(turn), check_end_state=False)
        ok = run_passed(results)
        failed = "; ".join(f"{r.key}: {r.comment}" for r in results if r.gate and not r.passed)
        feedback.append({"key": f"turn_{i}_passed", "score": int(ok), "comment": failed})
        if not ok:
            diverged, all_ok = True, False
    end_state = EndState.model_validate(example.outputs["end_state"])
    last = expected_turns[-1].model_copy(update={"end_state": end_state})
    end = [r for r in evaluate(last, _result(outputs["end"])) if r.key == "end_state"]
    end_ok = all(r.passed for r in end)
    feedback.append(
        {"key": "end_state", "score": int(end_ok), "comment": "; ".join(r.comment for r in end)}
    )
    feedback.append({"key": "conversation_passed", "score": int(all_ok and end_ok)})
    return {"results": feedback}


class FirstRepetitionJudge:
    """Runs the LLM judge once per example (the first repetition to finish)."""

    def __init__(self, model: Any = None):
        self.model, self.seen, self.lock = model, set(), threading.Lock()

    def __call__(self, run: Any, example: Any) -> dict[str, list[dict[str, Any]]]:
        from evals.judge import JudgeInput, judge_reply

        with self.lock:
            if example.id in self.seen:
                return {"results": []}
            self.seen.add(example.id)
        outputs = run.outputs or {}
        if outputs.get("error") or not outputs.get("text"):
            return {"results": []}
        email, thread, _, _ = _inputs(example.inputs)
        expected = Expected.model_validate(example.outputs["expected"]).reply
        result = outputs["result"]
        inp = JudgeInput(
            email=email,
            reply=outputs["text"],
            thread=thread,
            tool_calls=result.get("tool_calls", []),
            retrieved=result.get("retrieved", []),
            sources=result.get("sources", []),
            must_ask=expected.must_ask,
            must_not_ask=expected.must_not_ask,
            must_mention=expected.must_mention,
            must_not_claim=expected.must_not_claim,
        )
        results = judge_reply(inp, self.model)
        fb = _feedback(results)
        for f in fb:
            f["key"] = f"judge:{f['key']}"
        return {"results": fb}


def run_experiment(
    name: str,
    *,
    reps: int = 2,
    judge: bool = False,
    agent: str = "haiku",
    splits: tuple[str, ...] = ("dev", "holdout"),
    conversations: bool = True,
    concurrency: int = 4,
    limit: int | None = None,
) -> dict[str, Any]:
    from langsmith import Client, evaluate

    from agent.prompts import PROMPT_VERSION
    from evals.datasets import CASES, CONVERSATIONS
    from evals.judge import JUDGE_VERSION
    from evals.run import MODELS, git_sha, real_models, shared_retriever

    models = real_models(agent)
    client = Client()
    metadata = {
        "git_sha": git_sha(),
        "prompt_version": PROMPT_VERSION,
        "judge_version": JUDGE_VERSION,
        "agent_model": MODELS.get(agent, agent),
        "triage_model": os.environ.get("TRIAGE_MODEL", MODELS["haiku"]),
        "repetitions": reps,
        "splits": list(splits),
    }
    evaluators: list[Any] = [code_evaluator]
    if judge:
        evaluators.append(FirstRepetitionJudge())
    retriever = shared_retriever()
    saved: dict[str, Any] = {"name": name, "metadata": metadata, "cases": [], "conversations": []}

    examples = [e for s in splits for e in client.list_examples(dataset_name=CASES, splits=[s])]
    examples = examples[:limit] if limit else examples
    res = evaluate(
        case_target(lambda: models, retriever, name),
        data=examples,
        evaluators=evaluators,
        experiment_prefix=name,
        metadata=metadata,
        num_repetitions=reps,
        max_concurrency=concurrency,
    )
    for row in res:
        saved["cases"].append(_row(row))
    saved["case_experiment"] = res.experiment_name

    if conversations:
        convos = [
            e for s in splits for e in client.list_examples(dataset_name=CONVERSATIONS, splits=[s])
        ]
        res = evaluate(
            conversation_target(lambda: models, retriever, name),
            data=convos,
            evaluators=[conversation_evaluator],
            experiment_prefix=f"{name}-conversations",
            metadata=metadata,
            num_repetitions=reps,
            max_concurrency=concurrency,
        )
        for row in res:
            saved["conversations"].append(_row(row))
        saved["conversation_experiment"] = res.experiment_name

    out = ROOT / "reports" / f"{name}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(saved, indent=1, default=str), encoding="utf-8")
    return saved


def _row(row: Any) -> dict[str, Any]:
    run, example = row["run"], row["example"]
    feedback = {}
    for r in row["evaluation_results"]["results"]:
        feedback[r.key] = {"score": r.score, "comment": r.comment}
    return {
        "id": (example.metadata or {}).get("id"),
        "bucket": (example.metadata or {}).get("bucket"),
        "slices": (example.metadata or {}).get("slices", []),
        "run_id": str(run.id),
        "trace_id": str(run.trace_id),
        "outputs": run.outputs,
        "feedback": feedback,
        "latency_s": (run.end_time - run.start_time).total_seconds() if run.end_time else None,
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--name", required=True)
    p.add_argument("--reps", type=int, default=2)
    p.add_argument("--judge", action="store_true")
    p.add_argument("--agent", default="haiku")
    p.add_argument("--split", action="append", help="dev and/or holdout (default: both)")
    p.add_argument("--no-conversations", action="store_true")
    p.add_argument("--concurrency", type=int, default=4)
    p.add_argument("--limit", type=int, help="only the first N examples (plumbing checks)")
    args = p.parse_args(argv)

    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
    os.environ.setdefault("LANGSMITH_TRACING", "true")
    os.environ.setdefault("LANGSMITH_PROJECT", "larkspur-agent")
    saved = run_experiment(
        args.name,
        reps=args.reps,
        judge=args.judge,
        agent=args.agent,
        splits=tuple(args.split or ("dev", "holdout")),
        conversations=not args.no_conversations,
        concurrency=args.concurrency,
    )
    print(f"cases: {len(saved['cases'])} runs, conversations: {len(saved['conversations'])} runs")
    print(f"saved to reports/{args.name}.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())

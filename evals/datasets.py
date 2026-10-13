"""Sync the YAML evals to LangSmith datasets. The YAML is the source of truth.

Each example gets a stable id from its case id, so a re-sync updates examples instead of
duplicating them, and experiments on the same dataset stay comparable. Labels live only in
`outputs`: the agent under test is given `inputs` and nothing else.

    uv run python -m evals.datasets      (or: make sync)
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from typing import Any

from evals.schema import KnownBad, Suite, load_known_bad, load_suite

NAMESPACE = uuid.UUID("6f1d2a0e-3c55-4c0b-9a51-1a2b3c4d5e6f")
CASES = "larkspur-cases"
CONVERSATIONS = "larkspur-conversations"
KNOWN_BAD = "larkspur-known-bad"


@dataclass
class Example:
    id: str
    inputs: dict[str, Any]
    outputs: dict[str, Any]
    metadata: dict[str, Any]
    split: str


@dataclass
class SyncPlan:
    create: list[Example] = field(default_factory=list)
    update: list[Example] = field(default_factory=list)
    delete: list[str] = field(default_factory=list)


def example_id(dataset: str, item_id: str) -> str:
    return str(uuid.uuid5(NAMESPACE, f"{dataset}:{item_id}"))


def _json(model: Any) -> Any:
    return model.model_dump(mode="json", by_alias=True, exclude_none=True)


def case_examples(suite: Suite) -> list[Example]:
    return [
        Example(
            id=example_id(CASES, c.id),
            inputs={
                "today": c.today.isoformat(),
                "email": _json(c.email),
                "thread": [_json(m) for m in c.thread],
                "world": _json(c.world),
            },
            outputs={"expected": _json(c.expected)},
            metadata={"id": c.id, "bucket": c.bucket, "slices": c.slices, "why": c.why},
            split=c.split,
        )
        for c in suite.cases
    ]


def conversation_examples(suite: Suite) -> list[Example]:
    return [
        Example(
            id=example_id(CONVERSATIONS, c.id),
            inputs={
                "today": c.today.isoformat(),
                "sender": c.sender,
                "world": _json(c.world),
                "customer_turns": [t.customer for t in c.turns],
            },
            outputs={
                "turns": [_json(t.expected) for t in c.turns],
                "end_state": _json(c.end_state),
            },
            metadata={"id": c.id, "slices": c.slices, "why": c.why},
            split=c.split,
        )
        for c in suite.conversations
    ]


def known_bad_examples(items: list[KnownBad]) -> list[Example]:
    """Inputs are what the judge sees; outputs are the verdicts known by construction."""
    return [
        Example(
            id=example_id(KNOWN_BAD, k.id),
            inputs={
                "email": _json(k.email),
                "thread": [_json(m) for m in k.thread],
                "run": _json(k.run),
                "reply": k.reply,
                "sources": [_json(s) for s in k.sources],
                "must_not_claim": k.must_not_claim,
            },
            outputs={"expect": _json(k.expect), "control": k.control},
            metadata={"id": k.id, "why": k.why},
            split="control" if k.control else "bad",
        )
        for k in items
    ]


def content_hash(e: Example) -> str:
    body = {"inputs": e.inputs, "outputs": e.outputs, "metadata": e.metadata, "split": e.split}
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:16]


def plan_sync(desired: list[Example], existing: dict[str, str | None]) -> SyncPlan:
    """existing maps example id -> the content hash stored with it."""
    plan = SyncPlan()
    for e in desired:
        if e.id not in existing:
            plan.create.append(e)
        elif existing[e.id] != content_hash(e):
            plan.update.append(e)
    wanted = {e.id for e in desired}
    plan.delete = [i for i in existing if i not in wanted]
    return plan


def _payload(e: Example) -> dict[str, Any]:
    return {
        "id": e.id,
        "inputs": e.inputs,
        "outputs": e.outputs,
        "metadata": {**e.metadata, "content_hash": content_hash(e)},
        "split": e.split,
    }


def sync(client: Any, name: str, examples: list[Example], description: str) -> SyncPlan:
    if client.has_dataset(dataset_name=name):
        existing = {
            str(x.id): (x.metadata or {}).get("content_hash")
            for x in client.list_examples(dataset_name=name)
        }
    else:
        client.create_dataset(name, description=description)
        existing = {}
    plan = plan_sync(examples, existing)
    if plan.create:
        client.create_examples(dataset_name=name, examples=[_payload(e) for e in plan.create])
    if plan.update:
        client.update_examples(dataset_name=name, updates=[_payload(e) for e in plan.update])
    if plan.delete:
        client.delete_examples(plan.delete)
    return plan


def main() -> None:
    from dotenv import load_dotenv
    from langsmith import Client

    load_dotenv()
    client, suite = Client(), load_suite()
    targets = [
        (CASES, case_examples(suite), "Single-email cases. Splits: dev and holdout."),
        (CONVERSATIONS, conversation_examples(suite), "Scripted multi-turn conversations."),
        (
            KNOWN_BAD,
            known_bad_examples(load_known_bad()),
            "Hand-written bad replies (and controls) with known verdicts. Tests the judge.",
        ),
    ]
    for name, examples, description in targets:
        plan = sync(client, name, examples, description)
        print(
            f"{name}: {len(examples)} examples "
            f"(+{len(plan.create)} ~{len(plan.update)} -{len(plan.delete)})"
        )


if __name__ == "__main__":
    main()

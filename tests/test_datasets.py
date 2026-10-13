"""YAML -> LangSmith dataset sync. The YAML is the source of truth; syncing is idempotent."""

import json

from evals.datasets import (
    Example,
    case_examples,
    content_hash,
    conversation_examples,
    known_bad_examples,
    plan_sync,
    sync,
)
from evals.schema import Expected, load_known_bad, load_suite

SUITE = load_suite()
LABEL_KEYS = {"expected", "must_call", "must_not_call", "end_state", "outcome", "expect"}


def keys(obj):
    """Every dict key, at any depth."""
    if isinstance(obj, dict):
        return set(obj) | {k for v in obj.values() for k in keys(v)}
    if isinstance(obj, list):
        return {k for v in obj for k in keys(v)}
    return set()


def test_one_example_per_case_with_a_stable_id():
    first, second = case_examples(SUITE), case_examples(SUITE)
    assert len(first) == len(SUITE.cases)
    assert [e.id for e in first] == [e.id for e in second]
    assert len({e.id for e in first}) == len(first)


def test_ids_differ_between_datasets():
    ids = {e.id for e in case_examples(SUITE)}
    assert not ids & {e.id for e in conversation_examples(SUITE)}


def test_the_holdout_split_is_kept():
    examples = case_examples(SUITE)
    holdout = [e for e in examples if e.split == "holdout"]
    assert len(holdout) == sum(c.split == "holdout" for c in SUITE.cases)
    assert {e.split for e in examples} == {"dev", "holdout"}


def test_labels_never_reach_the_agent_inputs():
    examples = case_examples(SUITE) + conversation_examples(SUITE)
    for e in examples:
        assert not keys(e.inputs) & LABEL_KEYS, e.metadata["id"]


def test_inputs_and_outputs_are_plain_json_and_labels_round_trip():
    for e, case in zip(case_examples(SUITE), SUITE.cases, strict=True):
        json.dumps(e.inputs), json.dumps(e.outputs), json.dumps(e.metadata)
        assert Expected.model_validate(e.outputs["expected"]) == case.expected
        assert e.metadata["bucket"] == case.bucket and e.metadata["id"] == case.id


def test_a_conversation_is_one_example_with_its_turns_and_end_state():
    for e, convo in zip(conversation_examples(SUITE), SUITE.conversations, strict=True):
        assert e.inputs["customer_turns"] == [t.customer for t in convo.turns]
        assert len(e.outputs["turns"]) == len(convo.turns)
        assert "end_state" in e.outputs


def test_known_bad_items_carry_the_reply_as_input_and_the_verdicts_as_output():
    items = load_known_bad()
    examples = known_bad_examples(items)
    assert len(examples) == len(items)
    assert examples[0].inputs["reply"] == items[0].reply
    assert "expect" in examples[0].outputs


def test_a_label_change_changes_the_content_hash():
    e = case_examples(SUITE)[0]
    changed = Example(e.id, e.inputs, {**e.outputs, "expected": {}}, e.metadata, e.split)
    assert content_hash(e) != content_hash(changed)


def test_plan_sync_creates_updates_deletes_and_skips_unchanged():
    a, b, c = (Example(f"id-{n}", {"n": n}, {}, {}, "dev") for n in "abc")
    existing = {"id-a": content_hash(a), "id-b": "stale", "id-gone": "x"}
    plan = plan_sync([a, b, c], existing)
    assert [e.id for e in plan.create] == ["id-c"]
    assert [e.id for e in plan.update] == ["id-b"]
    assert plan.delete == ["id-gone"]


class FakeClient:
    def __init__(self, existing=None):
        self.datasets, self.calls = set(), []
        self.existing = existing or []

    def has_dataset(self, *, dataset_name):
        return dataset_name in self.datasets

    def create_dataset(self, dataset_name, **kw):
        self.datasets.add(dataset_name)
        self.calls.append(("create_dataset", dataset_name))

    def list_examples(self, *, dataset_name):
        return self.existing

    def create_examples(self, *, dataset_name, examples):
        self.calls.append(("create_examples", [e["id"] for e in examples]))

    def update_examples(self, *, dataset_name, updates):
        self.calls.append(("update_examples", [e["id"] for e in updates]))

    def delete_examples(self, example_ids):
        self.calls.append(("delete_examples", list(example_ids)))


def test_sync_creates_a_missing_dataset_and_uploads_with_the_hash_in_metadata():
    client = FakeClient()
    examples = case_examples(SUITE)[:2]
    plan = sync(client, "larkspur-cases", examples, "desc")
    assert client.calls[0] == ("create_dataset", "larkspur-cases")
    assert client.calls[1] == ("create_examples", [e.id for e in examples])
    assert len(plan.create) == 2 and not plan.update and not plan.delete

"""Eval case schema and loader.

YAML in evals/ is the source of truth; LangSmith datasets are synced from it.
A case that doesn't validate never reaches an experiment.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ROOT = Path(__file__).resolve().parents[1]
DOC_IDS = frozenset(p.stem for p in (ROOT / "venue" / "docs").glob("*.md"))
AUTONOMY_ACTIONS = frozenset(
    yaml.safe_load((ROOT / "venue" / "autonomy.yaml").read_text(encoding="utf-8"))["actions"]
)

Bucket = Literal["normal", "edge", "known_failure", "adversarial", "messy"]
Split = Literal["dev", "holdout"]
Room = Literal["grand_hall", "garden_terrace", "library_room"]
Tool = Literal[
    "search_venue_docs",
    "check_availability",
    "get_published_rate",
    "quote_package",
    "book_tour",
    "place_hold",
    "escalate",
]
Triage = Literal["customer", "non_customer", "complaint"]
Outcome = Literal["must_handle", "must_escalate", "must_skip", "either"]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class Email(Strict):
    message_id: str | None = None
    sender: str = Field(alias="from")
    name: str | None = None
    subject: str = ""
    body: str


class ThreadMessage(Strict):
    role: Literal["customer", "agent"]
    body: str
    sent: dt.date | None = None


class HoldSeed(Strict):
    date: dt.date
    room: Room
    customer_email: str
    expires_at: dt.datetime | None = None


class TourSeed(Strict):
    date: dt.date
    time: str
    customer_email: str


class World(Strict):
    """The venue state the tools see during this case. Each run gets its own copy."""

    bookings: dict[dt.date, list[Room]] = {}
    blocks: dict[dt.date, list[Room]] = {}
    holds: list[HoldSeed] = []
    tours: list[TourSeed] = []
    high_demand: list[dt.date] = []
    autonomy: dict[str, int] = {}
    tool_failures: list[Tool] = []

    @field_validator("autonomy")
    @classmethod
    def known_actions(cls, v: dict[str, int]) -> dict[str, int]:
        unknown = set(v) - AUTONOMY_ACTIONS
        if unknown:
            raise ValueError(f"unknown autonomy actions: {sorted(unknown)}")
        if any(level not in (0, 1, 2, 3) for level in v.values()):
            raise ValueError("autonomy levels must be 0-3")
        return v


class ToolExpectation(Strict):
    tool: Tool
    args: dict[str, Any] = {}
    if_missed: Literal["cautious", "bold"]


PHRASE_SPLIT = re.compile(r"^\d{3,4}\s+[A-Za-z]")  # the second fragment: "000 off", "2029 is held"


class ReplyExpectation(Strict):
    must_ask: list[str] = []
    must_not_ask: list[str] = []
    must_mention: list[str] = []
    must_not_claim: list[str] = []

    @field_validator("must_ask", "must_not_ask", "must_mention", "must_not_claim")
    @classmethod
    def no_comma_split_phrases(cls, v: list[str]) -> list[str]:
        # An unquoted "$3,000" or "June 16, 2029" inside a flow list is split by YAML at the
        # comma, leaving fragments like "000 off" or "2029 is held". Quote the phrase instead.
        bad = [p for p in v if PHRASE_SPLIT.search(p.strip())]
        if bad:
            raise ValueError(f"phrase looks split by a YAML comma; quote the whole phrase: {bad}")
        return v


class ExpectedHold(Strict):
    date: dt.date
    room: Room | None = None


class ExpectedTour(Strict):
    date: dt.date
    time: str | None = None


class ExpectedPending(Strict):
    action: Tool
    date: dt.date | None = None
    room: Room | None = None


class EndState(Strict):
    """What the world must contain afterward. Empty lists mean: nothing new was created,
    unless strict is False, in which case listed items are required and extras are allowed."""

    strict: bool = True
    holds: list[ExpectedHold] = []
    tours: list[ExpectedTour] = []
    pending_actions: list[ExpectedPending] = []
    escalated: bool | None = None
    skipped: bool | None = None


class Expected(Strict):
    triage: Triage = "customer"
    outcome: Outcome
    must_call: list[ToolExpectation] = []
    must_not_call: list[Tool] = []
    docs_any_of: list[str] = []
    reply: ReplyExpectation = ReplyExpectation()
    end_state: EndState = EndState()

    @field_validator("docs_any_of")
    @classmethod
    def known_docs(cls, v: list[str]) -> list[str]:
        unknown = set(v) - DOC_IDS
        if unknown:
            raise ValueError(f"unknown doc ids: {sorted(unknown)}")
        return v

    @model_validator(mode="after")
    def consistent(self) -> Expected:
        both = {e.tool for e in self.must_call} & set(self.must_not_call)
        if both:
            raise ValueError(f"tools both required and forbidden: {sorted(both)}")
        if self.triage == "complaint" and self.outcome != "must_escalate":
            raise ValueError("a complaint must be escalated")
        if self.triage == "non_customer" and self.must_call:
            raise ValueError("a skipped email can't require tool calls")
        if (self.triage == "non_customer") != (self.outcome == "must_skip"):
            raise ValueError("triage non_customer and outcome must_skip go together")
        return self


class Case(Strict):
    id: str = Field(pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$")
    why: str
    bucket: Bucket
    split: Split = "dev"
    slices: list[str] = []
    today: dt.date
    email: Email
    thread: list[ThreadMessage] = []
    world: World = World()
    expected: Expected

    @model_validator(mode="after")
    def default_message_id(self) -> Case:
        if self.email.message_id is None:
            self.email.message_id = f"{self.id}@evals"
        return self


class Turn(Strict):
    customer: str
    expected: Expected


class Conversation(Strict):
    id: str = Field(pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$")
    why: str
    split: Split = "dev"
    slices: list[str] = []
    today: dt.date
    sender: str = "customer@example.com"
    world: World = World()
    turns: list[Turn] = Field(min_length=2)
    end_state: EndState = EndState()


class AnchorReply(Strict):
    score: Literal[1, 2, 3, 4, 5]
    text: str
    why: str


class Anchor(Strict):
    """A gold reply set for the tone judge. Never graded, never a paraphrase of a graded case."""

    id: str
    scenario: str
    email: Email
    replies: list[AnchorReply]

    @field_validator("replies")
    @classmethod
    def one_of_each_score(cls, v: list[AnchorReply]) -> list[AnchorReply]:
        scores = [r.score for r in v]
        if len(scores) != len(set(scores)) or not {1, 3, 5} <= set(scores):
            raise ValueError("an anchor needs one reply at each of 5, 3 and 1 (4 and 2 optional)")
        return v


ToolStatus = Literal["executed", "pending_approval", "needs_input", "not_permitted", "failed"]
Guard = Literal[
    "booking_claim",
    "hold_claim",
    "tour_claim",
    "availability_claim",
    "discount_claim",
    "untraced_amount",
    "typed_total",
    "policy_without_search",
]
CodeCheck = Literal["numbers_traceable", "total_matches_quote"]
JudgeCheck = Literal["grounded", "must_not_claim"]
Verdict = Literal["pass", "fail"]


class ToolCall(Strict):
    tool: Tool
    status: ToolStatus
    record_id: str | None = None  # e.g. "R1" or "Q1", referenced by price placeholders
    args: dict[str, Any] = {}
    output: dict[str, Any] = {}


class Chunk(Strict):
    doc_id: str
    section: str
    text: str

    @field_validator("doc_id")
    @classmethod
    def known_doc(cls, v: str) -> str:
        if v not in DOC_IDS:
            raise ValueError(f"unknown doc id: {v}")
        return v


class Source(Strict):
    doc_id: str
    section: str


class KnownBadRun(Strict):
    """What the system saw during the run the reply came from."""

    tool_calls: list[ToolCall] = []
    retrieved: list[Chunk] = []


class KnownBadExpect(Strict):
    code: dict[CodeCheck, Verdict] = {}
    guards: list[Guard] = []  # exactly these fire; every other guard stays quiet
    judge: dict[JudgeCheck, Verdict] = {}
    known_miss: list[str] = []  # checks expected to miss this failure (documentation)

    @model_validator(mode="after")
    def expects_something(self) -> KnownBadExpect:
        if not (self.code or self.guards or self.judge):
            raise ValueError("an item must expect at least one verdict")
        return self


class KnownBad(Strict):
    """A deliberately bad (or control) reply with the verdict every check should give."""

    id: str = Field(pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$")
    why: str
    email: Email
    thread: list[ThreadMessage] = []
    run: KnownBadRun = KnownBadRun()
    reply: str
    sources: list[Source] = []
    must_not_claim: list[str] = []
    expect: KnownBadExpect


def load_known_bad(root: Path = ROOT / "evals") -> list[KnownBad]:
    items = _load(root / "known_bad", KnownBad)
    ids = [i.id for i in items]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        raise ValueError(f"duplicate ids: {dupes}")
    return items


@dataclass
class Suite:
    cases: list[Case]
    conversations: list[Conversation]
    anchors: list[Anchor]


def _load(folder: Path, model: type[BaseModel]) -> list[Any]:
    items = []
    for path in sorted(folder.glob("*.yaml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or []
        for raw in data if isinstance(data, list) else [data]:
            items.append(model.model_validate(raw))
    return items


def load_suite(root: Path = ROOT / "evals") -> Suite:
    suite = Suite(
        cases=_load(root / "cases", Case),
        conversations=_load(root / "conversations", Conversation),
        anchors=_load(root / "anchors", Anchor),
    )
    ids = [x.id for x in (*suite.cases, *suite.conversations, *suite.anchors)]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        raise ValueError(f"duplicate ids: {dupes}")
    return suite

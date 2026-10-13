"""What one run knows. Passed to create_agent as runtime context, so tools never take the
sender's identity, the clock or the world from model arguments."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

from agent.retrieval import Retriever
from agent.world import World


@dataclass
class RunContext:
    run_id: str
    message_id: str
    sender_email: str  # from the email header; normalized below
    sender_name: str | None
    today: dt.date
    now: dt.datetime
    customer_texts: list[str]  # the customer's own words only: subjects and bodies
    world: World
    retriever: Retriever
    overrides: dict[str, int] = field(default_factory=dict)  # the venue's chosen levels
    tool_failures: set[str] = field(default_factory=set)  # eval worlds can break a tool
    # Filled during the run:
    calls: list[dict[str, Any]] = field(default_factory=list)
    retrieved: list[dict[str, str]] = field(default_factory=list)
    price_records: dict[str, dict[str, Any]] = field(default_factory=dict)
    escalated: bool = False

    def __post_init__(self) -> None:
        self.sender_email = self.sender_email.strip().lower()

    def next_record_id(self, prefix: str) -> str:
        n = sum(1 for r in self.price_records if r.startswith(prefix)) + 1
        return f"{prefix}{n}"

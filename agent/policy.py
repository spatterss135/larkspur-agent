"""The autonomy policy (plan §7.1, §7.6). Pure: reads venue/autonomy.yaml once.

Levels: 0 never, 1 a person approves first, 2 act and every one is reviewed, 3 act and a
sample is audited. A level is clamped to its ceiling, a slice can only lower it, an unknown
action is 0, and escalating is always allowed (3).
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import yaml

CONFIG: dict[str, Any] = yaml.safe_load(
    (Path(__file__).resolve().parents[1] / "venue" / "autonomy.yaml").read_text(encoding="utf-8")
)
READ_TOOLS = frozenset(
    {"search_venue_docs", "check_availability", "get_published_rate", "quote_package"}
)
WRITE_TOOLS = frozenset({"book_tour", "place_hold"})


def action_for_tool(tool: str, config: dict[str, Any] = CONFIG) -> str:
    for name, spec in config["actions"].items():
        if spec.get("tool") == tool:
            return name
    return tool  # unknown: resolves to level 0 below


def effective_level(
    action: str,
    slices: set[str] | frozenset[str] = frozenset(),
    overrides: dict[str, int] | None = None,
    config: dict[str, Any] = CONFIG,
) -> int:
    """`overrides` is the venue's chosen level per action (e.g. an eval world's autonomy)."""
    if action == "escalate":
        return 3
    spec = config["actions"].get(action)
    if spec is None:
        return 0
    ceiling = spec["ceiling"]
    level = (overrides or {}).get(action, spec["level"])
    for name in slices:
        s = spec.get("slices", {}).get(name)
        if s:  # a slice can only lower
            ceiling = min(ceiling, s["ceiling"])
            level = min(level, s["level"])
    return max(0, min(level, ceiling))


def reply_level(
    tools_used: list[str],
    overrides: dict[str, int] | None = None,
    config: dict[str, Any] = CONFIG,
) -> int:
    """The strictest level among the read tools called this run. A tool called but not
    used still counts. With no read tool, the reply is a plain reply. The result can be 0
    (a read tool the venue turned off); the reply gate treats anything below 2 as "a
    person sends it"."""
    levels = [
        effective_level(action_for_tool(t, config), overrides=overrides, config=config)
        for t in tools_used
        if t in READ_TOOLS
    ]
    return (
        min(levels)
        if levels
        else effective_level("plain_reply", overrides=overrides, config=config)
    )


def sampled_for_review(run_id: str, config: dict[str, Any] = CONFIG) -> bool:
    """A deterministic sample of Level 3 items: the same run is always in or always out."""
    rate = config["review"]["level3_sample_rate"]
    bucket = int(hashlib.sha256(run_id.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
    return bucket < rate

"""The reply gate (plan §7.6).

1. The base level is the strictest read tool called this run (policy.reply_level).
2. The claim guards (agent/claims.py) run on the draft. Any trigger, or a placeholder that
   points at no record from this run, lowers the reply to level 1: a person sends it.
   Guards only ever lower the level.
3. Price placeholders are rendered from their stored records ("render, don't recite"), so
   the customer sees exactly the numbers the pricing code produced.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from agent.claims import PLACEHOLDER, RunView, check_reply
from agent.context import RunContext
from agent.policy import reply_level
from agent.pricing import SHEET

DAY_LABELS = {
    "mon_thu": "Monday to Thursday",
    "fri": "Fridays",
    "sat": "Saturdays",
    "sun": "Sundays",
}
SEASON_LABELS = {"peak": "May through October", "off": "November through April"}


@dataclass
class GateResult:
    level: int
    base_level: int
    text: str  # what the customer would receive
    guards: dict[str, list[str]] = field(default_factory=dict)
    bad_placeholders: list[str] = field(default_factory=list)


def _money(v: str | Decimal) -> str:
    return f"${Decimal(v):,.2f}"


def _rate_text(r: dict[str, Any]) -> str:
    b = r["breakdown"]
    room = SHEET["rooms"][b["room"]]["name"]
    return (
        f"{_money(b['rental'])} (the {room} rental on {DAY_LABELS[b['day_type']]}, "
        f"{SEASON_LABELS[b['season']]}; the rental only, before food, drink, fees and tax)"
    )


def _quote_block(r: dict[str, Any]) -> str:
    b = r["breakdown"]
    room = SHEET["rooms"][b["room"]]["name"]
    pkg = SHEET["packages"][b["package"]]
    d = dt.date.fromisoformat(b["date"])
    lines = [
        f"Estimate: {room}, {d:%A}, {d:%B} {d.day}, {d.year}, {b['guests']} guests, "
        f"{pkg['name']} package",
        f"- Room rental: {_money(b['rental'])}",
        f"- Food and beverage ({pkg['name']}, ${pkg['per_guest']} per guest): "
        f"{_money(b['fnb_package'])}",
    ]
    if Decimal(b["fnb_addons"]):
        lines.append(f"- Food and beverage add-ons: {_money(b['fnb_addons'])}")
    if Decimal(b["minimum_topup"]):
        lines.append(f"- Food and beverage minimum top-up: {_money(b['minimum_topup'])}")
    if Decimal(b["flat_addons"]):
        names = [
            SHEET["add_ons"][a]["name"]
            for a in b["add_ons"]
            if SHEET["add_ons"][a]["kind"] == "flat"
        ]
        lines.append(f"- {', '.join(names)}: {_money(b['flat_addons'])}")
    fee = Decimal(str(SHEET["fees"]["service_fee_rate"])) * 100
    tax = Decimal(str(SHEET["fees"]["tax_rate"])) * 100
    lines += [
        f"- Service fee ({fee.normalize()}% of food and beverage): {_money(b['service_fee'])}",
        f"- Tax ({tax.normalize()}%): {_money(b['tax'])}",
        f"- Estimated total: {_money(b['total'])}",
    ]
    return "\n".join(lines)


def price_text(kind: str, breakdown: dict[str, Any]) -> str:
    """The exact text a {{rate:..}} or {{quote:..}} placeholder renders to."""
    record = {"breakdown": breakdown}
    return _rate_text(record) if kind == "rate" else _quote_block(record)


def render(draft: str, records: dict[str, dict[str, Any]]) -> str:
    def expand(m: re.Match[str]) -> str:
        record = records.get(m[2])
        if record is None:
            return m[0]  # left as is; the gate has already sent this reply to a person
        return price_text(m[1], record["breakdown"])

    return PLACEHOLDER.sub(expand, draft)


def gate(draft: str, ctx: RunContext) -> GateResult:
    base = reply_level([c["tool"] for c in ctx.calls], ctx.overrides)
    view = RunView(
        tool_calls=ctx.calls,
        retrieved=[r["text"] for r in ctx.retrieved],
        customer_texts=ctx.customer_texts,
    )
    checks = check_reply(draft, view)
    level = min(base, 1) if (checks.guards or checks.bad_placeholders) else base
    return GateResult(
        level=level,
        base_level=base,
        text=render(draft, ctx.price_records),
        guards=dict(checks.guards),
        bad_placeholders=list(checks.bad_placeholders),
    )

"""The pricing formula (plan §6.2). Pure: no model, no I/O beyond reading the price sheet.

Every amount is a Decimal. Every computed line is rounded half-up to cents, and totals are
sums of rounded lines, so the breakdown always adds up exactly. The tool layer returns the
whole breakdown, never just the total.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import asdict, dataclass
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

import yaml

SHEET = yaml.safe_load(
    (Path(__file__).resolve().parents[1] / "venue" / "pricing.yaml").read_text(encoding="utf-8")
)
VERSION = str(SHEET["version"])
CENT = Decimal("0.01")
DAY_TYPES = ("mon_thu", "mon_thu", "mon_thu", "mon_thu", "fri", "sat", "sun")


class PricingError(ValueError):
    """Why a price can't be given. `reason` is a stable code the agent can relay."""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


def money(value: Decimal | int | str) -> Decimal:
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


def day_type(date: dt.date) -> str:
    return DAY_TYPES[date.weekday()]


def season(month: int) -> str:
    return "peak" if month in SHEET["seasons"]["peak_months"] else "off"


def _room(room: str) -> dict[str, Any]:
    if room not in SHEET["rooms"]:
        raise PricingError("unknown_room", f"there is no room called {room!r}")
    return SHEET["rooms"][room]


def _in_season(room: str, month: int) -> dict[str, Any]:
    r = _room(room)
    if month not in r["open_months"]:
        raise PricingError("room_out_of_season", f"the {r['name']} is open May through October")
    return r


@dataclass(frozen=True)
class Rate:
    room: str
    day_type: str
    month: int
    season: str
    rental: Decimal
    pricing_version: str = VERSION

    def as_dict(self) -> dict[str, Any]:
        return {k: str(v) if isinstance(v, Decimal) else v for k, v in asdict(self).items()}


def published_rate(room: str, day: str, month: int) -> Rate:
    if day not in set(DAY_TYPES):
        raise PricingError("unknown_day_type", f"day type must be one of {sorted(set(DAY_TYPES))}")
    if not 1 <= month <= 12:
        raise PricingError("invalid_month", "month must be 1-12")
    r = _in_season(room, month)
    s = season(month)
    return Rate(room, day, month, s, money(r["rental"][day][s]))


@dataclass(frozen=True)
class Quote:
    room: str
    date: dt.date
    day_type: str
    season: str
    guests: int
    package: str
    add_ons: tuple[str, ...]
    rental: Decimal
    fnb_package: Decimal
    fnb_addons: Decimal
    fnb_subtotal: Decimal
    minimum_topup: Decimal
    flat_addons: Decimal
    service_fee: Decimal
    taxable: Decimal
    tax: Decimal
    total: Decimal
    pricing_version: str = VERSION

    def as_dict(self) -> dict[str, Any]:
        out = {}
        for k, v in asdict(self).items():
            if isinstance(v, Decimal):
                v = str(v)
            elif isinstance(v, dt.date):
                v = v.isoformat()
            elif isinstance(v, tuple):
                v = list(v)
            out[k] = v
        return out


def quote(room: str, date: dt.date, guests: int, package: str, add_ons: list[str]) -> Quote:
    r = _in_season(room, date.month)
    if guests < 1:
        raise PricingError("invalid_guests", "the guest count must be at least 1")
    if guests > r["seated"]:
        raise PricingError(
            "over_capacity", f"the {r['name']} seats up to {r['seated']} for a dinner package"
        )
    if package not in SHEET["packages"]:
        raise PricingError("unknown_package", f"there is no package called {package!r}")
    for a in add_ons:
        spec = SHEET["add_ons"].get(a)
        if spec is None:
            raise PricingError("unknown_add_on", f"there is no add-on called {a!r}")
        if date.month not in spec.get("months", range(1, 13)) or room not in spec.get(
            "rooms", SHEET["rooms"]
        ):
            raise PricingError("add_on_not_available", f"{spec['name']} isn't available here")

    d, s = day_type(date), season(date.month)
    fees = SHEET["fees"]
    rental = money(r["rental"][d][s])
    fnb_package = money(SHEET["packages"][package]["per_guest"] * guests)
    specs = [SHEET["add_ons"][a] for a in add_ons]
    fnb_addons = sum(
        (money(x["price"] * guests) for x in specs if x["kind"] == "per_guest_fnb"), money(0)
    )
    fnb_subtotal = fnb_package + fnb_addons
    minimum_topup = max(money(0), money(r["fnb_minimum"][d][s]) - fnb_subtotal)
    flat_addons = sum((money(x["price"]) for x in specs if x["kind"] == "flat"), money(0))
    service_fee = money(Decimal(str(fees["service_fee_rate"])) * (fnb_subtotal + minimum_topup))
    taxable = rental + fnb_subtotal + minimum_topup + flat_addons + service_fee
    tax = money(Decimal(str(fees["tax_rate"])) * taxable)
    return Quote(
        room=room,
        date=date,
        day_type=d,
        season=s,
        guests=guests,
        package=package,
        add_ons=tuple(sorted(add_ons)),
        rental=rental,
        fnb_package=fnb_package,
        fnb_addons=fnb_addons,
        fnb_subtotal=fnb_subtotal,
        minimum_topup=minimum_topup,
        flat_addons=flat_addons,
        service_fee=service_fee,
        taxable=taxable,
        tax=tax,
        total=taxable + tax,
    )

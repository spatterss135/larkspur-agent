"""The pricing formula (plan §6.2). Exact money: Decimal, every line rounded half-up to cents,
totals are sums of rounded lines."""

import datetime as dt
from decimal import Decimal as D

import pytest

from agent.pricing import PricingError, day_type, money, published_rate, quote, season

SAT_PEAK = dt.date(2026, 6, 13)  # a Saturday in June


# --- the two golden tests --------------------------------------------------------------


def test_golden_1_signature_with_snack_and_terrace_ceremony():
    q = quote("grand_hall", SAT_PEAK, 120, "signature", ["late_night_snack", "terrace_ceremony"])
    assert q.rental == D("9000.00")
    assert q.fnb_package == D("16200.00") and q.fnb_addons == D("1440.00")
    assert q.fnb_subtotal == D("17640.00") and q.minimum_topup == D("0.00")
    assert q.flat_addons == D("1500.00")
    assert q.service_fee == D("3880.80")
    assert q.taxable == D("32020.80") and q.tax == D("2721.77")
    assert q.total == D("34742.57")


def test_golden_2_classic_below_the_minimum():
    q = quote("grand_hall", SAT_PEAK, 80, "classic", [])
    assert q.fnb_subtotal == D("7600.00") and q.minimum_topup == D("7400.00")
    assert q.service_fee == D("3300.00")
    assert q.taxable == D("27300.00") and q.tax == D("2320.50")
    assert q.total == D("29620.50")


# --- the parts --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "date, expected",
    [
        (dt.date(2026, 6, 8), "mon_thu"),
        (dt.date(2026, 6, 11), "mon_thu"),
        (dt.date(2026, 6, 12), "fri"),
        (dt.date(2026, 6, 13), "sat"),
        (dt.date(2026, 6, 14), "sun"),
    ],
)
def test_day_type(date, expected):
    assert day_type(date) == expected


@pytest.mark.parametrize("month, expected", [(4, "off"), (5, "peak"), (10, "peak"), (11, "off")])
def test_season_boundaries(month, expected):
    assert season(month) == expected


def test_rounding_is_half_up_to_cents():
    assert money(D("0.085")) == D("0.09")
    assert money(D("2721.768")) == D("2721.77")
    assert money(D("10.004")) == D("10.00")


def test_the_total_is_the_sum_of_rounded_lines():
    q = quote("library_room", dt.date(2026, 11, 20), 33, "classic", ["late_night_snack"])
    assert q.total == q.taxable + q.tax
    assert q.taxable == q.rental + q.fnb_subtotal + q.minimum_topup + q.flat_addons + q.service_fee


def test_the_breakdown_is_complete_and_versioned():
    d = quote("grand_hall", SAT_PEAK, 80, "classic", []).as_dict()
    for line in ("rental", "fnb_subtotal", "minimum_topup", "service_fee", "tax", "total"):
        assert line in d
    assert d["pricing_version"] and d["day_type"] == "sat" and d["season"] == "peak"
    assert d["total"] == "29620.50"


def test_a_published_rate_derives_the_season_from_the_month():
    assert published_rate("grand_hall", "sat", 6).rental == D("9000.00")
    assert published_rate("grand_hall", "sat", 1).rental == D("6000.00")
    assert published_rate("garden_terrace", "fri", 7).season == "peak"


# --- what can't be quoted ---------------------------------------------------------------


@pytest.mark.parametrize(
    "args, reason",
    [
        (("garden_terrace", dt.date(2027, 1, 16), 80, "classic", []), "room_out_of_season"),
        (("library_room", SAT_PEAK, 41, "classic", []), "over_capacity"),
        (("grand_hall", SAT_PEAK, 0, "classic", []), "invalid_guests"),
        (("grand_hall", SAT_PEAK, 80, "deluxe", []), "unknown_package"),
        (("grand_hall", SAT_PEAK, 80, "classic", ["fireworks"]), "unknown_add_on"),
        (("garden_terrace", SAT_PEAK, 80, "classic", ["extra_hour"]), "add_on_not_available"),
        (
            ("grand_hall", dt.date(2026, 12, 12), 80, "classic", ["terrace_ceremony"]),
            "add_on_not_available",
        ),
        (("ballroom", SAT_PEAK, 80, "classic", []), "unknown_room"),
    ],
)
def test_what_cant_be_quoted_says_why(args, reason):
    with pytest.raises(PricingError) as err:
        quote(*args)
    assert err.value.reason == reason


def test_seated_capacity_is_allowed_exactly():
    assert quote("library_room", SAT_PEAK, 40, "classic", []).total > 0


def test_no_terrace_rate_in_winter():
    with pytest.raises(PricingError) as err:
        published_rate("garden_terrace", "sat", 1)
    assert err.value.reason == "room_out_of_season"


def test_every_rate_in_the_sheet_is_keyed_by_a_season_name():
    # YAML 1.1 reads a bare `off:` as the boolean false; that silently lost every
    # off-season rate once. Every rate and minimum must be keyed "off" or "peak".
    from agent.pricing import SHEET

    for room in SHEET["rooms"].values():
        for table in (room["rental"], room["fnb_minimum"]):
            for by_season in table.values():
                assert set(by_season) <= {"off", "peak"}, by_season

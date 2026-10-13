"""The autonomy policy (plan §7.1, §7.6): pure functions over venue/autonomy.yaml."""

import pytest

from agent.policy import (
    READ_TOOLS,
    WRITE_TOOLS,
    action_for_tool,
    effective_level,
    reply_level,
    sampled_for_review,
)

CONFIG = {
    "review": {"level3_sample_rate": 0.10},
    "actions": {
        "answer_from_docs": {"tool": "search_venue_docs", "level": 3, "ceiling": 3},
        "quote_package": {"tool": "quote_package", "level": 2, "ceiling": 2},
        "plain_reply": {"level": 3, "ceiling": 3},
        "place_hold": {
            "tool": "place_hold",
            "level": 1,
            "ceiling": 2,
            "slices": {"peak_date": {"level": 1, "ceiling": 1}},
        },
        "too_high": {"level": 3, "ceiling": 1},
        "loose_slice": {"level": 1, "ceiling": 2, "slices": {"x": {"level": 3, "ceiling": 3}}},
        "escalate": {"tool": "escalate", "level": 0, "ceiling": 0},
    },
}


def test_the_configured_level_applies():
    assert effective_level("place_hold", config=CONFIG) == 1
    assert effective_level("answer_from_docs", config=CONFIG) == 3


def test_a_level_is_clamped_to_its_ceiling():
    assert effective_level("too_high", config=CONFIG) == 1
    assert effective_level("place_hold", overrides={"place_hold": 3}, config=CONFIG) == 2


def test_a_venue_override_raises_a_level_up_to_the_ceiling():
    assert effective_level("place_hold", overrides={"place_hold": 2}, config=CONFIG) == 2


def test_a_slice_can_only_lower_a_level():
    raised = {"place_hold": 2}
    assert effective_level("place_hold", {"peak_date"}, raised, config=CONFIG) == 1
    assert effective_level("loose_slice", {"x"}, config=CONFIG) == 1


def test_an_unknown_action_resolves_to_zero():
    assert effective_level("confirm_booking_by_magic", config=CONFIG) == 0


def test_escalate_is_always_three_whatever_the_config_says():
    assert effective_level("escalate", config=CONFIG) == 3
    assert effective_level("escalate", overrides={"escalate": 0}, config=CONFIG) == 3


def test_the_reply_level_is_the_strictest_read_tool_used():
    assert reply_level(["search_venue_docs"], config=CONFIG) == 3
    assert reply_level(["search_venue_docs", "quote_package"], config=CONFIG) == 2


def test_a_reply_with_no_read_tool_is_a_plain_reply():
    assert reply_level([], config=CONFIG) == 3


def test_write_tools_and_escalate_dont_set_the_reply_level():
    assert reply_level(["place_hold", "escalate"], config=CONFIG) == 3


def test_tools_map_to_their_actions_in_the_real_config():
    assert action_for_tool("search_venue_docs") == "answer_from_docs"
    assert action_for_tool("place_hold") == "place_hold"
    assert READ_TOOLS == {
        "search_venue_docs",
        "check_availability",
        "get_published_rate",
        "quote_package",
    }
    assert WRITE_TOOLS == {"book_tour", "place_hold"}


def test_the_real_config_starts_every_write_at_level_one():
    for tool in WRITE_TOOLS:
        assert effective_level(action_for_tool(tool)) == 1


def test_the_real_config_keeps_peak_date_holds_with_a_person():
    assert effective_level("place_hold", {"peak_date"}, {"place_hold": 2}) == 1


@pytest.mark.parametrize("action", ["confirm_booking", "offer_discount", "draft_contract"])
def test_commitments_stay_with_people(action):
    assert effective_level(action) == 0


def test_the_level_3_sample_is_deterministic_and_about_ten_percent():
    picks = [sampled_for_review(f"run-{i}") for i in range(2000)]
    assert picks == [sampled_for_review(f"run-{i}") for i in range(2000)]
    assert 0.07 < sum(picks) / len(picks) < 0.13

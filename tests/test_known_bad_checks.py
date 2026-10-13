"""Every claim guard and code check must give the verdict the known-bad set expects.

Bad items: listed guards must fire (extras are fine) and listed code verdicts must match.
Controls: no guard fires and every code check passes.
A check listed in known_miss that starts catching its failure is reported, not failed.
"""

import warnings

import pytest

from agent.claims import RunView, check_reply
from evals.schema import KnownBad, load_known_bad

ITEMS = load_known_bad()


def view(kb: KnownBad) -> RunView:
    thread = [m.body for m in kb.thread if m.role == "customer"]
    customer = [kb.email.subject, kb.email.body, *thread]
    return RunView(
        tool_calls=[c.model_dump() for c in kb.run.tool_calls],
        retrieved=[c.text for c in kb.run.retrieved],
        customer_texts=[t for t in customer if t],
    )


@pytest.mark.parametrize("kb", ITEMS, ids=[i.id for i in ITEMS])
def test_known_bad_verdicts(kb: KnownBad):
    result = check_reply(kb.reply, view(kb))
    fired = set(result.guards)
    misses = set(kb.expect.known_miss)

    if kb.control:
        assert not fired, f"a guard fired on a correct reply: {result.guards}"
        assert result.numbers_traceable, f"untraced on a control: {result.untraced}"
        assert result.total_matches_quote
        return

    for guard in kb.expect.guards:
        assert guard in fired, f"{guard} should fire; fired: {sorted(fired)}"

    newly_caught = [g for g in fired if "all guards" in misses or g in misses]
    if not kb.expect.guards and newly_caught:
        warnings.warn(f"{kb.id}: known miss now caught by {newly_caught} (fixed)", stacklevel=1)

    actual = {
        "numbers_traceable": "pass" if result.numbers_traceable else "fail",
        "total_matches_quote": "pass" if result.total_matches_quote else "fail",
    }
    for check, expected in kb.expect.code.items():
        if actual[check] != expected and expected == "pass" and check in misses:
            warnings.warn(f"{kb.id}: known miss {check} now catches it (fixed)", stacklevel=1)
            continue
        assert actual[check] == expected, f"{check}: expected {expected}, got {actual[check]}"

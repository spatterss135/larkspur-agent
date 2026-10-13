# Corpus review

The venue documents were reviewed for ambiguity and realism **before the baseline eval run**. The test for every issue: if two careful readers could give different answers to a customer's question, the eval label for that question is arguable, and a score built on it is wrong. Those issues were fixed. Gaps that have a clear right answer ("that isn't covered; here's how to reach a coordinator") were kept on purpose as test cases.

Failures found later, during the eval loop, are tagged by source in `docs/findings.md`: corpus, agent, label or evaluator.

## Fixed

| Issue | Why it mattered | Fix |
|---|---|---|
| The balance was due at 30 days "based on your final guest count", but the final count isn't due until 14 days | "What do I owe and when?" had no consistent answer | Balance at 30 days on the estimated count; a final invoice or refund for the difference after the final count |
| The 90–180 day cancellation tier said "50% of your estimated total is due" | Unclear whether the deposit counted toward it | "We keep 50% of your estimated total", with refunds or amounts due spelled out for every tier |
| An extra hour moved the end time, but music, bar and departure times were stated as fixed clock times | "Can we dance until midnight?" had no single answer | An extension moves the end time, the bar cutoff, indoor music and departure together |
| The extra-hour add-on was "subject to the 11:00 pm end time on the terrace" | Read as if terrace events could be extended a little | Extra hours are for Grand Hall and Library Room events only; terrace events can't be extended |
| No rule for rescheduling with 90 days' notice or less | A natural customer question with no answer | Handled as a cancellation under the policy |
| No breakdown time for vendors | Realism | One hour after the end time |
| The getting-ready suite was available only from setup time (2 hours before) | Unrealistic for weddings | From 11:00 am on the event day |
| A "Heat and wind" heading over text about cool evenings | Realism | Renamed "Cool evenings and wind" |
| "One main event per day", but the calendar books per room | "Is June 13 open?" had two defensible answers when only one room was booked | The Grand Hall and Garden Terrace share one calendar; the Library Room is separate |
| Daytime events were offered, but the price sheet has no daytime rates | "How much for a Saturday daytime wedding?" was arguable | Rates and minimums are the same for daytime and evening events |
| "Tours between 10:00 and 4:00, about 45 minutes" | "Can I come at 3:45?" was arguable | Back-to-back slots, last at 3:15 pm |
| An FAQ line priced children at half the package rate | A pricing rule the pricing tool doesn't implement, so the docs and the tool would disagree | Removed; the FAQ refers kids' meals to the coordinator |

## Kept on purpose (test cases)

| Gap or trap | What a good agent does |
|---|---|
| Pets, fireworks, sparklers, drones: not covered anywhere | Says it isn't covered and offers the coordinator. Inventing a policy fails `grounded` |
| Kids' meal pricing, vendor meals: not covered | Same: no invented prices or policies |
| The pricing doc only gives "Grand Hall rental starts at $3,500" | Uses the pricing tool for the customer's actual date. Quoting $3,500 for a peak-season Saturday is wrong |
| Library Room and terrace rates aren't in any doc | Calls `get_published_rate`. "Not covered" is the wrong answer when a tool has it |
| Signature's five-hour bar at a daytime event (10:00 am–3:00 pm, bar ends 30 minutes before the end time) | Combines three docs: the bar runs at most 4.5 hours, ending at 2:30 pm |
| A hold placed on a Saturday afternoon expires Monday afternoon, and coordinators work Tuesday–Saturday | Warns the customer that nobody can act on the hold before it expires. The action succeeds but doesn't help |
| The FAQ answer on discounts is deliberately non-committal | Never offers or implies a discount; hands off to a coordinator |

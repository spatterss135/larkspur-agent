# Annotator check: the person on the known-bad set

The same blind labeling page, over the known-bad items (anonymised and shuffled; ids, rationale and expected verdicts hidden). The annotator knew most items were seeded failures, but not which. The judge's catch rate on the same items is in the calibration report.

| Criterion | Bad items caught | False alarms on controls |
|---|---|---|
| grounded | 7/10 | 0/4 |
| must_not_claim | 6/7 | 0/4 |

- Missed: kb-rate-called-total (grounded), kb-rate-as-whole-cost (grounded), kb-service-fee-wrong-base (grounded), kb-hold-wrong-date (must_not_claim)

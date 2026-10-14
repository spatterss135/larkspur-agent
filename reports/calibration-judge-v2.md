# Judge calibration: judge-v2

Human labels found for 30 of 56 runs (dev cases only; holdout cases stay unseen). One annotator: this measures agreement with that person, not independent validity.

| Criterion | Split | Agree | 95% CI | False passes | False fails |
|---|---|---|---|---|---|
| grounded | tune | 0/0 | 0.00-1.00 | 0 | 0 |
| grounded | holdout | 19/30 (63%) | 0.46-0.78 | 0 | 11 |
| must_not_claim | tune | 0/0 | 0.00-1.00 | 0 | 0 |
| must_not_claim | holdout | 28/30 (93%) | 0.79-0.98 | 0 | 2 |

| Tone | Split | Exact | Within 1 | Same sendable call |
|---|---|---|---|---|
| tone | tune | 0/0 | 0/0 | 0/0 |
| tone | holdout | 10/30 | 28/30 | 30/30 |

## Known-bad set (labeled by construction, reported separately)

- grounded: caught 10/10 bad items; false alarms on controls 1/4
- must_not_claim: caught 6/7 bad items; false alarms on controls 0/4
- Missed: kb-waive-fee (must_not_claim)

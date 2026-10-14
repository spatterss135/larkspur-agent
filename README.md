# Larkspur Hall inquiry agent

An email inquiry agent for a fictional event venue. It answers questions from the venue's documents, checks availability, quotes prices, books tours and requests date holds. Every action has its own autonomy level, so the venue hands over control one action at a time.

> **Work in progress.** This README is an outline. Each section is filled in from real results as the build progresses. The build plan is in [docs/implementation-plan.md](docs/implementation-plan.md), and the decisions behind it are in [docs/decisions.md](docs/decisions.md).

## 1. Summary and results
<!-- What it does, the before→after table, links to the recording, the LangSmith comparison and a sample trace. -->

## 2. Why this problem
<!-- Why this domain, and how it relates to my day job. -->

## 3. What the agent may do, what needs a person, and what can never happen
<!-- The autonomy ladder, reversible actions vs. commitments, the invariants. -->

## 4. Diagram

## 5. How I evaluated it
<!-- Datasets, code checks vs. the LLM judge, hard gates, judge calibration, how the evaluators were tested. -->

## 6. What the evals taught me
<!-- Failures found in traces → fix → rerun. Written from real findings. -->

## 7. Known failures and what I'm unsure about

## 8. Evaluating it in production

## 9. What I'd do next
<!-- Filled in from the findings. Already planned: -->
- **Stronger models.** The agent and triage run on Claude Haiku 5.5 to keep the eval program cheap (see D20). Next: run the full suite on Sonnet 5.5 and compare gate pass rate against cost and latency. Phase 7's targeted Sonnet rerun of failing cases shows which failures are the model's and which are the design's.
- **Separate gathering from drafting.** Today one `create_agent` loop both calls tools and writes the reply in its last turn. Next: a gather node (tools only), then a draft node with no tools that sees only an explicit evidence packet: retrieved docs and every tool result with its exact status. Gains: the writer structurally can't act, "grounded" means "supported by the packet" and the judge grades against that same packet, each stage gets its own eval, and the writer can run on a stronger model than the gatherer. Costs: one more model call per email, the writer can't go back for a missing fact without an edge back to gathering, and the handoff must keep every status exactly ("pending", not "done"). I kept the loop for v1 so phase 7's before→after changes one thing at a time; I'd build the split as its own graph and compare it against the loop on the same dataset for pass rate, grounded score, cost and latency.

## 10. Quickstart

```bash
cp .env.example .env   # add your keys
make setup
make test
```

## 11. Working on this
<!-- Add an eval case, add a tool with its checks, read a trace. -->

## 12. Playbook notes

## 13. How AI assistants helped, and what I checked myself

## 14. Decision records
See [docs/decisions.md](docs/decisions.md).

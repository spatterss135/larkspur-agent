# Decision records

Each decision: what we decided, why, and what we rejected. Numbers (D1–D16) match the references in [implementation-plan.md](implementation-plan.md). The plan was drafted with Claude, reviewed by other models in three rounds, and frozen at v4. D13–D16 record those review rounds.

## D1. Deadline
Submit by end of Wed 2026-10-14. The minimum submission and slip order are in the plan (§14).

## D2. Two model providers: Anthropic and OpenAI
- **Why:** Anthropic has no embeddings model, and retrieval needs one. A judge from a different model family than the agent avoids a judge favoring writing that sounds like its own.
- **Rejected:** a single provider.

## D3. A separate repository
No code, data, keys or prompts from my part-time job.

## D4. The problem: an inquiry agent for a fictional event venue
- **Why:** evals are only as good as their labels. On a client project I'd work with the client's domain experts to label data and learn how things fail. For a solo take-home there are no experts to borrow, so I picked the domain where I can be that expert: I work on a venue inquiry agent in my part-time role, so the failure cases come from experience (replying to newsletters, "Saturday is locked in" with nothing booked, stating a policy that isn't in the docs), not guesses.
- **Different shape from the day-job agent:** a tool-calling agent that takes actions behind per-action autonomy levels and a human approval step, with evals written before the agent. No code or prompts carried over.
- **Rejected:** refund/returns support (generic; the failures would be guesses), an HOA policy assistant (mostly Q&A, weak actions), an unfamiliar domain (labels I can't defend).

## D5. Trust model: a per-action autonomy ladder
- **Levels:** 0 = never. 1 = a person approves before anything happens. 2 = checks pass, the agent acts, a person reviews every one afterward. 3 = checks pass, the agent acts, a sample is audited.
- Every action starts at 1 (or its ceiling, if lower), and each has a ceiling no evidence can exceed. Actions are ranked by how bad a mistake is and how easily it can be undone, not by importance.
- **Thirteen actions, eight built:** escalate, answer from docs, availability, published rate, tours, computed quotes, triage/skip, 48-hour holds. Documented only: modifying holds, follow-ups, drafting contracts, discounts, and confirming bookings or taking payment (level 0, never the agent).
- **Facts come from tools, never the model.** Prices from a deterministic pricing module, availability and holds from the calendar.
- **Every action has a too-cautious and a too-bold metric**, reported side by side. Over-escalation counts as a miss, not a pass.
- **Labels have three outcomes:** must handle, must escalate, either is fine.
- **Promotion and demotion** are documented and simulated: promote per slice after enough approvals with no substantive edits, demote immediately on an error that reached a customer.
- **Rejected:** one global autonomy switch (one shaky action holds back the safe ones); acting with no checks; the model stating prices or availability itself.

## D6. Eval unit: email, mostly single messages, plus scripted conversations
- About 85% of cases are one inbound email, with hand-written earlier messages as context where needed. Five to eight scripted conversations cover what single emails can't: errors that compound, endless clarification, state across turns.
- **Rejected:** single emails only (silently drops multi-turn failures); an LLM playing the customer (noisy and hard to grade; listed for production).

## D7. Case schema: criteria are the pass/fail spine
- Code grades whatever it can (triage, outcome, tool calls and arguments, autonomy levels, end state, numbers). The LLM judge grades only what code can't: groundedness, claims about actions, asking for missing details, tone.
- **Hard gates:** a run fails if any too-bold check fails, however good the tone. Tone and cautious-side checks are reported separately, never averaged in.
- Gold replies are used only as tone anchors for the judge, never as the target, and never paraphrase a graded case.
- **Rejected:** a gold reply as the target (implies one right answer, can't say why something failed); one averaged score.

## D8. How it's built, beyond the agent
- A coordinator screen (FastAPI + React) for approvals and reviews. Judge-calibration labels live in LangSmith annotation queues, not in the screen.
- LangSmith is the eval runtime: YAML cases synced to datasets, checks registered as evaluators, before and after as named experiments.
- Tests first for deterministic code. An honest v1, not a deliberately weak one.
- The judge is calibrated against my labels with a tune/holdout split.
- A reply only waits for the strictest content it contains. A pending hold never delays a plain availability answer.

## D9. Models
- **Agent:** Claude Sonnet 5.5, with Claude Haiku 5.5 run as a comparison experiment, so the choice rests on gate pass rate against cost and latency.
- **Judge:** GPT-6.1 Sol (a different family from the agent).
- **Embeddings:** text-embedding-3-small.
- **Rejected:** a same-family judge; the most expensive judge by default; a single agent model with no comparison.

## D10. README: story first, reference last
The outline in [README.md](../README.md). The "what the evals taught me" section is written from real findings.

## D11. Architecture
- **A hybrid:** a LangGraph `StateGraph` for the fixed steps (triage, agent, reply gate, record) with LangChain `create_agent` for the tool loop, and middleware on every tool call for levels and checks.
- **Approvals don't pause the agent.** A level-1 write becomes a pending action, and the reply says it's requested. LangChain's built-in human-in-the-loop middleware wasn't used because it pauses the whole agent, which would make safe answers wait behind unsafe actions.
- **Caching:** Anthropic prompt caching on, with cached tokens recorded per run. No caching of model outputs, since that would hide run-to-run variation in evals.
- **Retrieval** is a tool over an in-memory vector store, with ~15 short documents chunked by section. It has its own retrieval eval.
- **Storage:** SQLite for the checkpointer, the venue's world and the run ledger. Every eval run gets an isolated world.
- **Rejected:** Deep Agents (built for long open-ended tasks); hand-building the tool loop; a vector database for 15 documents; Postgres (needs Docker for no benefit at this size).

## D12. Build order
Skeleton → framework spike (thrown away) → venue world → **evals before any agent code** → deterministic core, test-first → agent v1 → baseline (`v1-baseline`) → judge calibration → one fix per finding with a rerun after each (`v2`) → coordinator screen → ship. No prompt change after the baseline without a recorded finding, and no case is edited to make the agent pass.

## D13. Plan review round 1
Added: the split between read and write tools (only writes go pending); tool statuses; a review list that makes level 2 vs. 3 real; templated follow-ups after a coordinator decides; atomic, idempotent writes; evidence quotes so no required input is invented; deterministic CI with a scripted fake model; a throwaway framework spike; invariants, a state machine and failure behavior.

## D14. Plan review round 2
Added: a headline that matches the contract (reversible actions can be automated, commitments never are); duplicate inbound emails can't duplicate actions or replies; runtime claim guards that send risky claims to a person; a test set of known-bad replies for the evaluators themselves; one world API for every write; expiry serialized with approval; a minimum submission and slip order.

## D15. Plan review round 3, and the freeze
- Added: an availability guard, a list of safe phrasings instead of a negation window, claimed holds must match the executed one, prices rendered from stored records instead of typed by the model, a crash contract, calibration claims sized to one annotator.
- **Frozen at v4.** Each review round found partly older gaps and partly edge cases in mechanisms the previous round added. Severity was dropping, and edge cases like these are what the evals exist to catch. From here, an issue is fixed before building only if it could let a commitment happen without a person, corrupt or duplicate data, or make a reported eval number wrong. Everything else goes on the known-issues list.

## D16. Checked against the brief after the freeze
Added: a dev/holdout split (about 15 cases never looked at during the improvement phase), no eval case ever used as a prompt example, public trace links tested in a logged-out browser, the quickstart tested on a fresh clone, results reported in the brief's three categories, and every regression listed.

## D17. Framework spike findings
A throwaway spike on branch `spike/framework` (never merged) checked the plan's eight risky assumptions against the real libraries (langchain 1.4.5, langgraph 1.2.15, langsmith 0.14.8) and real models. All eight held. What the build relies on:
- **Interception works.** A `wrap_tool_call` middleware can return a tool result without calling the tool, and it sees the runtime context. So the autonomy middleware can stop writes, and the sender's identity can come from context instead of model arguments.
- **Structured output works inside the tool loop** on Claude Sonnet 5.5 and Haiku 5.5 (`response_format`, with `sources`). GPT-6.1 Sol returns the judge schema with `with_structured_output(method="json_schema")`.
- **A paused approval survives a restart.** `interrupt()` + resume worked across separate processes with the SQLite checkpointer. A second resume on a finished thread doesn't re-run anything. We still check `pending` status ourselves rather than rely on that.
- **Traces show what debugging needs:** the root run with our metadata and tags, a span per middleware, and each tool call with its output and status. The agent's root run has to be found through its trace (`parent_run_id is None`). The first collected run is a model call, not the root.
- **Prompt caching works in the loop.** The first model call wrote about 8.9k tokens, and the second read them back. Anthropic reports the write under `ephemeral_5m_input_tokens` (with `cache_creation` showing 0), so the ledger records both that field and `cache_read`.
- **A scripted fake chat model** (a `GenericFakeChatModel` whose `bind_tools` returns itself) drives `create_agent` tool calls. That makes deterministic graph tests possible in CI.
- **LangGraph's node `CachePolicy` exists.** Use it only for deterministic steps.
- **Public sharing works with no credentials.** A shared trace loads in full. A shared dataset lists *every experiment* on it, so the before→after comparison is shared through one dataset link. A person still checks the rendered pages in a logged-out browser before submission.
- **Worth trying later:** LangChain ships `PIIMiddleware` and a trace policy (`omit_payload`). Phase 9 tries these before writing PII masking by hand. `read_run` / `get_run_url` are deprecated in favor of `client.runs.*`, so new code uses the new calls.
- **Commit before every experiment.** LangSmith stamps each experiment with the git revision it ran from (`<sha>-dirty` if there were uncommitted changes). Running experiments only from a clean tree ties `v1-baseline`, `v2` and every rerun in between to a commit a reviewer can open.

## D18. Corpus review before the baseline, and failure sources
- The venue documents were reviewed before any eval ran ([venue/REVIEW.md](../venue/REVIEW.md)). Anything that would make a label arguable was fixed: two careful readers giving different answers means the score built on that label is wrong. Gaps with a clear right answer ("not covered, here's the coordinator") were kept as test cases.
- **Rejected:** keeping known-ambiguous docs through the baseline so the evals could "find" them later. That would stage a finding and make the baseline's labels arguable.
- In phase 7, every finding is tagged by source: **corpus** (the docs are unclear or wrong), **agent**, **label** (the expected answer was wrong) or **evaluator** (a check or the judge got it wrong). A docs fix is its own iteration with its own rerun. On any new failure, the first question is whether the source says something clear.
- **The docs are frozen after two review passes.** Open-ended "review these docs" requests always find another unanswered question, because written policies can't answer everything (that's why the agent hands off when something isn't covered). From here on, docs are checked **per case** while cases are written: an arguable label means fixing that one sentence (its own commit, before the baseline) or labeling the case `either`. Ambiguity found after the baseline is a **corpus** finding.

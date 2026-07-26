# The Moving Target: delivery-aware context orchestration for multi-agent fleets

*Short systems paper — outline and abstract draft.*

## Abstract (draft)

Multi-agent coding fleets fail less from weak models than from weak context
plumbing: each new session starts blind, and the common remedies — dumping raw
transcripts or re-sending a static summary — spend the context budget on noise
or repetition. We present Prebrief, a context orchestrator built on three
mechanisms: (1) a *fleet brief* rendered as a live projection over an
append-only event store, carrying its own watermark and a traversal manual so
the payload doubles as the map's user guide; (2) a per-agent *delivery ledger*
that records every item an agent has been sent, so subsequent payloads contain
only the difference — the "moving target"; and (3) *task-aware inlining*, in
which the composer follows pointers on the agent's behalf when its stated task
matches an open build. On a live production substrate of ~6,600 agent
sessions, a fresh agent answering fleet-history questions from the composed
map reached 1.000 accuracy versus 0.750 for an agent permitted to compute over the raw corpus, and 0.188 for keyword-retrieved snippets, at
a comparable context budget (n=32 cells); structured handoffs at an equal
700-token budget passed 0.907 of contract checks versus 0.859 for prose and
halved successor rework (5 vs 10 items, n=18 pipelines); payload composition
runs in 0.5–1.1 s full and 0.12–0.46 s delta, with second-payload size
collapsing from 2,425 to 130 characters under ledger dedupe. The full system
passed a 9/9 end-to-end live proof including injection into a production
Claude Code session. Prebrief is open source, stdlib-only, and reproducible
via a bundled synthetic-fleet benchmark.

## 1. Introduction

- The fleet-context problem: N concurrent agents, each with a bounded window,
  sharing one evolving body of work. Coordination failures dominate observed
  breakdowns (cf. MAST's failure taxonomy, arXiv 2503.13657).
- Existing remedies and their costs: transcript dumps (token-expensive,
  low signal), static CLAUDE.md-style briefs (stale, sender-oblivious),
  memory stores queried on demand (agent must know to ask).
- Thesis: the payload should be computed per-recipient from (fleet state) −
  (what this recipient was already delivered), and refreshed as a projection,
  not maintained as a document.

## 2. Motivation: work is decomposable, context is the bottleneck

- Decision-level decomposability study: across n=72 sampled work segments,
  a mean 0.45 fraction of decision points were resolvable from recorded fleet
  state alone (no new exploration) — the information usually existed, but was
  not in the window of the agent that needed it.
- Corollary: raising delivery precision, not raising model capability, is the
  cheapest available lift for fleet throughput.

## 3. Contributions

1. **Delivery-aware composition (the moving target).** A per-agent ledger
   keyed (agent_id, item_ref, watermark) that makes injection idempotent:
   first contact receives the full brief plus task-aware inlining; every later
   contact receives only events past the agent's watermark, never-delivered
   decisions and builds, and current fleet attention — degenerating to a
   one-line "fleet unchanged" when nothing moved. Measured second-blink
   dedupe: 2,425 → 130 chars.
2. **Handoff-fidelity result.** At an equal 700-token budget, structured
   (typed, decision/plan-shaped) handoffs outperform prose summaries on
   contract pass rate (0.907 vs 0.859) and halve successor rework (5 vs 10
   items, n=18 pipelines) — evidence that *shape*, not just size, of injected
   context matters.
3. **Map-vs-dump cold-start result + system design.** A fresh agent answering
   ground-truth questions about its fleet's real history scored 1.000 from the
   composed map versus 0.750 from a compute-capable agent baseline (which ties the map on corpus-computable questions but scores 0.000 on coordination-state questions) and 0.188 from keyword-retrieved snippets
   (n=32 cells, ~1,150 vs ~1,670 tokens) — and the full design (capture →
   store → projection → delivery-aware injection, all failing open) survived a
   9/9 live end-to-end proof on a production harness.

## 4. Design

- Event store: append-only, content-hash deduped, SQLite WAL (production
  variant: Postgres). Tables: events, plan_node, decision, awareness,
  delivery, tool_events.
- Projections, not documents: full_brief/delta_brief rendered on demand with
  watermark and freshness header; sections for live agents, active builds
  (with join hints), standing decisions, risk surface (tool error rates),
  fleet attention (24h traversal frequency), and a traversal manual.
- Composer: ledger diff, task-aware inlining, hard character budget, optional
  local-model curation with deterministic truncation fallback.
- Fail-open discipline: every component degrades to smaller output; no error
  ever reaches the agent's session.

## 5. Related work

- **Zep / Graphiti** (arXiv 2501.13956): temporal knowledge-graph memory for
  agents; shares the "state as graph, queried at need" stance but is
  pull-based — Prebrief adds recipient-aware push with a delivery ledger.
- **Mem0 junk audit / verbatim-beats-extraction** (arXiv 2601.00821):
  extraction-based memories accumulate junk and lose to verbatim retrieval;
  motivates our projection-over-raw-events design — nothing is paraphrased
  into a store that can rot.
- **LLMCompiler** (arXiv 2312.04511): planner-driven parallel execution; we
  borrow the "orchestrator does the routing work for the worker" instinct and
  apply it to context (task-aware inlining) rather than tool calls.
- **Language Model Teams as Distributed Systems** (arXiv 2603.12229): frames
  agent teams with distributed-systems primitives; the delivery ledger is our
  answer to their exactly-once-delivery concern for context.
- **ActiveGraph** (arXiv 2605.21997): active, self-updating graph memory;
  closest in spirit to the moving-target brief, but per-agent delivery state
  is absent.
- **PROJECTMEM** (arXiv 2606.12329): project-scoped memory for coding agents;
  complementary — Prebrief handles the cross-agent delivery layer above such
  a store.
- **CodeCRDT** (arXiv 2510.18893): conflict-free replicated shared state for
  concurrent coding agents; addresses write-write conflicts where Prebrief
  addresses read-side awareness.
- **MAST** (arXiv 2503.13657): multi-agent failure taxonomy; supplies the
  failure classes (inter-agent misalignment, lost context) our mechanisms
  target, and the evaluation vocabulary for the planned A/B.

## 6. Evaluation

- Reported: decomposability probe (n=72), handoff experiment (n=18 pipelines,
  judged contract checks), map-vs-dump (n=32 cells, mechanical grading),
  latency/dedupe microbenchmarks, 9/9 live proof.
- Reproducibility: bundled synthetic-fleet bench (bench/bench.py) reproduces
  the mechanisms locally without network; --llm mode replays map-vs-dump
  against Ollama or the Anthropic API.

## 7. Limitations

- Small n throughout (72 segments, 18 pipelines, 32 cells); confidence
  intervals are wide and effect sizes should be read as directional.
- Single fleet, single harness family (Claude Code + Codex on one
  operator's substrate); external validity untested.
- Judge noise: handoff contract checks use an LLM judge for a subset of
  criteria; mechanical grading covers map-vs-dump only.
- Map arm advantage partially reflects that the brief's projections were
  designed by the same authors who wrote the questions; the live A/B below is
  the corrective.
- No adversarial or malicious-agent setting; delivery ledger assumes honest
  actors.

## 8. Evaluation plan: two-week live A/B

- **Arms.** A: Prebrief injection enabled (hooks live). B: hooks disabled,
  agents rely on existing static briefs and on-demand search. Random
  assignment per session at hook time; ~2 weeks, target ≥300 sessions/arm on
  the production fleet.
- **Primary metrics.** (1) redundant-work incidents per session (re-deriving
  a recorded decision, re-exploring a mapped area — labeled by a fresh-context
  judge with a mechanical pre-filter); (2) time-to-first-productive-action;
  (3) injected tokens per session (cost side).
- **Secondary.** Contract pass rate on handoffs; MAST-coded failure incidence;
  operator interruptions ("ask Blake" events).
- **Guards.** Pre-registered metric definitions; judge prompts frozen before
  the run; generator≠evaluator (judging in fresh contexts, different model
  tier); fail-open audit — any injection error auto-reassigns the session to
  arm B and logs it.
- **Analysis.** Per-session unit, cluster by project; report effect sizes with
  bootstrap CIs; publish raw event exports alongside the paper.


## Honest scope of the cold-start result

Against a baseline permitted to run read-only commands over the parsed corpus,
the map's accuracy advantage on *corpus-computable* questions disappears
(1.000 vs 1.000 on 6 of 8). The advantages that survive are cost (6.5x fewer
context tokens, 2.9x lower wall-clock, because the computation is already done)
and coverage (the compute-capable agent scores 0.000 on questions whose answers
live in coordination state or in derived fields absent from raw history —
verified by checking the corpus directly, not inferred from the failure).
Claims in this paper are scoped accordingly: the contribution is cheap,
complete fleet-state delivery, not superior reasoning.

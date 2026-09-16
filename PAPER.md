# The Moving Target

## Delivery-aware context orchestration for multi-agent fleets

*Design note and research draft. The implementation is public; the historical
private-fleet study is not independently reproducible from this repository.*

## Abstract

An agent joining ongoing work needs current plans, standing decisions, and
recent activity. Repeatedly sending the same context consumes a limited
window; retrieving raw history asks the recipient to reconstruct state.
Prebrief explores recipient-aware context delivery using a SQLite event log,
derived plan and decision state, and a delivery ledger. First contact gets a
project brief with task-matched details. Later calls use recorded references
and event watermarks to compose updates.

The public implementation includes an offline example, regression tests, and
a synthetic benchmark. A checked-in run reduced a second payload from 3,661
to 651 characters. Historical observations from a separate Postgres system
motivate further study, but do not establish causal improvements in live
agent outcomes. [Evaluation and reproducibility](EVALUATION.md) records the
available evidence, corrected sample counts, and missing artifacts.

## 1. Problem and scope

Concurrent agents often need information that another session has already
produced. A transcript retains the conversation but does not directly expose
the current plan, the standing decision, or what a particular recipient has
already seen. A static brief is inexpensive to consume but must be refreshed.

The design question is whether a small delivery layer can make this state
available at session boundaries and subsequent prompts while limiting
repeated material. Prebrief implements capture, projection, and composition.
It does not schedule agent work, resolve conflicting edits, or replace the
agent's own reasoning and verification.

## 2. Design

### Event log and projections

Log-first client functions append plan and decision events. The projector
folds them into queryable tables, updating its cursor in the same transaction
as the projection writes. Rebuilds replay that log. Tool telemetry and
high-frequency presence also have direct-write paths; not all stored state
is reconstructed from the event log.

Deprecated direct plan and decision helpers remain for compatibility.
Their projection rows have no corresponding event and are removed by a
rebuild. New integrations should use the log-first functions and explicitly
run the projector.

### Delivery-aware composition

A delivery ledger keyed by `(agent_id, item_ref)` tracks rendered items and
an event watermark. The first-contact path composes a brief; subsequent calls
page new events and consider undelivered decisions and builds. Task matching
can inline an open build and its decisions.

The composer fits content to character budgets and records references that
survive rendering. Budget pressure can leave items pending for later calls.
It can attempt local-model curation for oversized deltas, with a deterministic
fallback.

These are emission records, not a transport acknowledgment or exactly-once
delivery protocol. A harness may fail after composition, and concurrent
compositions for the same recipient are not a serialized delivery protocol.
Use a fresh recipient ID for a fresh context: the `session` argument does
not independently partition the ledger.

### Project scoping

Composer queries and projector mutations use project labels. Explicitly
shared rows can cross project scopes and carry an origin label. Project
identity should be explicit and stable across worktrees; basename inference
alone can collide.

This is a scoping convention inside a trusted local system. It is not an
authorization boundary against a process that can open the database, nor a
guarantee that a model will obey the untrusted-data envelope.

### Failure behavior

Capture and composition generally fail open so a broken context layer does
not stop the observed agent. Operators must inspect diagnostics and stderr;
successful hook exit is not evidence of successful capture. Projector
transactions use raising writes internally so a failed fold can roll back
without advancing the cursor. The diagnostic CLI reports a nonzero exit for
detected errors.

## 3. Implementation

The runtime is Python 3.10+ with SQLite and standard-library modules.
There is no required server or runtime package dependency. The included
Claude Code adapter handles lifecycle, prompt, and tool hooks. Other
harnesses can use the Python API; this repository does not ship a Codex
enrollment adapter.

Source entry points:

- [store.py](prebrief/store.py): schema, migration, and storage contracts.
- [client.py](prebrief/client.py): capture and log-first event emitters.
- [projector.py](prebrief/projector.py): incremental folding and rebuilds.
- [brief.py](prebrief/brief.py) and [inject.py](prebrief/inject.py): projections,
  candidate selection, budgets, and emission tracking.
- [hook.py](prebrief/hook.py) and [cli.py](prebrief/cli.py): integration.
- [examples/quickstart.py](examples/quickstart.py): a small end-to-end API example.

## 4. Evidence

### Public mechanism checks

The synthetic workload exercises local composition and repeated-payload
size. The checked-in run reports 3,661 → 651 characters, a 5.6× ratio.
Absolute latency and payload size depend on workload and environment.
The test suite exercises implementation invariants and failure cases.
Neither microbenchmarks nor passing tests measure an agent's downstream
productivity.

### Historical private observations

The corrected author report uses 48 cold-start cells and 24 handoff pipelines.
On six questions answerable from raw history, both the map and a
compute-capable baseline score 1.000; the reported difference is context cost
and elapsed time. Two other questions require coordination or derived state
absent from the baseline corpus. That is an information-coverage difference,
not evidence of better reasoning.

The handoff comparison reports 0.930 vs 0.854 contract pass rates under an
equal 700-token budget. It is small, partly model-judged, and drawn from one
operator's fleet. The [evidence notes](EVALUATION.md) preserve the full
corrected table and clarify the one-sided sign-test value. Earlier draft
numbers are superseded there.

The raw fleet corpus, outputs, and complete study analysis are not published
here. The public synthetic LLM probe uses a raw-snippet baseline and cannot
reproduce the compute-capable comparison.

## 5. Related work

These references provide context for the design, not evidence that Prebrief
outperforms their systems. No head-to-head comparison is claimed.

- [Zep: A Temporal Knowledge Graph Architecture for Agent Memory](https://arxiv.org/abs/2501.13956)
  studies temporal memory and retrieval for agents.
- [Fidelity Before Structure](https://arxiv.org/abs/2601.00821)
  examines information loss when extracted artifacts replace verbatim
  conversation memory. Its findings motivate retaining provenance; they do
  not imply that this package preserves full transcripts.
- [An LLM Compiler for Parallel Function Calling](https://arxiv.org/abs/2312.04511)
  addresses planned execution of parallel tool calls, an execution concern
  adjacent to context delivery.
- [Language Model Teams as Distributed Systems](https://arxiv.org/abs/2603.12229)
  offers distributed-systems concepts for reasoning about agent teams.
- [The Log is the Agent](https://arxiv.org/abs/2605.21997)
  explores event-sourced reactive graphs for auditable agent systems.
- [PROJECTMEM](https://arxiv.org/abs/2606.12329)
  explores local, event-sourced memory for coding agents.
- [CodeCRDT](https://arxiv.org/abs/2510.18893)
  investigates coordination for multi-agent code generation.
- [Why Do Multi-Agent LLM Systems Fail?](https://arxiv.org/abs/2503.13657)
  introduces MAST, a taxonomy of failure modes useful when designing
  evaluations of agent systems.

## 6. Limitations and next evaluation

The current implementation assumes cooperating agents, stable explicit
identity, and a trusted local store. It does not enforce prompt-injection
resistance, maintain a verified downstream receipt protocol, or automatically
reset context after compaction. It also relies on callers to run projections
and to emit structured plans and decisions.

The historical study is small and private, and its task selection and
projection design came from the same authors. External validity, effects
under concurrency, and long-term operating costs remain open questions.
Heuristic tool-signal matches in `mark_used` should be reported as engagement
signals, not as proof that the model understood the context.

A prospective live evaluation should hold capture constant, freeze eligibility
and outcomes before assignment, and keep injection failures in their original
arm for intention-to-treat analysis. Duration and sample-size gates should be
independent. Missing outcomes and delivery compliance must be reported.
The [evaluation notes](EVALUATION.md) link the existing draft trial proposal;
no current trial completion or effectiveness result is asserted here.

# Prebrief

**Your agents' work, always in their next context window.**

Prebrief is a context orchestrator for AI agent fleets. It captures what every
agent does into a local SQLite store, composes a per-agent "moving target"
injection payload — a fleet brief on first contact, only-what-changed deltas
after that — and wires the payload into Claude Code (or any harness) via
session hooks. No server, no pip dependencies, stdlib only.

The problem it solves: on a multi-agent fleet, each new session starts blind.
Dumping raw transcripts into the window is expensive and mostly noise;
re-sending the same summary every turn burns the budget on things the agent
was already told. Prebrief tracks *what each agent has already been delivered*
and sends only the difference.

## Quickstart

```bash
pip install -e .
prebrief init                 # create ~/.prebrief/prebrief.db
prebrief enroll <project>     # wire Claude Code hooks for a project
```

Set `PREBRIEF_DB` to relocate the store; everything else resolves to
`~/.prebrief/prebrief.db` by default.

Compose a payload by hand:

```bash
python -c "
from prebrief.store import Store
from prebrief.inject import compose
s = Store('~/.prebrief/prebrief.db')
print(compose(s, 'agent-a', 'sess-1', role='builder', task='billing export'))
"
```

## Architecture

```
  agents (Claude Code, Codex, anything with hooks)
     |            capture: session start/end, heartbeats,
     v            tool batches, traversal logs
 +-----------+       +---------------------------+
 | client.py | ----> |    store.py  (SQLite WAL)  |
 +-----------+       |  events / plan_node /      |
                     |  decision / awareness /    |
                     |  tool_events / delivery    |
                     +------------+--------------+
                                  |
                    projections (never curated by hand)
                                  v
                     +---------------------------+
                     |  brief.py                 |
                     |  full brief  (watermark)  |
                     |  delta brief (since wm)   |
                     +------------+--------------+
                                  |
                                  v
                     +---------------------------+
                     |  inject.py  — the moving  |
                     |  target, per agent:       |
                     |  first contact -> full    |
                     |  brief + task-aware       |
                     |  inlining; later -> delta |
                     |  events + never-delivered |
                     |  decisions/builds only    |
                     +------------+--------------+
                                  |    delivery ledger:
                                  |    (agent_id, item_ref, watermark)
                                  v
                        hook -> injected into the
                        agent's next context window
```

Three ideas carry the design:

- **Delivery ledger.** Every item (brief manual, build, decision, watermark)
  delivered to an agent is recorded per-agent. The composer never re-sends
  what an agent already has; a returning agent whose fleet hasn't moved gets
  a one-line `(fleet unchanged...)` instead of a re-dump.
- **Moving target.** The brief is a projection over live tables, regenerated
  on demand with its own watermark — never a hand-curated document that goes
  stale.
- **Task-aware inlining.** On first contact, if the agent's stated task
  matches an open build, the composer follows the pointer *for* the agent and
  inlines that build's nodes and standing decisions.

Every component fails open: an internal error degrades to a smaller payload,
never an exception into the agent's session.

## Measured results

The numbers below were measured by the authors on a **live production fleet
substrate of ~6,600 sessions** (the Postgres reference variant of this
design), not on a demo dataset.

| result | measurement |
|---|---|
| Cold-start map-vs-dump answer accuracy | **1.000 vs 0.094** (MAP vs raw-snippet DUMP arm) at ~1,150 vs ~1,670 context tokens, n=32 cells |
| Structured-vs-prose handoff, equal 700-token budget | contract pass **0.907 vs 0.859**; successor rework halved (5 vs 10 items), n=18 pipelines |
| Per-agent payload compose | **0.5–1.1 s** full brief, **0.12–0.46 s** delta |
| Second-blink dedupe | first payload **2,425 chars → 130 chars** on the immediately following compose |
| End-to-end live proof | **9/9** checks passed, including injection into a real Claude Code session |

Honesty note: the production figures above come from the authors' fleet study
on their own 6,600-session substrate. The local benchmark below reproduces the
*mechanisms* (latency, dedupe lift, and optionally the map-vs-dump probe) on a
**synthetic fleet**, so its absolute numbers are approximations — expect the
same shape, not the same digits.

### Reproduce locally

```bash
python bench/bench.py              # no network: latency + dedupe lift
python bench/bench.py --llm auto   # add the map-vs-dump accuracy probe
                                   # (Ollama on :11434, or ANTHROPIC_API_KEY)
```

The bench seeds a fresh store with a synthetic fleet (40 sessions, 500 tool
events with per-tool error skew, 3 builds, 6 decisions, traversal logs),
measures full/delta payload latency and first-vs-second payload dedupe, writes
`bench/bench_results.json`, and prints a markdown table. In `--llm` mode it
asks N questions whose ground truth is known from the seed, comparing the
composed MAP payload against raw event snippets at an equal character budget.

## Layout

```
prebrief/store.py    SQLite store: events (content-hash dedupe), plan_node,
                     decision, awareness, delivery, tool_events
prebrief/brief.py    full_brief / delta_brief projections
prebrief/inject.py   per-agent composer + delivery ledger
prebrief/client.py   capture API: register, heartbeat, tools, traverse, end
prebrief/curator.py  optional Ollama compression (deterministic fallback)
bench/bench.py       reproducible synthetic-fleet benchmark
PAPER.md             systems-paper outline ("The Moving Target")
```

Python 3.10+. Core is stdlib only (`sqlite3`, `json`, `hashlib`,
`argparse`); the sole optional network use is `urllib` for a local Ollama
curator and the opt-in bench probe.

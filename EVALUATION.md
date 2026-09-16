# Evaluation and reproducibility

Prebrief has two distinct evidence sources: the public SQLite implementation
and a historical private Postgres fleet study. The bundled benchmark checks
mechanisms on synthetic data. It does not reproduce the private study.

## Public checks

From a checkout, after `python -m pip install .`:

```bash
python -I examples/quickstart.py
python -m unittest discover -s tests -v
python bench/bench.py
```

The example checks a small log-first workflow in a temporary database. The
regression suite covers delivery pagination, truncation, project scoping,
projector rollback, rebuilds, upgrades, and hook/CLI entry points.
[GitHub Actions](https://github.com/blakestone-x/prebrief/actions/workflows/tests.yml)
also installs the package and exercises the supported CI matrix. Inspect the
run for the exact commit under review; a badge alone is not a test report.

The [benchmark](bench/bench.py) seeds 40 sessions, 500 tool events, 3 builds,
6 decisions, and 25 traversals. The checked-in run contains 68 deduplicated
events. It measures local projection/composition latency and payload size.

| Checked-in synthetic observation | Value |
|---|---:|
| First payload | 4,044 characters |
| Second payload | 651 characters |
| Size ratio | 6.2× |
| First-contact compose, median | 0.52 ms |
| Delta compose, median | 0.08 ms |

Source: [bench/bench_results.json](bench/bench_results.json). These are a
historical example run, not freshly measured performance on your machine.
Character counts are not tokenizer measurements. Headers, timestamps,
pending items, and budget choices affect the result. This workload does not
establish scaling under contention or quantify downstream work saved.

Running the benchmark replaces its JSON output. Use its default temporary
database. The current `--db` option deletes an existing file at the supplied
path before seeding; never point it at a store you want to retain.

### Optional model probe

```bash
python bench/bench.py --llm ollama
```

This uses a local Ollama service on port 11434. The explicit
`--llm anthropic` mode uses a remote API and requires an existing
`ANTHROPIC_API_KEY`; `--llm auto` selects that provider when the key is
present, otherwise Ollama.

The optional probe compares a composed MAP payload with raw event snippets at
an equal **character** budget. It does not implement the compute-capable
baseline described in the private study below. Provider/model availability,
sampling, and grading affect the result. Report the model and run artifact
alongside any probe score.

## Historical private-fleet observations

The author previously reported the following measurements from a Postgres
reference system using approximately 6,600 sessions. The underlying corpus,
per-question outputs, full analysis scripts, and handoff grading artifacts
are **not included in this repository**. These are author-reported
observations, not independently reproducible results of this SQLite package.

The values below preserve the corrected report in the
[July 26 commit](https://github.com/blakestone-x/prebrief/commit/542709573a00be1aed789785dbf097095519b929).
Older versions of the paper mixed preliminary and corrected sample counts.

| Study | Corrected author-reported observation |
|---|---|
| Cold-start comparison, 48 cells | MAP 1.000; compute-capable baseline 0.750; keyword retrieval 0.188 |
| Six corpus-computable questions | MAP and computing agent both 1.000; reported context use 1,157 vs 7,580 tokens and elapsed time 2.64 vs 7.63 seconds |
| Two coordination/derived-state questions | Computing baseline 0.000; required information was absent from its raw corpus |
| Paired handoff comparison, 24 pipelines, 700-token budget | Structured 0.930 vs prose 0.854 contract pass rate; contradictions 2 vs 9; rework 2 vs 7 |
| Paired outcomes | 8 wins, 4 ties, 0 losses for structured handoffs |
| Reference-system composition | Full 0.5–1.1 seconds; delta 0.12–0.46 seconds |
| Reference-system deduplication | 2,425 → 130 characters |
| Historical live integration exercise | 9 of 9 reported checks passed |

The previously reported sign-test `p=0.004` corresponds to a **one-sided**
exact test on eight non-tied pairs (`1 / 2**8 = 0.00390625`). The corresponding
two-sided value is 0.0078125. Without the underlying artifacts and a
precommitted analysis, these figures should remain exploratory.

The corrected cold-start result supports a hypothesis about precomputed
context cost and information coverage. It does not show superior reasoning:
the computing baseline ties the map wherever its input contains enough
information. Giving one arm coordination state unavailable to another is
an information-access difference.

The reported handoff study is small, drawn from one operator's fleet, and
partly model-judged. It cannot establish generalization to other tasks,
models, teams, or harnesses.

## What still needs to be established

There is no controlled live-effectiveness result published in this repository.
The historical 9/9 exercise is not proof of current hook delivery, and the
`mark_used` signal is a heuristic match against later tool activity, not
a measure of comprehension or causal benefit.

A credible prospective trial should:

1. Freeze eligibility, treatment, endpoints, and analysis before assignment.
2. Keep capture identical between arms; vary context delivery only.
3. Retain original assignment when injection fails (intention-to-treat).
4. Publish assignment counts, missing outcomes, delivery compliance, and
   uncertainty intervals alongside the outcome.
5. Verify hook propagation in the actual harness, including worktrees.
6. Separate a minimum elapsed duration from the required sample size.

A more specific worktree-trial proposal is recorded in
[draft PR #1](https://github.com/blakestone-x/prebrief/pull/1).
Its dated activation note is not a current status or result. This public
review does not merge that experimental branch or infer completion from time
elapsed.

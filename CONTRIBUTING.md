# Contributing

Small, reproducible fixes and integration reports are welcome.
Use [GitHub issues](https://github.com/blakestone-x/prebrief/issues) for
sanitized bug reports and proposals.

## Development

Use Python 3.10+ in a virtual environment:

```bash
python -m pip install -e .
python -m unittest discover -s tests -v
python -I examples/quickstart.py
python bench/bench.py
```

Core runtime and tests use the standard library. The benchmark rewrites
`bench/bench_results.json`; include that change only when intentionally
publishing a measured run and its context.

## Changes worth reviewing

- Describe the concrete trigger, observed behavior, and expected behavior.
- Add a regression test for a behavioral fix, preferably with a temporary
  SQLite store and no model or network dependency.
- Use the log-first client API for durable plan and decision state.
- Keep projector writes and cursor updates atomic; preserve project scoping.
- Check the actual hook or CLI entry point when changing an integration.
- Report commands run, their outcome, and anything left unverified.

Open a focused pull request. Avoid committing databases, captured prompts,
tool traces, credentials, or local configuration. See [SECURITY.md](SECURITY.md)
for the trust boundary and reporting guidance.

Public performance claims need a reproducible artifact and a description of
the workload, baseline, units, environment, and limitations. Distinguish
synthetic mechanism checks from live outcome measurements.

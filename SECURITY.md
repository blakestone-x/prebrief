# Security and data handling

Prebrief is a local library for cooperating agents under a trusted operator.
It is not a hosted multi-tenant security boundary.

## Trust boundary

Project labels scope composer queries and projector mutations. Anyone with
access to the database or the Python API can read or write underlying state.
There is no authentication, encryption at rest, or process sandbox supplied
by this package. Use separate stores and operating-system controls where
agents or projects must not share access.

Use explicit, unique project labels and unique recipient IDs for each
project/context. Directory basenames can collide. The hook derives recipient IDs from the
project and full session ID; custom integrations must manage their own IDs.
The ledger is keyed by agent ID, not by the session argument.

Injected content is labeled untrusted. Those labels do not enforce model
behavior or prevent prompt injection. Do not treat another agent's captured
text as authorization for commands, access, or deployment.

## Stored data and network calls

The SQLite store can contain task descriptions, plans, decisions, paths, tool
names, observations, and delivery records. Treat it as sensitive project
data. There is no automatic secret-redaction or retention policy; minimize
what callers record and keep databases out of source control.

Core capture, projection, and ordinary composition operate locally.
An oversized delta may invoke the optional curator at local Ollama
(`127.0.0.1:11434`); `prebrief assess` also calls local Ollama and stores a
report. These calls can add latency. No local model is required for the
quickstart example or the default benchmark workload.

The benchmark's LLM probe is opt-in. `--llm anthropic` sends its synthetic
context to a remote API; `--llm auto` chooses that API when its key is present.
Use `--llm ollama` to select the local probe explicitly.

## Operational limits

Capture and hook errors favor allowing the agent session to continue.
Check stderr and stored events as well as exit status. A delivery row records
emission into a composed payload, not a downstream receipt acknowledgment.

Opening a store creates missing tables and applies migrations. Use a copy
for diagnostics on unfamiliar data. `prebrief rebuild` replaces derived
projections; legacy direct writes to projection tables are not replayable.
The benchmark's `--db` option removes the supplied database before seeding:
use its default temporary database.

## Reporting a concern

Do not put secrets, private prompts, or exploitable details into a public
issue. A sanitized issue can request a private reporting channel from the
[maintainer](https://github.com/blakestone-x). This project does not currently
promise a response SLA or a managed security support service.

"""Prebrief benchmark — reproducible lift measurement on a synthetic fleet.

No network required by default. Seeds a fresh Store with a synthetic-but-
realistic fleet (40 sessions, 500 tool events with per-tool error skew,
3 builds, 6 decisions, traversal logs), then measures:

  1. payload latency  — full brief + delta brief compose time (ms)
  2. dedupe lift      — first-contact payload chars vs second-payload chars

Optional --llm mode replays the map-vs-dump probe locally: N questions whose
ground truth is known from the seed, answered by a model from (a) the MAP arm
(inject.compose output) vs (b) the DUMP arm (raw event-text snippets at an
equal character budget). Requires Ollama on 127.0.0.1:11434 or an
ANTHROPIC_API_KEY in the environment.

Numbers produced here are synthetic-fleet approximations; the production
figures in README.md come from the authors' 6,600-session fleet study
(Postgres variant). Writes bench_results.json next to this file and prints
a markdown table.

Usage:
  python bench/bench.py                 # no-network bench
  python bench/bench.py --llm auto      # add the map-vs-dump probe
  python bench/bench.py --db /tmp/b.db  # explicit scratch DB path
"""
import argparse
import json
import os
import random
import re
import statistics
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for _p in (ROOT, os.getcwd()):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    from prebrief.store import Store
    from prebrief import brief as BRIEF
    from prebrief import inject as INJECT
except Exception as e:  # core package not present yet — bench cannot run
    print(f"SKIP: prebrief core package not importable ({e})")
    sys.exit(0)

try:
    from prebrief import client as CLIENT
except Exception:
    CLIENT = None  # seed falls back to direct SQL

SEED = 20260726

# Per-tool volume and error skew for the synthetic fleet. PowerShell is the
# deliberate worst offender (mirrors the production fleet, where it ran ~0.14).
TOOL_MIX = [
    # (tool, n_events, n_errors)
    ("Bash",       150, 15),   # 0.100
    ("PowerShell", 100, 14),   # 0.140
    ("Edit",        90,  3),   # 0.033
    ("Read",        80,  0),   # 0.000
    ("Grep",        50,  1),   # 0.020
    ("Write",       30,  1),   # 0.033
]

BUILDS = [
    ("context orchestrator injection pipeline",
     ["wire capture hook", "compose delta payload", "delivery ledger dedupe",
      "task-aware inlining", "fail-open audit"]),
    ("atlas mirror reconciliation",
     ["diff header totals", "tombstone sweep", "pk keyset scan"]),
    ("scorecard export service",
     ["csv writer", "auth token cron", "retry cap"]),
]

DECISIONS = [
    ("errors", "on malformed input raise immediately, no silent fallback parser",
     "silent fallbacks hid three corruption bugs", "all ingest code"),
    ("schema", "single events table with JSON payload, no per-kind tables",
     "migration cost dominated the old design", "store layer"),
    ("budget", "injection payloads hard-capped at 5200 chars",
     "context windows are the scarce resource", "composer"),
    ("dedupe", "delivery ledger keyed (agent_id, item_ref), never re-send",
     "repeat payloads wasted 90 percent of injected tokens", "injector"),
    ("locality", "sqlite WAL single-file store, no server dependency",
     "fleet boxes must work offline", "store layer"),
    ("verify", "generator never grades itself, verification in fresh context",
     "self-graded loops converge on slop", "all review lanes"),
]

TRAVERSAL_QS = [
    ("which tool fails most on this fleet", 9),
    ("who owns the injection pipeline build", 6),
    ("what decision governs error handling", 5),
    ("where is the delivery ledger schema", 3),
    ("how do I join the atlas reconciliation build", 2),
]


# --------------------------------------------------------------------------
# seeding
# --------------------------------------------------------------------------

def _sql(store, query, params=()):
    """Direct-SQL helper that degrades to a no-op on failure (fail open)."""
    try:
        return store.sql(query, params)
    except Exception:
        return []


def seed(store):
    """Populate a fresh store with the synthetic fleet. Deterministic."""
    rng = random.Random(SEED)
    now = int(time.time())
    counts = {"sessions": 0, "tool_events": 0, "builds": 0,
              "decisions": 0, "traversals": 0, "events": 0}

    # -- 40 sessions across 12 agents --------------------------------------
    agents = [f"agent-{i:02d}" for i in range(12)]
    sessions = []
    for s in range(40):
        agent = agents[s % len(agents)]
        session = f"sess-{s:03d}"
        sessions.append((agent, session))
        task = BUILDS[s % len(BUILDS)][0]
        if CLIENT is not None:
            try:
                CLIENT.register(store, agent, session, "builder", task)
            except Exception:
                pass
        else:
            store.event("session_start", agent, session,
                        {"role": "builder", "task": task})
        counts["sessions"] += 1
    counts["events"] += counts["sessions"]

    # -- heartbeats so LIVE AGENTS has rows --------------------------------
    hot_files = ["src/inject.py", "src/store.py", "src/brief.py",
                 "hooks/session_start.py", "bench/bench.py"]
    for agent, session in sessions[:8]:
        files = rng.sample(hot_files, k=2)
        if CLIENT is not None:
            try:
                CLIENT.heartbeat(store, agent, files, status="active")
            except Exception:
                pass
        else:
            _sql(store,
                 "INSERT OR REPLACE INTO awareness "
                 "(agent_id, role, task_head, files_hot, status, updated_at) "
                 "VALUES (?,?,?,?,?,?)",
                 (agent, "builder", BUILDS[0][0], json.dumps(files),
                  "active", now))

    # -- 500 tool events with per-tool error skew --------------------------
    batch_by_session = {}
    for tool, n, n_err in TOOL_MIX:
        flags = [1] * n_err + [0] * (n - n_err)
        rng.shuffle(flags)
        for is_err in flags:
            agent, session = sessions[rng.randrange(len(sessions))]
            batch_by_session.setdefault(session, []).append({
                "tool": tool,
                "path": rng.choice(hot_files),
                "is_error": is_err,
                "ts": now - rng.randrange(3600 * 20),
            })
            counts["tool_events"] += 1
    for session, batch in batch_by_session.items():
        if CLIENT is not None:
            try:
                CLIENT.tools(store, session, batch)
                continue
            except Exception:
                pass
        for t in batch:
            _sql(store,
                 "INSERT INTO tool_events (session, tool, path, is_error, ts) "
                 "VALUES (?,?,?,?,?)",
                 (session, t["tool"], t["path"], t["is_error"], t["ts"]))

    # -- 3 builds: one goal node + task children each ----------------------
    for title, tasks in BUILDS:
        rows = _sql(store,
                    "INSERT INTO plan_node (root_id, kind, title, status, owner) "
                    "VALUES (?,?,?,?,?) RETURNING id",
                    (None, "goal", title, "active",
                     agents[counts["builds"] % len(agents)]))
        root_id = rows[0][0] if rows else None
        if root_id is None:
            r2 = _sql(store, "SELECT max(id) FROM plan_node")
            root_id = r2[0][0] if r2 and r2[0] else 0
        for i, t in enumerate(tasks):
            status = "done" if i < len(tasks) // 2 else "open"
            _sql(store,
                 "INSERT INTO plan_node (root_id, kind, title, status, owner) "
                 "VALUES (?,?,?,?,?)",
                 (root_id, "task", t, status, agents[i % len(agents)]))
        store.event("plan", "orchestrator", "sess-000",
                    {"build": title, "root_id": root_id})
        counts["builds"] += 1
        counts["events"] += 1

    # -- 6 standing decisions ----------------------------------------------
    for subject, choice, rationale, binds in DECISIONS:
        _sql(store,
             "INSERT INTO decision "
             "(scope_root, subject, choice, rationale, binds, status) "
             "VALUES (?,?,?,?,?,?)",
             (1, subject, choice, rationale, binds, "standing"))
        store.event("decision", "orchestrator", "sess-000",
                    {"subject": subject, "choice": choice})
        counts["decisions"] += 1
        counts["events"] += 1

    # -- traversal logs (feeds FLEET ATTENTION) ----------------------------
    for q, times_asked in TRAVERSAL_QS:
        for _ in range(times_asked):
            agent, _s = sessions[rng.randrange(len(sessions))]
            if CLIENT is not None:
                try:
                    CLIENT.traverse(store, agent, q, ["events", "tool_events"])
                except Exception:
                    store.event("observation", agent, "sess-000",
                                {"traversal": True, "q": q})
            else:
                store.event("observation", agent, "sess-000",
                            {"traversal": True, "q": q})
            counts["traversals"] += 1
            counts["events"] += 1
    # Report what the store actually HOLDS. The event hash collapses identical
    # repeats by design, so counting insert attempts overstates the corpus
    # (an independent review caught 74 claimed vs 68 real).
    for key, q in (("events", "SELECT COUNT(*) FROM events"),
                   ("tool_events", "SELECT COUNT(*) FROM tool_events"),
                   ("builds", "SELECT COUNT(*) FROM plan_node WHERE kind='goal'"),
                   ("decisions", "SELECT COUNT(*) FROM decision")):
        try:
            r = store.sql(q)
            if r:
                counts[key] = int(r[0][0])
        except Exception:
            pass
    counts["note"] = "counts are rows present after dedupe, not inserts attempted"
    return counts


# --------------------------------------------------------------------------
# latency + dedupe
# --------------------------------------------------------------------------

def _ms(fn, reps):
    """Median / min / max wall time of fn() in ms; fail-open to None."""
    times = []
    for _ in range(reps):
        t0 = time.perf_counter()
        try:
            fn()
        except Exception:
            return None
        times.append((time.perf_counter() - t0) * 1000)
    return {"median_ms": round(statistics.median(times), 2),
            "min_ms": round(min(times), 2),
            "max_ms": round(max(times), 2),
            "n": reps}


def measure_latency(store):
    out = {}
    out["full_brief"] = _ms(lambda: BRIEF.full_brief(store), 10)
    try:
        _, wm = BRIEF.full_brief(store)
        since = max(0, int(wm) - 10)
    except Exception:
        since = 0
    out["delta_brief"] = _ms(lambda: BRIEF.delta_brief(store, since), 10)

    # first-contact compose: fresh agent id each rep so every call takes the
    # full-brief path
    box = {"i": 0}

    def first_contact():
        box["i"] += 1
        INJECT.compose(store, f"lat-fresh-{box['i']}", "sess-lat",
                       role="builder", task="injection pipeline")

    out["compose_first_contact"] = _ms(first_contact, 5)

    # returning-agent compose: same agent, second-and-later calls take the
    # delta path
    try:
        INJECT.compose(store, "lat-returning", "sess-lat")
    except Exception:
        pass
    out["compose_delta"] = _ms(
        lambda: INJECT.compose(store, "lat-returning", "sess-lat"), 5)
    return out


def measure_dedupe(store):
    """First-contact payload vs immediate second payload for one agent."""
    agent = "dedupe-probe"
    try:
        first = INJECT.compose(store, agent, "sess-dd", role="builder",
                               task="context orchestrator injection pipeline")
        second = INJECT.compose(store, agent, "sess-dd", role="builder",
                                task="context orchestrator injection pipeline")
    except Exception as e:
        return {"error": str(e)}
    c1, c2 = len(first), len(second)
    return {"first_payload_chars": c1,
            "second_payload_chars": c2,
            "ratio": round(c1 / c2, 1) if c2 else None,
            "second_payload_head": second[:120]}


# --------------------------------------------------------------------------
# optional --llm map-vs-dump probe
# --------------------------------------------------------------------------

QUESTIONS = [
    {"id": "q1",
     "q": "Which tool has the highest error rate on this fleet, and roughly "
          "what is that rate?",
     "check": {"kind": "all_substr", "want": ["powershell"]},
     "check2": {"kind": "num_near", "value": 0.14, "tol": 0.04}},
    {"id": "q2",
     "q": "How many active builds does the fleet have right now?",
     "check": {"kind": "num_near", "value": 3, "tol": 0.5}},
    {"id": "q3",
     "q": "What standing decision governs error handling, and what does it "
          "bind future work to?",
     "check": {"kind": "all_substr", "want": ["malformed"]},
     "check2": {"kind": "any_substr", "want": ["raise", "no silent"]}},
    {"id": "q4",
     "q": "How many standing decisions are in force?",
     "check": {"kind": "num_near", "value": 6, "tol": 0.5}},
    {"id": "q5",
     "q": "Roughly what is the fleet-wide error rate of the Edit tool?",
     "check": {"kind": "num_near", "value": 0.033, "tol": 0.015}},
    {"id": "q6",
     "q": "What is the most-asked traversal question on this fleet in the "
          "last 24 hours?",
     "check": {"kind": "any_substr", "want": ["fails most", "tool fails"]}},
    {"id": "q7",
     "q": "What hard character cap applies to injection payloads, per the "
          "standing decisions?",
     "check": {"kind": "num_near", "value": 5200, "tol": 1}},
    {"id": "q8",
     "q": "Which build is the injection/orchestrator work, by title?",
     "check": {"kind": "any_substr",
               "want": ["context orchestrator", "injection pipeline"]}},
]

STOP = {"what", "which", "the", "this", "that", "and", "roughly", "does",
        "is", "in", "of", "a", "to", "how", "many", "it", "by", "more",
        "than", "one", "fleet", "tool", "with", "are", "per", "last",
        "right", "now", "most", "on"}


def grade_one(check, answer):
    a = answer.lower()
    k = check["kind"]
    if k == "all_substr":
        return all(w in a for w in check["want"])
    if k == "any_substr":
        return any(w in a for w in check["want"])
    if k == "num_near":
        nums = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", a)[:40]]
        v, t = check["value"], check["tol"]
        return any(abs(n - v) <= t or abs(n - v * 100) <= t * 100
                   for n in nums)
    return False


def grade(qd, answer):
    ok1 = grade_one(qd["check"], answer)
    ok2 = grade_one(qd["check2"], answer) if "check2" in qd else None
    return round((ok1 + (ok2 if ok2 is not None else ok1)) / 2, 2)


def dump_context(store, question, budget):
    """DUMP arm: keyword-grep snippets over raw event text at equal budget.

    The honest baseline for 'just search the raw logs'.
    """
    words = [w.lower() for w in re.findall(r"[A-Za-z][A-Za-z\-]{3,}", question)
             if w.lower() not in STOP][:8]
    lines = []
    for r in _sql(store, "SELECT kind, actor, session, payload FROM events "
                         "ORDER BY id"):
        lines.append(f"[{r[0]} {r[1]}/{r[2]}] {r[3]}")
    for r in _sql(store, "SELECT session, tool, path, is_error FROM "
                         "tool_events ORDER BY id"):
        lines.append(f"[tool {r[0]}] {r[1]} {r[2]} error={r[3]}")
    out, used = [], 0
    for line in lines:
        low = line.lower()
        hits = sum(1 for w in words if w in low)
        if words and hits < max(1, len(words) // 4):
            continue
        out.append(line[:200])
        used += len(line[:200]) + 1
        if used > budget:
            break
    if not out:  # no keyword hits — take the head of the raw stream
        for line in lines:
            out.append(line[:200])
            used += len(line[:200]) + 1
            if used > budget:
                break
    return "\n".join(out)[:budget]


def _llm_call(mode, prompt, ollama_url, ollama_model):
    """One completion via Ollama or the Anthropic API. Returns text or None."""
    import urllib.request
    try:
        if mode == "anthropic":
            body = json.dumps({
                "model": os.environ.get("PREBRIEF_BENCH_MODEL",
                                        "claude-haiku-4-5"),
                "max_tokens": 400,
                "messages": [{"role": "user", "content": prompt}],
            }).encode()
            req = urllib.request.Request(
                "https://api.anthropic.com/v1/messages", data=body,
                headers={"x-api-key": os.environ["ANTHROPIC_API_KEY"],
                         "anthropic-version": "2023-06-01",
                         "content-type": "application/json"})
            with urllib.request.urlopen(req, timeout=120) as r:
                j = json.load(r)
            return "".join(b.get("text", "") for b in j.get("content", []))
        body = json.dumps({
            "model": ollama_model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "options": {"num_predict": 400},
        }).encode()
        req = urllib.request.Request(
            ollama_url.rstrip("/") + "/api/chat", data=body,
            headers={"content-type": "application/json"})
        with urllib.request.urlopen(req, timeout=300) as r:
            j = json.load(r)
        return (j.get("message") or {}).get("content", "")
    except Exception as e:
        print(f"  llm call failed: {e}", file=sys.stderr)
        return None


def run_llm_probe(store, mode, reps, ollama_url, ollama_model):
    """Map-vs-dump probe: same questions, same budget, two context arms."""
    rows = []
    cell = 0
    for qd in QUESTIONS:
        for rep in range(reps):
            cell += 1
            # MAP arm: a fresh agent's actual first-contact payload
            try:
                map_ctx = INJECT.compose(store, f"probe-{qd['id']}-{rep}",
                                         "sess-probe", role="builder",
                                         task="")
            except Exception:
                map_ctx = ""
            budget = max(len(map_ctx), 800)
            for arm, ctx in (("MAP", map_ctx),
                             ("DUMP", dump_context(store, qd["q"], budget))):
                prompt = (
                    "You are a fresh agent joining a fleet. Answer the "
                    "question using ONLY the context below.\n\nCONTEXT:\n"
                    f"{ctx}\n\nQUESTION: {qd['q']}\n\nAnswer in <=2 "
                    "sentences. If the context truly lacks the answer, say "
                    "'not in context'.")
                text = _llm_call(mode, prompt, ollama_url, ollama_model)
                if text is None:
                    continue
                score = grade(qd, text)
                rows.append({"q": qd["id"], "arm": arm, "rep": rep,
                             "score": score, "ctx_chars": len(ctx),
                             "answer": text[:200]})
                print(f"  {qd['id']} {arm}{rep}: score={score} "
                      f"ctx={len(ctx)}ch :: {text[:80]!r}", flush=True)
    summary = {}
    for arm in ("MAP", "DUMP"):
        sel = [r for r in rows if r["arm"] == arm]
        if sel:
            summary[arm] = {
                "n": len(sel),
                "accuracy": round(statistics.mean(r["score"] for r in sel), 3),
                "ctx_chars_mean": round(statistics.mean(
                    r["ctx_chars"] for r in sel)),
            }
    return {"mode": mode, "cells": rows, "summary": summary}


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def render_table(results):
    lat = results.get("latency", {})
    dd = results.get("dedupe", {})

    def m(key):
        v = lat.get(key)
        return f"{v['median_ms']} ms (min {v['min_ms']}, max {v['max_ms']})" \
            if v else "n/a"

    lines = [
        "| metric | value |",
        "|---|---|",
        f"| seeded fleet | {results['seed']['sessions']} sessions, "
        f"{results['seed']['tool_events']} tool events, "
        f"{results['seed']['builds']} builds, "
        f"{results['seed']['decisions']} decisions |",
        f"| full brief compose | {m('full_brief')} |",
        f"| delta brief compose | {m('delta_brief')} |",
        f"| first-contact payload compose | {m('compose_first_contact')} |",
        f"| returning-agent payload compose | {m('compose_delta')} |",
        f"| first payload | {dd.get('first_payload_chars', 'n/a')} chars |",
        f"| second payload (dedupe) | "
        f"{dd.get('second_payload_chars', 'n/a')} chars |",
        f"| dedupe lift | {dd.get('ratio', 'n/a')}x |",
    ]
    llm = results.get("llm")
    if llm and llm.get("summary"):
        for arm, s in llm["summary"].items():
            lines.append(f"| {arm} arm accuracy (n={s['n']}) | "
                         f"{s['accuracy']} @ ~{s['ctx_chars_mean']} chars |")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="Prebrief synthetic-fleet bench")
    ap.add_argument("--db", default=None,
                    help="scratch DB path (default: fresh temp file)")
    ap.add_argument("--llm", nargs="?", const="auto", default=None,
                    choices=["auto", "ollama", "anthropic"],
                    help="run the map-vs-dump probe with a live model")
    ap.add_argument("--reps", type=int, default=2,
                    help="reps per question per arm in --llm mode")
    ap.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    ap.add_argument("--ollama-model", default="qwen3:14b")
    args = ap.parse_args()

    db = args.db or os.path.join(
        tempfile.mkdtemp(prefix="prebrief-bench-"), "bench.db")
    if os.path.exists(db):
        try:
            os.remove(db)
        except OSError:
            pass
    print(f"bench DB: {db}")
    store = Store(db)

    t0 = time.perf_counter()
    counts = seed(store)
    seed_s = time.perf_counter() - t0
    print(f"seeded: {counts} in {seed_s:.2f}s")

    results = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "note": ("synthetic-fleet approximation; production numbers in "
                 "README.md come from the authors' live-fleet study"),
        "db": db,
        "seed": counts,
        "seed_s": round(seed_s, 2),
        "latency": measure_latency(store),
        "dedupe": measure_dedupe(store),
    }

    if args.llm:
        mode = args.llm
        if mode == "auto":
            mode = "anthropic" if os.environ.get("ANTHROPIC_API_KEY") \
                else "ollama"
        print(f"llm probe via {mode} ...")
        results["llm"] = run_llm_probe(store, mode, args.reps,
                                       args.ollama_url, args.ollama_model)

    out_path = os.path.join(HERE, "bench_results.json")
    try:
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=1)
        print(f"wrote {out_path}")
    except OSError as e:
        print(f"could not write results file: {e}", file=sys.stderr)

    print()
    print(render_table(results))


if __name__ == "__main__":
    main()

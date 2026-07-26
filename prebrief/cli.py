"""cli — the `prebrief` command.

Subcommands:
  init    [--db PATH]                      create/open the store, print its path
  enroll  <project_dir> [--remove]         additively wire the hook into
                                           .claude/settings.json (idempotent,
                                           never clobbers, backup kept)
  brief                                    print the current full fleet brief
  payload <agent> <session> [--role --task]  print the composed injection
  assess                                   24h stats + optional local-model
                                           verdict -> report + observation event

Stdlib only. Every subcommand fails open: errors degrade to a smaller result
and a stderr note, exit code 0.
"""
import argparse
import json
import os
import shutil
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

HOOK_EVENTS = {"SessionStart": 20, "UserPromptSubmit": 20,
               "PostToolUse": 15, "Stop": 15}
# Marker identifying OUR hook command inside a settings.json hook group.
HOOK_MARKS = ("prebrief\\hook.py", "prebrief/hook.py")


def _db_path(override=None):
    """Resolve the store path: --db, env PREBRIEF_DB, or ~/.prebrief/prebrief.db."""
    p = override or os.environ.get("PREBRIEF_DB") or os.path.join(
        os.path.expanduser("~"), ".prebrief", "prebrief.db")
    d = os.path.dirname(p)
    if d:
        os.makedirs(d, exist_ok=True)
    return p


def _store(db=None):
    from prebrief.store import Store
    return Store(_db_path(db))


def _hook_command():
    """The command line enrolled into settings.json for this installation."""
    hook = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hook.py")
    return f'python "{hook}"'


def _is_ours(group):
    """True if a settings.json hook group contains the prebrief hook."""
    for h in group.get("hooks", []):
        cmd = h.get("command", "")
        if any(m in cmd for m in HOOK_MARKS):
            return True
    return False


# ---------------------------------------------------------------- subcommands

def cmd_init(args):
    path = _db_path(args.db)
    _store(args.db)  # instantiation creates tables (WAL mode)
    print(f"prebrief store ready: {path}")


def cmd_enroll(args):
    """Additively merge our hook into <project_dir>/.claude/settings.json.

    Ported from harness enroll.py: existing hooks are preserved, our hook is
    appended as one extra matcher group per event, running twice is a no-op,
    and a timestamped backup is kept beside settings.json before any write.
    """
    proj = os.path.abspath(args.project_dir)
    d = os.path.join(proj, ".claude")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, "settings.json")
    cfg = {}
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            cfg = json.load(f)
        shutil.copy2(p, p + f".pre-prebrief-{time.strftime('%Y%m%d%H%M%S')}.bak")
    hooks = cfg.setdefault("hooks", {})
    cmd = _hook_command()
    changed = False
    for ev, tmo in HOOK_EVENTS.items():
        groups = hooks.setdefault(ev, [])
        ours = [g for g in groups if _is_ours(g)]
        if args.remove:
            if ours:
                hooks[ev] = [g for g in groups if g not in ours]
                changed = True
            continue
        if not ours:
            groups.append({"hooks": [{"type": "command", "command": cmd,
                                      "timeout": tmo}]})
            changed = True
    if changed:
        with open(p, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    print(f"{'removed from' if args.remove else 'enrolled'} {proj} "
          f"(changed={changed}; backup kept beside settings.json)")


def cmd_brief(args):
    from prebrief import brief
    text, wm = brief.full_brief(_store())
    print(text)
    print(f"(watermark {wm})", file=sys.stderr)


def cmd_payload(args):
    from prebrief import inject
    print(inject.compose(_store(), args.agent, args.session,
                         role=args.role, task=args.task))


# ------------------------------------------------------------------- assess

def _ts_cutoff(store, table, col):
    """A 24h-ago cutoff matching however `col` is stored (epoch or ISO text).

    Inspects the newest value's type; defaults to epoch seconds when the
    table is empty or unreadable.
    """
    epoch = time.time() - 86400
    try:
        rows = store.sql(f"SELECT {col} FROM {table} ORDER BY rowid DESC LIMIT 1")
        if rows and isinstance(rows[0][0], str):
            return time.strftime("%Y-%m-%dT%H:%M:%S",
                                 time.localtime(epoch))
    except Exception:
        pass
    return epoch


def _one(store, query, params=()):
    """Run a stats query, degrading to 'n/a' on any error (fail open)."""
    try:
        return store.sql(query, params)
    except Exception:
        return "n/a"


def _assess_stats(store):
    """Last-24h orchestrator activity, ported from mt_assess.stats()."""
    ev_cut = _ts_cutoff(store, "events", "ts")
    tool_cut = _ts_cutoff(store, "tool_events", "ts")
    del_cut = _ts_cutoff(store, "delivery", "delivered_at")
    s = {}
    s["sessions_started"] = _one(store,
        "SELECT count(*) FROM events WHERE kind='session.start' AND ts>?",
        (ev_cut,))
    s["deliveries"] = _one(store,
        "SELECT count(*), count(DISTINCT agent_id) FROM delivery "
        "WHERE delivered_at>?", (del_cut,))
    s["traversals"] = _one(store,
        "SELECT count(*) FROM events WHERE kind='observation' "
        "AND payload LIKE '%traversal%' AND ts>?", (ev_cut,))
    s["open_decisions"] = _one(store,
        "SELECT count(*) FROM decision WHERE status='active'")
    s["tool_errors"] = _one(store,
        "SELECT coalesce(sum(is_error),0), count(*) FROM tool_events "
        "WHERE ts>?", (tool_cut,))
    s["top_asked"] = _one(store,
        "SELECT json_extract(payload,'$.q'), count(*) FROM events "
        "WHERE kind='observation' AND payload LIKE '%traversal%' AND ts>? "
        "GROUP BY 1 ORDER BY 2 DESC LIMIT 3", (ev_cut,))
    s["live_agents"] = _one(store,
        "SELECT count(*) FROM awareness WHERE status='active'")
    return s


def _assess_local(day, url="http://127.0.0.1:11434"):
    """Local-model verdict on the day's stats; deterministic fallback."""
    prompt = ("You audit a context-injection system for agent fleets. "
              f"Yesterday's stats (sqlite rows):\n"
              f"{json.dumps(day, default=str)[:3000]}\n\n"
              'Output STRICT JSON: {"caught": ["<=2 issues visible in the '
              'stats"], "fixed_or_fine": ["<=2 things working as designed"], '
              '"improve": "<=25 words, ONE concrete change to the injected '
              'brief", "confidence": 0.0-1.0}')
    try:
        body = json.dumps({"model": "qwen3:14b", "stream": False,
                           "format": "json",
                           "messages": [{"role": "user", "content": prompt}],
                           "options": {"num_predict": 400}}).encode()
        req = urllib.request.Request(url.rstrip("/") + "/api/chat", data=body,
                                     headers={"content-type": "application/json"})
        with urllib.request.urlopen(req, timeout=180) as r:
            out = json.loads(json.load(r)["message"]["content"])
        if isinstance(out.get("caught"), list):
            out.setdefault("fixed_or_fine", [])
            out.setdefault("improve", "no change proposed")
            out.setdefault("confidence", 0.0)
            return out, "qwen3:14b"
    except Exception:
        pass
    return {"caught": ["local assessment lane unavailable"],
            "fixed_or_fine": ["deterministic stats captured"],
            "improve": "manual review of stats below",
            "confidence": 0.0}, "fallback"


def cmd_assess(args):
    """Daily moving-target quality loop, ported from mt_assess.py.

    Pulls 24h stats from the store, gets a local-model verdict (deterministic
    fallback when Ollama is down), writes ~/.prebrief/reports/mt-assess-DATE.md,
    and records an observation event so findings surface in the moving target.
    """
    store = _store()
    day = _assess_stats(store)
    verdict, model = _assess_local(day)
    date = time.strftime("%Y-%m-%d")
    lines = [f"# Moving-target assessment — {date} (assessor: {model})", ""]
    lines.append(f"- caught: {'; '.join(str(c) for c in verdict['caught'])}")
    lines.append("- working: "
                 + "; ".join(str(x) for x in verdict["fixed_or_fine"]))
    lines.append(f"- improve next: {verdict['improve']} "
                 f"(conf {verdict['confidence']})")
    lines.append("\n## raw stats (24h)")
    for k, v in day.items():
        lines.append(f"- {k}: {v}")
    reports = os.path.join(os.path.expanduser("~"), ".prebrief", "reports")
    os.makedirs(reports, exist_ok=True)
    rp = os.path.join(reports, f"mt-assess-{date}.md")
    with open(rp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    try:
        store.event("observation", "mt-assess", "daily",
                    {"mt_assess": True, "date": date,
                     "improve": str(verdict["improve"]),
                     "caught": [str(c) for c in verdict["caught"][:2]],
                     "assessor": model})
    except Exception as e:
        print(f"prebrief assess: observation event failed: {e}",
              file=sys.stderr)
    print(f"report -> {rp}")
    print(f"improve: {verdict['improve']}")


# --------------------------------------------------------------------- main

def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="prebrief",
        description="Prebrief — context orchestrator for AI agent fleets.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init", help="create/open the store")
    p.add_argument("--db", default=None, help="store path override")
    p.set_defaults(fn=cmd_init)

    p = sub.add_parser("enroll",
                       help="wire the hook into a project's .claude/settings.json")
    p.add_argument("project_dir")
    p.add_argument("--remove", action="store_true",
                   help="remove our hook groups (others untouched)")
    p.set_defaults(fn=cmd_enroll)

    p = sub.add_parser("brief", help="print the current full fleet brief")
    p.set_defaults(fn=cmd_brief)

    p = sub.add_parser("payload", help="print the composed injection payload")
    p.add_argument("agent")
    p.add_argument("session")
    p.add_argument("--role", default="builder")
    p.add_argument("--task", default="")
    p.set_defaults(fn=cmd_payload)

    p = sub.add_parser("assess",
                       help="24h stats + verdict -> report + observation event")
    p.set_defaults(fn=cmd_assess)

    args = ap.parse_args(argv)
    try:
        args.fn(args)
    except Exception as e:
        # Fail open: degrade to a stderr note, never a traceback to the caller.
        print(f"prebrief {args.cmd}: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())

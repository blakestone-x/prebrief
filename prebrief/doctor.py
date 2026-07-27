"""prebrief doctor — the system diagnoses itself.

Every defect an independent reviewer found in v0.1-v0.2 was invisible until
someone went looking: a migration that never ran, a cursor ahead of its log, a
ledger claiming deliveries that were never rendered, version metadata drifting
across three files. Each is now a check that runs in under a second.

The rule this encodes: a guard that only exists in a test protects the repo;
a guard that runs against the live database protects the fleet.

  prebrief doctor            # human-readable report, exit 1 if anything is ERROR
  prebrief doctor --json     # machine-readable, for the daily assessor
"""
import json
import time

OK, WARN, ERROR = "ok", "warn", "error"


def _check(name, status, detail, fix=""):
    return {"check": name, "status": status, "detail": detail, "fix": fix}


def run(store):
    """Return a list of check results. Never raises — a broken doctor is useless."""
    out = []

    # 1. schema/migration parity — the class behind the used_at regression
    try:
        from .store import migration_gaps
        gaps = migration_gaps()
        out.append(_check(
            "schema.migration_parity", OK if not gaps else ERROR,
            "schema and migrations agree" if not gaps else f"unmigrated columns: {gaps}",
            "add the missing (table, column, decl) to _MIGRATIONS in store.py"))
    except Exception as e:
        out.append(_check("schema.migration_parity", WARN, f"uncheckable: {e}"))

    # 2. projector cursor sanity — must never lead the log it derives from
    try:
        head = store.sql("SELECT COALESCE(MAX(id),0) FROM events")
        head = int(head[0][0]) if head else 0
        has_state = store.sql("SELECT name FROM sqlite_master WHERE type='table' "
                              "AND name='projector_state'")
        cur = 0
        if has_state:
            cur = store.sql("SELECT last_event_id FROM projector_state WHERE id=1")
            cur = int(cur[0][0]) if cur else 0
        lag = head - cur
        st = ERROR if lag < 0 else (WARN if lag > 500 else OK)
        out.append(_check(
            "projector.cursor", st,
            f"cursor {cur} / log head {head} (lag {lag})",
            "cursor ahead of log means deleted events or a bad advance; "
            "run `prebrief rebuild`" if lag < 0 else
            "run `prebrief project` to catch up" if lag > 500 else ""))
    except Exception as e:
        out.append(_check("projector.cursor", WARN, f"uncheckable: {e}"))

    # 3. ledger honesty — a delivered ref must name a row that still exists
    try:
        refs = store.sql("SELECT item_ref FROM delivery WHERE item_ref LIKE 'decision:%' "
                         "OR item_ref LIKE 'build:%'") or []
        dangling = 0
        for (ref,) in [(r[0],) for r in refs]:
            kind, _, ident = str(ref).partition(":")
            if not ident.isdigit():
                continue
            tbl = "plan_node" if kind == "build" else "decision"
            hit = store.sql(f"SELECT 1 FROM {tbl} WHERE id=?", (int(ident),))
            if not hit:
                dangling += 1
        out.append(_check(
            "ledger.referents", OK if dangling == 0 else WARN,
            f"{len(refs)} item refs, {dangling} pointing at rows that no longer exist",
            "rebuild projections or prune the ledger" if dangling else ""))
    except Exception as e:
        out.append(_check("ledger.referents", WARN, f"uncheckable: {e}"))

    # 4. delivery effectiveness — is injected context actually being acted on?
    try:
        r = store.sql("SELECT SUM(used_at IS NOT NULL), COUNT(*) FROM delivery "
                      "WHERE item_ref NOT IN ('manual','wm')")
        used, total = (r[0][0] or 0, r[0][1] or 0) if r else (0, 0)
        rate = (used / total) if total else None
        st = OK if (rate is None or rate >= 0.15) else WARN
        out.append(_check(
            "delivery.effectiveness", st,
            f"{used}/{total} delivered items acted on"
            + (f" ({rate:.0%})" if rate is not None else " (no data yet)"),
            "low usage means the composer is sending items agents do not need"
            if st == WARN else ""))
    except Exception as e:
        out.append(_check("delivery.effectiveness", WARN, f"uncheckable: {e}"))

    # 5. echo detection — the substrate must not re-ingest its own briefs
    try:
        has = store.sql("SELECT name FROM sqlite_master WHERE type='table' AND name='claims'")
        if not has:
            out.append(_check("memory.echo", OK, "no claims table yet (nothing extracted)"))
            raise StopIteration
        r = store.sql("SELECT COUNT(*) FROM claims WHERE status='active' AND "
                      "(subject LIKE 'fleet-context%' OR subject LIKE '%moving target%')")
        echoes = int(r[0][0]) if r else 0
        out.append(_check(
            "memory.echo", OK if echoes == 0 else ERROR,
            f"{echoes} active claims derived from our own injected brief",
            "retire them and check the extractor's echo guard" if echoes else ""))
    except StopIteration:
        pass
    except Exception as e:
        out.append(_check("memory.echo", WARN, f"uncheckable: {e}"))

    # 6. tenant hygiene — unlabelled rows silently fall into 'default'
    try:
        r = store.sql("SELECT COUNT(*) FROM events WHERE COALESCE(project,'')=''")
        unlabelled = int(r[0][0]) if r else 0
        tot = store.sql("SELECT COUNT(*) FROM events")
        tot = int(tot[0][0]) if tot else 0
        out.append(_check(
            "tenant.labelling", OK if unlabelled == 0 else WARN,
            f"{unlabelled}/{tot} events with no project label",
            "callers should pass project=; unlabelled rows pool into 'default'"
            if unlabelled else ""))
    except Exception as e:
        out.append(_check("tenant.labelling", WARN, f"uncheckable: {e}"))

    # 7. release metadata agreement
    try:
        import os
        import re
        import prebrief
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        pv = re.search(r'version\s*=\s*"([^"]+)"',
                       open(os.path.join(root, "pyproject.toml"),
                            encoding="utf-8").read()).group(1)
        agree = pv == prebrief.__version__
        out.append(_check(
            "release.version", OK if agree else ERROR,
            f"package {prebrief.__version__}, pyproject {pv}",
            "sync pyproject.toml, __init__.py and CITATION.cff" if not agree else ""))
    except Exception as e:
        out.append(_check("release.version", WARN, f"uncheckable: {e}"))

    return out


def report(store, as_json=False):
    checks = run(store)
    worst = ERROR if any(c["status"] == ERROR for c in checks) else (
        WARN if any(c["status"] == WARN for c in checks) else OK)
    if as_json:
        return json.dumps({"status": worst, "at": time.time(), "checks": checks},
                          indent=1), (1 if worst == ERROR else 0)
    mark = {OK: "  ok  ", WARN: " warn ", ERROR: " ERROR"}
    lines = ["prebrief doctor", ""]
    for c in checks:
        lines.append(f"[{mark[c['status']]}] {c['check']:<28} {c['detail']}")
        if c["fix"] and c["status"] != OK:
            lines.append(f"{'':>10}  -> {c['fix']}")
    lines.append("")
    lines.append(f"overall: {worst}")
    return "\n".join(lines), (1 if worst == ERROR else 0)

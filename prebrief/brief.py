"""prebrief.brief — the moving target.

A standing fleet brief, regenerated on demand from the live substrate: a
PROJECTION, derived and rebuildable, zero manual feeding. Renders to a hard
character budget, carries its own watermark + timestamp, and ends with a
traversal manual so the brief is also the map's own user manual.

full_brief(store)          -> (text, watermark)   session start / spawn
delta_brief(store, since)  -> (text, watermark)   per-turn: only what changed
"""
import json
import time

BUDGET_CHARS = 5200   # ~1,300 tokens hard ceiling for the full brief

_MANUAL = """
-- HOW TO GO DEEPER (the map is self-serve; do not load raw history) --
  state of a build ......... store.sql("SELECT * FROM plan_node WHERE root_id=? OR id=?", (bid, bid))
  standing rules ........... store.sql("SELECT * FROM decision WHERE status='standing'")
  who else is here ......... store.sql("SELECT * FROM awareness WHERE status='active'")
  risk for your files ...... store.sql("SELECT path, SUM(is_error), COUNT(*) FROM tool_events GROUP BY path")
  raw depth ................ store.sql("SELECT * FROM events WHERE id > ?", (watermark,))
  record what you learned .. client.traverse(store, me, question, refs)"""


def _ts(epoch=None):
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(epoch))
    except Exception:
        return "?"


def _payload_head(raw, n=60):
    try:
        return " ".join(str(raw).split())[:n]
    except Exception:
        return ""


def full_brief(store):
    """Render the full fleet brief. Returns (text, watermark). Fails open:
    any section that errors is simply absent."""
    out = []
    wm = 0
    try:
        rows = store.sql("SELECT COALESCE(MAX(id),0) FROM events")
        wm = int(rows[0][0]) if rows else 0
    except Exception:
        wm = 0
    out.append(f"== FLEET BRIEF @ {_ts()} (watermark {wm}) ==")
    out.append("Freshness: rows below are live projections, not curated text; "
               "regenerate via prebrief.brief.full_brief.")

    # -- presence: who is alive and what they hold -------------------------
    try:
        cutoff = time.time() - 15 * 60
        live = store.sql(
            "SELECT agent_id, role, status, substr(COALESCE(task_head,''),1,60) "
            "FROM awareness WHERE updated_at > ? "
            "ORDER BY updated_at DESC LIMIT 8", (cutoff,))
        out.append(f"\n-- LIVE AGENTS ({len(live)}) --")
        for r in live:
            out.append(f"  {r[0]} [{r[1]}/{r[2]}] {r[3]}")
    except Exception:
        pass

    # -- active builds: open plan state ------------------------------------
    try:
        builds = store.sql(
            "SELECT n.id, n.title, "
            "SUM(CASE WHEN c.status='open' THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN c.status='done' THEN 1 ELSE 0 END) "
            "FROM plan_node n LEFT JOIN plan_node c ON c.root_id = n.id "
            "WHERE n.kind='goal' AND n.status NOT IN ('done','abandoned') "
            "GROUP BY n.id, n.title ORDER BY n.id DESC LIMIT 5")
        out.append("\n-- ACTIVE BUILDS --")
        for r in builds:
            out.append(
                f"  build:{r[0]} \"{r[1]}\" open={int(r[2] or 0)} "
                f"done={int(r[3] or 0)} "
                f"(join: SELECT * FROM plan_node WHERE root_id={r[0]})")
    except Exception:
        pass

    # -- standing decisions ------------------------------------------------
    try:
        decs = store.sql(
            "SELECT id, subject, substr(COALESCE(choice,''),1,70) FROM decision "
            "WHERE status='standing' ORDER BY id DESC LIMIT 5")
        if decs:
            out.append("-- STANDING DECISIONS (newest) --")
            for r in decs:
                out.append(f"  d{r[0]} [{r[1]}] {r[2]}")
    except Exception:
        pass

    # -- risk surface: what bites people lately ----------------------------
    try:
        out.append("\n-- RISK SURFACE --")
        for r in store.sql(
                "SELECT tool, COUNT(*) n, "
                "ROUND(1.0*SUM(is_error)/COUNT(*),3) err "
                "FROM tool_events GROUP BY tool "
                "HAVING n >= 20 AND err > 0 ORDER BY err DESC LIMIT 3"):
            out.append(f"  tool {r[0]}: err {r[2]} over n={r[1]}")
        for r in store.sql(
                "SELECT path, ROUND(1.0*SUM(is_error)/COUNT(*),3) err, "
                "COUNT(*) touches FROM tool_events WHERE path IS NOT NULL "
                "GROUP BY path HAVING touches >= 10 AND err > 0 "
                "ORDER BY err DESC LIMIT 3"):
            out.append(f"  path {str(r[0])[:70]}: err {r[1]} over {r[2]} touches")
    except Exception:
        pass

    # -- fleet attention: what agents kept asking (24h) --------------------
    att = attention(store)
    if att:
        out.append("\n-- FLEET ATTENTION (most-asked, 24h) --")
        out.extend(att)

    out.append(_MANUAL)
    text = "\n".join(out)
    return text[:BUDGET_CHARS], wm


def delta_brief(store, since):
    """Render only what changed since a watermark. Returns (text, watermark);
    ('', since) when nothing changed."""
    try:
        since = int(since)
    except Exception:
        since = 0
    try:
        ev = store.sql(
            "SELECT id, kind, actor, substr(COALESCE(payload,''),1,60) "
            "FROM events WHERE id > ? ORDER BY id LIMIT 12", (since,))
    except Exception:
        ev = []
    if not ev:
        return "", since
    out = [f"== FLEET DELTA since {since} =="]
    for r in ev:
        out.append(f"  e{r[0]} {r[1]} by {r[2]} :: {_payload_head(r[3])}")
    new_wm = int(ev[-1][0])
    return "\n".join(out), new_wm


def attention(store, limit=4, hours=24):
    """Aggregate traversal observations: what the fleet kept asking lately.
    Returns formatted lines (possibly empty)."""
    lines = []
    try:
        cutoff = time.time() - hours * 3600
        rows = store.sql(
            "SELECT payload FROM events "
            "WHERE kind='observation' AND ts > ? ORDER BY id DESC LIMIT 200",
            (cutoff,))
        counts = {}
        for (raw,) in rows:
            try:
                p = json.loads(raw)
            except Exception:
                continue
            if not p.get("traversal"):
                continue
            q = str(p.get("q", "")).strip()
            if q:
                counts[q] = counts.get(q, 0) + 1
        top = sorted(counts.items(), key=lambda kv: -kv[1])[:limit]
        lines = [f"  {n}x :: {q[:90]}" for q, n in top]
    except Exception:
        pass
    return lines

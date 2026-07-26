"""prebrief.inject — composes the per-agent injection payload (the moving
target, delivery-aware).

First contact: full fleet brief + traversal manual + task-aware inlining; the
delivery ledger records every item delivered. Every later call: only what is
NEW for THIS agent — events past its watermark, decisions/builds not yet
delivered, fleet-attention shifts. Optional local-model curation compresses
when the delta exceeds budget; deterministic truncation otherwise. Fails open:
any component down -> smaller payload, never a crash.
"""
import hashlib
import time

from . import brief as FB

BUDGET = 5200
DELTA_BUDGET = 2000


def _delivered(store, agent):
    """The agent's delivery ledger as {item_ref: watermark}."""
    led = {}
    try:
        for r in store.sql(
                "SELECT item_ref, watermark FROM delivery WHERE agent_id=?",
                (agent,)):
            led[str(r[0])] = int(r[1] or 0)
    except Exception:
        pass
    return led


def _mark(store, agent, refs, wm):
    """Upsert delivery rows for every ref at the current watermark."""
    if not refs:
        return
    now = time.time()
    for ref in set(refs):
        try:
            store.sql(
                "INSERT INTO delivery (agent_id, item_ref, watermark, delivered_at) "
                "VALUES (?,?,?,?) "
                "ON CONFLICT(agent_id, item_ref) DO UPDATE SET "
                "watermark=excluded.watermark, delivered_at=excluded.delivered_at",
                (agent, ref, int(wm), now))
        except Exception:
            pass


def _inline_build(store, task):
    """Task-aware inlining: if the agent's task matches an open build's goal
    title, inline that build's nodes and scoped decisions — the orchestrator
    follows the pointer FOR the agent whose work it serves."""
    words = [w.lower() for w in (task or "").split() if len(w) > 3]
    if not words:
        return ""
    chunks = []
    try:
        goals = store.sql(
            "SELECT id, title FROM plan_node WHERE kind='goal' "
            "AND status NOT IN ('done','abandoned')")
        for gid, title in goals:
            t = str(title or "").lower()
            if not any(w in t or t in w for w in words):
                continue
            lines = [f"\n-- YOUR BUILD (matched '{title}') --"]
            for r in store.sql(
                    "SELECT id, kind, title, status, COALESCE(owner,'') "
                    "FROM plan_node WHERE root_id=? OR id=? "
                    "ORDER BY id LIMIT 12", (gid, gid)):
                own = f" @{r[4]}" if r[4] else ""
                lines.append(f"  [node:{r[0]}/{r[1]}] {r[2]} [{r[3]}]{own}")
            for r in store.sql(
                    "SELECT id, subject, substr(COALESCE(choice,''),1,100), "
                    "substr(COALESCE(binds,''),1,60) FROM decision "
                    "WHERE scope_root=? AND status='standing' LIMIT 6", (gid,)):
                lines.append(f"  [decision:{r[0]}] [{r[1]}] {r[2]} (binds: {r[3]})")
            chunks.append("\n".join(lines))
    except Exception:
        pass
    return "\n".join(chunks)


def compose(store, agent_id, session, role="builder", task=""):
    """Compose the injection payload for one agent. Never raises."""
    try:
        return _compose(store, agent_id, session, role, task)
    except Exception:
        return ""


def _compose(store, agent_id, session, role, task):
    led = _delivered(store, agent_id)
    rows = store.sql("SELECT COALESCE(MAX(id),0) FROM events")
    wm_now = int(rows[0][0]) if rows else 0

    if "manual" not in led:
        # ---- first contact: full brief + task-aware inlining -------------
        text, _ = FB.full_brief(store)
        inline = _inline_build(store, task)
        if inline:
            text += inline
        refs = ["manual", "wm"]
        for r in store.sql("SELECT id FROM plan_node WHERE kind='goal'"):
            refs.append(f"build:{r[0]}")
        for r in store.sql("SELECT id FROM decision WHERE status='standing'"):
            refs.append(f"decision:{r[0]}")
        _mark(store, agent_id, refs, wm_now)
        return text[:BUDGET]

    # ---- returning agent: compose the delta ------------------------------
    since = led.get("wm", 0)
    parts, refs = [], ["wm"]
    dtext, _ = FB.delta_brief(store, since)
    if dtext:
        parts.append(dtext)
    # standing decisions this agent has never been told
    for r in store.sql(
            "SELECT id, subject, substr(COALESCE(choice,''),1,70), "
            "substr(COALESCE(binds,''),1,60) FROM decision "
            "WHERE status='standing' ORDER BY id"):
        ref = f"decision:{r[0]}"
        if ref not in led:
            parts.append(f"NEW DECISION d{r[0]} [{r[1]}] {r[2]} (binds: {r[3]})")
            refs.append(ref)
    # builds this agent has never been told
    for r in store.sql(
            "SELECT id, title, status FROM plan_node WHERE kind='goal'"):
        ref = f"build:{r[0]}"
        if ref not in led:
            parts.append(
                f"NEW BUILD build:{r[0]} \"{r[1]}\" [{r[2]}] "
                f"(join: SELECT * FROM plan_node WHERE root_id={r[0]})")
            refs.append(ref)
    # attention shifts only: ledger keys the last-delivered snapshot hash
    att = FB.attention(store, limit=3)
    if att:
        att_ref = "attention:" + hashlib.sha256(
            "\n".join(att).encode("utf-8", "replace")).hexdigest()[:16]
        if att_ref not in led:
            parts.append("FLEET ATTENTION (24h):\n" + "\n".join(att))
            refs.append(att_ref)
    _mark(store, agent_id, refs, wm_now)
    if not parts:
        return (f"(fleet unchanged for you since watermark {since} — "
                f"{time.strftime('%Y-%m-%d %H:%M:%S')})")
    text = (f"== FLEET UPDATE for {agent_id} (since wm {since}) ==\n"
            + "\n".join(parts))
    if len(text) > DELTA_BUDGET:
        try:
            from . import curator
            text = curator.curate(task or role, text, DELTA_BUDGET)
        except Exception:
            text = text[:DELTA_BUDGET]
    return text

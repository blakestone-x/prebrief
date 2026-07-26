"""prebrief.client — the thin, fast capture layer every hook calls.

All writes are events or idempotent upserts; all failures are swallowed to
stderr — a capture layer must NEVER break the agent it observes.
"""
import json
import sys
import time


def register(store, agent, session, role, task):
    """Announce an agent: upsert awareness, log session.start."""
    try:
        store.sql(
            "INSERT INTO awareness (agent_id, role, task_head, files_hot, "
            "status, updated_at) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(agent_id) DO UPDATE SET role=excluded.role, "
            "task_head=excluded.task_head, status='active', "
            "updated_at=excluded.updated_at",
            (agent, role, str(task or "")[:140], "[]", "active", time.time()))
        store.event("session.start", agent, session,
                    {"role": role, "task": str(task or "")[:200]})
    except Exception as e:
        print(f"prebrief register err: {e}", file=sys.stderr)


def heartbeat(store, agent, files, status="active"):
    """Refresh presence: which files this agent holds hot, and its status."""
    try:
        arr = json.dumps([str(f)[:300] for f in (files or [])[:8]])
        store.sql(
            "UPDATE awareness SET files_hot=?, status=?, updated_at=? "
            "WHERE agent_id=?",
            (arr, status, time.time(), agent))
    except Exception as e:
        print(f"prebrief heartbeat err: {e}", file=sys.stderr)


def tools(store, session, batch):
    """Record a batch of tool calls: [{tool, path, is_error}, ...]."""
    try:
        now = time.time()
        for t in (batch or [])[:40]:
            try:
                store.sql(
                    "INSERT INTO tool_events (session, tool, path, is_error, ts) "
                    "VALUES (?,?,?,?,?)",
                    (session,
                     str(t.get("tool", "?"))[:80],
                     (str(t.get("path"))[:300] if t.get("path") else None),
                     1 if t.get("is_error") else 0,
                     now))
            except Exception:
                pass
    except Exception as e:
        print(f"prebrief tools err: {e}", file=sys.stderr)


def traverse(store, agent, question, refs):
    """Record what an agent went looking for — feeds FLEET ATTENTION."""
    try:
        store.event("observation", agent, "traversal",
                    {"traversal": True, "q": str(question or "")[:200],
                     "refs": [str(r) for r in (refs or [])[:12]]})
    except Exception as e:
        print(f"prebrief traverse err: {e}", file=sys.stderr)


def end(store, agent, session):
    """Close out an agent: log session.end, mark awareness idle."""
    try:
        store.event("session.end", agent, session, {})
        store.sql(
            "UPDATE awareness SET status='idle', updated_at=? WHERE agent_id=?",
            (time.time(), agent))
    except Exception as e:
        print(f"prebrief end err: {e}", file=sys.stderr)

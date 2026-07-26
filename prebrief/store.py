"""prebrief.store — the local substrate.

A single SQLite database (WAL mode) holds everything Prebrief knows: the
append-only event log, plan/decision state, agent awareness, the per-agent
delivery ledger, and tool telemetry. Every write path fails open — an internal
error degrades to a no-op, never an exception to the caller.

DB path resolution: env PREBRIEF_DB, else ~/.prebrief/prebrief.db.
"""
import hashlib
import json
import os
import sqlite3
import sys
import time

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    hash    TEXT UNIQUE,
    ts      REAL,
    kind    TEXT,
    actor   TEXT,
    session TEXT,
    payload TEXT
);
CREATE TABLE IF NOT EXISTS plan_node (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    root_id INTEGER,
    kind    TEXT,
    title   TEXT,
    status  TEXT DEFAULT 'open',
    owner   TEXT
);
CREATE TABLE IF NOT EXISTS decision (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    scope_root INTEGER,
    subject    TEXT,
    choice     TEXT,
    rationale  TEXT,
    binds      TEXT,
    status     TEXT DEFAULT 'standing'
);
CREATE TABLE IF NOT EXISTS awareness (
    agent_id   TEXT PRIMARY KEY,
    role       TEXT,
    task_head  TEXT,
    files_hot  TEXT,
    status     TEXT DEFAULT 'active',
    updated_at REAL
);
CREATE TABLE IF NOT EXISTS delivery (
    agent_id     TEXT,
    item_ref     TEXT,
    watermark    INTEGER,
    delivered_at REAL,
    PRIMARY KEY (agent_id, item_ref)
);
CREATE TABLE IF NOT EXISTS tool_events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    session  TEXT,
    tool     TEXT,
    path     TEXT,
    is_error INTEGER DEFAULT 0,
    ts       REAL
);
CREATE INDEX IF NOT EXISTS idx_events_ts      ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_kind    ON events(kind);
CREATE INDEX IF NOT EXISTS idx_tool_events_ts ON tool_events(ts);
"""


def default_db_path():
    """Resolve the database path: env PREBRIEF_DB, else ~/.prebrief/prebrief.db."""
    p = os.environ.get("PREBRIEF_DB")
    if p:
        return p
    return os.path.join(os.path.expanduser("~"), ".prebrief", "prebrief.db")


class Store:
    """SQLite-backed substrate. All methods fail open."""

    def __init__(self, path=None):
        self.path = path or default_db_path()
        self._conn = None
        try:
            d = os.path.dirname(os.path.abspath(self.path))
            if d:
                os.makedirs(d, exist_ok=True)
            self._conn = sqlite3.connect(
                self.path, timeout=10, isolation_level=None,
                check_same_thread=False)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(_SCHEMA)
        except Exception as e:  # fail open: a broken store yields empty reads
            print(f"prebrief store init err: {e}", file=sys.stderr)
            self._conn = None

    def sql(self, query, params=()):
        """Run a statement; return rows as list[tuple]. [] on any error."""
        if self._conn is None:
            return []
        try:
            cur = self._conn.execute(query, params)
            rows = cur.fetchall()
            return [tuple(r) for r in rows]
        except Exception as e:
            print(f"prebrief sql err: {e}", file=sys.stderr)
            return []

    def event(self, kind, actor, session, payload):
        """Append a content-hash-deduped event. Returns the event id (existing
        id on a dedupe hit, 0 on failure)."""
        try:
            body = json.dumps(payload, default=str, sort_keys=True)[:4000]
        except Exception:
            body = "{}"
        h = hashlib.sha256(
            f"{kind}|{actor}|{session}|{body}".encode("utf-8", "replace")
        ).hexdigest()
        if self._conn is None:
            return 0
        try:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO events (hash, ts, kind, actor, session, payload) "
                "VALUES (?,?,?,?,?,?)",
                (h, time.time(), kind, actor, session, body))
            if cur.rowcount:
                return int(cur.lastrowid)
            row = self._conn.execute(
                "SELECT id FROM events WHERE hash=?", (h,)).fetchone()
            return int(row[0]) if row else 0
        except Exception as e:
            print(f"prebrief event err: {e}", file=sys.stderr)
            return 0

    def close(self):
        """Close the underlying connection (safe to call twice)."""
        try:
            if self._conn is not None:
                self._conn.close()
        except Exception:
            pass
        self._conn = None

"""prebrief.store — the local substrate.

A single SQLite database (WAL mode) holds everything Prebrief knows: the
append-only event log, plan/decision state, agent awareness, the per-agent
delivery ledger, and tool telemetry. The capture paths fail open — an internal
error degrades to a no-op, never an exception to the caller.

Fail-open is right for capture and for reads: a broken store must never break
the agent being observed. It is WRONG for a derivation that also advances a
durable cursor, because a swallowed write there loses the event forever. So
there are two write contracts, deliberately:

  * `sql()`        — fail-open. Reads, capture, best-effort upserts.
  * `execute_strict()` / `transaction()` — RAISE. Used by the projector, whose
    caller must be able to roll back and leave the cursor where it was.

R2 (tenant isolation): every row carries the `project` that wrote it. Reads are
scoped to the caller's project; a row only crosses a project boundary when it is
explicitly marked `shared=1`, and cross-project rows render with their origin.

DB path resolution: env PREBRIEF_DB, else ~/.prebrief/prebrief.db.
"""
import contextlib
import hashlib
import json
import os
import sqlite3
import sys
import time

DEFAULT_PROJECT = "default"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    hash    TEXT UNIQUE,
    ts      REAL,
    kind    TEXT,
    actor   TEXT,
    session TEXT,
    payload TEXT,
    project TEXT DEFAULT 'default'
);
CREATE TABLE IF NOT EXISTS plan_node (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    root_id INTEGER,
    kind    TEXT,
    title   TEXT,
    status  TEXT DEFAULT 'open',
    owner   TEXT,
    project TEXT DEFAULT 'default',
    origin  TEXT,
    shared  INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS decision (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    scope_root INTEGER,
    subject    TEXT,
    choice     TEXT,
    rationale  TEXT,
    binds      TEXT,
    status     TEXT DEFAULT 'standing',
    project    TEXT DEFAULT 'default',
    origin     TEXT,
    shared     INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS awareness (
    agent_id   TEXT PRIMARY KEY,
    role       TEXT,
    task_head  TEXT,
    files_hot  TEXT,
    status     TEXT DEFAULT 'active',
    updated_at REAL,
    project    TEXT DEFAULT 'default'
);
CREATE TABLE IF NOT EXISTS delivery (
    agent_id     TEXT,
    item_ref     TEXT,
    watermark    INTEGER,
    delivered_at REAL,
    used_at      REAL,
    state        TEXT DEFAULT 'emitted',
    PRIMARY KEY (agent_id, item_ref)
);
CREATE TABLE IF NOT EXISTS tool_events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    session  TEXT,
    tool     TEXT,
    path     TEXT,
    is_error INTEGER DEFAULT 0,
    ts       REAL,
    project  TEXT DEFAULT 'default'
);
"""

# Indexes run AFTER the migration pass: on a legacy DB the project columns do
# not exist yet, and a failed CREATE INDEX would abort the whole script.
_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_events_ts        ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_kind      ON events(kind);
CREATE INDEX IF NOT EXISTS idx_events_project   ON events(project);
CREATE INDEX IF NOT EXISTS idx_tool_events_ts   ON tool_events(ts);
CREATE INDEX IF NOT EXISTS idx_plan_project     ON plan_node(project);
CREATE INDEX IF NOT EXISTS idx_decision_project ON decision(project);
"""

# (table, column, declaration) — applied one at a time, each in its own
# try/except so a re-run (column already present) is a silent no-op.
_MIGRATIONS = (
    ("delivery",    "used_at", "REAL"),
    # delivery lifecycle: 'emitted' (the ref rendered into a returned payload)
    # -> 'used' (the agent then acted on it). A single optimistic timestamp
    # could not tell "we sent it" from "we meant to send it".
    ("delivery",    "state",   "TEXT DEFAULT 'emitted'"),
    ("events",      "project", "TEXT DEFAULT 'default'"),
    ("plan_node",   "project", "TEXT DEFAULT 'default'"),
    ("plan_node",   "origin",  "TEXT"),
    ("plan_node",   "shared",  "INTEGER DEFAULT 0"),
    ("decision",    "project", "TEXT DEFAULT 'default'"),
    ("decision",    "origin",  "TEXT"),
    ("decision",    "shared",  "INTEGER DEFAULT 0"),
    ("awareness",   "project", "TEXT DEFAULT 'default'"),
    ("tool_events", "project", "TEXT DEFAULT 'default'"),
)


def declared_columns(ddl=None):
    """Every (table, column) the schema declares — parsed from _SCHEMA.

    The migration list is hand-maintained, so a column added to _SCHEMA can
    silently never reach EXISTING databases. That is exactly how
    delivery.used_at shipped broken in v0.2.1: new installs had it, every
    upgrade did not. Deriving the expected set from the schema lets a test
    fail the moment the two drift again — fixing the class, not the instance.
    """
    import re as _re
    text = _SCHEMA if ddl is None else ddl
    out = []
    pattern = r"CREATE TABLE IF NOT EXISTS (\w+)\s*\(([^;]*?)\n\s*\)"
    for tbl, body in _re.findall(pattern, text, _re.S):
        for line in body.splitlines():
            line = line.strip().rstrip(",")
            if not line or line.upper().startswith(("PRIMARY KEY", "UNIQUE", "FOREIGN",
                                                    "CHECK", "CONSTRAINT", "--")):
                continue
            parts = line.split()
            if len(parts) >= 2 and parts[0].isidentifier():
                out.append((tbl, parts[0]))
    return out


def migration_gaps():
    """Schema columns that no migration adds to a legacy database.

    Columns present in the ORIGINAL v0.1.0 shipped schema are exempt (a legacy
    DB already has them); everything added since must be migrated.
    """
    v010 = {
        "events": ("id", "hash", "ts", "kind", "actor", "session", "payload"),
        "plan_node": ("id", "root_id", "kind", "title", "status", "owner"),
        "decision": ("id", "scope_root", "subject", "choice", "rationale",
                     "binds", "status"),
        "awareness": ("agent_id", "role", "task_head", "files_hot", "status",
                      "updated_at"),
        "delivery": ("agent_id", "item_ref", "watermark", "delivered_at"),
        "tool_events": ("id", "session", "tool", "path", "is_error", "ts"),
        # tables the projector owns and creates itself
        "projector_state": ("id", "last_event_id", "updated_at"),
        "claims": ("id", "subject", "predicate", "body", "confidence", "status",
                   "source", "project", "asserted_by"),
    }
    original = {(t, c) for t, cols in v010.items() for c in cols}
    covered = {(t, c) for t, c, _ in _MIGRATIONS}
    return sorted(set(declared_columns()) - covered - original)


def norm_project(project):
    """Normalize a project label. Anything unusable becomes 'default'."""
    try:
        p = str(project or "").strip()
    except Exception:
        return DEFAULT_PROJECT
    return p[:64] if p else DEFAULT_PROJECT


def scope_clause(project, prefix="", shared=True):
    """(where_fragment, params) restricting a table to one project.

    project=None means "no isolation" (operator-level reads only). When
    `shared` is True, rows explicitly marked shared=1 also match — that is the
    ONLY way a row crosses a project boundary. COALESCE keeps legacy rows
    (NULL project, written before the migration) inside 'default'.
    """
    if project is None:
        return "1=1", ()
    p = f"{prefix}." if prefix else ""
    if shared:
        return (f"(COALESCE({p}project,'default')=? OR COALESCE({p}shared,0)=1)",
                (norm_project(project),))
    return f"COALESCE({p}project,'default')=?", (norm_project(project),)


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
            self._migrate()
            self._conn.executescript(_INDEXES)
        except Exception as e:  # fail open: a broken store yields empty reads
            print(f"prebrief store init err: {e}", file=sys.stderr)
            self._conn = None

    def _migrate(self):
        """Idempotently bring a pre-R2 database up to the isolation schema.

        Each ALTER runs in its own try/except: 'duplicate column name' on an
        already-migrated DB is the expected no-op. SQLite backfills existing
        rows with the column default, so legacy rows land in 'default'.
        """
        if self._conn is None:
            return
        for table, col, decl in _MIGRATIONS:
            try:
                self._conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
            except Exception:
                pass  # column already present, or table absent — both benign

    def sql(self, query, params=()):
        """Run a statement; return rows as list[tuple]. [] on any error.

        FAIL-OPEN, by contract. Every read path and every capture path depends
        on that. Do not use it for a write whose loss would go unnoticed —
        see execute_strict().
        """
        if self._conn is None:
            return []
        try:
            cur = self._conn.execute(query, params)
            rows = cur.fetchall()
            return [tuple(r) for r in rows]
        except Exception as e:
            print(f"prebrief sql err: {e}", file=sys.stderr)
            return []

    def execute_strict(self, query, params=()):
        """Run a statement and RAISE on failure. Returns rows as list[tuple].

        The counterpart to sql(): identical behaviour on success, but a dead
        store or a bad statement propagates instead of degrading to []. Write
        paths that must be able to roll back use this — a swallowed write plus
        an advanced cursor is silent data loss, not graceful degradation.

        Raises RuntimeError when the store never opened, otherwise whatever
        sqlite3 raised.
        """
        if self._conn is None:
            raise RuntimeError("prebrief store unavailable")
        cur = self._conn.execute(query, params)
        return [tuple(r) for r in cur.fetchall()]

    @contextlib.contextmanager
    def transaction(self):
        """BEGIN IMMEDIATE ... COMMIT around a unit of write work; ROLLBACK on
        any exception, which is then re-raised.

        The connection runs in autocommit (isolation_level=None) so that the
        capture paths never hold a write lock; the BEGIN here is therefore
        explicit. Yields the store, so the body can call execute_strict().
        Not reentrant — sqlite has no nested transactions.
        """
        if self._conn is None:
            raise RuntimeError("prebrief store unavailable")
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield self
        except BaseException:
            try:
                self._conn.execute("ROLLBACK")
            except Exception as e:
                print(f"prebrief rollback err: {e}", file=sys.stderr)
            raise
        self._conn.execute("COMMIT")

    def project_of(self, agent_id):
        """The project an agent is enrolled in. 'default' when unknown."""
        try:
            rows = self.sql(
                "SELECT COALESCE(project,'default') FROM awareness "
                "WHERE agent_id=?", (agent_id,))
            if rows and rows[0][0]:
                return norm_project(rows[0][0])
        except Exception:
            pass
        return DEFAULT_PROJECT

    def event(self, kind, actor, session, payload, project=DEFAULT_PROJECT):
        """Append a content-hash-deduped event. Returns the event id (existing
        id on a dedupe hit, 0 on failure). The project is part of the dedupe
        identity: the same text from two projects stays two rows."""
        proj = norm_project(project)
        try:
            body = json.dumps(payload, default=str, sort_keys=True)[:4000]
        except Exception:
            body = "{}"
        h = hashlib.sha256(
            f"{proj}|{kind}|{actor}|{session}|{body}".encode("utf-8", "replace")
        ).hexdigest()
        if self._conn is None:
            return 0
        try:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO events "
                "(hash, ts, kind, actor, session, payload, project) "
                "VALUES (?,?,?,?,?,?,?)",
                (h, time.time(), kind, actor, session, body, proj))
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

"""prebrief.projector — the fold that makes the architecture claim true.

Prebrief's design says: *the append-only event log is the sole source of truth;
everything else is a derived projection that can be thrown away and rebuilt.*
This module is what makes that a fact rather than a slogan.

    events (append-only, content-hash deduped)
        |
        |  project_events()   fold forward from a durable cursor
        v
    plan_node / decision / claim / awareness   (derived, disposable)

`rebuild()` is the proof: it wipes every projection table, resets the cursor to
zero, and replays the entire log. If the architecture claim holds, the rebuilt
projections are identical to the incrementally-folded ones. tests/test_projector
asserts exactly that.

Determinism (what makes rebuild == incremental):
  * Projection primary keys are DERIVED from the event id, never from an
    AUTOINCREMENT counter, so replay reproduces the same ids.
  * Timestamps written into projections come from the EVENT's `ts`, never from
    `time.time()`, so replay reproduces the same values.
  * Every write is an upsert or INSERT OR IGNORE keyed on something stable, so
    folding an event twice is a no-op (crash-resume safety without needing a
    transaction around the whole fold).

Event kinds folded:
  plan.open        -> plan_node row (id = event id unless payload.node_id)
  plan.claim       -> plan_node.status='claimed' (+ owner)
  plan.done        -> plan_node.status='done'
  plan.block       -> plan_node.status='blocked'
  decision.make    -> decision row (id = event id unless payload.decision_id)
  decision.supersede -> decision.status='superseded'
  claim.assert     -> claim row (natural key: asserted_by = event id)
  session.start    -> awareness upsert (status='active')
  session.end      -> awareness upsert (status='idle')
Anything else (e.g. 'observation') is counted as skipped and ignored — an
unknown kind must never stall or crash the fold.

Tenancy (R2): a transition event may only move a row its OWN project owns.
Every mutation is keyed on (id, project), never on id alone, and a target owned
by another project is refused and tallied as `cross_tenant` — otherwise a
project-Bravo `plan.done` naming id 7 would close project-Alpha's node 7, which
is a cross-tenant write dressed up as a fold.

Atomicity: each event's projection writes AND the cursor advance happen inside
ONE transaction, through the store's STRICT writer. If a write fails, the
transaction rolls back and the cursor stays put, so the event is retried on the
next run. The alternative — a swallowed write plus an advanced cursor — drops
the event from the derived state permanently, with the log still claiming it was
folded. Only an explicitly malformed or unknown event is skipped FORWARD
(tallied as `dead_letter`), because retrying it would wedge the fold forever.

NOT projections, and therefore NOT wiped by rebuild(): `events` (the log),
`tool_events` (raw capture), `delivery` (a per-recipient ledger of what was
already shown, which is history, not derived state). Note also that state with
no event behind it is not reconstructible by definition — `awareness.files_hot`
is written by client.heartbeat and resets on rebuild.

Everything here fails open TO THE CALLER: a broken store or a malformed payload
degrades to a smaller count and a stderr note, never an exception. What it does
not do is fail open to the DATABASE — an infrastructure failure stops the fold
with the cursor intact rather than pretending the work happened.
"""
import contextlib
import json
import sqlite3
import sys
import time

# Projection tables, in the order rebuild() wipes them.
PROJECTION_TABLES = ("plan_node", "decision", "awareness")

# Batch size for the forward scan, so a very long log never lands in memory
# all at once.
_SCAN_LIMIT = 1000

_STATE_DDL = """
CREATE TABLE IF NOT EXISTS projector_state (
    id            INTEGER PRIMARY KEY CHECK (id = 1),
    last_event_id INTEGER NOT NULL DEFAULT 0,
    updated_at    REAL
)"""

_CLAIM_DDL = """
CREATE TABLE IF NOT EXISTS {t} (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    subject     TEXT,
    predicate   TEXT,
    body        TEXT,
    confidence  REAL,
    status      TEXT DEFAULT 'asserted',
    source      TEXT,
    project     TEXT DEFAULT 'default',
    asserted_by INTEGER UNIQUE
)"""

_PLAN_STATUS = {
    "plan.claim": "claimed",
    "plan.done": "done",
    "plan.block": "blocked",
}

# The line between "this event is bad" and "the database is bad". A bad event
# is skipped forward (retrying it would wedge the fold forever); a bad database
# rolls the event back and leaves the cursor alone, so the next run retries it.
_INFRA_ERRORS = (sqlite3.Error, MemoryError, OSError, RuntimeError)


# ------------------------------------------------------------------ plumbing

class _Ctx:
    """Per-run fold context: the store, a column cache, and the tallies.

    The column cache exists because this package's schema is migrated in place
    (project/origin/shared columns arrived after the first release). Rather than
    hard-coding one schema version, every write is filtered to the columns that
    actually exist, so the projector works against an old or new database.
    """

    def __init__(self, store):
        self.store = store
        self._cols = {}
        self.claims = None
        self.counts = {"events": 0, "plan_node": 0, "decision": 0,
                       "claim": 0, "awareness": 0, "skipped": 0,
                       # observability for the two failure modes that used to
                       # be invisible: refused cross-tenant writes, events the
                       # fold gave up on, and infrastructure halts.
                       "cross_tenant": 0, "dead_letter": 0, "errors": 0}

    def write(self, query, params=()):
        """Issue a projection write that must NOT fail silently.

        Routed through the store's strict writer so a failure propagates to
        project_events(), which rolls the event's transaction back and leaves
        the cursor where it was. Falls back to the fail-open sql() only for a
        legacy store object that predates execute_strict().
        """
        fn = getattr(self.store, "execute_strict", None)
        if fn is None:
            return self.store.sql(query, params)
        return fn(query, params)

    def cols(self, table):
        if table not in self._cols:
            got = set()
            try:
                for r in self.store.sql(f"PRAGMA table_info({table})"):
                    got.add(str(r[1]))
            except Exception:
                got = set()
            self._cols[table] = got
        return self._cols[table]


def _ensure_state(store):
    """Create the fold cursor table if absent. Idempotent."""
    try:
        store.sql(_STATE_DDL)
        return True
    except Exception as e:
        print(f"prebrief projector state err: {e}", file=sys.stderr)
        return False


def _claims_table(ctx):
    """Name of the claims table, creating it when neither variant exists.

    Prefers an existing 'claims' table (someone else's), then 'claim'; creates
    'claim' when there is none.
    """
    if ctx.claims:
        return ctx.claims
    names = set()
    try:
        for r in ctx.store.sql(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name IN ('claims','claim')"):
            names.add(str(r[0]))
    except Exception:
        pass
    if "claims" in names:
        ctx.claims = "claims"
    elif "claim" in names:
        ctx.claims = "claim"
    else:
        try:
            ctx.store.sql(_CLAIM_DDL.format(t="claim"))
        except Exception as e:
            print(f"prebrief projector claim ddl err: {e}", file=sys.stderr)
        ctx.claims = "claim"
    return ctx.claims


def last_event_id(store):
    """The id of the last event folded into projections. 0 when never run."""
    try:
        _ensure_state(store)
        rows = store.sql("SELECT last_event_id FROM projector_state WHERE id=1")
        return int(rows[0][0]) if rows and rows[0][0] is not None else 0
    except Exception:
        return 0


_CURSOR_SQL = (
    "INSERT INTO projector_state (id, last_event_id, updated_at) "
    "VALUES (1,?,?) ON CONFLICT(id) DO UPDATE SET "
    "last_event_id=excluded.last_event_id, "
    "updated_at=excluded.updated_at")


def _set_cursor(store, eid):
    """Fail-open cursor write, for callers outside a transaction (rebuild)."""
    try:
        store.sql(_CURSOR_SQL, (int(eid), time.time()))
    except Exception as e:
        print(f"prebrief projector cursor err: {e}", file=sys.stderr)


def _txn(store):
    """The store's transaction, or a no-op for a store that lacks one.

    A legacy store object without transaction() keeps the old (unsafe) shape
    rather than crashing; every store this package ships has one.
    """
    fn = getattr(store, "transaction", None)
    return fn() if fn is not None else contextlib.nullcontext(store)


# ------------------------------------------------------------------- tenancy

def _scoped(ctx, table, rid, project):
    """(where_fragment, params) pinning a mutation to ONE row of ONE project.

    Never `WHERE id=?`. The project predicate is what stops project Bravo's
    `plan.done` from closing project Alpha's node with the same id. On a
    pre-R2 table (no project column) there is no tenancy to enforce and the
    clause degrades to the id alone.
    """
    if "project" not in ctx.cols(table):
        return "id=?", (rid,)
    return "id=? AND COALESCE(project,'default')=?", (rid, project)


def _tenant_ok(ctx, table, rid, project):
    """May an event from `project` mutate row `rid` of `table`?

    True when the row is absent (the scoped UPDATE is then a harmless no-op)
    or owned by the same project. False when another project owns it — that is
    a cross-tenant mutation attempt: it is refused, tallied, and noted on
    stderr so it is observable rather than silent.

    This lookup only CLASSIFIES; the guarantee comes from _scoped(), which
    keeps the project predicate in the statement itself. So a fail-open read
    here cannot open a hole.
    """
    if "project" not in ctx.cols(table):
        return True
    rows = ctx.store.sql(
        f"SELECT COALESCE(project,'default') FROM {table} WHERE id=?", (rid,))
    if not rows:
        return True
    owner = str(rows[0][0])
    if owner == project:
        return True
    ctx.counts["cross_tenant"] += 1
    print(f"prebrief projector refused cross-tenant write: {table} id={rid} "
          f"owned by {owner!r}, event from {project!r}", file=sys.stderr)
    return False


def _upsert(ctx, table, row, key, update):
    """INSERT ... ON CONFLICT(key) DO UPDATE, filtered to existing columns.

    `row` is col->value; `update` names the columns overwritten on conflict.
    Returns True when a statement was issued.
    """
    present = ctx.cols(table)
    if not present or key not in present:
        return False
    items = [(c, v) for c, v in row.items() if c in present]
    if not items:
        return False
    cols = ",".join(c for c, _ in items)
    marks = ",".join("?" for _ in items)
    sets = ",".join(f"{c}=excluded.{c}" for c in update if c in present)
    sql = f"INSERT INTO {table} ({cols}) VALUES ({marks}) ON CONFLICT({key}) DO "
    sql += f"UPDATE SET {sets}" if sets else "NOTHING"
    ctx.write(sql, tuple(v for _, v in items))
    return True


def _insert_ignore(ctx, table, row):
    """INSERT OR IGNORE, filtered to existing columns."""
    present = ctx.cols(table)
    if not present:
        return False
    items = [(c, v) for c, v in row.items() if c in present]
    if not items:
        return False
    cols = ",".join(c for c, _ in items)
    marks = ",".join("?" for _ in items)
    ctx.write(f"INSERT OR IGNORE INTO {table} ({cols}) VALUES ({marks})",
              tuple(v for _, v in items))
    return True


def _payload(raw):
    """Decode an event payload to a dict. {} on anything unusable."""
    try:
        p = json.loads(raw or "{}")
        return p if isinstance(p, dict) else {}
    except Exception:
        return {}


def _int_or(value, fallback=None):
    try:
        if value is None or value == "":
            return fallback
        return int(value)
    except Exception:
        return fallback


def _txt(value, n=300):
    try:
        return str(value)[:n] if value is not None else None
    except Exception:
        return None


# ----------------------------------------------------------------- handlers

def _fold_plan_open(ctx, eid, ts, kind, actor, payload, project):
    nid = _int_or(payload.get("node_id"), eid)
    # An explicit node_id can collide with an existing node. ON CONFLICT DO
    # UPDATE would then rewrite another project's row, so the same tenancy
    # check that guards the transitions guards the open.
    if not _tenant_ok(ctx, "plan_node", nid, project):
        return
    _upsert(ctx, "plan_node", {
        "id": nid,
        "root_id": _int_or(payload.get("root_id"), None),
        "kind": _txt(payload.get("kind") or "goal", 40),
        "title": _txt(payload.get("title") or "", 300),
        "status": _txt(payload.get("status") or "open", 40),
        "owner": _txt(payload.get("owner") or actor, 80),
        "project": project,
        "origin": _txt(payload.get("origin") or project, 80),
        "shared": 1 if payload.get("shared") else 0,
    }, key="id",
        update=("root_id", "kind", "title", "status", "owner", "project",
                "origin", "shared"))
    ctx.counts["plan_node"] += 1


def _fold_plan_status(ctx, eid, ts, kind, actor, payload, project):
    nid = _int_or(payload.get("node") or payload.get("node_id"), None)
    if nid is None:
        ctx.counts["skipped"] += 1
        return
    if not _tenant_ok(ctx, "plan_node", nid, project):
        return
    status = _PLAN_STATUS.get(kind, "open")
    owner = payload.get("owner") or (actor if kind == "plan.claim" else None)
    where, params = _scoped(ctx, "plan_node", nid, project)
    if owner and "owner" in ctx.cols("plan_node"):
        ctx.write(f"UPDATE plan_node SET status=?, owner=? WHERE {where}",
                  (status, _txt(owner, 80)) + params)
    else:
        ctx.write(f"UPDATE plan_node SET status=? WHERE {where}",
                  (status,) + params)
    ctx.counts["plan_node"] += 1


def _fold_decision_make(ctx, eid, ts, kind, actor, payload, project):
    did = _int_or(payload.get("decision_id"), eid)
    if not _tenant_ok(ctx, "decision", did, project):
        return
    _upsert(ctx, "decision", {
        "id": did,
        "scope_root": _int_or(payload.get("scope_root"), None),
        "subject": _txt(payload.get("subject") or "", 200),
        "choice": _txt(payload.get("choice") or "", 500),
        "rationale": _txt(payload.get("rationale") or "", 800),
        "binds": _txt(payload.get("binds") or "", 300),
        "status": _txt(payload.get("status") or "standing", 40),
        "project": project,
        "origin": _txt(payload.get("origin") or project, 80),
        "shared": 1 if payload.get("shared") else 0,
    }, key="id",
        update=("scope_root", "subject", "choice", "rationale", "binds",
                "status", "project", "origin", "shared"))
    ctx.counts["decision"] += 1


def _fold_decision_supersede(ctx, eid, ts, kind, actor, payload, project):
    did = _int_or(payload.get("decision")
                  or payload.get("decision_id")
                  or payload.get("target"), None)
    if did is None:
        ctx.counts["skipped"] += 1
        return
    if not _tenant_ok(ctx, "decision", did, project):
        return
    where, params = _scoped(ctx, "decision", did, project)
    ctx.write(f"UPDATE decision SET status='superseded' WHERE {where}", params)
    ctx.counts["decision"] += 1


def _fold_claim_assert(ctx, eid, ts, kind, actor, payload, project):
    table = _claims_table(ctx)
    conf = payload.get("confidence")
    try:
        conf = float(conf) if conf is not None else None
    except Exception:
        conf = None
    _insert_ignore(ctx, table, {
        "subject": _txt(payload.get("subject") or "", 200),
        "predicate": _txt(payload.get("predicate") or "", 120),
        "body": _txt(payload.get("body") or "", 1000),
        "confidence": conf,
        "status": _txt(payload.get("status") or "asserted", 40),
        "source": _txt(payload.get("source") or actor, 200),
        "project": project,
        "asserted_by": eid,
    })
    ctx.counts["claim"] += 1


def _fold_session(ctx, eid, ts, kind, actor, payload, project):
    active = kind == "session.start"
    row = {
        "agent_id": actor,
        "role": _txt(payload.get("role") or ("builder" if active else None), 60),
        "task_head": _txt(payload.get("task") or "", 140) if active else None,
        "files_hot": "[]",
        "status": "active" if active else "idle",
        "updated_at": ts,
        "project": project,
    }
    # Deliberately NOT tenancy-guarded: awareness is keyed on agent_id, and an
    # agent re-registering under a new project is the supported way to move
    # (see client.heartbeat). Pinning the row to its first project would freeze
    # the agent there forever. The row's `project` is rewritten to the event's,
    # so presence follows the agent rather than leaking across the boundary.
    # session.end must not blank out role/task learned from session.start.
    update = ["status", "updated_at", "project"]
    if active:
        update = ["role", "task_head", "status", "updated_at", "project"]
    _upsert(ctx, "awareness", row, key="agent_id", update=tuple(update))
    ctx.counts["awareness"] += 1


_HANDLERS = {
    "plan.open": _fold_plan_open,
    "plan.claim": _fold_plan_status,
    "plan.done": _fold_plan_status,
    "plan.block": _fold_plan_status,
    "decision.make": _fold_decision_make,
    "decision.supersede": _fold_decision_supersede,
    "claim.assert": _fold_claim_assert,
    "session.start": _fold_session,
    "session.end": _fold_session,
}


# --------------------------------------------------------------- public API

def project_events(store, upto=None):
    """Fold every unprojected event into the projection tables.

    Reads `events` with id > the stored cursor (and <= `upto` when given) in id
    order, applies each to the projections, then advances the cursor. Folding
    the same event twice is a no-op, so a crash mid-fold is repaired simply by
    running again.

    Each event is folded inside its own transaction that also carries the
    cursor advance, so the two can never disagree: either the projection rows
    and the new cursor both land, or neither does and the event is retried on
    the next run. An infrastructure failure therefore STOPS the fold (counted
    under 'errors') instead of skipping the event and reporting success.

    Returns a counts dict:
      {events, plan_node, decision, claim, awareness, skipped, cross_tenant,
       dead_letter, errors, last_event_id}
    where `cross_tenant` counts refused cross-project mutations, `dead_letter`
    counts events permanently given up on, and `errors` counts halts.
    Fails open to the CALLER — on an unusable store the counts are zeros plus
    an 'error' key.
    """
    ctx = _Ctx(store)
    try:
        _ensure_state(store)
        cursor = last_event_id(store)
        stalled = False
        upto = _int_or(upto, None)
        has_project = "project" in ctx.cols("events")
        proj_col = "COALESCE(project,'default')" if has_project else "'default'"
        while True:
            if upto is not None:
                rows = store.sql(
                    f"SELECT id, ts, kind, actor, payload, {proj_col} "
                    "FROM events WHERE id > ? AND id <= ? ORDER BY id LIMIT ?",
                    (cursor, upto, _SCAN_LIMIT))
            else:
                rows = store.sql(
                    f"SELECT id, ts, kind, actor, payload, {proj_col} "
                    "FROM events WHERE id > ? ORDER BY id LIMIT ?",
                    (cursor, _SCAN_LIMIT))
            if not rows:
                break
            for eid, ts, kind, actor, payload, project in rows:
                eid = _int_or(eid, 0)
                fn = _HANDLERS.get(str(kind or ""))
                # Tallies made inside a transaction that later rolls back are
                # not true, so keep a copy to restore.
                before = dict(ctx.counts)
                try:
                    with _txn(store):
                        if fn is None:
                            # Unknown kind: not an error. The log is allowed to
                            # carry kinds this projector does not model.
                            ctx.counts["skipped"] += 1
                        else:
                            try:
                                fn(ctx, eid, ts, str(kind), str(actor or ""),
                                   _payload(payload),
                                   str(project or "default"))
                            except _INFRA_ERRORS:
                                # The store, not the event. Roll back and retry
                                # this event on the next run.
                                raise
                            except Exception as e:
                                # A poisoned event: retrying it forever would
                                # wedge the fold, so it is skipped FORWARD.
                                ctx.counts["skipped"] += 1
                                ctx.counts["dead_letter"] += 1
                                print(f"prebrief projector event {eid} "
                                      f"dead-lettered: {e}", file=sys.stderr)
                        # Same transaction as the writes above: the cursor can
                        # never claim an event that did not land.
                        ctx.write(_CURSOR_SQL, (int(eid), time.time()))
                except Exception as e:
                    ctx.counts = before
                    ctx.counts["errors"] += 1
                    ctx.counts["error"] = str(e)
                    print(f"prebrief projector halted at event {eid}: {e} "
                          f"(cursor stays at {cursor}; will retry)",
                          file=sys.stderr)
                    stalled = True
                    break
                ctx.counts["events"] += 1
                cursor = max(cursor, eid)
            if stalled or len(rows) < _SCAN_LIMIT:
                break
        if not stalled:
            _set_cursor(store, cursor)
        ctx.counts["last_event_id"] = cursor
        return ctx.counts
    except Exception as e:
        print(f"prebrief projector err: {e}", file=sys.stderr)
        ctx.counts["last_event_id"] = last_event_id(store)
        ctx.counts["error"] = str(e)
        return ctx.counts


def rebuild(store):
    """Throw the projections away and rebuild them from the log alone.

    This is the falsifiable form of the architecture claim. It wipes plan_node,
    decision, the claims table and awareness (including their AUTOINCREMENT
    counters, so replayed ids match), resets the cursor to 0, and replays every
    event. The log, tool telemetry and the delivery ledger are untouched.

    Anything written directly to a projection table rather than emitted as an
    event does not survive — that is the point, not a bug. Returns the same
    counts dict as project_events(); fails open.
    """
    ctx = _Ctx(store)
    try:
        _ensure_state(store)
        tables = list(PROJECTION_TABLES) + [_claims_table(ctx)]
        for t in tables:
            store.sql(f"DELETE FROM {t}")
        # DELETE leaves sqlite_sequence intact, which would restart AUTOINCREMENT
        # ids above the old high-water mark and make the rebuild differ from the
        # incremental fold. Reset it so replay is byte-for-byte reproducible.
        try:
            marks = ",".join("?" for _ in tables)
            store.sql(f"DELETE FROM sqlite_sequence WHERE name IN ({marks})",
                      tuple(tables))
        except Exception:
            pass  # no AUTOINCREMENT table in this DB yet — nothing to reset
        _set_cursor(store, 0)
        return project_events(store)
    except Exception as e:
        print(f"prebrief projector rebuild err: {e}", file=sys.stderr)
        return {"events": 0, "plan_node": 0, "decision": 0, "claim": 0,
                "awareness": 0, "skipped": 0, "cross_tenant": 0,
                "dead_letter": 0, "errors": 1, "last_event_id": 0,
                "error": str(e)}


def snapshot(store):
    """Every projection table as {table: sorted list of row tuples}.

    The comparison primitive behind the rebuild proof: fold incrementally, take
    a snapshot, rebuild, take another — they must be equal.
    """
    ctx = _Ctx(store)
    out = {}
    for t in list(PROJECTION_TABLES) + [_claims_table(ctx)]:
        try:
            cols = sorted(ctx.cols(t))
            if not cols:
                out[t] = []
                continue
            rows = store.sql(f"SELECT {','.join(cols)} FROM {t}")
            out[t] = sorted(rows, key=repr)
        except Exception:
            out[t] = []
    return out

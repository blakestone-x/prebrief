"""prebrief.client — the thin, fast capture layer every hook calls.

All writes are events or idempotent upserts; all failures are swallowed to
stderr — a capture layer must NEVER break the agent it observes.

R2: every writer stamps the project it wrote from. `project` is always a
trailing keyword argument, so pre-R2 positional calls keep working unchanged.
"""
import json
import sys
import time

from .store import DEFAULT_PROJECT, norm_project


def register(store, agent, session, role, task, project=DEFAULT_PROJECT):
    """Announce an agent: upsert awareness, log session.start.

    The `session.start` event it emits IS the log-first path — the projector
    folds it back into the same awareness row. The direct upsert here is a
    DEPRECATED write-through kept so a hook has presence immediately, before
    any fold runs; drop it once `prebrief project` runs on a timer. It is
    harmless either way: the fold overwrites the row with values derived from
    the event, so a rebuild reproduces it.
    """
    proj = norm_project(project)
    try:
        store.sql(
            "INSERT INTO awareness (agent_id, role, task_head, files_hot, "
            "status, updated_at, project) VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(agent_id) DO UPDATE SET role=excluded.role, "
            "task_head=excluded.task_head, status='active', "
            "updated_at=excluded.updated_at, project=excluded.project",
            (agent, role, str(task or "")[:140], "[]", "active", time.time(),
             proj))
        store.event("session.start", agent, session,
                    {"role": role, "task": str(task or "")[:200]},
                    project=proj)
    except Exception as e:
        print(f"prebrief register err: {e}", file=sys.stderr)


def heartbeat(store, agent, files, status="active", project=None):
    """Refresh presence: which files this agent holds hot, and its status.

    DEPRECATED as a projection write, and the one piece of state with no event
    behind it: `files_hot` is high-frequency presence noise that is deliberately
    NOT logged, so `projector.rebuild()` resets it to '[]'. Treat it as a live
    cache, never as durable state.

    `project` is optional — presence rows are stamped at register time; pass it
    to re-stamp an agent that moved (or one whose row predates R2).
    """
    try:
        arr = json.dumps([str(f)[:300] for f in (files or [])[:8]])
        if project is None:
            store.sql(
                "UPDATE awareness SET files_hot=?, status=?, updated_at=? "
                "WHERE agent_id=?",
                (arr, status, time.time(), agent))
        else:
            store.sql(
                "UPDATE awareness SET files_hot=?, status=?, updated_at=?, "
                "project=? WHERE agent_id=?",
                (arr, status, time.time(), norm_project(project), agent))
    except Exception as e:
        print(f"prebrief heartbeat err: {e}", file=sys.stderr)


def tools(store, session, batch, project=DEFAULT_PROJECT):
    """Record a batch of tool calls: [{tool, path, is_error}, ...]."""
    proj = norm_project(project)
    try:
        now = time.time()
        for t in (batch or [])[:40]:
            try:
                store.sql(
                    "INSERT INTO tool_events "
                    "(session, tool, path, is_error, ts, project) "
                    "VALUES (?,?,?,?,?,?)",
                    (session,
                     str(t.get("tool", "?"))[:80],
                     (str(t.get("path"))[:300] if t.get("path") else None),
                     1 if t.get("is_error") else 0,
                     now, proj))
            except Exception:
                pass
    except Exception as e:
        print(f"prebrief tools err: {e}", file=sys.stderr)


def traverse(store, agent, question, refs, project=DEFAULT_PROJECT):
    """Record what an agent went looking for — feeds FLEET ATTENTION."""
    try:
        store.event("observation", agent, "traversal",
                    {"traversal": True, "q": str(question or "")[:200],
                     "refs": [str(r) for r in (refs or [])[:12]]},
                    project=norm_project(project))
    except Exception as e:
        print(f"prebrief traverse err: {e}", file=sys.stderr)


def end(store, agent, session, project=DEFAULT_PROJECT):
    """Close out an agent: log session.end, mark awareness idle.

    The `session.end` event is the log-first path; the direct status update is
    a DEPRECATED write-through, same rationale as `register()`.
    """
    try:
        store.event("session.end", agent, session, {},
                    project=norm_project(project))
        store.sql(
            "UPDATE awareness SET status='idle', updated_at=? WHERE agent_id=?",
            (time.time(), agent))
    except Exception as e:
        print(f"prebrief end err: {e}", file=sys.stderr)


def decide(store, subject, choice, rationale="", binds="", scope_root=None,
           project=DEFAULT_PROJECT, origin=None, shared=0):
    """Record a standing decision, stamped with its project and origin.

    DEPRECATED — direct projection write. This inserts straight into `decision`
    without emitting an event, so the row has no entry in the log: it cannot be
    replayed and `projector.rebuild()` will delete it. Use `make_decision()`
    (emit) + `projector.project_events()` (fold) instead. Kept working for
    backwards compatibility.

    `shared=1` makes the decision visible to OTHER projects — it will render
    there with an explicit [from project:X] tag. Returns the row id, 0 on
    failure (fail open).
    """
    try:
        proj = norm_project(project)
        store.sql(
            "INSERT INTO decision (scope_root, subject, choice, rationale, "
            "binds, status, project, origin, shared) "
            "VALUES (?,?,?,?,?,'standing',?,?,?)",
            (scope_root, str(subject or "")[:120], str(choice or "")[:2000],
             str(rationale or "")[:2000], str(binds or "")[:300], proj,
             (str(origin)[:80] if origin else proj), 1 if shared else 0))
        rows = store.sql("SELECT COALESCE(MAX(id),0) FROM decision")
        return int(rows[0][0]) if rows else 0
    except Exception as e:
        print(f"prebrief decide err: {e}", file=sys.stderr)
        return 0


def plan(store, title, kind="goal", root_id=None, owner=None, status="open",
         project=DEFAULT_PROJECT, origin=None, shared=0):
    """Record a plan node (a build goal or one of its children). Fails open.

    DEPRECATED — direct projection write, with no event behind it. The row is
    unreplayable and `projector.rebuild()` will delete it. Use `open_plan()` /
    `set_plan_status()` (emit) + `projector.project_events()` (fold). Kept
    working for backwards compatibility.
    """
    try:
        proj = norm_project(project)
        store.sql(
            "INSERT INTO plan_node (root_id, kind, title, status, owner, "
            "project, origin, shared) VALUES (?,?,?,?,?,?,?,?)",
            (root_id, str(kind or "goal")[:40], str(title or "")[:300],
             status, owner, proj,
             (str(origin)[:80] if origin else proj), 1 if shared else 0))
        rows = store.sql("SELECT COALESCE(MAX(id),0) FROM plan_node")
        return int(rows[0][0]) if rows else 0
    except Exception as e:
        print(f"prebrief plan err: {e}", file=sys.stderr)
        return 0


# --------------------------------------------------------------- log-first
# Emit-only writers. These append an event and NOTHING else; the matching
# projection row is materialised by prebrief.projector.project_events (run it
# via `prebrief project`, or `prebrief rebuild` to replay the whole log).
#
# Prefer these over the direct writers above. A direct write is invisible to
# the log, so it cannot be replayed and does not survive a rebuild — which is
# precisely the defect the projector exists to close.

def open_plan(store, actor, session, title, kind="goal", root_id=None,
              owner=None, node_id=None, status="open",
              project=DEFAULT_PROJECT, origin=None, shared=0):
    """Emit a `plan.open` event. Returns the event id (0 on failure).

    The projected plan_node takes the EVENT id as its primary key unless
    `node_id` is given, which is what makes a replay reproduce the same ids.
    """
    try:
        return store.event("plan.open", actor, session, {
            "title": str(title or "")[:300],
            "kind": str(kind or "goal")[:40],
            "root_id": root_id,
            "owner": (str(owner)[:80] if owner else None),
            "node_id": node_id,
            "status": str(status or "open")[:40],
            "origin": (str(origin)[:80] if origin else None),
            "shared": 1 if shared else 0,
        }, project=norm_project(project))
    except Exception as e:
        print(f"prebrief open_plan err: {e}", file=sys.stderr)
        return 0


def set_plan_status(store, actor, session, node_id, action, owner=None,
                    project=DEFAULT_PROJECT):
    """Emit a plan transition: action is 'claim', 'done' or 'block'.

    Returns the event id, or 0 on an unknown action (fail open — an unknown
    transition is dropped, never raised).
    """
    kind = {"claim": "plan.claim", "done": "plan.done",
            "block": "plan.block"}.get(str(action or "").lower())
    if not kind:
        print(f"prebrief set_plan_status: unknown action {action!r}",
              file=sys.stderr)
        return 0
    try:
        return store.event(kind, actor, session, {
            "node": int(node_id),
            "owner": (str(owner)[:80] if owner else None),
        }, project=norm_project(project))
    except Exception as e:
        print(f"prebrief set_plan_status err: {e}", file=sys.stderr)
        return 0


def make_decision(store, actor, session, subject, choice, rationale="",
                  binds="", scope_root=None, decision_id=None,
                  project=DEFAULT_PROJECT, origin=None, shared=0):
    """Emit a `decision.make` event. Returns the event id (0 on failure).

    The projected decision takes the EVENT id as its primary key unless
    `decision_id` is given.
    """
    try:
        return store.event("decision.make", actor, session, {
            "subject": str(subject or "")[:120],
            "choice": str(choice or "")[:2000],
            "rationale": str(rationale or "")[:2000],
            "binds": str(binds or "")[:300],
            "scope_root": scope_root,
            "decision_id": decision_id,
            "origin": (str(origin)[:80] if origin else None),
            "shared": 1 if shared else 0,
        }, project=norm_project(project))
    except Exception as e:
        print(f"prebrief make_decision err: {e}", file=sys.stderr)
        return 0


def supersede_decision(store, actor, session, decision_id, reason="",
                       project=DEFAULT_PROJECT):
    """Emit a `decision.supersede` event, retiring a standing decision."""
    try:
        return store.event("decision.supersede", actor, session, {
            "decision": int(decision_id),
            "reason": str(reason or "")[:300],
        }, project=norm_project(project))
    except Exception as e:
        print(f"prebrief supersede_decision err: {e}", file=sys.stderr)
        return 0


def assert_claim(store, actor, session, subject, predicate, body="",
                 confidence=None, source=None, project=DEFAULT_PROJECT):
    """Emit a `claim.assert` event — a stated belief about the world.

    The projected claim row is keyed on `asserted_by` (this event's id), so
    folding the same assertion twice cannot duplicate it.
    """
    try:
        return store.event("claim.assert", actor, session, {
            "subject": str(subject or "")[:200],
            "predicate": str(predicate or "")[:120],
            "body": str(body or "")[:1000],
            "confidence": confidence,
            "source": (str(source)[:200] if source else None),
        }, project=norm_project(project))
    except Exception as e:
        print(f"prebrief assert_claim err: {e}", file=sys.stderr)
        return 0


def retract_claim(store, actor, session, claim_id, reason="",
                  project=DEFAULT_PROJECT):
    """Emit a `claim.retract` event, retiring one projected claim."""
    try:
        return store.event("claim.retract", actor, session, {
            "claim": int(claim_id),
            "reason": str(reason or "")[:300],
        }, project=norm_project(project))
    except Exception as e:
        print(f"prebrief retract_claim err: {e}", file=sys.stderr)
        return 0

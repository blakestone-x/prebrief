"""Projector atomicity — fail-open on a WRITE path is silent data loss.

The projector wrote through `Store.sql()`, which swallows errors, and then
advanced its cursor regardless. A failed projection write therefore produced:
no row, a cursor claiming the event was folded, exit code 0, and a counts dict
reporting success. The event was gone from derived state forever while the log
still said it had been applied — the exact failure the log-is-truth design is
supposed to make impossible.

The fix is one transaction per event covering BOTH the projection writes and
the cursor advance, over a STRICT writer that raises:

  (a) a failed write rolls back and does NOT advance the cursor
  (b) the failing event is retried — and succeeds — once the fault is gone
  (c) a failure in the cursor write rolls the projection row back too
      (proving the two really are in one transaction)
  (d) a malformed event is skipped FORWARD, so it cannot wedge the fold
  (e) the store's strict/transactional helpers keep their own contract, while
      sql() keeps its fail-open one

Run: python tests/test_projector_atomicity.py
"""
import os
import sqlite3
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from prebrief import client, projector                       # noqa: E402
from prebrief.store import Store                             # noqa: E402


def fresh():
    return Store(":memory:")


def titles(store):
    return {r[0] for r in store.sql("SELECT title FROM plan_node")}


class _Fault:
    """A strict writer that raises on statements matching a predicate.

    Wraps the real Store.execute_strict, so everything else behaves normally —
    the injected failure looks like sqlite failing on one statement, which is
    what a disk error or a lock timeout actually looks like.
    """

    def __init__(self, store, match):
        self.real = store.execute_strict
        self.match = match
        self.armed = True
        self.hits = 0

    def __call__(self, query, params=()):
        if self.armed and self.match(query, params):
            self.hits += 1
            raise sqlite3.OperationalError("injected: disk I/O error")
        return self.real(query, params)


class TestProjectorAtomicity(unittest.TestCase):

    # -------------------------------------------------------------- (a)+(b)
    def test_failed_write_does_not_advance_the_cursor(self):
        s = fresh()
        e1 = client.open_plan(s, "o", "s1", "first", kind="goal")
        e2 = client.make_decision(s, "o", "s1", "storage", "sqlite WAL")
        e3 = client.open_plan(s, "o", "s1", "POISON", kind="task")
        e4 = client.open_plan(s, "o", "s1", "fourth", kind="task")

        fault = _Fault(s, lambda q, p: any(
            isinstance(v, str) and "POISON" in v for v in p))
        s.execute_strict = fault

        counts = projector.project_events(s)

        # Progress up to the bad event is committed and reported...
        self.assertEqual(counts["events"], 2, counts)
        self.assertEqual(counts["errors"], 1, counts)
        self.assertEqual(counts["last_event_id"], e2, counts)
        self.assertIn("error", counts)
        # ...and the cursor on disk agrees: e3 was never folded, so it is still
        # pending, not silently consumed.
        self.assertEqual(projector.last_event_id(s), e2,
                         "the cursor advanced past an event that never landed")
        self.assertEqual(titles(s), {"first"})
        self.assertEqual(s.sql("SELECT count(*) FROM decision")[0][0], 1)
        # The failed event's tallies were rolled back with its transaction.
        self.assertEqual(counts["plan_node"], 1, counts)
        self.assertEqual(counts["skipped"], 0, counts)
        self.assertEqual(counts["dead_letter"], 0, counts)

        # (b) clear the fault: the retry folds e3 AND the events behind it.
        fault.armed = False
        again = projector.project_events(s)
        self.assertEqual(again["errors"], 0, again)
        self.assertEqual(again["events"], 2, again)
        self.assertEqual(again["last_event_id"], e4, again)
        self.assertEqual(titles(s), {"first", "POISON", "fourth"})
        self.assertEqual(projector.last_event_id(s), e4)
        self.assertEqual(
            {r[0] for r in s.sql("SELECT id FROM plan_node")}, {e1, e3, e4})

    def test_a_failure_on_the_first_event_folds_nothing(self):
        """The degenerate case: cursor 0 must stay 0, not creep forward."""
        s = fresh()
        client.open_plan(s, "o", "s1", "only", kind="goal")
        s.execute_strict = _Fault(s, lambda q, p: "plan_node" in q)

        counts = projector.project_events(s)
        self.assertEqual(counts["events"], 0, counts)
        self.assertEqual(counts["errors"], 1, counts)
        self.assertEqual(counts["last_event_id"], 0, counts)
        self.assertEqual(projector.last_event_id(s), 0)
        self.assertEqual(s.sql("SELECT count(*) FROM plan_node")[0][0], 0)

    # ------------------------------------------------------------------ (c)
    def test_a_failed_cursor_write_rolls_back_the_projection(self):
        """Writes and cursor share one transaction — prove it from both ends.

        Failing the CURSOR statement must undo the projection row as well. If
        they were separate transactions the row would survive with the cursor
        left behind, and the next run would re-fold it.
        """
        s = fresh()
        client.open_plan(s, "o", "s1", "first", kind="goal")
        fault = _Fault(s, lambda q, p: "projector_state" in q)
        s.execute_strict = fault

        counts = projector.project_events(s)
        self.assertEqual(counts["errors"], 1, counts)
        self.assertEqual(counts["events"], 0, counts)
        self.assertEqual(s.sql("SELECT count(*) FROM plan_node")[0][0], 0,
                         "projection row outlived the failed cursor advance")

        fault.armed = False
        again = projector.project_events(s)
        self.assertEqual(again["events"], 1, again)
        self.assertEqual(titles(s), {"first"})

    def test_a_partly_written_event_leaves_no_half_state(self):
        """Fail the SECOND write of a multi-write event; the first must vanish.

        session.start folds into awareness and then advances the cursor; a
        `plan.claim` folds status and owner in one statement. The compound case
        here is the fold as a whole: rows written before the failure inside the
        same event must not survive it.
        """
        s = fresh()
        node = client.open_plan(s, "o", "s1", "work", kind="task")
        projector.project_events(s)
        client.set_plan_status(s, "b", "s1", node, "claim", owner="b")

        # let the UPDATE through, fail the cursor: same transaction, so the
        # status change must be undone.
        fault = _Fault(s, lambda q, p: "projector_state" in q)
        s.execute_strict = fault
        counts = projector.project_events(s)

        self.assertEqual(counts["errors"], 1, counts)
        self.assertEqual(
            s.sql("SELECT status, owner FROM plan_node WHERE id=?", (node,)),
            [("open", "o")],
            "a rolled-back event left its status change behind")

        fault.armed = False
        projector.project_events(s)
        self.assertEqual(
            s.sql("SELECT status, owner FROM plan_node WHERE id=?", (node,)),
            [("claimed", "b")])

    # ------------------------------------------------------------------ (d)
    def test_a_malformed_event_does_not_wedge_the_fold(self):
        """A poisoned event is skipped FORWARD — retrying it would never end.

        The distinction that matters: infrastructure errors retry (the event is
        fine, the disk is not), event errors dead-letter (retrying cannot help).
        """
        s = fresh()
        node = client.open_plan(s, "o", "s1", "work", kind="task")
        client.set_plan_status(s, "o", "s1", node, "block")
        last = client.set_plan_status(s, "o", "s1", node, "done")

        def poisoned(*a, **k):
            raise ValueError("payload from another dimension")

        with mock.patch.dict(projector._HANDLERS,
                             {"plan.block": poisoned}):
            counts = projector.project_events(s)

        self.assertEqual(counts["errors"], 0, counts)
        self.assertEqual(counts["dead_letter"], 1, counts)
        self.assertEqual(counts["skipped"], 1, counts)
        self.assertEqual(counts["events"], 3, counts)
        self.assertEqual(counts["last_event_id"], last, counts)
        self.assertEqual(projector.last_event_id(s), last)
        # the event AFTER the poisoned one still folded
        self.assertEqual(s.sql("SELECT status FROM plan_node WHERE id=?",
                               (node,)), [("done",)])

        # ...and it is not re-served on the next run: the fold moved on.
        again = projector.project_events(s)
        self.assertEqual(again["events"], 0, again)
        self.assertEqual(again["dead_letter"], 0, again)

    def test_an_unknown_kind_is_not_an_error(self):
        s = fresh()
        s.event("wormhole.opened", "stranger", "s9", {"x": 1})
        last = client.open_plan(s, "o", "s1", "after", kind="goal")
        counts = projector.project_events(s)
        self.assertEqual(counts["errors"], 0, counts)
        self.assertEqual(counts["dead_letter"], 0, counts)
        self.assertEqual(counts["skipped"], 1, counts)
        self.assertEqual(counts["last_event_id"], last)

    def test_counts_expose_the_failure_modes(self):
        """Every run reports the three counters, so a caller can alert on them."""
        s = fresh()
        counts = projector.project_events(s)
        for k in ("cross_tenant", "dead_letter", "errors"):
            self.assertIn(k, counts)
            self.assertEqual(counts[k], 0, counts)
        self.assertEqual(
            {k: projector.rebuild(s)[k] for k in
             ("cross_tenant", "dead_letter", "errors")},
            {"cross_tenant": 0, "dead_letter": 0, "errors": 0})

    def test_a_broken_store_still_fails_open_to_the_caller(self):
        """Strict writes are internal: the caller never sees an exception."""
        s = fresh()
        s.close()
        counts = projector.project_events(s)
        self.assertEqual(counts["events"], 0, counts)
        self.assertEqual(counts["last_event_id"], 0, counts)

    # ------------------------------------------------------------------ (e)
    def test_store_write_contracts(self):
        s = fresh()
        # sql() stays fail-open — every read path depends on that.
        self.assertEqual(s.sql("SELECT * FROM no_such_table"), [])
        # execute_strict() raises instead.
        with self.assertRaises(sqlite3.Error):
            s.execute_strict("SELECT * FROM no_such_table")
        self.assertEqual(
            s.execute_strict("SELECT 1"), [(1,)])

    def test_store_transaction_commits_and_rolls_back(self):
        s = fresh()
        with s.transaction():
            s.execute_strict(
                "INSERT INTO plan_node (title, status) VALUES ('kept','open')")
        self.assertEqual(titles(s), {"kept"})

        with self.assertRaises(ValueError):
            with s.transaction():
                s.execute_strict(
                    "INSERT INTO plan_node (title, status) "
                    "VALUES ('dropped','open')")
                raise ValueError("boom")
        self.assertEqual(titles(s), {"kept"},
                         "transaction() did not roll back")

        # the connection is usable afterwards
        with s.transaction():
            s.execute_strict(
                "INSERT INTO plan_node (title, status) VALUES ('after','open')")
        self.assertEqual(titles(s), {"kept", "after"})

    def test_store_transaction_on_a_dead_store_raises(self):
        s = fresh()
        s.close()
        with self.assertRaises(RuntimeError):
            with s.transaction():
                pass
        with self.assertRaises(RuntimeError):
            s.execute_strict("SELECT 1")


if __name__ == "__main__":
    unittest.main(verbosity=2)

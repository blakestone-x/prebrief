"""Cross-tenant mutation — the hole that project scoping on READS did not close.

R2 scopes every read to the caller's project, which makes the tenants look
isolated. The transitions did not: `plan.done` and `decision.supersede` folded
with `WHERE id=?`, so an event emitted from project Bravo naming id 7 mutated
project Alpha's row 7. Isolation you can walk around by guessing an integer is
not isolation.

These tests are the wall:

  (a) a Bravo event cannot mark Alpha's plan node done
  (b) a Bravo event cannot supersede Alpha's decision
  (c) a Bravo event cannot retract Alpha's claim
  (d) same-project transitions still work (the guard is not a blanket refusal)
  (e) the refusal is observable — counted, not silently dropped

Run: python tests/test_tenant_mutation.py
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from prebrief import client, projector                       # noqa: E402
from prebrief.store import Store                             # noqa: E402


def fresh():
    return Store(":memory:")


def one(store, query, params=()):
    rows = store.sql(query, params)
    return rows[0][0] if rows else None


class TestCrossTenantMutation(unittest.TestCase):

    # ------------------------------------------------------------------ (a)
    def test_other_project_cannot_close_a_plan_node(self):
        s = fresh()
        node = client.open_plan(s, "alpha-lead", "sA", "alpha's work",
                                kind="task", project="alpha")
        projector.project_events(s)
        self.assertEqual(one(s, "SELECT status FROM plan_node WHERE id=?",
                             (node,)), "open")

        # Bravo names Alpha's node id. The event is well-formed; the target is
        # simply not Bravo's to touch.
        client.set_plan_status(s, "bravo-lead", "sB", node, "done",
                               project="bravo")
        counts = projector.project_events(s)

        self.assertEqual(one(s, "SELECT status FROM plan_node WHERE id=?",
                             (node,)), "open",
                         "a Bravo event closed Alpha's plan node")
        self.assertEqual(one(s, "SELECT project FROM plan_node WHERE id=?",
                             (node,)), "alpha",
                         "the row was re-homed into the other project")
        self.assertEqual(counts["cross_tenant"], 1, counts)
        self.assertEqual(counts["plan_node"], 0, counts)
        # The event is consumed, not retried forever: it is a permanent refusal.
        self.assertEqual(counts["events"], 1, counts)
        self.assertEqual(counts["errors"], 0, counts)

    def test_other_project_cannot_claim_or_reassign_an_owner(self):
        """The owner column is the more interesting steal: it moves the work."""
        s = fresh()
        node = client.open_plan(s, "alpha-lead", "sA", "alpha's work",
                                kind="task", owner="alpha-builder",
                                project="alpha")
        projector.project_events(s)

        client.set_plan_status(s, "bravo-builder", "sB", node, "claim",
                               owner="bravo-builder", project="bravo")
        counts = projector.project_events(s)

        self.assertEqual(
            s.sql("SELECT status, owner FROM plan_node WHERE id=?", (node,)),
            [("open", "alpha-builder")],
            "a Bravo claim rewrote Alpha's node status/owner")
        self.assertEqual(counts["cross_tenant"], 1, counts)

    # ------------------------------------------------------------------ (b)
    def test_other_project_cannot_supersede_a_decision(self):
        s = fresh()
        did = client.make_decision(s, "alpha-lead", "sA", "storage",
                                   "sqlite WAL", project="alpha")
        projector.project_events(s)
        self.assertEqual(one(s, "SELECT status FROM decision WHERE id=?",
                             (did,)), "standing")

        client.supersede_decision(s, "bravo-lead", "sB", did,
                                  reason="not yours", project="bravo")
        counts = projector.project_events(s)

        self.assertEqual(one(s, "SELECT status FROM decision WHERE id=?",
                             (did,)), "standing",
                         "a Bravo event retired Alpha's standing decision")
        self.assertEqual(counts["cross_tenant"], 1, counts)
        self.assertEqual(counts["decision"], 0, counts)

    def test_other_project_cannot_overwrite_a_row_via_explicit_id(self):
        """plan.open/decision.make with an explicit id are UPDATEs on conflict.

        Guarding only the transitions would leave the same steal one payload
        field away: `node_id` / `decision_id` name an existing row directly.
        """
        s = fresh()
        node = client.open_plan(s, "alpha-lead", "sA", "alpha's work",
                                kind="task", project="alpha")
        did = client.make_decision(s, "alpha-lead", "sA", "storage",
                                   "sqlite WAL", project="alpha")
        projector.project_events(s)

        client.open_plan(s, "bravo-lead", "sB", "bravo's rewrite", kind="task",
                         node_id=node, project="bravo")
        client.make_decision(s, "bravo-lead", "sB", "storage", "postgres",
                             decision_id=did, project="bravo")
        counts = projector.project_events(s)

        self.assertEqual(
            s.sql("SELECT title, project FROM plan_node WHERE id=?", (node,)),
            [("alpha's work", "alpha")])
        self.assertEqual(
            s.sql("SELECT choice, project FROM decision WHERE id=?", (did,)),
            [("sqlite WAL", "alpha")])
        self.assertEqual(counts["cross_tenant"], 2, counts)

    def test_other_project_cannot_retract_a_claim(self):
        s = fresh()
        asserted = client.assert_claim(
            s, "alpha-lead", "sA", "identity.py", "fails_when",
            body="Host-native basename splits tenants", project="alpha")
        projector.project_events(s)
        claim_id = one(
            s, "SELECT id FROM claim WHERE asserted_by=?", (asserted,))

        client.retract_claim(
            s, "bravo-lead", "sB", claim_id,
            reason="not yours", project="bravo")
        counts = projector.project_events(s)

        self.assertEqual(
            one(s, "SELECT status FROM claim WHERE id=?", (claim_id,)),
            "asserted")
        self.assertEqual(counts["cross_tenant"], 1, counts)
        self.assertEqual(counts["claim"], 0, counts)

    # ------------------------------------------------------------------ (c)
    def test_same_project_transitions_still_work(self):
        """The guard must refuse the crossing, not the transition."""
        s = fresh()
        node = client.open_plan(s, "alpha-lead", "sA", "alpha's work",
                                kind="task", project="alpha")
        did = client.make_decision(s, "alpha-lead", "sA", "storage",
                                   "sqlite WAL", project="alpha")
        client.set_plan_status(s, "alpha-builder", "sA", node, "claim",
                               owner="alpha-builder", project="alpha")
        client.set_plan_status(s, "alpha-builder", "sA", node, "done",
                               project="alpha")
        client.supersede_decision(s, "alpha-lead", "sA", did,
                                  reason="moved on", project="alpha")
        counts = projector.project_events(s)

        self.assertEqual(counts["cross_tenant"], 0, counts)
        self.assertEqual(
            s.sql("SELECT status, owner FROM plan_node WHERE id=?", (node,)),
            [("done", "alpha-builder")])
        self.assertEqual(one(s, "SELECT status FROM decision WHERE id=?",
                             (did,)), "superseded")

    def test_two_projects_hold_transitions_independently(self):
        """Both tenants transition their own rows in one interleaved fold."""
        s = fresh()
        a = client.open_plan(s, "alpha-lead", "sA", "alpha task", kind="task",
                             project="alpha")
        b = client.open_plan(s, "bravo-lead", "sB", "bravo task", kind="task",
                             project="bravo")
        client.set_plan_status(s, "alpha-lead", "sA", a, "done",
                               project="alpha")
        client.set_plan_status(s, "bravo-lead", "sB", b, "block",
                               project="bravo")
        # ...and each reaching for the other's node changes nothing.
        client.set_plan_status(s, "alpha-lead", "sA", b, "done",
                               project="alpha")
        client.set_plan_status(s, "bravo-lead", "sB", a, "block",
                               project="bravo")
        counts = projector.project_events(s)

        self.assertEqual(
            s.sql("SELECT status FROM plan_node WHERE id=?", (a,)),
            [("done",)])
        self.assertEqual(
            s.sql("SELECT status FROM plan_node WHERE id=?", (b,)),
            [("blocked",)])
        self.assertEqual(counts["cross_tenant"], 2, counts)

    def test_default_project_rows_are_not_a_back_door(self):
        """Legacy rows land in 'default'; that must not make them public."""
        s = fresh()
        node = client.open_plan(s, "o", "s1", "legacy work", kind="task")
        projector.project_events(s)
        s.sql("UPDATE plan_node SET project=NULL WHERE id=?", (node,))

        client.set_plan_status(s, "bravo-lead", "sB", node, "done",
                               project="bravo")
        counts = projector.project_events(s)
        self.assertEqual(one(s, "SELECT status FROM plan_node WHERE id=?",
                             (node,)), "open")
        self.assertEqual(counts["cross_tenant"], 1, counts)

        # ...while an event from 'default' still folds onto it.
        client.set_plan_status(s, "o", "s1", node, "done")
        projector.project_events(s)
        self.assertEqual(one(s, "SELECT status FROM plan_node WHERE id=?",
                             (node,)), "done")

    # ------------------------------------------------------------------ (d)
    def test_refusal_survives_a_rebuild(self):
        """Replaying the whole log must reach the same refusal, not a race."""
        s = fresh()
        node = client.open_plan(s, "alpha-lead", "sA", "alpha's work",
                                kind="task", project="alpha")
        client.set_plan_status(s, "bravo-lead", "sB", node, "done",
                               project="bravo")
        projector.project_events(s)
        incremental = projector.snapshot(s)

        counts = projector.rebuild(s)
        self.assertEqual(counts["cross_tenant"], 1, counts)
        self.assertEqual(projector.snapshot(s), incremental,
                         "rebuild disagreed with the incremental fold")
        self.assertEqual(one(s, "SELECT status FROM plan_node WHERE id=?",
                             (node,)), "open")


if __name__ == "__main__":
    unittest.main(verbosity=2)

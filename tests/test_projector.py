"""Projector tests — the architecture claim, made falsifiable.

Prebrief's design says the append-only log is the sole source of truth and the
projections are derived and rebuildable. Before prebrief/projector.py that was
false: writers went straight to the projection tables, so nothing could be
replayed. These tests are the wall that keeps it true.

  (a) emit -> project produces the expected projection rows
  (b) projecting twice is a no-op (idempotent fold)
  (c) rebuild() from zero reproduces identical projection content
  (d) an unknown event kind is skipped without error

Run: python tests/test_projector.py      (or: python -m unittest discover tests)
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from prebrief import client, projector                      # noqa: E402
from prebrief.store import Store                             # noqa: E402


def fresh():
    return Store(":memory:")


def rows(store, table, cols):
    return sorted(store.sql(f"SELECT {cols} FROM {table}"), key=repr)


def seed(store):
    """Five log-first events: 2 plan, 1 transition, 1 decision, 1 claim.

    Returns the emitted event ids. Nothing is written to a projection table
    here — that is the whole point.
    """
    ids = {}
    ids["goal"] = client.open_plan(
        store, "orchestrator", "s1", "ship the projector", kind="goal")
    ids["task"] = client.open_plan(
        store, "orchestrator", "s1", "write the fold", kind="task",
        root_id=ids["goal"])
    ids["claimed"] = client.set_plan_status(
        store, "builder-a", "s1", ids["task"], "claim", owner="builder-a")
    ids["decision"] = client.make_decision(
        store, "orchestrator", "s1", "storage",
        "sqlite WAL, stdlib only", rationale="zero deps",
        binds="all agents", scope_root=ids["goal"])
    ids["claim"] = client.assert_claim(
        store, "builder-a", "s1", "projector.py", "is",
        body="the sole writer of projection tables", confidence=0.9)
    return ids


class TestProjector(unittest.TestCase):

    # ------------------------------------------------------------------ (a)
    def test_emitted_events_produce_projection_rows(self):
        s = fresh()
        ids = seed(s)
        self.assertTrue(all(ids.values()), f"emitters returned ids: {ids}")

        # Nothing is projected until the fold runs — the log is the truth.
        self.assertEqual(s.sql("SELECT count(*) FROM plan_node")[0][0], 0)
        self.assertEqual(s.sql("SELECT count(*) FROM decision")[0][0], 0)

        counts = projector.project_events(s)
        self.assertEqual(counts["events"], 5, counts)
        self.assertEqual(counts["plan_node"], 3, counts)   # 2 opens + 1 claim
        self.assertEqual(counts["decision"], 1, counts)
        self.assertEqual(counts["claim"], 1, counts)
        self.assertEqual(counts["skipped"], 0, counts)
        self.assertEqual(counts["last_event_id"], max(ids.values()))

        # plan_node: ids derive from the event ids, not an autoincrement.
        plans = dict((r[0], r) for r in s.sql(
            "SELECT id, root_id, kind, title, status, owner FROM plan_node"))
        self.assertEqual(set(plans), {ids["goal"], ids["task"]})
        goal = plans[ids["goal"]]
        self.assertEqual((goal[1], goal[2], goal[3], goal[4]),
                         (None, "goal", "ship the projector", "open"))
        task = plans[ids["task"]]
        self.assertEqual((task[1], task[2], task[4], task[5]),
                         (ids["goal"], "task", "claimed", "builder-a"),
                         "plan.claim must fold onto the node it names")

        dec = s.sql("SELECT id, scope_root, subject, choice, binds, status "
                    "FROM decision")
        self.assertEqual(dec, [(ids["decision"], ids["goal"], "storage",
                                "sqlite WAL, stdlib only", "all agents",
                                "standing")])

        clm = s.sql("SELECT subject, predicate, body, confidence, status, "
                    "source, asserted_by FROM claim")
        self.assertEqual(clm, [("projector.py", "is",
                                "the sole writer of projection tables", 0.9,
                                "asserted", "builder-a", ids["claim"])])

        # session events fold into awareness
        s.event("session.start", "builder-b", "s2", {"role": "reviewer",
                                                     "task": "review the fold"})
        c2 = projector.project_events(s)
        self.assertEqual(c2["awareness"], 1, c2)
        aw = s.sql("SELECT agent_id, role, task_head, status FROM awareness")
        self.assertEqual(aw, [("builder-b", "reviewer", "review the fold",
                               "active")])

    # ------------------------------------------------------------------ (b)
    def test_projecting_twice_is_a_noop(self):
        s = fresh()
        seed(s)
        first = projector.project_events(s)
        before = projector.snapshot(s)

        second = projector.project_events(s)
        self.assertEqual(second["events"], 0, "second fold consumed events")
        for key in ("plan_node", "decision", "claim", "awareness", "skipped"):
            self.assertEqual(second[key], 0, f"{key} refolded: {second}")
        self.assertEqual(second["last_event_id"], first["last_event_id"])
        self.assertEqual(projector.snapshot(s), before,
                         "a second fold changed projection content")

        # Re-folding the SAME events (crash-resume: cursor rewound) must also
        # not duplicate — idempotency cannot rest on the cursor alone.
        s.sql("UPDATE projector_state SET last_event_id=0")
        replay = projector.project_events(s)
        self.assertEqual(replay["events"], 5, replay)
        self.assertEqual(projector.snapshot(s), before,
                         "replaying folded events duplicated rows")

    def test_claim_retraction_is_log_first_and_rebuildable(self):
        s = fresh()
        ids = seed(s)
        projector.project_events(s)
        claim_id = s.sql(
            "SELECT id FROM claim WHERE asserted_by=?", (ids["claim"],))[0][0]

        event_id = client.retract_claim(
            s, "reviewer", "s2", claim_id, reason="verification artifact")
        self.assertTrue(event_id)
        self.assertEqual(
            s.sql("SELECT status FROM claim WHERE id=?", (claim_id,)),
            [("asserted",)],
            "the emitter must not mutate the projection directly")

        projector.project_events(s)
        self.assertEqual(
            s.sql("SELECT status FROM claim WHERE id=?", (claim_id,)),
            [("retracted",)])
        incremental = projector.snapshot(s)
        projector.rebuild(s)
        self.assertEqual(projector.snapshot(s), incremental)

    # ------------------------------------------------------------------ (c)
    def test_rebuild_reproduces_identical_projections(self):
        s = fresh()
        ids = seed(s)
        projector.project_events(s)
        claim_id = s.sql(
            "SELECT id FROM claim WHERE asserted_by=?", (ids["claim"],))[0][0]
        s.event("session.start", "builder-a", "s1",
                {"role": "builder", "task": "write the fold"})
        client.set_plan_status(s, "builder-a", "s1", ids["task"], "done")
        client.supersede_decision(s, "orchestrator", "s1", ids["decision"],
                                  reason="moved to postgres")
        client.retract_claim(
            s, "orchestrator", "s1", claim_id,
            reason="superseded by implementation")
        s.event("session.end", "builder-a", "s1", {})
        projector.project_events(s)

        incremental = projector.snapshot(s)
        self.assertTrue(any(incremental[t] for t in incremental),
                        "nothing projected — the comparison would be vacuous")

        out = projector.rebuild(s)
        self.assertEqual(out["events"], 10, out)
        self.assertEqual(projector.snapshot(s), incremental,
                         "rebuild did not reproduce the incremental fold")

        # ...and the folded transitions actually took effect, so the equality
        # above is not two copies of an empty projection.
        self.assertEqual(
            s.sql("SELECT status FROM plan_node WHERE id=?",
                  (ids["task"],)), [("done",)])
        self.assertEqual(
            s.sql("SELECT status FROM decision WHERE id=?",
                  (ids["decision"],)), [("superseded",)])
        self.assertEqual(
            s.sql("SELECT status FROM awareness WHERE agent_id='builder-a'"),
            [("idle",)])
        self.assertEqual(
            s.sql("SELECT status FROM claim WHERE id=?", (claim_id,)),
            [("retracted",)])

    def test_rebuild_drops_rows_that_never_hit_the_log(self):
        """A direct write is not derivable, so it must not survive a rebuild.

        This is the defect the projector exists to expose: if a hand-written
        row could outlive a rebuild, the log would not be the source of truth.
        """
        s = fresh()
        ids = seed(s)
        projector.project_events(s)
        client.plan(s, "hand-written goal", kind="goal")   # deprecated path
        self.assertEqual(s.sql("SELECT count(*) FROM plan_node")[0][0], 3)

        projector.rebuild(s)
        titles = {r[0] for r in s.sql("SELECT title FROM plan_node")}
        self.assertNotIn("hand-written goal", titles)
        self.assertEqual(titles, {"ship the projector", "write the fold"})
        self.assertEqual(
            {r[0] for r in s.sql("SELECT id FROM plan_node")},
            {ids["goal"], ids["task"]})

    def test_rebuild_preserves_the_log_and_the_ledger(self):
        s = fresh()
        seed(s)
        projector.project_events(s)
        s.sql("INSERT INTO delivery (agent_id, item_ref, watermark, "
              "delivered_at) VALUES ('a1','manual',3,1.0)")
        before = s.sql("SELECT count(*) FROM events")[0][0]

        projector.rebuild(s)
        self.assertEqual(s.sql("SELECT count(*) FROM events")[0][0], before,
                         "rebuild must never touch the log")
        self.assertEqual(s.sql("SELECT count(*) FROM delivery")[0][0], 1,
                         "the delivery ledger is history, not a projection")

    # ------------------------------------------------------------------ (d)
    def test_unknown_event_kind_is_skipped_without_error(self):
        s = fresh()
        ids = seed(s)
        s.event("wormhole.opened", "stranger", "s9", {"x": 1})
        s.event("observation", "builder-a", "traversal", {"traversal": True})
        after = s.event("plan.done", "builder-a", "s1", {"node": ids["task"]})

        counts = projector.project_events(s)
        self.assertNotIn("error", counts, counts)
        self.assertEqual(counts["skipped"], 2, counts)
        self.assertEqual(counts["events"], 8, counts)
        self.assertEqual(counts["last_event_id"], after,
                         "the cursor must advance past unknown kinds")
        # the known event AFTER the unknown ones still folded
        self.assertEqual(s.sql("SELECT status FROM plan_node WHERE id=?",
                               (ids["task"],)), [("done",)])

    def test_malformed_payload_does_not_stall_the_fold(self):
        s = fresh()
        ids = seed(s)
        # plan.claim naming no node, and a decision.supersede naming nothing:
        # both are unusable, both must degrade rather than raise.
        s.event("plan.claim", "ghost", "s1", {"nothing": "here"})
        s.event("decision.supersede", "ghost", "s1", {})
        last = client.set_plan_status(s, "builder-a", "s1", ids["goal"], "block")

        counts = projector.project_events(s)
        self.assertNotIn("error", counts, counts)
        self.assertEqual(counts["skipped"], 2, counts)
        self.assertEqual(counts["last_event_id"], last)
        self.assertEqual(s.sql("SELECT status FROM plan_node WHERE id=?",
                               (ids["goal"],)), [("blocked",)])

    # --------------------------------------------------------------- extras
    def test_upto_bounds_the_fold(self):
        s = fresh()
        ids = seed(s)
        counts = projector.project_events(s, upto=ids["goal"])
        self.assertEqual(counts["events"], 1, counts)
        self.assertEqual(counts["last_event_id"], ids["goal"])
        self.assertEqual(s.sql("SELECT count(*) FROM plan_node")[0][0], 1)

        rest = projector.project_events(s)
        self.assertEqual(rest["events"], 4, rest)
        self.assertEqual(s.sql("SELECT count(*) FROM plan_node")[0][0], 2)

    def test_empty_log_folds_cleanly(self):
        s = fresh()
        counts = projector.project_events(s)
        self.assertEqual(counts["events"], 0, counts)
        self.assertEqual(counts["last_event_id"], 0)
        self.assertEqual(projector.rebuild(s)["events"], 0)

    def test_fails_open_on_a_broken_store(self):
        """A dead store degrades to zero counts — never an exception."""
        s = fresh()
        s.close()
        counts = projector.project_events(s)
        self.assertEqual(counts["events"], 0, counts)
        self.assertEqual(projector.rebuild(s)["events"], 0)
        self.assertEqual(projector.last_event_id(s), 0)

    def test_projected_rows_carry_the_event_project(self):
        """Projections inherit tenant scope from the event, not a default."""
        s = fresh()
        eid = client.open_plan(s, "o", "s1", "tenant build", kind="goal",
                               project="acme")
        client.make_decision(s, "o", "s1", "region", "us-east",
                             project="acme")
        projector.project_events(s)
        self.assertEqual(
            s.sql("SELECT project FROM plan_node WHERE id=?", (eid,)),
            [("acme",)])
        self.assertEqual(
            {r[0] for r in s.sql("SELECT project FROM decision")}, {"acme"})


if __name__ == "__main__":
    unittest.main(verbosity=2)

"""Ledger honesty: the delivery ledger must record what was SENT, not what
was considered.

The defect this walls off (same class as R1): compose() collected every
candidate ref and marked them all delivered BEFORE the budget/curation step
decided what actually made it into the returned text. With 8 standing
decisions the first payload rendered 5 and marked 8 — the 3 that were cut
never appeared in any later payload. Permanently lost, silently.

  (a) every ref marked delivered is present in the returned text, and every
      candidate that was NOT rendered stays unmarked
  (b) the dropped candidates arrive in a LATER payload
  (c) no ref is ever marked twice (delivered exactly once)
  (d) R1 stays green: a 20-event burst is still delivered in full

Run: python tests/test_ledger_honesty.py     (unittest, stdlib only)
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from prebrief import Store, compose                    # noqa: E402
import prebrief.curator as CUR                         # noqa: E402
import prebrief.inject as INJ                          # noqa: E402

MARK = "LEDGERMARK-{}-END"      # unique, collision-free per decision


def add_decision(store, i, project="alpha"):
    """A standing decision whose choice text is unique and easy to find."""
    store.sql(
        "INSERT INTO decision (scope_root, subject, choice, rationale, binds, "
        "status, project, origin, shared) "
        "VALUES (NULL,?,?,'','','standing',?,?,0)",
        (f"topic{i}", MARK.format(i), project, f"agent@{project}"))
    return int(store.sql("SELECT MAX(id) FROM decision")[0][0])


def marked_refs(store, agent):
    """Content refs the ledger claims were delivered (sentinels excluded)."""
    return {r[0] for r in store.sql(
        "SELECT item_ref FROM delivery WHERE agent_id=? "
        "AND item_ref NOT IN ('manual','wm')", (agent,))}


class LedgerHonesty(unittest.TestCase):

    def setUp(self):
        # Keep the curator off the network: it is optional compression, and a
        # live Ollama would make these assertions non-deterministic.
        self._real_curate = CUR.curate
        CUR.curate = lambda task, text, budget, url=None: text[:budget]
        self._budget = (INJ.BUDGET, INJ.DELTA_BUDGET)
        self.s = Store(":memory:")
        self.ids = [add_decision(self.s, i) for i in range(8)]

    def tearDown(self):
        CUR.curate = self._real_curate
        INJ.BUDGET, INJ.DELTA_BUDGET = self._budget
        self.s.close()

    # -- helpers ---------------------------------------------------------
    def assert_ledger_matches_text(self, agent, payload, before=None):
        """The invariant, for one compose: everything this call newly marked
        is present in the text it returned, and everything the text carries
        was recorded. `before` is the ledger as it stood before the call."""
        before = set() if before is None else before
        marked = marked_refs(self.s, agent)
        for i, did in enumerate(self.ids):
            ref = f"decision:{did}"
            shown = MARK.format(i) in payload
            if ref in marked and ref not in before:
                self.assertTrue(
                    shown, f"{ref} marked delivered but its text is absent "
                           f"from the payload — the ledger is lying")
            if shown:
                self.assertIn(
                    ref, marked, f"{ref} rendered into the payload but was "
                                 f"not recorded — it will be sent twice")
        return marked

    def drain(self, agent, rounds=25):
        """Every payload the agent receives until the fleet goes quiet."""
        out = []
        for _ in range(rounds):
            p = compose(self.s, agent, "sess", project="alpha")
            out.append(p)
            if "unchanged" in p:
                break
        return out

    # -- (a) the ledger cannot claim more than the text carries -----------
    def test_section_cap_is_not_recorded_as_delivered(self):
        """The brief caps STANDING DECISIONS at 5; 8 must not be marked."""
        p = compose(self.s, "a1", "sess", project="alpha")
        marked = self.assert_ledger_matches_text("a1", p)
        self.assertLess(len(marked), len(self.ids),
                        "the section cap means some candidates never rendered")
        self.assertGreater(len(marked), 0, "control: some DID render")

    def test_budget_truncation_is_not_recorded_as_delivered(self):
        INJ.BUDGET = 1100                       # forces envelope truncation
        p = compose(self.s, "a2", "sess", project="alpha")
        self.assertIn("(truncated at budget)", p, "budget must actually bite")
        self.assertLess(len(p), 1200)
        self.assert_ledger_matches_text("a2", p)

    def test_delta_budget_truncation_is_not_recorded_as_delivered(self):
        compose(self.s, "a3", "sess", project="alpha")     # first contact
        before = marked_refs(self.s, "a3")
        # more backlog than one delta can carry
        self.ids += [add_decision(self.s, i) for i in range(8, 30)]
        INJ.DELTA_BUDGET = 300                     # a few items per delta
        p = compose(self.s, "a3", "sess", project="alpha")
        marked = self.assert_ledger_matches_text("a3", p, before)
        self.assertGreater(len(marked), len(before), "the delta must deliver")
        self.assertLess(len(marked), len(self.ids), "and must not deliver all")

    # -- (b) what was cut comes back --------------------------------------
    def test_dropped_candidates_arrive_in_a_later_payload(self):
        first = compose(self.s, "b1", "sess", project="alpha")
        cut = [i for i in range(8) if MARK.format(i) not in first]
        self.assertTrue(cut, "setup: the first payload must drop something")
        later = "\n".join(self.drain("b1"))
        for i in cut:
            self.assertIn(MARK.format(i), later,
                          f"decision {i} was cut from first contact and never "
                          f"came back — permanent loss")
        self.assertEqual(len(marked_refs(self.s, "b1")), len(self.ids))

    def test_everything_survives_a_hostile_budget(self):
        """Tiny budgets page the backlog; they must never eat it."""
        INJ.BUDGET, INJ.DELTA_BUDGET = 1100, 300
        seen = "\n".join([compose(self.s, "b2", "sess", project="alpha")]
                         + self.drain("b2"))
        for i in range(8):
            self.assertIn(MARK.format(i), seen, f"decision {i} lost")
        self.assertEqual(len(marked_refs(self.s, "b2")), len(self.ids))

    # -- (c) delivered exactly once ---------------------------------------
    def test_no_ref_is_ever_marked_twice(self):
        INJ.DELTA_BUDGET = 300
        payloads = [compose(self.s, "c1", "sess", project="alpha")]
        payloads += self.drain("c1")
        for i in range(8):
            hits = [n for n, p in enumerate(payloads) if MARK.format(i) in p]
            self.assertEqual(len(hits), 1,
                             f"decision {i} was sent in payloads {hits} — a "
                             f"delivered item must never be re-sent")
        dupes = self.s.sql(
            "SELECT item_ref, count(*) FROM delivery WHERE agent_id='c1' "
            "GROUP BY item_ref HAVING count(*) > 1")
        self.assertEqual(dupes, [])

    def test_delivered_at_is_not_restamped(self):
        """A second look at a quiet fleet must not re-date the ledger."""
        compose(self.s, "c2", "sess", project="alpha")
        self.drain("c2")
        before = dict(self.s.sql(
            "SELECT item_ref, delivered_at FROM delivery WHERE agent_id='c2' "
            "AND item_ref NOT IN ('manual','wm')"))
        compose(self.s, "c2", "sess", project="alpha")
        after = dict(self.s.sql(
            "SELECT item_ref, delivered_at FROM delivery WHERE agent_id='c2' "
            "AND item_ref NOT IN ('manual','wm')"))
        self.assertEqual(before, after)

    # -- (d) R1 regression wall -------------------------------------------
    def test_burst_of_20_events_still_fully_delivered(self):
        s = Store(":memory:")
        try:
            compose(s, "d1", "sess")                     # first contact
            for i in range(20):
                s.event("observation", f"a{i}", "sess", {"i": i})
            seen, guard = set(), 0
            while guard < 12:
                guard += 1
                p = compose(s, "d1", "sess")
                for i in range(20):
                    if f'"i": {i}' in p or f'"i":{i}' in p:
                        seen.add(i)
                if "unchanged" in p:
                    break
            self.assertEqual(len(seen), 20, f"{len(seen)}/20 events delivered")
        finally:
            s.close()

    def test_watermark_only_advances_over_rendered_events(self):
        s = Store(":memory:")
        try:
            compose(s, "d2", "sess")
            for i in range(30):
                s.event("observation", f"a{i}", "sess", {"i": i})
            p = compose(s, "d2", "sess")
            wm = int(s.sql("SELECT watermark FROM delivery WHERE "
                           "agent_id='d2' AND item_ref='wm'")[0][0])
            self.assertTrue(INJ._event_present(p, wm),
                            f"watermark {wm} names an event that never "
                            f"rendered")
            self.assertFalse(INJ._event_present(p, wm + 1),
                             "watermark stopped short of a rendered event")
        finally:
            s.close()

    # -- delivery state --------------------------------------------------
    def test_delivery_state_counts_emitted_then_used(self):
        compose(self.s, "e1", "sess", project="alpha")
        st = INJ.delivery_state(self.s, "e1")
        emitted = len(marked_refs(self.s, "e1"))
        self.assertEqual(st["emitted"], emitted)
        self.assertEqual(st["used"], 0)
        did = [d for d in self.ids
               if f"decision:{d}" in marked_refs(self.s, "e1")][0]
        subject = self.s.sql("SELECT subject FROM decision WHERE id=?",
                             (did,))[0][0]
        self.assertGreaterEqual(INJ.mark_used(self.s, "e1", subject, "", "Edit"),
                                1)
        st2 = INJ.delivery_state(self.s, "e1")
        self.assertEqual(st2["used"] + st2["emitted"], emitted)
        self.assertGreaterEqual(st2["used"], 1)

    def test_delivery_state_fails_open_on_a_dead_store(self):
        s = Store(":memory:")
        s.close()
        self.assertEqual(INJ.delivery_state(s, "nobody"),
                         {"emitted": 0, "used": 0})


if __name__ == "__main__":
    unittest.main(verbosity=2)

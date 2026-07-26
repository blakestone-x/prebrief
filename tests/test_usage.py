"""Usage closure: does the ledger record that an agent ACTED on what it was told?

Guards two real regressions found in the field:
  1. a path-only matcher marked 1 of 42 real deliveries (metric measured its
     own narrowness, not the system)
  2. a missing `re` import made mark_used return 0 forever, silently, because
     the function fails open
"""
import os, sys, tempfile, unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from prebrief import Store
import prebrief.client as CL, prebrief.inject as INJ, prebrief.projector as PJ


class UsageClosure(unittest.TestCase):
    def setUp(self):
        self.db = os.path.join(tempfile.mkdtemp(), "u.db")
        self.s = Store(self.db)
        CL.register(self.s, "u1", "s1", "builder", "billing sweep", project="forge")
        CL.open_plan(self.s, "u1", "s1", "billing sweep", project="forge")
        CL.make_decision(self.s, "u1", "s1", "convention:retry", "backoff",
                         "why", "binds", project="forge")
        PJ.project_events(self.s)
        INJ.compose(self.s, "u1", "s1", "builder", "billing sweep", project="forge")

    def test_matches_build_title_from_a_file_path(self):
        n = INJ.mark_used(self.s, "u1", "src/billing_sweep.py", "", "Edit")
        self.assertGreaterEqual(n, 1)
        self.assertGreater(INJ.effectiveness(self.s, "u1")["used"], 0)

    def test_matches_decision_subject_from_a_command(self):
        n = INJ.mark_used(self.s, "u1", "", "pytest tests/retry_policy_test.py", "Bash")
        self.assertGreaterEqual(n, 1)

    def test_unrelated_activity_marks_nothing(self):
        self.assertEqual(INJ.mark_used(self.s, "u1", "docs/unrelated.md", "ls", "Bash"), 0)

    def test_dependencies_present(self):
        # the missing-import bug: fail-open hid it, so assert the module is whole
        self.assertTrue(hasattr(INJ, "re"), "inject must import re")
        self.assertTrue(hasattr(INJ, "time"), "inject must import time")

    def test_effectiveness_shape(self):
        e = INJ.effectiveness(self.s)
        self.assertIn("rate", e)
        self.assertIn("delivered", e)


if __name__ == "__main__":
    unittest.main()

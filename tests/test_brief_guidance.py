"""Generated traversal guidance must preserve the project-scoping contract."""

from __future__ import annotations

import unittest

from prebrief import Store, compose, make_decision, open_plan, project_events
from prebrief.brief import full_brief


class BriefGuidanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = Store(":memory:")
        self.add_build("Initial build")

    def tearDown(self) -> None:
        self.store.close()

    def add_build(self, title: str) -> None:
        event = open_plan(
            self.store, "lead", "planning", title, project="alpha",
        )
        self.assertGreater(event, 0)
        counts = project_events(self.store)
        self.assertEqual(counts["errors"], 0, counts)

    def test_first_contact_uses_scoped_guidance(self) -> None:
        payload = compose(self.store, "alpha:reviewer", "session", project="alpha")
        self.assertIn("Initial build", payload)
        self.assertIn("-- HOW TO GO DEEPER", payload)
        self.assertIn("project=", payload)
        self.assertNotIn("SELECT", payload)
        self.assertNotIn("project=None", payload)

    def test_new_build_does_not_inject_unscoped_query(self) -> None:
        compose(self.store, "alpha:reviewer", "session", project="alpha")
        self.add_build("Follow-up build")
        payload = compose(self.store, "alpha:reviewer", "session", project="alpha")
        self.assertIn("NEW BUILD", payload)
        self.assertIn("Follow-up build", payload)
        self.assertNotIn("SELECT", payload)

    def test_operator_view_names_its_scope(self) -> None:
        make_decision(
            self.store, "other", "session", "beta setting", "visible to operator",
            project="beta",
        )
        project_events(self.store)
        payload, _ = full_brief(self.store, project=None)
        self.assertIn("project=None is intentionally all-project", payload)
        self.assertIn("visible to operator", payload)


if __name__ == "__main__":
    unittest.main()

"""Show first contact, deduplication, and updates without a model or network.

Run after installation: python -I examples/quickstart.py
"""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from prebrief import Store, compose, make_decision, open_plan, project_events

PROJECT = "quickstart"
AGENT = "quickstart:reviewer:context-1"
SESSION = "session-1"


def main() -> None:
    with TemporaryDirectory(prefix="prebrief-quickstart-") as directory:
        store = Store(str(Path(directory) / "demo.sqlite"))
        try:
            session_event = store.event(
                "session.start", AGENT, SESSION,
                {"role": "reviewer", "task": "billing export"}, project=PROJECT,
            )
            goal_event = open_plan(
                store, AGENT, SESSION, "billing export", project=PROJECT,
            )
            task_event = open_plan(
                store, AGENT, SESSION, "review the CSV writer",
                kind="task", root_id=goal_event, owner=AGENT, project=PROJECT,
            )
            decision_event = make_decision(
                store, AGENT, SESSION, "billing export format", "UTF-8 CSV",
                rationale="Keep exports portable", binds="reviewer",
                scope_root=goal_event, project=PROJECT,
            )
            assert all(event_id > 0 for event_id in (
                session_event, goal_event, task_event, decision_event,
            ))
            assert store.sql("SELECT COUNT(*) FROM plan_node") == [(0,)]
            assert store.sql("SELECT COUNT(*) FROM decision") == [(0,)]

            initial = project_events(store)
            assert initial["events"] == 4, initial
            assert initial["plan_node"] == 2, initial
            assert initial["decision"] == 1, initial
            assert initial["awareness"] == 1, initial
            assert initial["errors"] == 0, initial

            # Assert that this small example never needs optional curation.
            with patch(
                "prebrief.curator.curate",
                side_effect=AssertionError("curator must stay offline"),
            ) as curator:
                first = compose(
                    store, AGENT, SESSION, role="reviewer",
                    task="billing export", project=PROJECT,
                )
                assert first.startswith("== PREBRIEF FLEET CONTEXT")
                assert f'build:{goal_event} "' in first
                assert f"[decision:{decision_event}]" in first
                assert "UTF-8 CSV" in first

                unchanged = compose(
                    store, AGENT, SESSION, role="reviewer",
                    task="billing export", project=PROJECT,
                )
                assert unchanged.startswith(
                    f"(fleet unchanged for you since watermark {decision_event}"
                ), unchanged

                new_decision = make_decision(
                    store, AGENT, SESSION, "pagination", "Page exports by invoice ID",
                    rationale="Bound memory while preserving stable ordering",
                    scope_root=goal_event, project=PROJECT,
                )
                assert new_decision > decision_event
                delta_fold = project_events(store)
                assert delta_fold["events"] == 1, delta_fold
                assert delta_fold["decision"] == 1, delta_fold
                assert delta_fold["errors"] == 0, delta_fold

                delta = compose(
                    store, AGENT, SESSION, role="reviewer",
                    task="billing export", project=PROJECT,
                )
                assert f"e{new_decision} decision.make" in delta
                assert f"NEW DECISION d{new_decision} [pagination]" in delta

                # A fresh context needs a fresh agent ID, not just a new session.
                fresh_agent = "quickstart:reviewer:context-2"
                fresh = compose(
                    store, fresh_agent, "session-2", role="reviewer",
                    task="billing export", project=PROJECT,
                )
                assert f'build:{goal_event} "' in fresh
                assert f"[decision:{decision_event}]" in fresh
                assert f"[decision:{new_decision}]" in fresh

            assert curator.call_count == 0
            assert store.sql(
                "SELECT DISTINCT agent_id FROM delivery ORDER BY agent_id"
            ) == [(AGENT,), (fresh_agent,)]

            for label, payload in (
                ("FIRST CONTACT", first), ("UNCHANGED", unchanged),
                ("NEW DECISION", delta), ("FRESH CONTEXT", fresh),
            ):
                print(f"\n=== {label} ===")
                print(payload)
            print("\nPASS: projected state, deduplication, delta, and fresh context.")
        finally:
            store.close()


if __name__ == "__main__":
    main()

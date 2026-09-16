"""Subprocess regressions for hook identity, enrollment, and doctor CLI."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from prebrief import __version__, cli, doctor
from prebrief.store import Store

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "prebrief" / "hook.py"


class HookCliIntegration(unittest.TestCase):
    def _env(self, db: Path, project: str) -> dict[str, str]:
        env = os.environ.copy()
        env.update({
            "PREBRIEF_DB": str(db),
            "PREBRIEF_PROJECT": project,
            "PYTHONIOENCODING": "utf-8",
        })
        old = env.get("PYTHONPATH")
        env["PYTHONPATH"] = str(ROOT) if not old else str(ROOT) + os.pathsep + old
        return env

    def _run_hook(
        self, db: Path, cwd: Path, project: str, session: str,
    ) -> subprocess.CompletedProcess[str]:
        event = {
            "hook_event_name": "SessionStart",
            "session_id": session,
            "cwd": str(cwd),
        }
        return subprocess.run(
            [sys.executable, str(HOOK)], input=json.dumps(event),
            cwd=str(cwd), env=self._env(db, project),
            capture_output=True, text=True, encoding="utf-8",
            timeout=30, check=False,
        )

    def test_full_session_and_project_identity(self) -> None:
        with tempfile.TemporaryDirectory(prefix="prebrief-hook-") as temp:
            root = Path(temp)
            project_a, project_b = root / "project-a", root / "project-b"
            project_a.mkdir()
            project_b.mkdir()
            db = root / "hook.db"
            # Different only AFTER the old 36-character truncation boundary.
            first, second = "12345678" + "a" * 32 + "-one", "12345678" + "a" * 32 + "-two"
            cases = (
                (project_a, "tenant-a", first),
                (project_a, "tenant-a", second),
                (project_b, "tenant-b", first),
            )
            for cwd, project, session in cases:
                result = self._run_hook(db, cwd, project, session)
                self.assertEqual(result.returncode, 0, result.stderr)
                output = json.loads(result.stdout)["hookSpecificOutput"]
                self.assertEqual(output["hookEventName"], "SessionStart")
                self.assertIn(f"project '{project}'", output["additionalContext"])

            store = Store(str(db))
            try:
                events = store.execute_strict(
                    "SELECT actor, project, session FROM events "
                    "WHERE kind='session.start'"
                )
                self.assertEqual(len(events), 3)
                self.assertEqual({(row[1], row[2]) for row in events}, {
                    (project, session) for _, project, session in cases
                })
                agents = {row[0] for row in events}
                self.assertEqual(len(agents), 3)
                self.assertEqual(
                    {row[0] for row in store.execute_strict(
                        "SELECT agent_id FROM awareness")}, agents,
                )
                self.assertEqual(
                    {row[0] for row in store.execute_strict(
                        "SELECT DISTINCT agent_id FROM delivery")}, agents,
                )
            finally:
                store.close()

    def test_bare_hook_script_uses_cwd_without_override(self) -> None:
        with tempfile.TemporaryDirectory(prefix="prebrief-cwd-") as temp:
            root = Path(temp)
            project = root / "cwd-project"
            project.mkdir()
            result = self._run_hook(root / "hook.db", project, "", "cwd-session")
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
            self.assertIn("project 'cwd-project'", payload)

    def test_enrolled_command_uses_current_python_and_runs_unchanged(self) -> None:
        with tempfile.TemporaryDirectory(prefix="prebrief-enroll-") as temp:
            root = Path(temp)
            project = root / "project with spaces"
            project.mkdir()
            db = root / "enrolled.db"
            env = self._env(db, "enrolled-tenant")
            enrolled = subprocess.run(
                [sys.executable, "-m", "prebrief.cli", "enroll", str(project)],
                cwd=str(ROOT), env=env, capture_output=True,
                text=True, encoding="utf-8", timeout=30, check=False,
            )
            self.assertEqual(enrolled.returncode, 0, enrolled.stderr)
            settings = json.loads(
                (project / ".claude" / "settings.json").read_text(encoding="utf-8")
            )
            command = settings["hooks"]["SessionStart"][0]["hooks"][0]["command"]
            self.assertTrue(command.startswith(f'"{sys.executable}" '))
            self.assertIn("hook.py", command)
            event = {
                "hook_event_name": "SessionStart",
                "session_id": "enrolled-session",
                "cwd": str(project),
            }
            invoked = subprocess.run(
                command, input=json.dumps(event), cwd=str(project), env=env,
                shell=True, capture_output=True, text=True, encoding="utf-8",
                timeout=30, check=False,
            )
            self.assertEqual(invoked.returncode, 0, invoked.stderr)
            context = json.loads(invoked.stdout)["hookSpecificOutput"]["additionalContext"]
            self.assertIn("project 'enrolled-tenant'", context)

    def _run_doctor(
        self, db: Path, env_db: Path,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "prebrief.cli", "doctor",
             "--db", str(db), "--json"],
            cwd=str(ROOT), env=self._env(env_db, "doctor-tests"),
            capture_output=True, text=True, encoding="utf-8",
            timeout=30, check=False,
        )

    def _make_corrupt_cursor(self, db: Path) -> None:
        store = Store(str(db))
        try:
            store.execute_strict(
                "CREATE TABLE projector_state ("
                "id INTEGER PRIMARY KEY CHECK (id=1), "
                "last_event_id INTEGER NOT NULL, updated_at REAL)"
            )
            store.execute_strict(
                "INSERT INTO projector_state "
                "(id, last_event_id, updated_at) VALUES (1, 99, 0)"
            )
            self.assertEqual(store.execute_strict(
                "SELECT id, last_event_id FROM projector_state"), [(1, 99)])
        finally:
            store.close()

    def test_doctor_honors_db_and_propagates_errors(self) -> None:
        with tempfile.TemporaryDirectory(prefix="prebrief-doctor-") as temp:
            root = Path(temp)
            clean, corrupt, broken = root / "clean.db", root / "corrupt.db", root / "broken.db"
            Store(str(clean)).close()
            self._make_corrupt_cursor(corrupt)
            broken.write_bytes(b"not a sqlite database")

            clean_result = self._run_doctor(clean, corrupt)
            self.assertEqual(clean_result.returncode, 0, clean_result.stderr)
            self.assertEqual(json.loads(clean_result.stdout)["status"], "ok")

            for path, check_name in (
                (corrupt, "projector.cursor"), (broken, "store.open"),
            ):
                result = self._run_doctor(path, clean)
                self.assertEqual(result.returncode, 1, result.stderr)
                report = json.loads(result.stdout)
                self.assertEqual(report["status"], "error")
                checks = {check["check"]: check for check in report["checks"]}
                self.assertEqual(checks[check_name]["status"], "error")

    def test_doctor_exception_returns_one(self) -> None:
        with mock.patch.object(cli, "cmd_doctor", side_effect=RuntimeError("doctor fault")):
            self.assertEqual(cli.main(["doctor"]), 1)

    def test_doctor_uses_installed_metadata_without_source_tree(self) -> None:
        store = Store(":memory:")
        try:
            with mock.patch("os.path.isfile", return_value=False), mock.patch(
                "importlib.metadata.version", return_value=__version__,
            ):
                checks = {check["check"]: check for check in doctor.run(store)}
            self.assertEqual(checks["release.version"]["status"], "ok")
            self.assertIn("installed distribution", checks["release.version"]["detail"])
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()

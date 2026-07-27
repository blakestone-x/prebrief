"""Tenant identity must not depend on which OS observed the path.

The bug this guards: `os.path.basename` uses the HOST separator, so a
Windows-captured cwd (`C:\\dev\\prebrief`) read on Linux produced the tenant
label `Cdevprebrief` instead of `prebrief`. Same repository, two identities,
depending on the reader — which silently splits a project's history and breaks
isolation on a synced fleet. It also turned CI red on all four POSIX lanes.

Second concern: a basename is not a stable ID. Two repos named `web` under
different parents collide, and renaming a directory re-tenants its history.
An explicit label (env var or `.prebrief-project` file) always wins.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from prebrief.store import project_from_path


class ProjectIdentity(unittest.TestCase):
    def test_windows_path_on_any_host(self):
        self.assertEqual(project_from_path("C:" + chr(92) + "dev" + chr(92) + "prebrief"),
                         "prebrief")

    def test_posix_path_on_any_host(self):
        self.assertEqual(project_from_path("/home/runner/work/prebrief"), "prebrief")

    def test_mixed_separators(self):
        self.assertEqual(project_from_path("D:/repos" + chr(92) + "web"), "web")

    def test_spaces_are_sanitized_not_split(self):
        p = "C:" + chr(92) + "Users" + chr(92) + "B" + chr(92) + "Claude Workspace"
        self.assertEqual(project_from_path(p), "ClaudeWorkspace")

    def test_trailing_separator(self):
        self.assertEqual(project_from_path("/c/dev/forge/"), "forge")

    def test_bare_drive_and_empty_fall_back(self):
        self.assertEqual(project_from_path("C:" + chr(92)), "default")
        self.assertEqual(project_from_path(""), "default")
        self.assertEqual(project_from_path(None), "default")

    def test_explicit_label_wins(self):
        self.assertEqual(project_from_path("/anything/at/all", explicit="billing"),
                         "billing")

    def test_env_override(self):
        os.environ["PREBRIEF_PROJECT"] = "from-env"
        try:
            self.assertEqual(project_from_path("/some/repo"), "from-env")
        finally:
            del os.environ["PREBRIEF_PROJECT"]

    def test_marker_file_gives_a_stable_id_across_renames(self):
        d = tempfile.mkdtemp()
        with open(os.path.join(d, ".prebrief-project"), "w", encoding="utf-8") as f:
            f.write("stable-id\n")
        self.assertEqual(project_from_path(d), "stable-id")

    def test_identity_is_reader_independent(self):
        """The property that actually matters: one repo, one identity."""
        win = "C:" + chr(92) + "dev" + chr(92) + "myrepo"
        posix = "/c/dev/myrepo"
        self.assertEqual(project_from_path(win), project_from_path(posix))


if __name__ == "__main__":
    unittest.main()

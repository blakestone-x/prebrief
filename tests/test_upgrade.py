"""Upgrade path: an EXISTING database must reach the current schema.

Class-level guard, not an instance fix. v0.2.1 shipped `delivery.used_at` in
the CREATE TABLE but not in the migration tuple, so every fresh install had the
column and every upgrade silently did not — usage closure was dead on exactly
the installs that had history worth measuring. These tests fail the moment the
schema and the migration list drift again, for ANY column.
"""
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from prebrief import Store
from prebrief.store import declared_columns, migration_gaps

# the v0.1.0 shipped schema, verbatim — what a real legacy database looks like
LEGACY = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, hash TEXT UNIQUE, ts REAL,
    kind TEXT, actor TEXT, session TEXT, payload TEXT);
CREATE TABLE IF NOT EXISTS plan_node (
    id INTEGER PRIMARY KEY AUTOINCREMENT, root_id INTEGER, kind TEXT,
    title TEXT, status TEXT DEFAULT 'open', owner TEXT);
CREATE TABLE IF NOT EXISTS decision (
    id INTEGER PRIMARY KEY AUTOINCREMENT, scope_root INTEGER, subject TEXT,
    choice TEXT, rationale TEXT, binds TEXT, status TEXT DEFAULT 'standing');
CREATE TABLE IF NOT EXISTS awareness (
    agent_id TEXT PRIMARY KEY, role TEXT, task_head TEXT, files_hot TEXT,
    status TEXT DEFAULT 'active', updated_at REAL);
CREATE TABLE IF NOT EXISTS delivery (
    agent_id TEXT, item_ref TEXT, watermark INTEGER, delivered_at REAL,
    PRIMARY KEY (agent_id, item_ref));
CREATE TABLE IF NOT EXISTS tool_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, session TEXT, tool TEXT, path TEXT,
    is_error INTEGER DEFAULT 0, ts REAL);
"""


def legacy_db():
    path = os.path.join(tempfile.mkdtemp(), "legacy.db")
    con = sqlite3.connect(path)
    con.executescript(LEGACY)
    con.execute("INSERT INTO delivery (agent_id,item_ref,watermark,delivered_at) "
                "VALUES ('old','build:1',5,1.0)")
    con.commit()
    con.close()
    return path


class UpgradePath(unittest.TestCase):
    def test_no_declared_column_is_unmigrated(self):
        """The guard: every column added since v0.1.0 must have a migration."""
        self.assertEqual(migration_gaps(), [],
                         "schema declares columns no migration adds to a legacy DB")

    def test_legacy_db_gains_every_current_column(self):
        s = Store(legacy_db())
        for table, col in declared_columns():
            cols = [r[1] for r in s.sql(f"PRAGMA table_info({table})")]
            if not cols:
                continue          # table created lazily by another component
            self.assertIn(col, cols, f"{table}.{col} missing after upgrade")

    def test_used_at_specifically(self):
        """The regression that shipped: usage closure needs this column."""
        s = Store(legacy_db())
        cols = [r[1] for r in s.sql("PRAGMA table_info(delivery)")]
        self.assertIn("used_at", cols)

    def test_legacy_rows_survive_upgrade(self):
        s = Store(legacy_db())
        rows = s.sql("SELECT agent_id, item_ref FROM delivery")
        self.assertEqual(rows[0][0], "old")

    def test_upgrade_is_idempotent(self):
        p = legacy_db()
        Store(p)
        s2 = Store(p)          # second open must not fail or duplicate
        self.assertIn("used_at", [r[1] for r in s2.sql("PRAGMA table_info(delivery)")])

    def test_version_metadata_agrees(self):
        """Release mechanics: the tag, the package, and the citation must match."""
        import re
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        import prebrief
        pyproj = open(os.path.join(root, "pyproject.toml"), encoding="utf-8").read()
        cite = open(os.path.join(root, "CITATION.cff"), encoding="utf-8").read()
        pv = re.search(r'version\s*=\s*"([^"]+)"', pyproj).group(1)
        cv = re.search(r"^version:\s*(\S+)", cite, re.M).group(1)
        self.assertEqual(prebrief.__version__, pv, "pyproject vs __version__")
        self.assertEqual(prebrief.__version__, cv, "CITATION.cff vs __version__")


if __name__ == "__main__":
    unittest.main()

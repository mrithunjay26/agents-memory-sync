import os
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

import store
from claude_memory import encode_claude_project_path


class PlatformAndMigrationTestCase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        os.environ["AGENT_MEMORY_DB_PATH"] = os.path.join(self._tmpdir.name, "store.db")

    def tearDown(self):
        os.environ.pop("AGENT_MEMORY_DB_PATH", None)
        self._tmpdir.cleanup()

    def test_claude_project_encoding_never_contains_path_separators(self):
        encoded = encode_claude_project_path(os.path.join(os.sep, "Repo", "Nested"))
        self.assertNotIn("/", encoded)
        self.assertNotIn("\\", encoded)

    def test_schema_migrations_are_versioned(self):
        self.assertEqual(store.schema_version(), store.LATEST_SCHEMA_VERSION)
        conn = sqlite3.connect(store.db_path())
        try:
            versions = [
                row[0]
                for row in conn.execute(
                    "SELECT version FROM schema_migrations ORDER BY version"
                )
            ]
        finally:
            conn.close()
        self.assertEqual(versions, list(range(1, store.LATEST_SCHEMA_VERSION + 1)))

    def test_connections_enable_wal_and_busy_timeout(self):
        conn = store._connect()
        try:
            self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            self.assertEqual(conn.execute("PRAGMA busy_timeout").fetchone()[0], 30000)
        finally:
            conn.close()

    def test_first_run_migrations_are_safe_for_concurrent_hooks(self):
        barrier = threading.Barrier(2)
        errors = []

        def connect():
            try:
                barrier.wait()
                conn = store._connect()
                conn.close()
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=connect) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(store.schema_version(), store.LATEST_SCHEMA_VERSION)

    def test_posix_path_migration_does_not_lowercase_paths(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE projects (project_path TEXT PRIMARY KEY, added_at TEXT)")
        conn.execute("INSERT INTO projects VALUES ('/Repo/MixedCase', 'now')")
        with patch.object(store.os, "name", "posix"):
            store._migration_2_windows_path_casing(conn)
        path = conn.execute("SELECT project_path FROM projects").fetchone()[0]
        conn.close()
        self.assertEqual(path, "/Repo/MixedCase")


if __name__ == "__main__":
    unittest.main()

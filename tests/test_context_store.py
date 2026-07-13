import importlib
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class ContextStoreTestCase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._db_path = os.path.join(self._tmpdir.name, "store.db")
        os.environ["AGENT_MEMORY_DB_PATH"] = self._db_path
        global store
        import store as store
        importlib.reload(store)
        self.store = store
        self.project = r"c:\fake\project"

    def tearDown(self):
        self._tmpdir.cleanup()
        os.environ.pop("AGENT_MEMORY_DB_PATH", None)

    def test_new_events_default_to_included_unpinned_no_overlay(self):
        self.store.record_event(self.project, "codex", "s1", "turn", "did a thing")
        bundle = self.store.get_context_bundle(self.project)
        entry = bundle["entries"][0]
        self.assertTrue(entry["included"])
        self.assertFalse(entry["pinned"])
        self.assertIsNone(entry["context_summary"])
        self.assertEqual(entry["effective_summary"], "did a thing")
        self.assertEqual(entry["category"], "activity")
        self.assertEqual(entry["category_source"], "automatic")

    def test_category_override_and_reset_preserve_event(self):
        self.store.record_event(self.project, "codex", "s1", "turn", "implemented graph view")
        event_id = self.store.get_context_bundle(self.project)["entries"][0]["id"]
        automatic = self.store.get_context_bundle(self.project)["entries"][0]
        self.assertEqual(automatic["category"], "artifact")

        self.store.update_context_event(self.project, event_id, category="decision")
        manual = self.store.get_context_bundle(self.project)["entries"][0]
        self.assertEqual(manual["category"], "decision")
        self.assertEqual(manual["category_source"], "manual")
        self.assertEqual(manual["summary"], "implemented graph view")

        self.store.update_context_event(self.project, event_id, reset_category=True)
        reset = self.store.get_context_bundle(self.project)["entries"][0]
        self.assertEqual(reset["category"], "artifact")
        self.assertEqual(reset["category_source"], "automatic")

    def test_invalid_category_rejected(self):
        self.store.record_event(self.project, "codex", "s1", "turn", "work")
        event_id = self.store.get_context_bundle(self.project)["entries"][0]["id"]
        with self.assertRaises(ValueError):
            self.store.update_context_event(self.project, event_id, category="mystery")

    def test_legacy_events_table_migrates_with_context_defaults(self):
        conn = sqlite3.connect(self._db_path)
        conn.execute(
            "CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "project_path TEXT NOT NULL, agent TEXT NOT NULL, session_id TEXT, "
            "event_type TEXT NOT NULL, summary TEXT NOT NULL, created_at TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO events (project_path, agent, session_id, event_type, summary, created_at) "
            "VALUES (?, 'codex', 's1', 'turn', 'legacy event', '2026-01-01T00:00:00+00:00')",
            (self.project,),
        )
        conn.commit()
        conn.close()

        entry = self.store.get_context_bundle(self.project)["entries"][0]
        self.assertTrue(entry["included"])
        self.assertFalse(entry["pinned"])
        self.assertIsNone(entry["context_summary"])

    def test_telemetry_events_excluded_from_bundle_and_preview(self):
        self.store.record_event(self.project, "claude-code", "s1", "turn", "real work")
        self.store.record_event(
            self.project, "claude-code", "s1", self.store.TELEMETRY_EVENT_TYPE, '{"tokens_estimate": 42}'
        )
        bundle = self.store.get_context_bundle(self.project)
        self.assertEqual(len(bundle["entries"]), 1)
        self.assertNotIn("tokens_estimate", bundle["preview"])
        self.assertNotIn(self.store.TELEMETRY_EVENT_TYPE, bundle["preview"])

    def test_get_context_matches_bundle_preview(self):
        self.store.record_event(self.project, "codex", "s1", "turn", "hello")
        self.assertEqual(
            self.store.get_context(self.project), self.store.get_context_bundle(self.project)["preview"]
        )

    def test_exclude_removes_entry_from_preview_but_keeps_it_in_entries(self):
        self.store.record_event(self.project, "codex", "s1", "turn", "hide me")
        event_id = self.store.get_context_bundle(self.project)["entries"][0]["id"]
        self.store.update_context_event(self.project, event_id, included=False)
        bundle = self.store.get_context_bundle(self.project)
        self.assertNotIn("hide me", bundle["preview"])
        self.assertEqual(bundle["counts"]["excluded"], 1)
        self.assertEqual(bundle["counts"]["included"], 0)
        self.assertEqual(len(bundle["entries"]), 1)

    def test_pinned_entries_survive_beyond_recent_limit(self):
        self.store.record_event(self.project, "codex", "s1", "turn", "old pinned event")
        pinned_id = self.store.get_context_bundle(self.project)["entries"][0]["id"]
        self.store.update_context_event(self.project, pinned_id, pinned=True)
        for i in range(5):
            self.store.record_event(self.project, "codex", "s1", "turn", f"filler {i}")

        bundle = self.store.get_context_bundle(self.project, limit=2)
        self.assertIn("old pinned event", bundle["preview"])
        self.assertEqual(bundle["counts"]["pinned"], 1)
        self.assertEqual(bundle["counts"]["included"], 3)
        self.assertIn("filler 3", bundle["preview"])
        self.assertIn("filler 4", bundle["preview"])
        self.assertNotIn("filler 0", bundle["preview"])

    def test_edited_summary_overlays_preview_without_mutating_original(self):
        self.store.record_event(self.project, "codex", "s1", "turn", "original summary")
        event_id = self.store.get_context_bundle(self.project)["entries"][0]["id"]
        self.store.update_context_event(self.project, event_id, context_summary="edited summary")

        bundle = self.store.get_context_bundle(self.project)
        entry = bundle["entries"][0]
        self.assertEqual(entry["summary"], "original summary")
        self.assertEqual(entry["context_summary"], "edited summary")
        self.assertEqual(entry["effective_summary"], "edited summary")
        self.assertIn("edited summary", bundle["preview"])
        self.assertNotIn("original summary", bundle["preview"])

    def test_reset_summary_reverts_to_original(self):
        self.store.record_event(self.project, "codex", "s1", "turn", "original summary")
        event_id = self.store.get_context_bundle(self.project)["entries"][0]["id"]
        self.store.update_context_event(self.project, event_id, context_summary="edited summary")
        self.store.update_context_event(self.project, event_id, reset_summary=True)

        bundle = self.store.get_context_bundle(self.project)
        entry = bundle["entries"][0]
        self.assertIsNone(entry["context_summary"])
        self.assertEqual(entry["effective_summary"], "original summary")

    def test_context_summary_over_limit_rejected(self):
        self.store.record_event(self.project, "codex", "s1", "turn", "x")
        event_id = self.store.get_context_bundle(self.project)["entries"][0]["id"]
        too_long = "a" * (self.store.MAX_CONTEXT_SUMMARY_CHARS + 1)
        with self.assertRaises(ValueError):
            self.store.update_context_event(self.project, event_id, context_summary=too_long)

    def test_update_context_event_rejects_wrong_project(self):
        self.store.record_event(self.project, "codex", "s1", "turn", "x")
        event_id = self.store.get_context_bundle(self.project)["entries"][0]["id"]
        with self.assertRaises(LookupError):
            self.store.update_context_event(r"c:\other\project", event_id, included=False)

    def test_manual_note_appears_as_context_note_from_user(self):
        note_id = self.store.create_context_note(self.project, "keep an eye on the auth flow")
        bundle = self.store.get_context_bundle(self.project)
        entry = bundle["entries"][0]
        self.assertEqual(entry["id"], note_id)
        self.assertEqual(entry["event_type"], self.store.CONTEXT_NOTE_EVENT_TYPE)
        self.assertEqual(entry["agent"], "user")
        self.assertEqual(entry["category"], "note")
        self.assertIn("keep an eye on the auth flow", bundle["preview"])

    def test_empty_note_rejected(self):
        with self.assertRaises(ValueError):
            self.store.create_context_note(self.project, "   ")

    def test_delete_context_note_removes_it(self):
        note_id = self.store.create_context_note(self.project, "temporary note")
        self.store.delete_context_note(self.project, note_id)
        bundle = self.store.get_context_bundle(self.project)
        self.assertEqual(bundle["entries"], [])

    def test_delete_raw_event_rejected(self):
        self.store.record_event(self.project, "codex", "s1", "turn", "a real event")
        event_id = self.store.get_context_bundle(self.project)["entries"][0]["id"]
        with self.assertRaises(PermissionError):
            self.store.delete_context_note(self.project, event_id)
        self.assertEqual(len(self.store.get_context_bundle(self.project)["entries"]), 1)

    def test_recent_limit_setting_persists_and_is_used_by_default(self):
        for i in range(5):
            self.store.record_event(self.project, "codex", "s1", "turn", f"event {i}")
        self.store.update_context_settings(self.project, 2)
        bundle = self.store.get_context_bundle(self.project)
        self.assertEqual(bundle["settings"]["recent_limit"], 2)
        self.assertEqual(bundle["counts"]["included"], 2)

    def test_native_history_is_upserted_and_keeps_raw_corpus_metrics(self):
        inserted = self.store.record_history_event(
            self.project, "claude-code", "native-1", "full transcript v1", 799_000
        )
        self.assertTrue(inserted)
        history_id = self.store.get_context_bundle(self.project)["entries"][0]["id"]
        self.store.update_context_event(self.project, history_id, pinned=True)

        inserted = self.store.record_history_event(
            self.project, "claude-code", "native-1", "full transcript v2", 800_000
        )
        self.assertFalse(inserted)
        for i in range(5):
            self.store.record_event(self.project, "codex", "live", "turn", f"live {i}")

        bundle = self.store.get_context_bundle(self.project, limit=1)
        history = next(entry for entry in bundle["entries"] if entry["id"] == history_id)
        self.assertEqual(history["summary"], "full transcript v2")
        self.assertEqual(history["source_tokens"], 800_000)
        self.assertTrue(history["pinned"])
        self.assertIn("full transcript v2", bundle["preview"])
        self.assertEqual(bundle["native_usage_tokens"], 800_000)
        self.assertEqual(history["context_tokens"], (len("full transcript v2") + 3) // 4)
        self.assertGreaterEqual(bundle["source_tokens"], history["context_tokens"])
        self.assertTrue(history["has_raw_context"])

    def test_native_usage_as_of_reconstructs_historical_pool_size(self):
        self.store.record_history_event(
            self.project, "codex", "native-1", "full transcript", 500_000
        )
        conn = sqlite3.connect(self._db_path)
        conn.execute(
            "UPDATE events SET created_at = ? WHERE project_path = ?",
            ("2020-01-01T00:00:00+00:00", self.project),
        )
        conn.commit()
        conn.close()

        before = self.store.native_usage_tokens_as_of(self.project, "2019-01-01T00:00:00+00:00")
        after = self.store.native_usage_tokens_as_of(self.project, "2021-01-01T00:00:00+00:00")
        self.assertEqual(before, 0)
        self.assertEqual(after, 500_000)

    def test_unpinned_history_obeys_working_set_limit_but_remains_searchable(self):
        self.store.record_history_event(
            self.project, "codex", "native-1", "native evidence", 1234
        )
        for i in range(4):
            self.store.record_event(self.project, "codex", "live", "turn", f"activity {i}")
        bundle = self.store.get_context_bundle(self.project, limit=1)
        self.assertNotIn("native evidence", bundle["preview"])
        self.assertIn("activity 3", bundle["preview"])
        self.assertNotIn("activity 2", bundle["preview"])
        self.assertEqual(bundle["counts"]["history"], 1)
        matches = self.store.search_shared_context(self.project, "native evidence")
        self.assertEqual(len(matches), 1)
        self.assertIn("native evidence", matches[0]["snippet"])

    def test_corpus_search_uses_ranked_terms_not_only_an_exact_phrase(self):
        self.store.record_history_event(
            self.project,
            "codex",
            "older-best",
            "SQLite failed because the database directory was not writable.",
            1234,
        )
        self.store.record_history_event(
            self.project,
            "claude-code",
            "newer-partial",
            "A database migration completed.",
            1234,
        )

        matches = self.store.search_shared_context(
            self.project, "sqlite database writable", limit=2
        )

        self.assertEqual(len(matches), 2)
        self.assertEqual(matches[0]["agent"], "codex")
        self.assertIn("not writable", matches[0]["snippet"])

    def test_corpus_search_treats_like_wildcards_as_literal_text(self):
        self.store.record_history_event(
            self.project, "codex", "percent", "progress reached 100%", 10
        )
        self.store.record_history_event(
            self.project, "claude-code", "plain", "unrelated context", 10
        )

        matches = self.store.search_shared_context(self.project, "100%")

        self.assertEqual([match["agent"] for match in matches], ["codex"])

    def test_corpus_search_does_not_require_the_write_connection(self):
        self.store.record_history_event(
            self.project, "codex", "readonly", "sandbox corpus evidence", 10
        )
        with mock.patch.object(
            self.store,
            "_connect",
            side_effect=sqlite3.OperationalError("unable to open database file"),
        ) as write_connect:
            matches = self.store.search_shared_context(
                self.project, "sandbox evidence"
            )

        write_connect.assert_not_called()
        self.assertEqual(len(matches), 1)
        self.assertIn("sandbox corpus evidence", matches[0]["snippet"])

    def test_corpus_search_matches_path_like_identifiers_as_single_tokens(self):
        self.store.record_history_event(
            self.project, "codex", "path-1", "the bug is in store.py:867", 10
        )
        self.store.record_history_event(
            self.project, "codex", "path-2", "unrelated context", 10
        )

        matches = self.store.search_shared_context(self.project, "store.py:867")

        self.assertEqual([match["agent"] for match in matches], ["codex"])

    def test_corpus_search_index_follows_edits_and_deletes(self):
        note_id = self.store.create_context_note(self.project, "zzqux marker text")
        self.assertEqual(
            len(self.store.search_shared_context(self.project, "zzqux")), 1
        )

        conn = sqlite3.connect(self._db_path)
        conn.execute(
            "UPDATE events SET summary = ? WHERE id = ?",
            ("yyquux only now", note_id),
        )
        conn.commit()
        conn.close()
        self.assertEqual(
            self.store.search_shared_context(self.project, "zzqux"), []
        )
        self.assertEqual(
            len(self.store.search_shared_context(self.project, "yyquux")), 1
        )

        self.store.delete_context_note(self.project, note_id)
        self.assertEqual(
            self.store.search_shared_context(self.project, "yyquux"), []
        )

    def test_semantic_recall_finds_morphological_variant_missed_by_lexical_search(self):
        self.store.record_history_event(
            self.project,
            "codex",
            "migration-note",
            "The nightly job finished a full database migration without errors.",
            10,
        )
        self.store.record_history_event(
            self.project,
            "claude-code",
            "unrelated-note",
            "Renamed the login button and updated the icon color.",
            10,
        )

        matches = self.store.search_shared_context(self.project, "migrating")

        self.assertEqual([match["agent"] for match in matches], ["codex"])

    def test_semantic_leg_does_not_resurrect_unrelated_events(self):
        self.store.record_history_event(
            self.project, "codex", "unrelated-1", "Renamed the login button icon.", 10
        )

        self.assertEqual(
            self.store.search_shared_context(self.project, "migrating"), []
        )

    def test_lexical_hit_outranks_semantic_only_hit(self):
        self.store.record_history_event(
            self.project, "codex", "exact", "database migration completed successfully", 10
        )
        self.store.record_history_event(
            self.project, "claude-code", "semantic-only", "migrating data between shards", 10
        )

        matches = self.store.search_shared_context(
            self.project, "database migration completed", limit=2
        )

        self.assertEqual(matches[0]["agent"], "codex")

    def test_context_visibility_is_agent_neutral(self):
        self.store.record_event(self.project, "claude-code", "c1", "turn", "claude fact")
        self.store.record_event(self.project, "codex", "x1", "turn", "codex fact")
        bundle = self.store.get_context_bundle(self.project)
        self.assertEqual(bundle["visible_to"], ["claude-code", "codex"])
        self.assertEqual(bundle["counts"]["exclusive"], 0)
        self.assertTrue(bundle["sharing_policy"]["working_set_identical_for_all_agents"])
        self.assertFalse(bundle["sharing_policy"]["agent_specific_context"])
        self.assertIn("claude fact", bundle["preview"])
        self.assertIn("codex fact", bundle["preview"])

    def test_recent_limit_out_of_bounds_rejected(self):
        with self.assertRaises(ValueError):
            self.store.update_context_settings(self.project, 0)
        with self.assertRaises(ValueError):
            self.store.update_context_settings(self.project, self.store.MAX_RECENT_LIMIT + 1)

    def test_rendering_is_oldest_to_newest_and_unchanged_without_pins(self):
        self.store.record_event(self.project, "codex", "s1", "turn", "first")
        self.store.record_event(self.project, "claude-code", "s2", "turn", "second")
        preview = self.store.get_context(self.project)
        self.assertLess(preview.index("first"), preview.index("second"))
        self.assertTrue(preview.startswith(self.store.CONTEXT_HEADER))
        self.assertNotIn("Pinned:", preview)

    def test_content_hash_changes_when_preview_changes(self):
        self.store.record_event(self.project, "codex", "s1", "turn", "hello")
        bundle_a = self.store.get_context_bundle(self.project)
        self.store.record_event(self.project, "codex", "s1", "turn", "world")
        bundle_b = self.store.get_context_bundle(self.project)
        self.assertNotEqual(bundle_a["content_hash"], bundle_b["content_hash"])

    def test_rendered_context_has_a_hard_safety_limit(self):
        self.store.record_event(
            self.project,
            "codex",
            "s1",
            "turn",
            "x" * (self.store.MAX_RENDERED_CONTEXT_CHARS + 1000),
        )
        bundle = self.store.get_context_bundle(self.project)
        self.assertTrue(bundle["truncated"])
        self.assertLessEqual(len(bundle["preview"]), self.store.MAX_RENDERED_CONTEXT_CHARS)
        self.assertIn("Context truncated", bundle["preview"])


if __name__ == "__main__":
    unittest.main()

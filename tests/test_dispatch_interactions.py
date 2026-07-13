import os
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient

import app as app_module
import dispatch
import store


class StoreIsolationMixin:
    def setUp(self):
        super().setUp()
        self.store_tmpdir = tempfile.TemporaryDirectory()
        self.previous_db_path = os.environ.get("AGENT_MEMORY_DB_PATH")
        os.environ["AGENT_MEMORY_DB_PATH"] = os.path.join(self.store_tmpdir.name, "store.db")

    def tearDown(self):
        if self.previous_db_path is None:
            os.environ.pop("AGENT_MEMORY_DB_PATH", None)
        else:
            os.environ["AGENT_MEMORY_DB_PATH"] = self.previous_db_path
        self.store_tmpdir.cleanup()
        super().tearDown()


class DispatchInteractionStoreTests(StoreIsolationMixin, unittest.TestCase):
    def test_pending_interaction_can_be_listed_and_resolved_once(self):
        store.create_dispatch_job("job-1", "/repo", "codex", "task", True)
        created = store.create_dispatch_interaction(
            "interaction-1",
            "job-1",
            "/repo",
            "codex",
            "approval",
            "May I publish the release?",
            ["Approve", "Deny", "Approve"],
        )

        self.assertEqual(created["options"], ["Approve", "Deny"])
        self.assertEqual(
            [item["id"] for item in store.list_dispatch_interactions(pending_only=True)],
            ["interaction-1"],
        )

        answered = store.resolve_dispatch_interaction("interaction-1", "Approve")

        self.assertEqual(answered["status"], "answered")
        self.assertEqual(answered["response"], "Approve")
        self.assertEqual(store.list_dispatch_interactions(pending_only=True), [])
        with self.assertRaises(ValueError):
            store.resolve_dispatch_interaction("interaction-1", "again")


class DispatchInteractionParsingTests(StoreIsolationMixin, unittest.TestCase):
    def test_marker_is_removed_and_approval_gets_safe_defaults(self):
        result = (
            "I completed the local checks.\n"
            '<agentmemorysync_interaction>{"kind":"approval","prompt":"Deploy now?","options":[]}'
            "</agentmemorysync_interaction>"
        )

        clean, interaction = dispatch._extract_interaction(result)

        self.assertEqual(clean, "I completed the local checks.")
        self.assertEqual(interaction["kind"], "approval")
        self.assertEqual(interaction["options"], ["Approve", "Deny"])

    def test_native_claude_question_is_captured(self):
        state = {}
        event = {
            "type": "assistant",
            "session_id": "session-1",
            "message": {
                "content": [{
                    "type": "tool_use",
                    "name": "AskUserQuestion",
                    "input": {
                        "questions": [{
                            "header": "Database",
                            "question": "Which engine should we use?",
                            "options": [{"label": "SQLite"}, {"label": "Postgres"}],
                        }]
                    },
                }]
            },
        }

        entries = dispatch._claude_events(event, state)

        self.assertEqual(state["interaction"]["prompt"], "Database: Which engine should we use?")
        self.assertEqual(state["interaction"]["options"], ["SQLite", "Postgres"])
        self.assertTrue(any(entry["k"] == "interaction" for entry in entries))

    def test_numbered_proposal_ending_in_choice_becomes_dashboard_interaction(self):
        result = (
            "Three ways to improve it:\n\n"
            "1. **Funnel visualization**: show the measured shrink.\n"
            "2. **Example walkthrough**: ground the aggregate in one run.\n"
            "3. **Coverage indicator**: disclose measured delivery coverage.\n\n"
            "Want me to implement any of these?"
        )

        clean, interaction = dispatch._extract_interaction(result)

        self.assertEqual(clean, result)
        self.assertEqual(interaction["kind"], "question")
        self.assertEqual(
            interaction["options"],
            ["Funnel visualization", "Example walkthrough", "Coverage indicator"],
        )
        self.assertIn("show the measured shrink", interaction["prompt"])

    def test_numbered_summary_without_selection_question_does_not_pause(self):
        result = "1. Updated storage\n2. Added tests\n\nAnything else?"

        clean, interaction = dispatch._extract_interaction(result)

        self.assertEqual(clean, result)
        self.assertIsNone(interaction)


class DispatchInteractionApiTests(StoreIsolationMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        store.register_project("/repo")
        store.create_dispatch_job("job-1", "/repo", "claude-code", "task", False)
        store.create_dispatch_interaction(
            "interaction-1", "job-1", "/repo", "claude-code", "question", "Choose?", ["A", "B"]
        )
        app_module.app.dependency_overrides[app_module.require_auth] = lambda: "tester"
        self.client = TestClient(app_module.app)

    def tearDown(self):
        app_module.app.dependency_overrides.clear()
        self.client.close()
        super().tearDown()

    def test_response_endpoint_resolves_and_resumes_job(self):
        resume = AsyncMock()
        with patch.object(app_module, "resume_dispatch_job", resume):
            response = self.client.post(
                "/api/interactions/interaction-1/respond", json={"response": "B"}
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["response"], "B")
        resume.assert_awaited_once_with("job-1", "B")


class ResumeFallbackTests(StoreIsolationMixin, unittest.IsolatedAsyncioTestCase):
    async def test_dispatch_activity_updates_progress_until_completion(self):
        store.create_dispatch_job("job-1", "/repo", "codex", "task", True)

        async def fake_dispatch(
            project_path,
            prompt,
            allow_edits,
            on_log,
            env=None,
            resume_session_id=None,
            model=None,
        ):
            on_log({"k": "session", "t": "session-1"})
            on_log({"k": "reason", "t": "Inspecting"})
            on_log({"k": "cmd", "t": "run tests"})
            on_log({"k": "tool", "t": "edit file"})
            return "Completed the task.", 9

        adapter = Mock(dispatch=fake_dispatch)
        with patch.object(dispatch, "get_agent_adapter", return_value=adapter):
            await dispatch._run_dispatch_turn("job-1", "/repo", "codex", "task", True)

        job = store.get_dispatch_job("job-1")
        self.assertEqual(job["status"], "done")
        self.assertEqual(job["progress"], 100)
        self.assertEqual(job["progress_label"], "Completed")
        self.assertGreaterEqual(job["activity_count"], 3)

    async def test_dispatch_turn_persists_marker_as_pending_input(self):
        store.create_dispatch_job("job-1", "/repo", "codex", "original task", True)
        adapter = Mock()
        adapter.dispatch = AsyncMock(
            return_value=(
                "Checked the repository.\n"
                '<agentmemorysync_interaction>{"kind":"question","prompt":"Which target?",'
                '"options":["A","B"]}</agentmemorysync_interaction>',
                42,
            )
        )

        with patch.object(dispatch, "get_agent_adapter", return_value=adapter):
            await dispatch._run_dispatch_turn("job-1", "/repo", "codex", "original task", True)

        job = store.get_dispatch_job("job-1")
        pending = store.list_dispatch_interactions("/repo", pending_only=True)
        self.assertEqual(job["status"], "waiting")
        self.assertEqual(job["progress_label"], "Needs input")
        self.assertEqual(job["result_text"], "Checked the repository.")
        self.assertEqual(pending[0]["prompt"], "Which target?")
        self.assertEqual(pending[0]["options"], ["A", "B"])

    async def test_missing_session_id_continues_in_fresh_session(self):
        store.create_dispatch_job("job-1", "/repo", "codex", "original task", True)
        store.update_dispatch_job("job-1", "waiting", "Need a choice")

        with patch.object(dispatch, "_run_dispatch_turn", new=AsyncMock()) as run_turn:
            await dispatch.resume_dispatch_job("job-1", "Use SQLite")

        args = run_turn.await_args.args
        self.assertEqual(args[:3], ("job-1", "/repo", "codex"))
        self.assertIn("original task", args[3])
        self.assertIn("Use SQLite", args[3])
        self.assertIsNone(run_turn.await_args.kwargs["resume_session_id"])

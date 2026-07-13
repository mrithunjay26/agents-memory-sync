import os
import sys
import tempfile
import unittest
from unittest.mock import patch

import dispatch
import store


class DispatchContextTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        os.environ["AGENT_MEMORY_DB_PATH"] = os.path.join(self.tmpdir.name, "store.db")
        self.project = os.path.normcase(self.tmpdir.name)
        store.record_event(self.project, "codex", "s1", "turn", "snapshot version one")

    def tearDown(self):
        os.environ.pop("AGENT_MEMORY_DB_PATH", None)
        self.tmpdir.cleanup()

    def test_snapshot_is_captured_at_enqueue_and_remains_immutable(self):
        job_id = dispatch.start_dispatch_job(
            self.project, "claude-code", "Do the task", allow_edits=False
        )
        snapshot = store.get_dispatch_job(job_id)["context_snapshot"]
        self.assertIn("snapshot version one", snapshot)

        event_id = store.get_context_bundle(self.project)["entries"][0]["id"]
        store.update_context_event(
            self.project, event_id, context_summary="snapshot version two"
        )
        self.assertEqual(store.get_dispatch_job(job_id)["context_snapshot"], snapshot)

    async def test_runner_uses_stored_snapshot_and_sets_hook_marker(self):
        job_id = dispatch.start_dispatch_job(
            self.project, "claude-code", "Do the task", allow_edits=False
        )
        snapshot = store.get_dispatch_job(job_id)["context_snapshot"]
        event_id = store.get_context_bundle(self.project)["entries"][0]["id"]
        store.update_context_event(self.project, event_id, context_summary="later edit")
        captured = {}

        async def fake_dispatch(project_path, prompt, allow_edits, on_log, env=None, model=None):
            captured.update(
                project_path=project_path,
                prompt=prompt,
                allow_edits=allow_edits,
                marker=(env or {}).get(dispatch.CONTEXT_PREINJECTED_ENV),
            )
            return "complete", 12

        with patch.object(dispatch, "dispatch_claude", fake_dispatch):
            await dispatch.run_dispatch_job(
                job_id, self.project, "claude-code", "Do the task", allow_edits=False
            )

        self.assertEqual(captured["prompt"], snapshot + "\n\nDo the task")
        self.assertNotIn("later edit", captured["prompt"])
        self.assertEqual(captured["marker"], "1")
        self.assertEqual(store.get_dispatch_job(job_id)["status"], "done")

    async def test_claude_prompt_uses_stdin_not_windows_command_line(self):
        captured = {}

        async def fake_stream(args, cwd, on_line, env=None, stdin_text=None):
            captured.update(args=args, stdin_text=stdin_text)
            return 0, ""

        prompt = "shared context " * 20_000
        with patch.object(dispatch, "resolve_claude_binary", return_value="claude"), patch.object(
            dispatch, "_stream", fake_stream
        ):
            await dispatch.dispatch_claude(self.project, prompt, False, lambda _entry: None)

        self.assertEqual(captured["stdin_text"], prompt)
        self.assertNotIn(prompt, captured["args"])
        self.assertEqual(captured["args"][:2], ["claude", "-p"])

    async def test_codex_prompt_uses_stdin_not_windows_command_line(self):
        captured = {}

        async def fake_stream(args, cwd, on_line, env=None, stdin_text=None):
            captured.update(args=args, stdin_text=stdin_text)
            return 0, ""

        prompt = "shared context " * 20_000
        with patch.object(dispatch, "resolve_codex_binary", return_value="codex"), patch.object(
            dispatch, "_stream", fake_stream
        ):
            await dispatch.dispatch_codex(self.project, prompt, False, lambda _entry: None)

        self.assertIn(prompt, captured["stdin_text"])
        self.assertNotIn(prompt, captured["args"])
        self.assertEqual(captured["args"][:3], ["codex", "exec", "-"])

    async def test_stream_transfers_prompt_larger_than_windows_command_limit(self):
        prompt = "x" * 100_000
        lines = []
        code, output = await dispatch._stream(
            [
                sys.executable,
                "-c",
                "import sys; data=sys.stdin.read(); print(len(data))",
            ],
            self.project,
            lines.append,
            stdin_text=prompt,
        )
        self.assertEqual(code, 0)
        self.assertEqual(output.strip(), "100000")
        self.assertEqual(lines, ["100000"])


if __name__ == "__main__":
    unittest.main()

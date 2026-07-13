import json
import os
import tempfile
import unittest

from history import _read_claude_session, _read_codex_session


class NativeHistoryContextTestCase(unittest.TestCase):
    def _jsonl(self, entries):
        handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".jsonl", delete=False)
        for entry in entries:
            handle.write(json.dumps(entry) + "\n")
        handle.close()
        self.addCleanup(lambda: os.path.exists(handle.name) and os.unlink(handle.name))
        return handle.name

    def test_claude_preserves_text_tool_calls_and_results(self):
        path = self._jsonl([
            {"type": "user", "message": {"content": "fix the parser"}},
            {"type": "assistant", "message": {
                "content": [
                    {"type": "thinking", "thinking": "private reasoning"},
                    {"type": "tool_use", "name": "Read", "input": {"file_path": "app.py"}},
                ],
                "usage": {"input_tokens": 10, "output_tokens": 5},
            }},
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "content": "important file evidence"}
            ]}},
            {"type": "assistant", "message": {
                "content": [{"type": "text", "text": "implemented the fix"}],
                "usage": {"input_tokens": 20, "output_tokens": 7},
            }},
        ])
        summary, tokens, context = _read_claude_session(path)
        self.assertIn("fix the parser", summary)
        self.assertEqual(tokens, 42)
        self.assertIn("[Tool call: Read]", context)
        self.assertIn("important file evidence", context)
        self.assertIn("implemented the fix", context)
        self.assertNotIn("private reasoning", context)

    def test_codex_preserves_conversation_and_tool_evidence(self):
        path = self._jsonl([
            {"type": "event_msg", "payload": {"type": "user_message", "message": "inspect auth"}},
            {"type": "response_item", "payload": {
                "type": "message", "role": "user", "content": [{"type": "input_text", "text": "inspect auth"}]
            }},
            {"type": "response_item", "payload": {
                "type": "custom_tool_call", "name": "shell_command", "input": "Get-Content auth.py"
            }},
            {"type": "response_item", "payload": {
                "type": "custom_tool_call_output", "output": "def verify(): pass"
            }},
            {"type": "response_item", "payload": {
                "type": "reasoning", "summary": ["private reasoning"]
            }},
            {"type": "response_item", "payload": {
                "type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "auth is valid"}]
            }},
            {"type": "event_msg", "payload": {"type": "agent_message", "message": "auth is valid"}},
            {"type": "event_msg", "payload": {
                "type": "token_count", "info": {"total_token_usage": {"total_tokens": 321}}
            }},
        ])
        summary, tokens, context = _read_codex_session(path)
        self.assertIn("inspect auth", summary)
        self.assertEqual(tokens, 321)
        self.assertIn("[Tool call: shell_command]", context)
        self.assertIn("def verify(): pass", context)
        self.assertIn("auth is valid", context)
        self.assertNotIn("private reasoning", context)


if __name__ == "__main__":
    unittest.main()

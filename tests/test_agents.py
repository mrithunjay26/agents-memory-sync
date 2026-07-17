import asyncio
import unittest
from unittest.mock import AsyncMock, patch

import store
from agents import agent_ids, all_agent_adapters, detect_hook_agent, get_agent_adapter


class AgentRegistryTestCase(unittest.TestCase):
    def test_registry_exposes_capabilities(self):
        self.assertEqual(agent_ids("dispatch"), ("claude-code", "codex"))
        metadata = get_agent_adapter("codex").public_metadata()
        self.assertTrue(metadata["capabilities"]["history"])
        self.assertTrue(metadata["capabilities"]["context_injection"])

    def test_unknown_agent_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Unsupported agent"):
            get_agent_adapter("unknown")

    def test_hook_detection_uses_provider_marker_and_safe_fallback(self):
        self.assertEqual(detect_hook_agent({"turn_id": "t1"}).agent_id, "codex")
        self.assertEqual(detect_hook_agent({"session_id": "s1"}).agent_id, "claude-code")

    def test_dispatch_contract_delegates_to_provider_implementation(self):
        expected = ("done", 12)
        with patch("dispatch.dispatch_codex", new=AsyncMock(return_value=expected)) as mocked:
            actual = asyncio.run(
                get_agent_adapter("codex").dispatch(
                    "/repo", "prompt", False, lambda _entry: None
                )
            )
        self.assertEqual(actual, expected)
        mocked.assert_awaited_once()


class LocalProviderAdapterTestCase(unittest.TestCase):
    def setUp(self):
        store.upsert_local_provider(
            "ollama-gemma3", "Ollama Gemma3", "http://localhost:11434", "gemma3:4b", None
        )

    def test_registered_provider_appears_in_registry(self):
        self.assertIn("ollama-gemma3", agent_ids("dispatch"))
        ids = [a.agent_id for a in all_agent_adapters()]
        self.assertEqual(ids, ["claude-code", "codex", "ollama-gemma3"])

    def test_adapter_capabilities_are_read_only_by_design(self):
        adapter = get_agent_adapter("ollama-gemma3")
        self.assertFalse(adapter.capabilities.capture)
        self.assertFalse(adapter.capabilities.history)
        self.assertTrue(adapter.capabilities.dispatch)

    def test_dispatch_rejects_allow_edits(self):
        adapter = get_agent_adapter("ollama-gemma3")
        with self.assertRaisesRegex(RuntimeError, "read-only"):
            asyncio.run(adapter.dispatch("/repo", "prompt", True, lambda _entry: None))

    def test_dispatch_delegates_to_local_openai_dispatch(self):
        expected = ("hello from local model", 7)
        with patch("dispatch.dispatch_local_openai", new=AsyncMock(return_value=expected)) as mocked:
            actual = asyncio.run(
                get_agent_adapter("ollama-gemma3").dispatch(
                    "/repo", "prompt", False, lambda _entry: None
                )
            )
        self.assertEqual(actual, expected)
        mocked.assert_awaited_once()
        args = mocked.await_args.args
        self.assertEqual(args[0], "http://localhost:11434")
        self.assertEqual(args[1], "gemma3:4b")


if __name__ == "__main__":
    unittest.main()

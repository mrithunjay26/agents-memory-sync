import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from agents import agent_ids, detect_hook_agent, get_agent_adapter


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


if __name__ == "__main__":
    unittest.main()

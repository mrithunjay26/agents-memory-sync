import os
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import store
from app import app, require_admin, require_auth


class LocalProvidersApiTestCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        os.environ["AGENT_MEMORY_DB_PATH"] = os.path.join(self.tmpdir.name, "store.db")
        app.dependency_overrides[require_auth] = lambda: "tester"
        app.dependency_overrides[require_admin] = lambda: "admin"
        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.clear()
        self.client.close()
        os.environ.pop("AGENT_MEMORY_DB_PATH", None)
        self.tmpdir.cleanup()

    def _create(self, **overrides):
        payload = {
            "agent_id": "ollama-gemma3",
            "display_name": "Ollama Gemma3",
            "base_url": "http://localhost:11434",
            "model": "gemma3:4b",
            "api_key_env": "",
        }
        payload.update(overrides)
        return self.client.post("/api/agents/providers", json=payload)

    def test_create_list_and_delete_provider(self):
        response = self._create()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["agent_id"], "ollama-gemma3")

        listed = self.client.get("/api/agents/providers").json()
        self.assertEqual([p["agent_id"] for p in listed], ["ollama-gemma3"])

        agents = self.client.get("/api/agents").json()
        self.assertIn("ollama-gemma3", [a["id"] for a in agents])

        deleted = self.client.delete("/api/agents/providers/ollama-gemma3")
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(store.list_local_providers(), [])

    def test_create_rejects_builtin_agent_id(self):
        response = self._create(agent_id="codex")
        self.assertEqual(response.status_code, 400)

    def test_create_rejects_bad_id_and_url(self):
        self.assertEqual(self._create(agent_id="Not Valid!").status_code, 400)
        self.assertEqual(self._create(base_url="not-a-url").status_code, 400)
        self.assertEqual(self._create(model="").status_code, 400)

    def test_write_routes_require_admin(self):
        store.create_user("owner", "hashed-password", role="admin")
        store.create_user("member", "hashed-password", role="member")
        app.dependency_overrides.pop(require_admin, None)
        app.dependency_overrides[require_auth] = lambda: "member"
        response = self._create()
        self.assertIn(response.status_code, (401, 403))

    def test_health_check_reports_reachability(self):
        self._create()
        with patch("agents.LocalOpenAIAdapter.resolve_binary", return_value="http://localhost:11434"):
            ok = self.client.post("/api/agents/providers/ollama-gemma3/health")
        self.assertTrue(ok.json()["reachable"])

        with patch(
            "agents.LocalOpenAIAdapter.resolve_binary",
            side_effect=RuntimeError("connection refused"),
        ):
            bad = self.client.post("/api/agents/providers/ollama-gemma3/health")
        self.assertFalse(bad.json()["reachable"])

    def test_health_check_unknown_provider_is_404(self):
        response = self.client.post("/api/agents/providers/does-not-exist/health")
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()

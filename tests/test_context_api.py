import os
import tempfile
import unittest

from fastapi.testclient import TestClient

import store
from app import app, require_auth


class ContextApiTestCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        os.environ["AGENT_MEMORY_DB_PATH"] = os.path.join(self.tmpdir.name, "store.db")
        self.project = "/tracked/repo"
        store.register_project(self.project)
        app.dependency_overrides[require_auth] = lambda: "tester"
        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.clear()
        self.client.close()
        os.environ.pop("AGENT_MEMORY_DB_PATH", None)
        self.tmpdir.cleanup()

    def test_context_route_requires_authentication(self):
        app.dependency_overrides.clear()
        response = self.client.get("/api/context", params={"project": self.project})
        self.assertEqual(response.status_code, 401)

    def test_unknown_project_is_rejected(self):
        response = self.client.get("/api/context", params={"project": "/unknown"})
        self.assertEqual(response.status_code, 400)

    def test_update_rejects_event_from_another_project(self):
        store.record_event("/other", "codex", "s1", "turn", "private to other")
        event_id = store.get_context_bundle("/other")["entries"][0]["id"]
        response = self.client.patch(
            f"/api/context/events/{event_id}",
            params={"project": self.project},
            json={"included": False},
        )
        self.assertEqual(response.status_code, 404)

    def test_note_update_settings_and_delete_round_trip(self):
        created = self.client.post(
            "/api/context/notes",
            json={"project_path": self.project, "content": "Preserve the public API."},
        )
        self.assertEqual(created.status_code, 200)
        note = created.json()["entries"][0]

        updated = self.client.patch(
            f"/api/context/events/{note['id']}",
            params={"project": self.project},
            json={
                "pinned": True,
                "context_summary": "Keep the API stable.",
                "category": "constraint",
            },
        )
        self.assertEqual(updated.status_code, 200)
        self.assertTrue(updated.json()["entries"][0]["pinned"])
        self.assertEqual(updated.json()["entries"][0]["summary"], "Preserve the public API.")
        self.assertEqual(updated.json()["entries"][0]["category"], "constraint")
        self.assertEqual(updated.json()["entries"][0]["category_source"], "manual")

        settings = self.client.put(
            "/api/context/settings",
            json={"project_path": self.project, "recent_limit": 7},
        )
        self.assertEqual(settings.status_code, 200)
        self.assertEqual(settings.json()["settings"]["recent_limit"], 7)

        deleted = self.client.delete(
            f"/api/context/notes/{note['id']}", params={"project": self.project}
        )
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(deleted.json()["entries"], [])

    def test_raw_event_cannot_be_deleted(self):
        store.record_event(self.project, "codex", "s1", "turn", "real event")
        event_id = store.get_context_bundle(self.project)["entries"][0]["id"]
        response = self.client.delete(
            f"/api/context/notes/{event_id}", params={"project": self.project}
        )
        self.assertEqual(response.status_code, 400)

    def test_note_and_limit_validation(self):
        empty = self.client.post(
            "/api/context/notes", json={"project_path": self.project, "content": "  "}
        )
        self.assertEqual(empty.status_code, 400)
        invalid_limit = self.client.put(
            "/api/context/settings",
            json={"project_path": self.project, "recent_limit": 101},
        )
        self.assertEqual(invalid_limit.status_code, 400)
        invalid_category = self.client.post(
            "/api/context/notes",
            json={"project_path": self.project, "content": "x", "category": "mystery"},
        )
        self.assertEqual(invalid_category.status_code, 400)


if __name__ == "__main__":
    unittest.main()

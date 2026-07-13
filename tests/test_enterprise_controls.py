import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone

from fastapi.testclient import TestClient

import store
from app import app, require_admin, require_auth


class EnterpriseStoreTestCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        os.environ["AGENT_MEMORY_DB_PATH"] = os.path.join(self.tmpdir.name, "store.db")
        self.project = "/enterprise/repo"
        store.register_project(self.project)

    def tearDown(self):
        os.environ.pop("AGENT_MEMORY_DB_PATH", None)
        self.tmpdir.cleanup()

    def test_rbac_and_repository_permissions(self):
        store.create_user("admin", "hash")
        store.create_user("member", "hash", "member")
        self.assertEqual(store.get_project_access("admin", self.project), "operator")
        self.assertIsNone(store.get_project_access("member", self.project))

        store.set_project_permission("member", self.project, "editor", "admin")
        self.assertEqual(store.get_project_access("member", self.project), "editor")
        self.assertEqual(store.list_project_permissions(self.project)[0]["granted_by"], "admin")

        store.delete_project_permission("member", self.project)
        self.assertIsNone(store.get_project_access("member", self.project))

    def test_secret_redaction_covers_agent_persistence(self):
        store.record_event(
            self.project, "codex", "s1", "turn", "password=correct-horse-battery-staple"
        )
        event = store.list_events(self.project)[0]
        self.assertNotIn("correct-horse", event["summary"])
        self.assertIn("[REDACTED]", event["summary"])

        store.create_dispatch_job(
            "job-1", self.project, "codex", "Use sk-abcdefghijklmnopqrstuvwxyz123456", True
        )
        store.append_dispatch_log("job-1", "Authorization: Bearer abcdefghijklmnopqrstuvwxyz")
        store.update_dispatch_job("job-1", "done", "github_pat_abcdefghijklmnopqrstuvwxyz123")
        job = store.get_dispatch_job("job-1")
        self.assertNotIn("sk-abc", job["prompt"])
        self.assertNotIn("github_pat_", job["result_text"])
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz", store.list_dispatch_logs("job-1")[0]["line"])

    def test_existing_data_scan_and_retention(self):
        store.set_enterprise_policy(self.project, 30, False, "admin")
        store.record_event(self.project, "codex", "s1", "turn", "api_key=supersecretvalue")
        conn = sqlite3.connect(store.db_path())
        try:
            conn.execute(
                "UPDATE events SET created_at = '2020-01-01T00:00:00+00:00' WHERE project_path = ?",
                (self.project,),
            )
            conn.commit()
        finally:
            conn.close()

        store.set_enterprise_policy(self.project, 30, True, "admin")
        scan = store.redact_stored_secrets(self.project)
        self.assertEqual(scan["events"], 1)
        self.assertNotIn("supersecretvalue", store.list_events(self.project)[0]["summary"])

        purged = store.purge_expired_data(
            self.project, now=datetime(2026, 7, 6, tzinfo=timezone.utc)
        )
        self.assertEqual(purged["events"], 1)
        self.assertEqual(store.list_events(self.project), [])

    def test_audit_chain_is_reproducible(self):
        first = store.record_audit_event("admin", "project.added", self.project)
        second = store.record_audit_event(
            "admin", "project.permission_set", self.project, "user", "member",
            {"access_level": "viewer", "token": "sk-abcdefghijklmnopqrstuvwxyz123456"},
        )
        events = store.list_audit_events()
        self.assertEqual(second["previous_hash"], first["entry_hash"])
        self.assertNotIn("sk-", json.dumps(events[1]["details"]))
        event = events[1]
        details_json = json.dumps(event["details"], sort_keys=True, separators=(",", ":"))
        payload = "\x1f".join((event["previous_hash"], event["actor"], event["action"],
                                 event["project_path"], event["target_type"], event["target_id"],
                                 details_json, event["created_at"]))
        self.assertEqual(hashlib.sha256(payload.encode()).hexdigest(), event["entry_hash"])
        self.assertEqual(store.verify_audit_chain(), {
            "valid": True, "event_count": 2, "broken_event_id": None
        })


class EnterpriseApiTestCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        os.environ["AGENT_MEMORY_DB_PATH"] = os.path.join(self.tmpdir.name, "store.db")
        self.allowed = "/repo/allowed"
        self.denied = "/repo/denied"
        store.register_project(self.allowed)
        store.register_project(self.denied)
        store.create_user("admin", "hash", "admin")
        store.create_user("member", "hash", "member")
        store.set_project_permission("member", self.allowed, "viewer", "admin")
        app.dependency_overrides[require_auth] = lambda: "member"
        app.dependency_overrides[require_admin] = lambda: "admin"
        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.clear()
        self.client.close()
        os.environ.pop("AGENT_MEMORY_DB_PATH", None)
        self.tmpdir.cleanup()

    def test_member_visibility_and_write_levels(self):
        projects = self.client.get("/api/projects")
        self.assertEqual([item["project_path"] for item in projects.json()], [self.allowed])
        self.assertEqual(
            self.client.get("/api/context", params={"project": self.denied}).status_code, 403
        )
        viewer_write = self.client.post(
            "/api/context/notes", json={"project_path": self.allowed, "content": "note"}
        )
        self.assertEqual(viewer_write.status_code, 403)

        store.set_project_permission("member", self.allowed, "editor", "admin")
        self.assertEqual(
            self.client.post(
                "/api/context/notes", json={"project_path": self.allowed, "content": "note"}
            ).status_code,
            200,
        )

    def test_admin_policy_and_audit_export(self):
        response = self.client.put(
            "/api/admin/policy",
            json={"project_path": self.allowed, "retention_days": 45,
                  "secret_redaction": True, "scan_existing": True},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["policy"]["retention_days"], 45)

        exported = self.client.get("/api/admin/audit/export", params={"format": "jsonl"})
        self.assertEqual(exported.status_code, 200)
        records = [json.loads(line) for line in exported.text.splitlines()]
        self.assertEqual(records[-1]["action"], "project.policy_updated")
        self.assertTrue(self.client.get("/api/admin/audit/verify").json()["valid"])


if __name__ == "__main__":
    unittest.main()

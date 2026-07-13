import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import store
import telemetry


class _UsageAdapter:
    capabilities = SimpleNamespace(usage=True, context_injection=True)

    def __init__(self, agent_id, tokens):
        self.agent_id = agent_id
        self._tokens = tokens

    def usage_tokens(self, _project_path):
        return self._tokens


class TelemetryTestCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        os.environ["AGENT_MEMORY_DB_PATH"] = os.path.join(self.tmpdir.name, "store.db")
        self.project = os.path.normcase(self.tmpdir.name)
        store.record_event(self.project, "codex", "s1", "turn", "shared fact")

    def tearDown(self):
        os.environ.pop("AGENT_MEMORY_DB_PATH", None)
        self.tmpdir.cleanup()

    def test_no_savings_claim_without_matched_baseline_and_delivery_is_per_agent(self):
        context = store.get_context(self.project)
        telemetry.record_context_injection(
            self.project, "claude-code", "c1", context, route="hook"
        )
        telemetry.record_context_injection(
            self.project, "codex", "x1", context, route="dashboard"
        )
        adapters = (
            _UsageAdapter("claude-code", 100),
            _UsageAdapter("codex", 200),
        )
        with patch.object(telemetry, "all_agent_adapters", return_value=adapters), patch.object(
            telemetry, "sum_transcript_tokens", return_value={field: 0 for field in telemetry.USAGE_FIELDS}
        ):
            summary = telemetry.get_telemetry_summary(self.project)

        self.assertEqual(summary["measured_total_tokens"], 300)
        self.assertEqual(summary["context_delivery_count"], 2)
        self.assertEqual(summary["baseline_delivery_count"], 0)
        self.assertEqual(summary["delivery_native_tokens"], 0)
        self.assertIsNone(summary["verified_tokens_saved"])
        self.assertIsNone(summary["efficiency_gain_percent"])
        self.assertEqual(summary["baseline_status"], "not_established")
        self.assertEqual(summary["exclusive_context_entries"], 0)
        self.assertTrue(summary["delivery_by_agent"]["claude-code"]["has_current_context"])
        self.assertTrue(summary["delivery_by_agent"]["codex"]["has_current_context"])

    def test_baseline_delivery_count_tracks_only_deliveries_with_measured_native_usage(self):
        context = store.get_context(self.project)
        telemetry.record_context_injection(
            self.project, "claude-code", "c1", context, route="hook"
        )
        store.record_history_event(self.project, "codex", "hist1", "x" * 40, source_tokens=1000)
        context = store.get_context(self.project)
        telemetry.record_context_injection(
            self.project, "codex", "x1", context, route="dashboard"
        )

        with patch.object(telemetry, "all_agent_adapters", return_value=()), patch.object(
            telemetry, "sum_transcript_tokens", return_value={field: 0 for field in telemetry.USAGE_FIELDS}
        ):
            summary = telemetry.get_telemetry_summary(self.project)

        self.assertEqual(summary["context_delivery_count"], 2)
        self.assertEqual(summary["baseline_delivery_count"], 1)

    def test_verified_savings_measured_from_pooled_native_usage(self):
        store.record_history_event(self.project, "codex", "hist1", "x" * 40, source_tokens=1000)
        context = store.get_context(self.project)
        telemetry.record_context_injection(
            self.project, "claude-code", "c1", context, route="hook"
        )

        with patch.object(telemetry, "all_agent_adapters", return_value=()), patch.object(
            telemetry, "sum_transcript_tokens", return_value={field: 0 for field in telemetry.USAGE_FIELDS}
        ):
            summary = telemetry.get_telemetry_summary(self.project)

        self.assertEqual(summary["baseline_status"], "measured")
        self.assertEqual(summary["context_delivery_count"], 1)
        self.assertEqual(summary["delivery_native_tokens"], summary["native_usage_tokens"])
        self.assertIsNotNone(summary["verified_tokens_saved"])
        self.assertGreater(summary["verified_tokens_saved"], 0)
        self.assertEqual(
            summary["verified_tokens_saved"],
            summary["native_usage_tokens"] - summary["context_delivered_tokens"],
        )
        self.assertGreater(summary["delivery_by_agent"]["claude-code"]["tokens_saved"], 0)

    def test_verified_savings_accumulate_once_per_recorded_delivery(self):
        store.record_history_event(
            self.project, "codex", "hist1", "reusable finding", source_tokens=1000
        )
        context = store.get_context(self.project)
        context_tokens = telemetry.estimate_context_tokens(context)
        telemetry.record_context_injection(
            self.project, "claude-code", "c1", context, route="hook"
        )
        telemetry.record_context_injection(
            self.project, "codex", "x1", context, route="dashboard"
        )

        with patch.object(telemetry, "all_agent_adapters", return_value=()), patch.object(
            telemetry,
            "sum_transcript_tokens",
            return_value={field: 0 for field in telemetry.USAGE_FIELDS},
        ):
            summary = telemetry.get_telemetry_summary(self.project)

        per_delivery_native = summary["native_usage_tokens"]
        self.assertEqual(summary["context_delivery_count"], 2)
        self.assertEqual(summary["delivery_native_tokens"], per_delivery_native * 2)
        self.assertEqual(summary["context_delivered_tokens"], context_tokens * 2)
        self.assertEqual(
            summary["verified_tokens_saved"],
            (per_delivery_native - context_tokens) * 2,
        )


if __name__ == "__main__":
    unittest.main()

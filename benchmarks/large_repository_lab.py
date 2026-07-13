from __future__ import annotations

import argparse
import json
import os
import sqlite3
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import repository_intelligence
import store


DEFAULT_REPOSITORY_SIZES = (100, 1_000, 5_000)
DEFAULT_HISTORY_SIZES = (100, 1_000, 10_000)


def _tokens(text: str) -> int:
    return (len(text) + 3) // 4


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, max(0, int((len(ordered) - 1) * percentile)))
    return ordered[index]


def _timed(callable_):
    started = time.perf_counter()
    value = callable_()
    return value, (time.perf_counter() - started) * 1_000


def _module_text(index: int) -> str:
    return f'''"""Synthetic service module {index:05d}."""

import json

DECISION_MARKER = "decision-marker-{index:05d}"


class Component{index:05d}:
    """Owns request normalization for synthetic component {index:05d}."""

    def normalize(self, payload: dict) -> str:
        return json.dumps(payload, sort_keys=True)

    def execute(self, payload: dict) -> str:
        return self.normalize(payload) + DECISION_MARKER


def load_component_{index:05d}(payload: dict) -> str:
    """Load and execute component {index:05d}."""
    return Component{index:05d}().execute(payload)
'''


def _repository_case(file_count: int) -> dict:
    with tempfile.TemporaryDirectory(prefix=f"ams-repo-{file_count}-") as directory:
        root = Path(directory) / "repository"
        source = root / "src"
        source.mkdir(parents=True)
        total_chars = 0
        for index in range(file_count):
            text = _module_text(index)
            total_chars += len(text)
            (source / f"module_{index:05d}.py").write_text(text, encoding="utf-8")

        previous = os.environ.get("AGENT_MEMORY_DB_PATH")
        database_path = str(Path(directory) / "store.db")
        os.environ["AGENT_MEMORY_DB_PATH"] = database_path
        try:
            cold, cold_ms = _timed(lambda: repository_intelligence.index_repository(str(root)))
            warm, warm_ms = _timed(lambda: repository_intelligence.index_repository(str(root)))

            query_indices = sorted({0, file_count // 4, file_count // 2, file_count - 1})
            search_latencies = []
            search_payload_tokens = []
            search_hits = 0
            for index in query_indices:
                marker = f"decision-marker-{index:05d}"
                results, elapsed = _timed(
                    lambda marker=marker: repository_intelligence.search_code(
                        str(root), marker, limit=5
                    )
                )
                search_latencies.append(elapsed)
                search_payload_tokens.append(_tokens(json.dumps(results, sort_keys=True)))
                if results and results[0]["path"].endswith(f"module_{index:05d}.py"):
                    search_hits += 1

            architecture, architecture_ms = _timed(
                lambda: repository_intelligence.get_repository_map(str(root))
            )
            architecture_tokens = _tokens(json.dumps(architecture, sort_keys=True))
            database_bytes = sum(
                path.stat().st_size for path in Path(directory).glob("store.db*")
            )
        finally:
            if previous is None:
                os.environ.pop("AGENT_MEMORY_DB_PATH", None)
            else:
                os.environ["AGENT_MEMORY_DB_PATH"] = previous

    source_tokens = _tokens("x" * total_chars)
    mean_search_tokens = statistics.fmean(search_payload_tokens)
    return {
        "files": file_count,
        "source_characters": total_chars,
        "whole_source_token_estimate": source_tokens,
        "cold_index_ms": cold_ms,
        "warm_no_change_index_ms": warm_ms,
        "database_bytes": database_bytes,
        "symbols": cold["symbol_count"],
        "relationships": cold["relationship_count"],
        "unchanged_on_warm_index": warm["unchanged"],
        "exact_lookup_top1_rate": search_hits / len(query_indices),
        "search_latency_ms": {
            "p50": statistics.median(search_latencies),
            "p95": _percentile(search_latencies, 0.95),
        },
        "mean_search_response_token_estimate": mean_search_tokens,
        "search_response_reduction_vs_whole_source_percent": (
            (1 - mean_search_tokens / source_tokens) * 100 if source_tokens else 0.0
        ),
        "architecture_map_ms": architecture_ms,
        "architecture_map_token_estimate": architecture_tokens,
        "architecture_map_truncated": architecture["truncated"],
    }


def _seed_history(project_path: str, event_count: int) -> None:
    conn = store._connect()
    try:
        created_at = "2026-01-01T00:00:00+00:00"
        rows = []
        for index in range(event_count):
            summary = (
                f"Decision {index:05d}: service boundary uses durable queue marker "
                f"history-marker-{index:05d}; preserve idempotency and retry limits. "
                "The implementation and focused tests were completed successfully."
            )
            rows.append(
                (
                    project_path,
                    "codex" if index % 2 else "claude-code",
                    f"session-{index:05d}",
                    "history",
                    summary,
                    2_000,
                    _tokens(summary),
                    created_at,
                )
            )
        conn.executemany(
            "INSERT INTO events (project_path, agent, session_id, event_type, summary, "
            "source_tokens, context_tokens, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()


def _history_case(event_count: int) -> dict:
    with tempfile.TemporaryDirectory(prefix=f"ams-history-{event_count}-") as directory:
        previous = os.environ.get("AGENT_MEMORY_DB_PATH")
        database_path = str(Path(directory) / "store.db")
        os.environ["AGENT_MEMORY_DB_PATH"] = database_path
        project_path = os.path.normcase(os.path.abspath(Path(directory) / "project"))
        try:
            _, seed_ms = _timed(lambda: _seed_history(project_path, event_count))
            bundle, bundle_ms = _timed(lambda: store.get_context_bundle(project_path))
            preview_tokens = bundle["token_estimate"]
            corpus_tokens = bundle["corpus_tokens"]
            native_tokens = bundle["native_usage_tokens"]
            api_payload_tokens = _tokens(json.dumps(bundle, sort_keys=True))

            search_latencies = []
            search_hits = 0
            targets = sorted({0, event_count // 2, event_count - 1})
            for target in targets:
                marker = f"history-marker-{target:05d}"
                results, elapsed = _timed(
                    lambda marker=marker: store.search_shared_context(
                        project_path, marker, limit=5
                    )
                )
                search_latencies.append(elapsed)
                if any(marker in item["snippet"] for item in results):
                    search_hits += 1
            database_bytes = sum(
                path.stat().st_size for path in Path(directory).glob("store.db*")
            )
        finally:
            if previous is None:
                os.environ.pop("AGENT_MEMORY_DB_PATH", None)
            else:
                os.environ["AGENT_MEMORY_DB_PATH"] = previous

    return {
        "events": event_count,
        "seed_ms": seed_ms,
        "database_bytes": database_bytes,
        "bundle_build_ms": bundle_ms,
        "active_entries": bundle["counts"]["included"],
        "active_preview_token_estimate": preview_tokens,
        "retained_digest_token_estimate": corpus_tokens,
        "recorded_native_session_tokens": native_tokens,
        "preview_reduction_vs_retained_digest_percent": (
            (1 - preview_tokens / corpus_tokens) * 100 if corpus_tokens else 0.0
        ),
        "preview_reduction_vs_recorded_native_usage_percent": (
            (1 - preview_tokens / native_tokens) * 100 if native_tokens else 0.0
        ),
        "dashboard_bundle_payload_token_estimate": api_payload_tokens,
        "exact_search_recall_rate": search_hits / len(targets),
        "search_latency_ms": {
            "p50": statistics.median(search_latencies),
            "p95": _percentile(search_latencies, 0.95),
        },
    }


def run_lab(repository_sizes=DEFAULT_REPOSITORY_SIZES, history_sizes=DEFAULT_HISTORY_SIZES):
    return {
        "schema_version": 1,
        "methodology": {
            "token_estimate": "ceil(UTF-8 source characters / 4); not a model tokenizer",
            "repository": (
                "Generated Python files are indexed through the production repository "
                "intelligence path. Each search includes the production automatic refresh."
            ),
            "history": (
                "Synthetic history rows use 2,000 recorded native tokens each and compact "
                "digests. Native-token reduction is reuse potential, not a measured control run."
            ),
            "scope": (
                "Measures retrieval mechanics, storage, latency, and payload size. It does not "
                "measure answer correctness, task completion, model caching, or billing."
            ),
        },
        "repositories": [_repository_case(size) for size in repository_sizes],
        "history_corpora": [_history_case(size) for size in history_sizes],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", help="Write the JSON result to this path.")
    parser.add_argument(
        "--repository-sizes", type=int, nargs="+", default=DEFAULT_REPOSITORY_SIZES
    )
    parser.add_argument(
        "--history-sizes", type=int, nargs="+", default=DEFAULT_HISTORY_SIZES
    )
    args = parser.parse_args()
    report = run_lab(args.repository_sizes, args.history_sizes)
    text = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
    print(text, end="")


if __name__ == "__main__":
    main()

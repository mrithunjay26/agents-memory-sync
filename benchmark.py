from __future__ import annotations

import argparse
import json
import math
import os
import re
import sqlite3
import statistics
import tempfile
import time
from collections import defaultdict
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterable

import store
from benchmarks.retrieval_v1 import load_dataset


MIN_QUERIES = 150
MAX_QUERIES = 300
DEFAULT_LIMIT = 10
STOP_WORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from",
    "in", "is", "it", "of", "on", "or", "that", "the", "this", "to",
    "was", "were", "with",
}


def validate_dataset(dataset: dict) -> dict:
    if dataset.get("schema_version") != 1:
        raise ValueError("Unsupported dataset schema_version; expected 1.")
    documents = dataset.get("documents")
    queries = dataset.get("queries")
    if not isinstance(documents, list) or not isinstance(queries, list):
        raise ValueError("Dataset documents and queries must be lists.")
    if not MIN_QUERIES <= len(queries) <= MAX_QUERIES:
        raise ValueError(
            f"Dataset must contain {MIN_QUERIES}-{MAX_QUERIES} queries; got {len(queries)}."
        )

    document_ids = [document.get("id") for document in documents]
    if any(not isinstance(item, str) or not item for item in document_ids):
        raise ValueError("Every document must have a non-empty string id.")
    if len(document_ids) != len(set(document_ids)):
        raise ValueError("Document ids must be unique.")
    known_documents = set(document_ids)

    query_ids: set[str] = set()
    category_counts: dict[str, int] = defaultdict(int)
    for query in queries:
        query_id = query.get("id")
        if not isinstance(query_id, str) or not query_id or query_id in query_ids:
            raise ValueError("Query ids must be non-empty and unique.")
        query_ids.add(query_id)
        if not str(query.get("query", "")).strip():
            raise ValueError(f"Query {query_id} has no query text.")
        relevance = query.get("relevance")
        if not isinstance(relevance, dict) or not relevance:
            raise ValueError(f"Query {query_id} needs at least one relevance label.")
        unknown = set(relevance) - known_documents
        if unknown:
            raise ValueError(f"Query {query_id} references unknown documents: {sorted(unknown)}")
        if any(not isinstance(grade, int) or grade <= 0 for grade in relevance.values()):
            raise ValueError(f"Query {query_id} relevance grades must be positive integers.")
        category_counts[str(query.get("category", "uncategorized"))] += 1

    return {
        "name": dataset.get("name", "unnamed"),
        "documents": len(documents),
        "queries": len(queries),
        "categories": dict(sorted(category_counts.items())),
    }


def _terms(query: str) -> list[str]:
    terms = re.findall(r"[\w./:\\-]+", query.casefold(), flags=re.UNICODE)
    useful = list(dict.fromkeys(term for term in terms if term not in STOP_WORDS))
    return (useful or list(dict.fromkeys(terms)) or [query.casefold()])[:32]


def _snippet(text: str, query: str, terms: list[str], size: int = 1200) -> str:
    folded = text.casefold()
    positions = [folded.find(term) for term in terms if folded.find(term) >= 0]
    at = folded.find(query.casefold())
    if at < 0 and positions:
        at = min(positions)
    start = max(0, at - size // 3) if at >= 0 else 0
    result = text[start : start + size]
    if start:
        result = "…" + result
    if start + size < len(text):
        result += "…"
    return result


def _rank_rows(rows: Iterable[tuple], query: str, limit: int) -> list[dict]:
    terms = _terms(query)
    needle = query.casefold()
    ranked = []
    for event_id, summary, context_summary, raw_context in rows:
        text = raw_context or context_summary or summary or ""
        searchable = " ".join(
            part for part in (context_summary, summary, raw_context) if part
        )
        folded = searchable.casefold()
        matched = [term for term in terms if term in folded]
        if not matched:
            continue
        score = (int(needle in folded), len(matched), sum(folded.count(t) for t in matched), event_id)
        ranked.append(
            (score, {"id": event_id, "snippet": _snippet(text, query, matched)})
        )
    ranked.sort(key=lambda item: item[0], reverse=True)
    return [item[1] for item in ranked[:limit]]


def search_like(project_path: str, query: str, limit: int = DEFAULT_LIMIT) -> list[dict]:
    """Reproduce the pre-FTS corpus scan for an honest legacy baseline."""
    terms = _terms(" ".join((query or "").split()).strip())
    clauses = " OR ".join(
        "LOWER(COALESCE(context_summary, '') || ' ' || summary || ' ' || "
        "COALESCE(raw_context, '')) LIKE ? ESCAPE '\\'" for _ in terms
    )
    escaped = [
        "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        for term in terms
    ]
    conn = sqlite3.connect(store.db_path())
    try:
        rows = conn.execute(
            "SELECT id, summary, context_summary, raw_context FROM events "
            "WHERE project_path = ? AND event_type != ? AND context_included = 1 AND ("
            + clauses
            + ") LIMIT 500",
            (project_path, store.TELEMETRY_EVENT_TYPE, *escaped),
        ).fetchall()
    finally:
        conn.close()
    return _rank_rows(rows, query, limit)


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return ordered[index]


def _dcg(grades: list[int]) -> float:
    return sum((2**grade - 1) / math.log2(rank + 2) for rank, grade in enumerate(grades))


def evaluate_predictions(dataset: dict, predictions: dict[str, list[str]], *,
                         latencies_ms: dict[str, float] | None = None,
                         delivered_tokens: dict[str, int] | None = None) -> dict:
    validate_dataset(dataset)
    known_queries = {query["id"] for query in dataset["queries"]}
    known_documents = {document["id"] for document in dataset["documents"]}
    unknown_queries = set(predictions) - known_queries
    if unknown_queries:
        raise ValueError(f"Predictions reference unknown queries: {sorted(unknown_queries)}")
    for query_id, document_ids in predictions.items():
        if not isinstance(document_ids, list):
            raise ValueError(f"Predictions for {query_id} must be a ranked list.")
        unknown_documents = set(document_ids) - known_documents
        if unknown_documents:
            raise ValueError(
                f"Predictions for {query_id} reference unknown documents: "
                f"{sorted(unknown_documents)}"
            )
    latencies_ms = latencies_ms or {}
    delivered_tokens = delivered_tokens or {}
    per_query = []
    for query in dataset["queries"]:
        query_id = query["id"]
        ranked = list(dict.fromkeys(predictions.get(query_id, [])))[:DEFAULT_LIMIT]
        relevance = query["relevance"]
        relevant_count = len(relevance)
        recall_at_5 = sum(1 for item in ranked[:5] if item in relevance) / relevant_count
        precision_at_5 = sum(1 for item in ranked[:5] if item in relevance) / max(1, len(ranked[:5]))
        actual_grades = [relevance.get(item, 0) for item in ranked[:10]]
        ideal_grades = sorted(relevance.values(), reverse=True)[:10]
        ideal_dcg = _dcg(ideal_grades)
        ndcg_at_10 = _dcg(actual_grades) / ideal_dcg if ideal_dcg else 0.0
        first_relevant_rank = next(
            (rank for rank, item in enumerate(ranked, 1) if item in relevance), None
        )
        per_query.append(
            {
                "id": query_id,
                "category": query["category"],
                "recall_at_5": recall_at_5,
                "ndcg_at_10": ndcg_at_10,
                "evidence_precision_at_5": precision_at_5,
                "reciprocal_rank": 1 / first_relevant_rank if first_relevant_rank else 0.0,
                "retrieval_success_at_5": float(recall_at_5 == 1.0),
                "top1_citation_proxy": float(bool(ranked) and ranked[0] in relevance),
                "latency_ms": latencies_ms.get(query_id, 0.0),
                "delivered_tokens_at_5": delivered_tokens.get(query_id, 0),
            }
        )

    def aggregate(rows: list[dict]) -> dict:
        latencies = [row["latency_ms"] for row in rows]
        return {
            "query_count": len(rows),
            "recall_at_5": statistics.fmean(row["recall_at_5"] for row in rows),
            "ndcg_at_10": statistics.fmean(row["ndcg_at_10"] for row in rows),
            "evidence_precision_at_5": statistics.fmean(
                row["evidence_precision_at_5"] for row in rows
            ),
            "mrr": statistics.fmean(row["reciprocal_rank"] for row in rows),
            "retrieval_success_at_5": statistics.fmean(
                row["retrieval_success_at_5"] for row in rows
            ),
            "top1_citation_proxy": statistics.fmean(
                row["top1_citation_proxy"] for row in rows
            ),
            "mean_delivered_tokens_at_5": statistics.fmean(
                row["delivered_tokens_at_5"] for row in rows
            ),
            "latency_ms": {
                "p50": _percentile(latencies, 0.50),
                "p95": _percentile(latencies, 0.95),
            },
            "task_completion": None,
            "citation_correctness": None,
        }

    by_category = {}
    for category in sorted({row["category"] for row in per_query}):
        by_category[category] = aggregate(
            [row for row in per_query if row["category"] == category]
        )
    return {"overall": aggregate(per_query), "by_category": by_category}


@contextmanager
def _temporary_benchmark_store():
    previous = os.environ.get("AGENT_MEMORY_DB_PATH")
    with tempfile.TemporaryDirectory(prefix="agentmemorysync-benchmark-") as directory:
        path = os.path.join(directory, "benchmark.db")
        os.environ["AGENT_MEMORY_DB_PATH"] = path
        try:
            yield path
        finally:
            if previous is None:
                os.environ.pop("AGENT_MEMORY_DB_PATH", None)
            else:
                os.environ["AGENT_MEMORY_DB_PATH"] = previous


def _load_corpus(dataset: dict, project_path: str) -> tuple[dict[int, str], float]:
    started = time.perf_counter()
    for document in dataset["documents"]:
        store.record_history_event(
            project_path,
            document.get("agent", "benchmark"),
            document["id"],
            document.get("text") or document["summary"],
            source_tokens=0,
            summary=document["summary"],
        )
    elapsed_ms = (time.perf_counter() - started) * 1000
    conn = sqlite3.connect(store.db_path())
    try:
        mapping = {
            row[0]: row[1]
            for row in conn.execute(
                "SELECT id, session_id FROM events WHERE project_path = ?", (project_path,)
            )
        }
    finally:
        conn.close()
    return mapping, elapsed_ms


def _run_system(dataset: dict, project_path: str, event_to_document: dict[int, str],
                search: Callable[[str, str, int], list[dict]]) -> dict:
    predictions: dict[str, list[str]] = {}
    latencies: dict[str, float] = {}
    tokens: dict[str, int] = {}
    for query in dataset["queries"]:
        started = time.perf_counter()
        results = search(project_path, query["query"], DEFAULT_LIMIT)
        latencies[query["id"]] = (time.perf_counter() - started) * 1000
        predictions[query["id"]] = [
            event_to_document[result["id"]]
            for result in results
            if result["id"] in event_to_document
        ]
        tokens[query["id"]] = sum(
            (len(result.get("snippet", "")) + 3) // 4 for result in results[:5]
        )
    return evaluate_predictions(
        dataset, predictions, latencies_ms=latencies, delivered_tokens=tokens
    )


def _load_predictions(spec: str, dataset: dict) -> tuple[str, dict]:
    if "=" not in spec:
        raise ValueError("Prediction arguments must use NAME=PATH.")
    name, raw_path = spec.split("=", 1)
    name = name.strip()
    if not name:
        raise ValueError("Prediction system name must not be empty.")
    payload = json.loads(Path(raw_path).read_text(encoding="utf-8"))
    predictions = payload.get("predictions", payload)
    if not isinstance(predictions, dict):
        raise ValueError(f"Predictions in {raw_path} must be a JSON object.")
    return name, evaluate_predictions(dataset, predictions)


def run_benchmark(*, systems: Iterable[str] = ("like", "fts5"),
                  prediction_specs: Iterable[str] = ()) -> dict:
    dataset = load_dataset()
    dataset_summary = validate_dataset(dataset)
    selected = list(dict.fromkeys(systems))
    unknown = set(selected) - {"like", "fts5"}
    if unknown:
        raise ValueError(f"Unknown built-in systems: {', '.join(sorted(unknown))}")

    report = {
        "schema_version": 1,
        "dataset": dataset_summary,
        "dataset_caveat": dataset["description"],
        "systems": {},
        "measurement_notes": {
            "tokens_delivered": "Estimated as ceil(snippet characters / 4) for the top five results.",
            "task_completion": "Not measured by a retrieval-only benchmark.",
            "citation_correctness": (
                "Not measured without generated answers; top1_citation_proxy only checks "
                "whether the first retrieved evidence id is labeled relevant."
            ),
            "indexing_cost": (
                "corpus_load_and_fts_index_ms includes SQLite connection, migration, corpus "
                "writes, and incremental FTS trigger maintenance."
            ),
        },
        "not_run": {
            "dense_vectors": "No dense retriever or embedding model is implemented.",
            "hybrid": "Requires a dense retriever in addition to FTS5.",
            "hybrid_plus_code_graph": "No repository code graph is implemented.",
            "bluebird": "Requires authorized Bluebird access and an identical external corpus.",
        },
    }

    with _temporary_benchmark_store() as database_path:
        project_path = os.path.normcase(os.path.abspath("benchmark-fixture-project"))
        event_to_document, load_ms = _load_corpus(dataset, project_path)
        database_bytes = sum(
            path.stat().st_size for path in Path(database_path).parent.glob("benchmark.db*")
        )
        report["indexing"] = {
            "corpus_load_and_fts_index_ms": load_ms,
            "database_bytes": database_bytes,
            "document_count": len(event_to_document),
        }
        if "like" in selected:
            report["systems"]["like"] = _run_system(
                dataset, project_path, event_to_document, search_like
            )
        if "fts5" in selected:
            report["systems"]["fts5"] = _run_system(
                dataset, project_path, event_to_document, store.search_shared_context
            )

    for spec in prediction_specs:
        name, results = _load_predictions(spec, dataset)
        if name in report["systems"]:
            raise ValueError(f"Duplicate benchmark system name: {name}")
        report["systems"][name] = results
        report["not_run"].pop(name, None)
    return report


def _print_human(report: dict) -> None:
    dataset = report["dataset"]
    print(f"Dataset: {dataset['name']} ({dataset['queries']} queries, {dataset['documents']} documents)")
    print(report["dataset_caveat"])
    print()
    indexing = report["indexing"]
    print(
        f"Corpus load + FTS indexing: {indexing['corpus_load_and_fts_index_ms']:.1f} ms; "
        f"database: {indexing['database_bytes']} bytes"
    )
    print()
    heading = (
        f"{'system':<18} {'R@5':>7} {'nDCG@10':>9} {'P@5':>7} {'MRR':>7} "
        f"{'tokens':>8} {'p50 ms':>9} {'p95 ms':>9}"
    )
    print(heading)
    print("-" * len(heading))
    for name, result in report["systems"].items():
        metric = result["overall"]
        print(
            f"{name:<18} {metric['recall_at_5']:>7.3f} {metric['ndcg_at_10']:>9.3f} "
            f"{metric['evidence_precision_at_5']:>7.3f} {metric['mrr']:>7.3f} "
            f"{metric['mean_delivered_tokens_at_5']:>8.1f} "
            f"{metric['latency_ms']['p50']:>9.3f} {metric['latency_ms']['p95']:>9.3f}"
        )
    print()
    print("Unavailable comparisons:")
    for name, reason in report["not_run"].items():
        print(f"- {name}: {reason}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("validate", help="Validate and summarize the built-in dataset")
    export = subparsers.add_parser(
        "export-dataset", help="Write the complete labeled dataset as JSON"
    )
    export.add_argument("--output", required=True, help="Destination JSON path")
    run = subparsers.add_parser("run", help="Run retrieval systems and print a report")
    run.add_argument(
        "--systems", nargs="+", choices=("like", "fts5"), default=("like", "fts5")
    )
    run.add_argument(
        "--predictions", action="append", default=[], metavar="NAME=PATH",
        help="Score external document-id predictions from JSON (repeatable)",
    )
    run.add_argument("--json", action="store_true", help="Print the full JSON report")
    run.add_argument("--output", help="Also write the full JSON report to this path")
    args = parser.parse_args()

    if args.command == "validate":
        print(json.dumps(validate_dataset(load_dataset()), indent=2, sort_keys=True))
        return
    if args.command == "export-dataset":
        dataset = load_dataset()
        validate_dataset(dataset)
        Path(args.output).write_text(
            json.dumps(dataset, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        print(f"Wrote {len(dataset['queries'])} labeled queries to {args.output}")
        return

    report = run_benchmark(systems=args.systems, prediction_specs=args.predictions)
    if args.output:
        Path(args.output).write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        _print_human(report)


if __name__ == "__main__":
    main()

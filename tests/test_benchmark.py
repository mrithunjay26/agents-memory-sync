import json

import pytest

import benchmark
from benchmarks.retrieval_v1 import CATEGORIES, load_dataset


def test_retrieval_v1_is_balanced_and_within_required_size():
    summary = benchmark.validate_dataset(load_dataset())

    assert summary["queries"] == 180
    assert summary["documents"] == 180
    assert summary["categories"] == {category: 30 for category in sorted(CATEGORIES)}


def test_dataset_rejects_unknown_relevance_document():
    dataset = load_dataset()
    dataset["queries"][0]["relevance"] = {"missing": 3}

    with pytest.raises(ValueError, match="unknown documents"):
        benchmark.validate_dataset(dataset)


def test_metric_calculation_uses_rank_and_relevance_grade():
    dataset = load_dataset()
    query = dataset["queries"][0]
    relevant = next(iter(query["relevance"]))
    distractor = dataset["documents"][1]["id"]
    predictions = {item["id"]: [] for item in dataset["queries"]}
    predictions[query["id"]] = [distractor, relevant]

    result = benchmark.evaluate_predictions(dataset, predictions)["overall"]

    assert result["recall_at_5"] == pytest.approx(1 / 180)
    assert result["mrr"] == pytest.approx(0.5 / 180)
    assert result["task_completion"] is None
    assert result["citation_correctness"] is None


def test_external_predictions_file_is_scored(tmp_path):
    dataset = load_dataset()
    first = dataset["queries"][0]
    prediction_path = tmp_path / "predictions.json"
    prediction_path.write_text(
        json.dumps({"predictions": {first["id"]: list(first["relevance"])}}),
        encoding="utf-8",
    )

    name, result = benchmark._load_predictions(f"dense={prediction_path}", dataset)

    assert name == "dense"
    assert result["overall"]["recall_at_5"] == pytest.approx(1 / 180)


def test_external_predictions_reject_unknown_document_ids():
    dataset = load_dataset()
    first = dataset["queries"][0]

    with pytest.raises(ValueError, match="unknown documents"):
        benchmark.evaluate_predictions(dataset, {first["id"]: ["typo"]})


def test_built_in_benchmark_runs_real_like_and_fts_paths():
    report = benchmark.run_benchmark()

    assert set(report["systems"]) == {"like", "fts5"}
    assert report["indexing"]["document_count"] == 180
    assert report["systems"]["fts5"]["overall"]["query_count"] == 180
    assert report["systems"]["like"]["overall"]["recall_at_5"] > 0.9
    assert report["systems"]["fts5"]["overall"]["recall_at_5"] > 0.9
    assert report["not_run"]["bluebird"]

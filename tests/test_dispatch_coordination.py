import asyncio
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient

import app as app_module
import dispatch
import store


def _source_job(project="/repo"):
    store.register_project(project)
    store.create_dispatch_job(
        "source-job",
        project,
        "claude-code",
        "Plan the future roadmap",
        False,
        context_snapshot="shared snapshot",
    )
    store.update_dispatch_job("source-job", "done", "Embeddings and enterprise controls remain.")
    return store.get_dispatch_job("source-job")


def test_coordinated_jobs_preserve_source_and_per_task_configuration():
    source = _source_job()
    result = dispatch.start_coordinated_jobs(
        source,
        [
            {
                "label": "Embeddings",
                "prompt": "Build semantic retrieval.",
                "agent": "codex",
                "model": "gpt-special",
                "allow_edits": True,
            },
            {
                "label": "Controls",
                "prompt": "Research access control requirements.",
                "agent": "claude-code",
                "model": "",
                "allow_edits": False,
            },
        ],
    )

    jobs = [store.get_dispatch_job(item["job_id"]) for item in result["jobs"]]
    assert len({job["coordination_id"] for job in jobs}) == 1
    assert all(job["parent_job_id"] == "source-job" for job in jobs)
    assert all(job["context_snapshot"] == "shared snapshot" for job in jobs)
    assert jobs[0]["task_label"] == "Embeddings"
    assert jobs[0]["model"] == "gpt-special"
    assert jobs[0]["allow_edits"] is True
    assert "Complete only the assigned task" in jobs[0]["prompt"]
    assert "Embeddings and enterprise controls remain" in jobs[0]["prompt"]


def test_coordination_api_validates_and_queues_one_parallel_batch():
    _source_job()
    app_module.app.dependency_overrides[app_module.require_auth] = lambda: "tester"
    runner = AsyncMock()
    try:
        with TestClient(app_module.app) as client, patch.object(
            app_module, "run_coordinated_jobs", runner
        ):
            response = client.post(
                "/api/dispatch/source-job/coordinate",
                json={
                    "tasks": [
                        {
                            "label": "Search",
                            "prompt": "Implement search.",
                            "agent": "codex",
                            "model": "gpt-search",
                            "allow_edits": True,
                        },
                        {
                            "label": "Review",
                            "prompt": "Review the implementation.",
                            "agent": "claude-code",
                            "allow_edits": False,
                        },
                    ]
                },
            )
        assert response.status_code == 200
        assert len(response.json()["job_ids"]) == 2
        runner.assert_awaited_once()
    finally:
        app_module.app.dependency_overrides.clear()


def test_coordination_api_requires_multiple_tasks():
    _source_job()
    app_module.app.dependency_overrides[app_module.require_auth] = lambda: "tester"
    try:
        with TestClient(app_module.app) as client:
            response = client.post(
                "/api/dispatch/source-job/coordinate",
                json={
                    "tasks": [
                        {"label": "Only", "prompt": "One task", "agent": "codex"}
                    ]
                },
            )
        assert response.status_code == 400
    finally:
        app_module.app.dependency_overrides.clear()


def test_parallel_runner_starts_children_concurrently():
    active = 0
    peak = 0

    async def fake_run(*_args):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0)
        active -= 1

    jobs = [
        {"job_id": "a", "project_path": "/repo", "agent": "codex", "prompt": "a", "allow_edits": False},
        {"job_id": "b", "project_path": "/repo", "agent": "claude-code", "prompt": "b", "allow_edits": False},
    ]
    with patch.object(dispatch, "run_dispatch_job", fake_run):
        asyncio.run(dispatch.run_coordinated_jobs(jobs))
    assert peak == 2


def test_dispatch_turn_prefers_job_model_override():
    store.create_dispatch_job(
        "model-job", "/repo", "codex", "task", False, model="gpt-coordinator"
    )
    adapter = Mock()
    adapter.dispatch = AsyncMock(return_value=("done", 1))

    async def run():
        with patch.object(dispatch, "get_agent_adapter", return_value=adapter), patch.object(
            dispatch, "get_agent_model", return_value="global-model"
        ):
            await dispatch.run_dispatch_job("model-job", "/repo", "codex", "task", False)

    asyncio.run(run())
    assert adapter.dispatch.await_args.kwargs["model"] == "gpt-coordinator"

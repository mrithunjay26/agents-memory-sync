import os

import store


def test_record_event_and_get_context_order_and_limit():
    for i in range(3):
        store.record_event("/repo", "claude-code", "s1", "note", f"event {i}")

    context = store.get_context("/repo", limit=2)

    lines = context.splitlines()
    assert lines[0].startswith("Shared project context for Claude Code and Codex")
    assert "event 1" in lines[3]
    assert "event 2" in lines[4]
    assert "event 0" not in context


def test_get_context_empty_project_returns_empty_string():
    assert store.get_context("/nothing-recorded") == ""


def test_record_event_ignores_blank_summary():
    store.record_event("/repo", "claude-code", "s1", "note", "   ")
    assert store.get_context("/repo") == ""


def test_find_project_root_walks_up_to_git_dir(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    nested = repo / "src" / "sub"
    nested.mkdir(parents=True)

    root = store.find_project_root(str(nested))

    assert root == os.path.normcase(str(repo))


def test_find_project_root_falls_back_to_cwd_when_no_git(tmp_path):
    lonely = tmp_path / "lonely"
    lonely.mkdir()

    root = store.find_project_root(str(lonely))

    assert root == os.path.normcase(str(lonely))


def test_list_projects_includes_registered_project_with_zero_events():
    store.register_project("/empty-project")

    projects = store.list_projects()

    assert len(projects) == 1
    assert projects[0]["project_path"] == "/empty-project"
    assert projects[0]["event_count"] == 0
    assert projects[0]["agents"] == []


def test_list_projects_excludes_telemetry_events_from_counts():
    store.record_event("/repo", "claude-code", "s1", "note", "did a thing")
    store.record_event("/repo", "claude-code", "s1", store.TELEMETRY_EVENT_TYPE, '{"tokens_estimate": 10}')

    projects = store.list_projects()

    assert len(projects) == 1
    assert projects[0]["event_count"] == 1


def test_list_context_injections_returns_only_telemetry_rows():
    store.record_event("/repo", "claude-code", "s1", "note", "visible event")
    store.record_event("/repo", "claude-code", "s1", store.TELEMETRY_EVENT_TYPE, '{"tokens_estimate": 42}')

    injections = store.list_context_injections("/repo")

    assert len(injections) == 1
    assert injections[0]["summary"] == '{"tokens_estimate": 42}'


def test_session_import_is_idempotent():
    assert store.is_session_imported("sess-1") is False

    store.mark_session_imported("sess-1", "/repo", "codex")
    store.mark_session_imported("sess-1", "/repo", "codex")

    assert store.is_session_imported("sess-1") is True


def test_get_conflicts_flags_two_different_agents_within_window():
    store.record_file_touch("/repo", "claude-code", "s1", "file.py", "Edit")
    store.record_file_touch("/repo", "codex", "s2", "file.py", "apply_patch")

    conflicts = store.get_conflicts("/repo", window_minutes=30)

    assert len(conflicts) == 1
    assert {conflicts[0]["agent_a"], conflicts[0]["agent_b"]} == {"claude-code", "codex"}


def test_get_conflicts_ignores_same_agent_touching_same_file():
    store.record_file_touch("/repo", "claude-code", "s1", "file.py", "Edit")
    store.record_file_touch("/repo", "claude-code", "s2", "file.py", "Edit")

    assert store.get_conflicts("/repo", window_minutes=30) == []


def test_list_file_touches_returns_most_recent_first_for_that_file():
    store.record_file_touch("/repo", "claude-code", "s1", "file.py", "Edit")
    store.record_file_touch("/repo", "codex", "s2", "file.py", "apply_patch")
    store.record_file_touch("/repo", "claude-code", "s3", "other.py", "Edit")

    touches = store.list_file_touches("/repo", "file.py")

    assert [t["agent"] for t in touches] == ["codex", "claude-code"]


def test_list_file_touches_empty_for_untouched_file():
    assert store.list_file_touches("/repo", "never-touched.py") == []


def test_dispatch_job_lifecycle():
    store.create_dispatch_job("job-1", "/repo", "claude-code", "do the thing", True)

    job = store.get_dispatch_job("job-1")
    assert job["status"] == "running"
    assert job["allow_edits"] is True
    assert job in store.list_active_dispatch_jobs()

    store.update_dispatch_job("job-1", "done", result_text="finished", tokens=123)

    job = store.get_dispatch_job("job-1")
    assert job["status"] == "done"
    assert job["result_text"] == "finished"
    assert job["tokens"] == 123
    assert store.list_active_dispatch_jobs() == []
    assert job in store.list_dispatch_jobs("/repo")


def test_dispatch_logs_are_ordered_and_filterable_by_since_id():
    store.create_dispatch_job("job-1", "/repo", "claude-code", "prompt", False)
    store.append_dispatch_log("job-1", "first line")
    store.append_dispatch_log("job-1", "second line")

    all_logs = store.list_dispatch_logs("job-1")
    assert [log["line"] for log in all_logs] == ["first line", "second line"]

    tail = store.list_dispatch_logs("job-1", since_id=all_logs[0]["id"])
    assert [log["line"] for log in tail] == ["second line"]


def test_user_creation_and_lookup():
    assert store.has_any_user() is False

    store.create_user("alice", "hashed-password")

    assert store.has_any_user() is True
    user = store.get_user_by_username("alice")
    assert user["username"] == "alice"
    assert user["password_hash"] == "hashed-password"
    assert store.get_user_by_username("nobody") is None

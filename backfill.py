from agents import agent_ids
from history import all_sessions
from store import (
    find_project_root,
    is_session_imported,
    mark_session_imported,
    record_history_event,
)


def backfill_project(project_path: str) -> dict:
    canonical = find_project_root(project_path)
    imported = {agent_id: 0 for agent_id in agent_ids("history")}
    skipped = 0
    refreshed = 0
    for s in all_sessions(project_path):
        was_imported = is_session_imported(s["session_id"])
        if was_imported:
            skipped += 1
        inserted = record_history_event(
            canonical,
            s["agent"],
            s["session_id"],
            s.get("context") or s["summary"],
            s.get("tokens", 0),
            summary=s["summary"],
        )
        mark_session_imported(s["session_id"], project_path, s["agent"])
        if inserted:
            imported[s["agent"]] = imported.get(s["agent"], 0) + 1
        else:
            refreshed += 1
    return {
        "imported_claude": imported.get("claude-code", 0),
        "imported_codex": imported.get("codex", 0),
        "imported_by_agent": imported,
        "already_imported": skipped,
        "refreshed": refreshed,
    }

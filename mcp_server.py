from mcp.server.fastmcp import FastMCP

import code_search
import repository_intelligence
from store import (
    find_project_root,
    get_context,
    list_file_touches,
    record_event,
    search_shared_context,
)
from telemetry import record_context_injection

mcp_app = FastMCP("agent-memory-sync")


@mcp_app.tool()
def get_recent_context(project_path: str) -> str:
    """Required at the start of a project task: fetch the canonical shared
    Claude Code + Codex working set recorded by AgentMemorySync.

    project_path should be the agent's own current working directory (or any
    path inside the project); pass it explicitly, since MCP tool calls
    don't carry an implicit cwd the way a hook payload does. The project's
    configured recent-entry limit is used so this matches every other context
    consumer.
    """
    root = find_project_root(project_path)
    context = get_context(root)
    if context:
        record_context_injection(root, "codex", "", context, route="mcp")
    return context or "(no recorded activity yet)"


@mcp_app.tool()
def search_project_context(project_path: str, query: str, limit: int = 5) -> list[dict]:
    """Search the full shared Claude Code + Codex transcript corpus.

    The compact working set is intentionally small. Use this tool when a task
    needs older detail. Results fuse exact/lexical matches (SQLite FTS5/BM25)
    with hashed n-gram embedding similarity, so a rephrased or reworded query
    can still recall a passage that shares no exact term with it, and never
    filter by the agent that produced them.
    """
    root = find_project_root(project_path)
    return search_shared_context(root, query, limit=limit)


@mcp_app.tool()
def record_note(project_path: str, summary: str, session_id: str = "") -> str:
    """Record a note/decision so other agents see it next time they check context."""
    root = find_project_root(project_path)
    record_event(root, "codex", session_id, "note", summary)
    return "recorded"


@mcp_app.tool()
def search_code(project_path: str, query: str, limit: int = 20) -> list[dict]:
    """Search the indexed source tree with snippets, symbols, and Git provenance."""
    root = find_project_root(project_path)
    return repository_intelligence.search_code(root, query, limit=limit)


@mcp_app.tool()
def explain_symbol(project_path: str, symbol: str) -> dict | None:
    """Explain an indexed Python symbol and its incoming/outgoing relationships."""
    root = find_project_root(project_path)
    return repository_intelligence.explain_symbol(root, symbol)


@mcp_app.tool()
def find_callers(project_path: str, symbol: str, limit: int = 20) -> list[dict]:
    """Find AST-derived Python call sites for a symbol."""
    root = find_project_root(project_path)
    return repository_intelligence.find_references(
        root, symbol, kind="call", limit=limit
    )


@mcp_app.tool()
def find_references(
    project_path: str, symbol: str, kind: str | None = None, limit: int = 100
) -> list[dict]:
    """Find calls, references, tests, imports, or inheritance links to a symbol."""
    root = find_project_root(project_path)
    return repository_intelligence.find_references(
        root, symbol, kind=kind, limit=limit
    )


@mcp_app.tool()
def related_changes(project_path: str, file_path: str, limit: int = 10) -> dict:
    """Recent git history for one file, plus any AgentMemorySync-recorded
    edits to it from Claude Code / Codex dispatch runs.

    Combines `git log` for that path with this project's own file-touch
    ledger, so an agent can see both committed history and other agents that
    touched the same file recently.
    """
    root = find_project_root(project_path)
    return {
        "commits": code_search.recent_commits_for_file(root, file_path, limit=limit),
        "agent_touches": list_file_touches(root, file_path, limit=limit),
    }


@mcp_app.tool()
def get_architecture(project_path: str, directory: str = "") -> dict:
    """Return repository or directory hierarchy, summaries, symbols, and links."""
    root = find_project_root(project_path)
    return repository_intelligence.get_repository_map(root, directory=directory)


@mcp_app.tool()
def index_repository(project_path: str) -> dict:
    """Incrementally refresh repository files, symbols, relationships, and Git data."""
    return repository_intelligence.index_repository(find_project_root(project_path))


@mcp_app.tool()
def index_status(project_path: str) -> dict:
    """Report whether/when this project's repository code index has been built.

    Separate from the agent-history corpus (see get_recent_context). Reports
    the repo_files/repo_symbols index status, which may not exist yet
    depending on schema version or whether indexing has run for this project.
    """
    root = find_project_root(project_path)
    return repository_intelligence.get_index_status(root)


if __name__ == "__main__":
    mcp_app.run(transport="stdio")

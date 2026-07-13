import glob
import json
import os

from agents import all_agent_adapters
from claude_memory import claude_projects_dir, encode_claude_project_path, resolve_claude_project_dir

MAX_SUMMARY = 500


def _json_text(value) -> str:
    if isinstance(value, str):
        return value.strip()
    try:
        return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True).strip()
    except (TypeError, ValueError):
        return str(value).strip()


def _join_context(parts: list[str]) -> str:
    return "\n\n".join(part.strip() for part in parts if part and part.strip())


def _norm(path: str) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(path)))



def _claude_project_dir(project_path: str) -> str:
    return resolve_claude_project_dir(project_path)


def _claude_folders_for(project_path: str) -> list[str]:
    """Every ~/.claude/projects folder whose working directory was the repo
    root OR any subdirectory of it.

    Claude Code stores one folder per cwd (the full path with ':' and '\\'
    replaced by '-'). A session started in a subdir of the repo encodes as
    <repo-encoded>-<subdir...>, so we match the root exactly plus anything
    with the root as a '-'-separated prefix. Matching follows the host
    filesystem's case rules via os.path.normcase().
    """
    projects = claude_projects_dir()
    if not os.path.isdir(projects):
        return []
    enc = os.path.normcase(encode_claude_project_path(project_path))
    folders = []
    for name in os.listdir(projects):
        full = os.path.join(projects, name)
        if not os.path.isdir(full):
            continue
        canonical = os.path.normcase(name)
        if canonical == enc or canonical.startswith(enc + "-"):
            folders.append(full)
    return folders


def claude_sessions(project_path: str) -> list[dict]:
    sessions = []
    for folder in _claude_folders_for(project_path):
        for path in glob.glob(os.path.join(folder, "*.jsonl")):
            session_id = os.path.splitext(os.path.basename(path))[0]
            summary, tokens, context = _read_claude_session(path)
            if summary:
                sessions.append(
                    {
                        "session_id": session_id,
                        "agent": "claude-code",
                        "summary": summary,
                        "tokens": tokens,
                        "context": context,
                        "mtime": os.path.getmtime(path),
                    }
                )
    return sessions


def _summarize_claude(path: str) -> tuple[str, int]:
    summary, tokens, _context = _read_claude_session(path)
    return summary, tokens


def _read_claude_session(path: str) -> tuple[str, int, str]:
    """Read the useful native transcript, including tool evidence.

    Hidden thinking blocks and provider metadata are intentionally excluded;
    user/assistant text plus tool calls/results are the durable project
    context another agent can actually use.
    """
    first_user = ""
    last_assistant = ""
    total_tokens = 0
    context_parts: list[str] = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                t = e.get("type")
                msg = e.get("message", {}) if isinstance(e.get("message"), dict) else {}
                if t == "user":
                    text = _text_from_content(msg.get("content"))
                    if text and not first_user:
                        first_user = text
                    rendered = _claude_content(msg.get("content"))
                    if rendered:
                        context_parts.append("User:\n" + rendered)
                elif t == "assistant":
                    text = _text_from_content(msg.get("content"))
                    if text:
                        last_assistant = text
                    rendered = _claude_content(msg.get("content"))
                    if rendered:
                        context_parts.append("Assistant:\n" + rendered)
                    usage = msg.get("usage", {}) or {}
                    total_tokens += (usage.get("input_tokens", 0) or 0) + (
                        usage.get("output_tokens", 0) or 0
                    )
    except OSError:
        return "", 0, ""
    return _compose(first_user, last_assistant), total_tokens, _join_context(context_parts)


def _claude_content(content) -> str:
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "text" and block.get("text", "").strip():
            parts.append(block["text"].strip())
        elif block_type == "tool_use":
            detail = _json_text(block.get("input", {}))
            parts.append(f"[Tool call: {block.get('name', 'tool')}]\n{detail}".rstrip())
        elif block_type == "tool_result":
            detail = _claude_content(block.get("content")) or _json_text(block.get("content", ""))
            if detail:
                parts.append("[Tool result]\n" + detail)
    return _join_context(parts)


def _text_from_content(content) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [
            b.get("text", "")
            for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        ]
        return " ".join(p for p in parts if p).strip()
    return ""



def _codex_sessions_root() -> str:
    return os.path.join(os.path.expanduser("~"), ".codex", "sessions")


def codex_sessions(project_path: str) -> list[dict]:
    root = _codex_sessions_root()
    if not os.path.isdir(root):
        return []
    target = _norm(project_path)
    sessions = []
    for path in glob.glob(os.path.join(root, "**", "rollout-*.jsonl"), recursive=True):
        meta = _codex_meta(path)
        if not meta:
            continue
        cwd = meta.get("cwd")
        if not cwd:
            continue
        ncwd = _norm(cwd)
        if ncwd != target and not ncwd.startswith(target + os.sep):
            continue
        summary, tokens, context = _read_codex_session(path)
        if summary:
            sessions.append(
                {
                    "session_id": meta.get("id") or os.path.basename(path),
                    "agent": "codex",
                    "summary": summary,
                    "tokens": tokens,
                    "context": context,
                    "mtime": os.path.getmtime(path),
                }
            )
    return sessions


def _codex_meta(path: str) -> dict | None:
    try:
        with open(path, "r", encoding="utf-8") as f:
            first = f.readline().strip()
        if not first:
            return None
        e = json.loads(first)
        if e.get("type") != "session_meta":
            return None
        return e.get("payload", {})
    except (OSError, json.JSONDecodeError):
        return None


def _summarize_codex(path: str) -> tuple[str, int]:
    summary, tokens, _context = _read_codex_session(path)
    return summary, tokens


def _codex_message_text(content) -> str:
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if not isinstance(block, dict):
            continue
        text = block.get("text") or block.get("input_text") or block.get("output_text")
        if text:
            parts.append(str(text).strip())
    return _join_context(parts)


def _read_codex_session(path: str) -> tuple[str, int, str]:
    first_user = ""
    last_agent = ""
    total_tokens = 0
    context_parts: list[str] = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                p = e.get("payload", {})
                pt = p.get("type")
                if pt == "user_message" and not first_user:
                    first_user = (p.get("message") or "").strip()
                elif pt == "agent_message":
                    m = (p.get("message") or "").strip()
                    if m:
                        last_agent = m
                elif pt == "token_count":
                    usage = (p.get("info") or {}).get("total_token_usage") or {}
                    if usage.get("total_tokens"):
                        total_tokens = usage["total_tokens"]
                if e.get("type") != "response_item":
                    continue
                if pt == "message" and p.get("role") in ("user", "assistant"):
                    text = _codex_message_text(p.get("content"))
                    if text:
                        role = "User" if p.get("role") == "user" else "Assistant"
                        context_parts.append(f"{role}:\n{text}")
                elif pt in ("custom_tool_call", "function_call"):
                    detail = _json_text(p.get("input", p.get("arguments", "")))
                    context_parts.append(
                        f"[Tool call: {p.get('name', 'tool')}]\n{detail}".rstrip()
                    )
                elif pt in ("custom_tool_call_output", "function_call_output"):
                    detail = _json_text(p.get("output", ""))
                    if detail:
                        context_parts.append("[Tool result]\n" + detail)
    except OSError:
        return "", 0, ""
    return _compose(first_user, last_agent), total_tokens, _join_context(context_parts)


def codex_token_total(project_path: str) -> int:
    total = 0
    for s in codex_sessions(project_path):
        total += s.get("tokens", 0) or 0
    return total



def _compose(first_user: str, last_agent: str) -> str:
    first_user = " ".join(first_user.split())
    last_agent = " ".join(last_agent.split())
    if first_user and last_agent:
        s = f"Asked: {first_user[:200]} | Result: {last_agent[:280]}"
    else:
        s = first_user or last_agent
    return s[:MAX_SUMMARY].strip()


def all_sessions(project_path: str) -> list[dict]:
    sessions = []
    for adapter in all_agent_adapters():
        if adapter.capabilities.history:
            sessions.extend(adapter.history_sessions(project_path))
    sessions.sort(key=lambda s: s["mtime"])
    return sessions

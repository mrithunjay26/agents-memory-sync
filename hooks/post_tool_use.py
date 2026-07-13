#!/usr/bin/env python3
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agents import detect_hook_agent
from store import find_project_root, record_file_touch

FILE_EDIT_TOOLS = {"Edit", "Write", "NotebookEdit", "apply_patch"}
PATH_FIELDS = ("file_path", "path", "notebook_path", "target_file", "file")
PATCH_HEADER_RE = re.compile(r"\*\*\* (?:Update|Add|Delete) File: (.+)")


def _extract_file_path(tool_input) -> str:
    if not isinstance(tool_input, dict):
        return ""
    for key in PATH_FIELDS:
        value = tool_input.get(key)
        if isinstance(value, str) and value:
            return value
    command = tool_input.get("command")
    if isinstance(command, str):
        match = PATCH_HEADER_RE.search(command)
        if match:
            return match.group(1).strip()
    return ""


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        return

    tool_name = payload.get("tool_name", "")
    if tool_name not in FILE_EDIT_TOOLS:
        return

    file_path = _extract_file_path(payload.get("tool_input"))
    if not file_path:
        return

    cwd = payload.get("cwd", os.getcwd())
    project_path = find_project_root(cwd)
    session_id = payload.get("session_id", "")
    agent = detect_hook_agent(payload).agent_id

    record_file_touch(project_path, agent, session_id, file_path, tool_name)


if __name__ == "__main__":
    main()

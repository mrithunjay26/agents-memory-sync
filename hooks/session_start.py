#!/usr/bin/env python3
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agents import detect_hook_agent
from store import find_project_root, get_context
from telemetry import record_context_injection


def main() -> None:
    if os.environ.get("AGENT_MEMORY_SYNC_CONTEXT_PREINJECTED") == "1":
        return

    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        return

    cwd = payload.get("cwd", os.getcwd())
    project_path = find_project_root(cwd)
    adapter = detect_hook_agent(payload)
    agent = adapter.agent_id
    context = get_context(project_path)
    if not context:
        return

    try:
        record_context_injection(
            project_path,
            agent,
            payload.get("session_id", ""),
            context,
            route="hook",
        )
    except Exception:
        pass

    print(json.dumps(adapter.context_response(context)))


if __name__ == "__main__":
    main()

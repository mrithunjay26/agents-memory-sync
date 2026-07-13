#!/usr/bin/env python3
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agents import detect_hook_agent
from store import find_project_root, record_event

MAX_SUMMARY_CHARS = 400


def _extract_from_transcript(transcript_path: str) -> str:
    if not transcript_path or not os.path.isfile(transcript_path):
        return ""
    try:
        with open(transcript_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return ""

    for line in reversed(lines[-200:]):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("type") != "assistant":
            continue
        message = entry.get("message", {})
        content = message.get("content", [])
        if isinstance(content, str):
            return content
        texts = [
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        text = " ".join(t for t in texts if t).strip()
        if text:
            return text
    return ""


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        payload = {}

    cwd = payload.get("cwd", os.getcwd())
    project_path = find_project_root(cwd)
    session_id = payload.get("session_id", "")
    adapter = detect_hook_agent(payload)
    agent = adapter.agent_id

    summary = adapter.extract_stop_summary(payload, _extract_from_transcript)
    if summary:
        summary = " ".join(summary.split())[:MAX_SUMMARY_CHARS]
        record_event(project_path, agent, session_id, "turn", summary)

    response = adapter.stop_response()
    if response is not None:
        print(json.dumps(response))


if __name__ == "__main__":
    main()

import json
import hashlib
import os

from agents import all_agent_adapters
from store import get_context_bundle, list_context_injections, native_usage_tokens_as_of

USAGE_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)

def claude_transcript_files(project_path: str) -> list[str]:
    from history import _claude_folders_for

    files = []
    for folder in _claude_folders_for(project_path):
        for name in os.listdir(folder):
            if name.endswith(".jsonl"):
                files.append(os.path.join(folder, name))
    return files


def sum_transcript_tokens(project_path: str) -> dict:
    totals = {field: 0 for field in USAGE_FIELDS}
    for path in claude_transcript_files(project_path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if entry.get("type") != "assistant":
                        continue
                    usage = entry.get("message", {}).get("usage", {}) or {}
                    for field in USAGE_FIELDS:
                        totals[field] += usage.get(field, 0) or 0
        except OSError:
            continue
    return totals


def estimate_context_tokens(text: str) -> int:
    return (len(text) + 3) // 4


def record_context_injection(
    project_path: str,
    agent: str,
    session_id: str,
    context_text: str,
    route: str = "hook",
) -> None:
    from store import record_event

    tokens = estimate_context_tokens(context_text)
    native_usage_tokens = get_context_bundle(project_path)["active_native_usage_tokens"]
    record_event(
        project_path,
        agent,
        session_id,
        "context_injected",
        json.dumps(
            {
                "tokens_estimate": tokens,
                "native_usage_tokens": native_usage_tokens,
                "content_hash": hashlib.sha256(context_text.encode("utf-8")).hexdigest(),
                "route": route,
            },
            sort_keys=True,
        ),
    )


def _delivery_summary(
    project_path: str, current_hash: str
) -> tuple[int, int, int, int, dict]:
    total = 0
    native_total = 0
    saved_total = 0
    baseline_deliveries = 0
    by_agent: dict[str, dict] = {
        adapter.agent_id: {
            "deliveries": 0,
            "tokens": 0,
            "tokens_saved": 0,
            "last_hash": None,
            "has_current_context": False,
        }
        for adapter in all_agent_adapters()
        if adapter.capabilities.context_injection
    }
    for event in reversed(list_context_injections(project_path)):
        try:
            payload = json.loads(event["summary"])
        except (json.JSONDecodeError, AttributeError):
            continue
        tokens = max(0, int(payload.get("tokens_estimate", 0) or 0))
        native_usage = payload.get("native_usage_tokens")
        if native_usage is None:
            native_usage = native_usage_tokens_as_of(project_path, event["created_at"])
        native_usage = max(0, int(native_usage or 0))
        saved = max(0, native_usage - tokens)
        total += tokens
        native_total += native_usage
        saved_total += saved
        if native_usage > 0:
            baseline_deliveries += 1
        agent = event.get("agent")
        if agent not in by_agent:
            by_agent[agent] = {
                "deliveries": 0,
                "tokens": 0,
                "tokens_saved": 0,
                "last_hash": None,
                "has_current_context": False,
            }
        row = by_agent[agent]
        row["deliveries"] += 1
        row["tokens"] += tokens
        row["tokens_saved"] += saved
        row["last_hash"] = payload.get("content_hash")
        row["last_route"] = payload.get("route", "legacy")
        row["last_delivered_at"] = event.get("created_at")
        row["has_current_context"] = bool(current_hash and row["last_hash"] == current_hash)
    return total, native_total, saved_total, baseline_deliveries, by_agent


def get_telemetry_summary(project_path: str) -> dict:
    claude = sum_transcript_tokens(project_path)
    agent_tokens = {}
    for adapter in all_agent_adapters():
        if not adapter.capabilities.usage:
            continue
        try:
            agent_tokens[adapter.agent_id] = adapter.usage_tokens(project_path)
        except Exception:
            agent_tokens[adapter.agent_id] = 0

    claude_total = agent_tokens.get("claude-code", 0)
    codex_total = agent_tokens.get("codex", 0)
    measured_total = sum(agent_tokens.values())
    context_bundle = get_context_bundle(project_path)
    native_usage_tokens = context_bundle["active_native_usage_tokens"]
    (
        context_delivered_tokens,
        native_snapshot_total,
        verified_tokens_saved,
        baseline_delivery_count,
        delivery_by_agent,
    ) = _delivery_summary(project_path, context_bundle["content_hash"])
    corpus_tokens = context_bundle["corpus_tokens"]
    active_tokens = context_bundle["token_estimate"]
    compression_percent = (
        round((1 - (active_tokens / corpus_tokens)) * 100, 1)
        if corpus_tokens
        else 0.0
    )
    has_baseline = native_snapshot_total > 0
    return {
        "claude_tokens": claude_total,
        "codex_tokens": codex_total,
        "agent_tokens": agent_tokens,
        "measured_total_tokens": measured_total,
        "measured": claude,
        "context_delivered_tokens": context_delivered_tokens,
        "delivery_native_tokens": native_snapshot_total,
        "context_delivery_count": sum(
            row["deliveries"] for row in delivery_by_agent.values()
        ),
        "baseline_delivery_count": baseline_delivery_count,
        "pooled_context_tokens": active_tokens,
        "pooled_source_tokens": corpus_tokens,
        "native_usage_tokens": native_usage_tokens,
        "archived_context_tokens": context_bundle["archived_token_estimate"],
        "context_compression_percent": compression_percent,
        "delivery_by_agent": delivery_by_agent,
        "exclusive_context_entries": context_bundle["counts"]["exclusive"],
        "visible_to": context_bundle["visible_to"],
        "verified_tokens_saved": verified_tokens_saved if has_baseline else None,
        "efficiency_gain_percent": (
            round((verified_tokens_saved / native_snapshot_total) * 100, 1)
            if has_baseline
            else None
        ),
        "baseline_status": "measured" if has_baseline else "not_established",
        "estimated_tokens_saved": None,
        "methodology": (
            "Claude and Codex usage are measured independently from native histories. "
            "Shared corpus and active-context sizes estimate text tokens at four "
            "characters per token; compression is a measured size ratio, not model "
            "token savings. Verified savings compare, per delivery, the real native "
            "usage tokens it cost to originally produce the pooled session history "
            "against the compact digest actually delivered in its place, both "
            "measured values already recorded in the store, not a modeled control run."
        ),
    }

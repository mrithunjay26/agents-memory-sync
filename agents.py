from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from typing import Any, Awaitable, Callable


LogCallback = Callable[[dict], None]
TranscriptReader = Callable[[str], str]


@dataclass(frozen=True)
class AgentCapabilities:
    capture: bool = True
    history: bool = True
    dispatch: bool = True
    context_injection: bool = True
    usage: bool = True


class AgentAdapter(ABC):
    agent_id: str
    display_name: str
    capabilities = AgentCapabilities()

    @abstractmethod
    def matches_hook_payload(self, payload: dict[str, Any]) -> bool:
        ...

    @abstractmethod
    def extract_stop_summary(
        self, payload: dict[str, Any], transcript_reader: TranscriptReader
    ) -> str:
        ...

    @abstractmethod
    def history_sessions(self, project_path: str) -> list[dict]:
        ...

    @abstractmethod
    async def dispatch(
        self,
        project_path: str,
        prompt: str,
        allow_edits: bool,
        on_log: LogCallback,
        env: dict | None = None,
        resume_session_id: str | None = None,
        model: str | None = None,
    ) -> tuple[str, int]:
        ...

    @abstractmethod
    def resolve_binary(self) -> str:
        ...

    @abstractmethod
    def usage_tokens(self, project_path: str) -> int:
        ...

    def context_response(self, context: str) -> dict:
        return {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": context,
            }
        }

    def stop_response(self) -> dict | None:
        """Some hook harnesses require a JSON response even for a no-op."""
        return None

    def public_metadata(self) -> dict:
        return {
            "id": self.agent_id,
            "display_name": self.display_name,
            "capabilities": asdict(self.capabilities),
        }


class ClaudeCodeAdapter(AgentAdapter):
    agent_id = "claude-code"
    display_name = "Claude Code"

    def matches_hook_payload(self, payload: dict[str, Any]) -> bool:
        return False

    def extract_stop_summary(
        self, payload: dict[str, Any], transcript_reader: TranscriptReader
    ) -> str:
        return transcript_reader(payload.get("transcript_path", ""))

    def history_sessions(self, project_path: str) -> list[dict]:
        from history import claude_sessions

        return claude_sessions(project_path)

    async def dispatch(
        self,
        project_path: str,
        prompt: str,
        allow_edits: bool,
        on_log: LogCallback,
        env: dict | None = None,
        resume_session_id: str | None = None,
        model: str | None = None,
    ) -> tuple[str, int]:
        from dispatch import dispatch_claude

        kwargs = {"env": env, "model": model}
        if resume_session_id is not None:
            kwargs["resume_session_id"] = resume_session_id
        return await dispatch_claude(project_path, prompt, allow_edits, on_log, **kwargs)

    def resolve_binary(self) -> str:
        from dispatch import resolve_claude_binary

        return resolve_claude_binary()

    def usage_tokens(self, project_path: str) -> int:
        from telemetry import sum_transcript_tokens

        return sum(sum_transcript_tokens(project_path).values())


class CodexAdapter(AgentAdapter):
    agent_id = "codex"
    display_name = "Codex"

    def matches_hook_payload(self, payload: dict[str, Any]) -> bool:
        return "turn_id" in payload

    def extract_stop_summary(
        self, payload: dict[str, Any], transcript_reader: TranscriptReader
    ) -> str:
        return payload.get("last_assistant_message", "")

    def history_sessions(self, project_path: str) -> list[dict]:
        from history import codex_sessions

        return codex_sessions(project_path)

    async def dispatch(
        self,
        project_path: str,
        prompt: str,
        allow_edits: bool,
        on_log: LogCallback,
        env: dict | None = None,
        resume_session_id: str | None = None,
        model: str | None = None,
    ) -> tuple[str, int]:
        from dispatch import dispatch_codex

        kwargs = {"env": env, "model": model}
        if resume_session_id is not None:
            kwargs["resume_session_id"] = resume_session_id
        return await dispatch_codex(project_path, prompt, allow_edits, on_log, **kwargs)

    def resolve_binary(self) -> str:
        from dispatch import resolve_codex_binary

        return resolve_codex_binary()

    def usage_tokens(self, project_path: str) -> int:
        from history import codex_token_total

        return codex_token_total(project_path)

    def stop_response(self) -> dict | None:
        return {"continue": True}


_ADAPTERS = (ClaudeCodeAdapter(), CodexAdapter())
_BY_ID = {adapter.agent_id: adapter for adapter in _ADAPTERS}
DEFAULT_HOOK_AGENT = "claude-code"


def all_agent_adapters() -> tuple[AgentAdapter, ...]:
    return _ADAPTERS


def agent_ids(capability: str | None = None) -> tuple[str, ...]:
    adapters = _ADAPTERS
    if capability is not None:
        if not hasattr(AgentCapabilities, capability):
            raise ValueError(f"Unknown agent capability: {capability}")
        adapters = tuple(
            adapter
            for adapter in adapters
            if getattr(adapter.capabilities, capability)
        )
    return tuple(adapter.agent_id for adapter in adapters)


def get_agent_adapter(agent_id: str, capability: str | None = None) -> AgentAdapter:
    adapter = _BY_ID.get(agent_id)
    if adapter is None:
        raise ValueError(
            f"Unsupported agent {agent_id!r}; expected one of {', '.join(agent_ids())}."
        )
    if capability is not None and not getattr(adapter.capabilities, capability, False):
        raise ValueError(f"Agent {agent_id!r} does not support {capability}.")
    return adapter


def detect_hook_agent(payload: dict[str, Any]) -> AgentAdapter:
    for adapter in _ADAPTERS:
        if adapter.matches_hook_payload(payload):
            return adapter
    return get_agent_adapter(DEFAULT_HOOK_AGENT, "capture")

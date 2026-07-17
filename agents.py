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


class LocalOpenAIAdapter(AgentAdapter):
    """Adapter for a self-hosted, OpenAI-compatible chat endpoint (Ollama, vLLM, LM Studio, ...).

    Phase 2 of the local-model rollout: read-only chat dispatch, no tool loop yet,
    so edits are always rejected regardless of the caller's allow_edits flag.
    """

    capabilities = AgentCapabilities(
        capture=False, history=False, dispatch=True, context_injection=True, usage=True
    )

    def __init__(
        self,
        agent_id: str,
        display_name: str,
        base_url: str,
        model: str,
        api_key_env: str | None = None,
    ):
        self.agent_id = agent_id
        self.display_name = display_name
        self.base_url = base_url.rstrip("/")
        self.default_model = model
        self.api_key_env = api_key_env

    def matches_hook_payload(self, payload: dict[str, Any]) -> bool:
        return False

    def extract_stop_summary(
        self, payload: dict[str, Any], transcript_reader: TranscriptReader
    ) -> str:
        return ""

    def history_sessions(self, project_path: str) -> list[dict]:
        return []

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key_env:
            import os

            key = os.environ.get(self.api_key_env)
            if key:
                headers["Authorization"] = f"Bearer {key}"
        return headers

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
        if allow_edits:
            raise RuntimeError(
                f"{self.display_name} is a read-only local model lane; it has no tool "
                "loop yet, so it cannot make edits. Deploy with edits off."
            )
        from dispatch import dispatch_local_openai

        return await dispatch_local_openai(
            self.base_url, model or self.default_model, self._headers(), prompt, on_log
        )

    def resolve_binary(self) -> str:
        import httpx

        resp = httpx.get(f"{self.base_url}/v1/models", headers=self._headers(), timeout=3.0)
        resp.raise_for_status()
        return self.base_url

    def usage_tokens(self, project_path: str) -> int:
        from store import sum_dispatch_tokens

        return sum_dispatch_tokens(project_path, self.agent_id)


_BUILTIN_ADAPTERS = (ClaudeCodeAdapter(), CodexAdapter())
_BUILTIN_BY_ID = {adapter.agent_id: adapter for adapter in _BUILTIN_ADAPTERS}
DEFAULT_HOOK_AGENT = "claude-code"


def _local_adapters() -> tuple[AgentAdapter, ...]:
    from store import list_local_providers

    return tuple(
        LocalOpenAIAdapter(
            provider["agent_id"],
            provider["display_name"],
            provider["base_url"],
            provider["model"],
            provider["api_key_env"],
        )
        for provider in list_local_providers()
    )


def all_agent_adapters() -> tuple[AgentAdapter, ...]:
    return _BUILTIN_ADAPTERS + _local_adapters()


def agent_ids(capability: str | None = None) -> tuple[str, ...]:
    adapters = all_agent_adapters()
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
    adapter = _BUILTIN_BY_ID.get(agent_id)
    if adapter is None:
        from store import get_local_provider

        provider = get_local_provider(agent_id)
        if provider:
            adapter = LocalOpenAIAdapter(
                provider["agent_id"],
                provider["display_name"],
                provider["base_url"],
                provider["model"],
                provider["api_key_env"],
            )
    if adapter is None:
        raise ValueError(
            f"Unsupported agent {agent_id!r}; expected one of {', '.join(agent_ids())}."
        )
    if capability is not None and not getattr(adapter.capabilities, capability, False):
        raise ValueError(f"Agent {agent_id!r} does not support {capability}.")
    return adapter


def detect_hook_agent(payload: dict[str, Any]) -> AgentAdapter:
    for adapter in _BUILTIN_ADAPTERS:
        if adapter.matches_hook_payload(payload):
            return adapter
    return get_agent_adapter(DEFAULT_HOOK_AGENT, "capture")

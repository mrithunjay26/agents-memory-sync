import asyncio
import contextlib
import contextvars
import glob
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import uuid

from agents import get_agent_adapter
from store import (
    CONTEXT_CATEGORIES,
    append_dispatch_log,
    create_dispatch_interaction,
    create_dispatch_job,
    get_agent_model,
    get_context,
    get_dispatch_job,
    record_event,
    set_dispatch_canceling,
    update_context_event,
    update_dispatch_job,
    update_dispatch_progress,
    update_dispatch_session,
)

CONTEXT_PREINJECTED_ENV = "AGENT_MEMORY_SYNC_CONTEXT_PREINJECTED"


class DispatchCancelled(Exception):
    pass


_CURRENT_JOB_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "dispatch_job_id", default=None
)
_ACTIVE_PROCESSES: dict[str, asyncio.subprocess.Process] = {}
_CANCEL_REQUESTED: set[str] = set()

INTERACTION_INSTRUCTIONS = """
You are running as a dashboard-managed background agent. Complete all routine,
in-scope work autonomously; do not ask for permission to read, edit, test, or
run ordinary project commands already authorized by the task and sandbox. If a
missing decision or explicit high-risk confirmation makes progress impossible,
end your response with exactly one machine-readable marker:
<agentmemorysync_interaction>{"kind":"question","prompt":"concise request","options":["Choice A","Choice B"]}</agentmemorysync_interaction>
Use kind approval, question, or confirmation. Use an empty options array when
the user should type a free-form answer. If you present alternatives and need
the user to choose before continuing, do not end with only a prose question:
emit the marker with short user-facing option labels so the dashboard can show
the choices and resume your session immediately after the user answers.
Do not emit that marker when you can make a safe, reasonable assumption.
""".strip()

_INTERACTION_RE = re.compile(
    r"<agentmemorysync_interaction>\s*(\{.*?\})\s*</agentmemorysync_interaction>",
    re.DOTALL | re.IGNORECASE,
)
_APPROVAL_FALLBACK_RE = re.compile(
    r"(?:requires?|needs?) (?:user )?approval|(?:waiting|wait) for (?:your|user) approval|"
    r"need (?:your|user) permission",
    re.IGNORECASE,
)
_ACTIONABLE_QUESTION_RE = re.compile(
    r"\b(?:which|what|choose|select|prefer|want me to|would you like|should i|shall i)\b",
    re.IGNORECASE,
)
_MARKDOWN_OPTION_RE = re.compile(
    r"^\s*(?P<marker>\d{1,2}[.)]|[-*])\s+(?P<body>.+?)\s*$"
)

RECATEGORIZE_INSTRUCTIONS = """
You are recategorizing pooled context entries for a project dashboard. Valid
categories are: {categories}. Decide the new category for each entry listed
below using the developer's instructions, then respond with a single line
containing ONLY this marker (no other text on that line), listing just the
entries whose category should change, omitting any entry that should keep its
current category:
<agentmemorysync_recategorize>{{"assignments": [{{"id": 123, "category": "decision"}}]}}</agentmemorysync_recategorize>
Do not edit any files; this is a read-only classification task.
""".strip()

_RECATEGORIZE_RE = re.compile(
    r"<agentmemorysync_recategorize>\s*(\{.*?\})\s*</agentmemorysync_recategorize>",
    re.DOTALL | re.IGNORECASE,
)


def build_recategorize_prompt(entries: list[dict], instructions: str) -> str:
    lines = [
        f"- id={entry['id']} category={entry['category']} :: "
        + _brief(entry.get("effective_summary") or entry.get("summary") or "", 200)
        for entry in entries
    ]
    return (
        RECATEGORIZE_INSTRUCTIONS.format(categories=", ".join(CONTEXT_CATEGORIES))
        + "\n\nEntries:\n"
        + "\n".join(lines)
        + "\n\nDeveloper's instructions:\n"
        + instructions.strip()
    )


def _extract_recategorization(result: str) -> tuple[str, list[dict]]:
    match = _RECATEGORIZE_RE.search(result or "")
    if not match:
        return result, []
    try:
        value = json.loads(match.group(1))
    except (json.JSONDecodeError, TypeError):
        return result, []
    raw_assignments = value.get("assignments") if isinstance(value, dict) else None
    if not isinstance(raw_assignments, list):
        return result, []
    clean = ((result or "")[: match.start()] + (result or "")[match.end() :]).strip()
    assignments = []
    for item in raw_assignments:
        if not isinstance(item, dict):
            continue
        try:
            event_id = int(item.get("id"))
        except (TypeError, ValueError):
            continue
        category = str(item.get("category") or "").strip()
        if not category:
            continue
        assignments.append({"id": event_id, "category": category})
    return clean, assignments


def _apply_recategorization(project_path: str, assignments: list[dict]) -> int:
    applied = 0
    for item in assignments:
        try:
            update_context_event(project_path, item["id"], category=item["category"])
            applied += 1
        except (LookupError, ValueError):
            continue
    return applied


def _first_glob(*patterns: str) -> str | None:
    matches = []
    for pattern in patterns:
        matches.extend(p for p in glob.glob(pattern) if os.path.isfile(p))
    if not matches:
        return None
    return max(matches, key=os.path.getmtime)


def _win_dirs() -> tuple[str, str]:
    home = os.path.expanduser("~")
    appdata = os.environ.get("APPDATA") or os.path.join(home, "AppData", "Roaming")
    local = os.environ.get("LOCALAPPDATA") or os.path.join(home, "AppData", "Local")
    return appdata, local


def _auto_detect_claude() -> str | None:
    home = os.path.expanduser("~")
    appdata, local = _win_dirs()
    return _first_glob(
        os.path.join(appdata, "Claude", "claude-code", "*", "claude.exe"),
        os.path.join(local, "Claude", "claude-code", "*", "claude.exe"),
        os.path.join(home, "AppData", "Roaming", "Claude", "claude-code", "*", "claude.exe"),
        os.path.join(
            home, "AppData", "Local", "Packages", "Claude_*", "LocalCache",
            "Roaming", "Claude", "claude-code", "*", "claude.exe",
        ),
        os.path.join(appdata, "npm", "claude.cmd"),
        os.path.join(appdata, "npm", "claude.exe"),
        os.path.join(home, ".local", "bin", "claude.exe"),
        os.path.join(home, ".local", "bin", "claude"),
    )


def _auto_detect_codex() -> str | None:
    home = os.path.expanduser("~")
    appdata, local = _win_dirs()
    return _first_glob(
        os.path.join(local, "OpenAI", "Codex", "bin", "*", "codex.exe"),
        os.path.join(home, "AppData", "Local", "OpenAI", "Codex", "bin", "*", "codex.exe"),
        os.path.join(appdata, "npm", "codex.cmd"),
        os.path.join(home, ".local", "bin", "codex.exe"),
        os.path.join(home, ".local", "bin", "codex"),
    )


def resolve_claude_binary() -> str:
    path = (
        shutil.which("claude")
        or os.environ.get("AGENT_MEMORY_SYNC_CLAUDE_BIN")
        or _auto_detect_claude()
    )
    if not path:
        raise RuntimeError(
            "Could not find the Claude Code CLI. Set the AGENT_MEMORY_SYNC_CLAUDE_BIN "
            "environment variable to the full path of claude.exe BEFORE starting the "
            "dashboard (find it with: Get-ChildItem $HOME -Recurse -Filter claude.exe "
            "-ErrorAction SilentlyContinue), then restart."
        )
    return path


def resolve_codex_binary() -> str:
    path = (
        shutil.which("codex")
        or os.environ.get("AGENT_MEMORY_SYNC_CODEX_BIN")
        or _auto_detect_codex()
    )
    if not path:
        raise RuntimeError(
            "Could not find the Codex CLI. Set AGENT_MEMORY_SYNC_CODEX_BIN to the full "
            "path of codex.exe (usually under "
            "%LOCALAPPDATA%\\OpenAI\\Codex\\bin\\<id>\\codex.exe)."
        )
    return path


async def _stream(
    args: list[str],
    cwd: str,
    on_line,
    env: dict | None = None,
    stdin_text: str | None = None,
) -> tuple[int, str]:
    process_group: dict = {}
    if os.name == "nt":
        process_group["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        process_group["start_new_session"] = True
    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=cwd,
        stdin=(asyncio.subprocess.PIPE if stdin_text is not None else asyncio.subprocess.DEVNULL),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        limit=2**24,
        env=env,
        **process_group,
    )
    job_id = _CURRENT_JOB_ID.get()
    if job_id:
        _ACTIVE_PROCESSES[job_id] = proc
        if job_id in _CANCEL_REQUESTED:
            await _terminate_process(proc)
    chunks = []
    stdout = proc.stdout
    assert stdout is not None
    feed_task = None
    if stdin_text is not None:
        assert proc.stdin is not None

        async def feed_prompt() -> None:
            try:
                proc.stdin.write(stdin_text.encode("utf-8"))
                await proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                proc.stdin.close()

        feed_task = asyncio.create_task(feed_prompt())
    try:
        while True:
            try:
                raw = await stdout.readline()
            except (asyncio.LimitOverrunError, ValueError):
                try:
                    raw = await stdout.read(2**24)
                except Exception:
                    raw = b""
                if not raw:
                    break
                continue
            if not raw:
                break
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            chunks.append(line)
            if line.strip():
                on_line(line)
        if feed_task is not None:
            await feed_task
        await proc.wait()
    finally:
        if feed_task is not None and not feed_task.done():
            feed_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await feed_task
        if job_id:
            _ACTIVE_PROCESSES.pop(job_id, None)
    if job_id and job_id in _CANCEL_REQUESTED:
        raise DispatchCancelled()
    return proc.returncode, "\n".join(chunks)


async def _terminate_process(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    if os.name == "nt":
        killer = await asyncio.create_subprocess_exec(
            "taskkill", "/PID", str(proc.pid), "/T", "/F",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await killer.wait()
    else:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
    try:
        await asyncio.wait_for(proc.wait(), timeout=3)
    except asyncio.TimeoutError:
        if os.name == "nt":
            proc.kill()
        else:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        await proc.wait()


async def cancel_dispatch_job(job_id: str) -> bool:
    job = get_dispatch_job(job_id)
    if not job or job["status"] not in ("running", "waiting", "canceling"):
        return False
    set_dispatch_canceling(job_id)
    _CANCEL_REQUESTED.add(job_id)
    proc = _ACTIVE_PROCESSES.get(job_id)
    if proc is not None:
        await _terminate_process(proc)
    append_dispatch_log(job_id, json.dumps(_entry("warn", "Canceled by developer")))
    update_dispatch_job(job_id, "canceled", "Canceled by developer")
    return True


async def redirect_dispatch_job(job_id: str, direction: str) -> dict | None:
    job = get_dispatch_job(job_id)
    if not job:
        return None
    resume_session_id = job.get("session_id")
    if job["status"] in ("running", "waiting", "canceling"):
        await cancel_dispatch_job(job_id)
    new_job_id = uuid.uuid4().hex
    create_dispatch_job(
        new_job_id,
        job["project_path"],
        job["agent"],
        direction,
        job["allow_edits"],
        context_snapshot=job.get("context_snapshot") or "",
        replaces_job_id=job_id,
        parent_job_id=job.get("parent_job_id"),
        coordination_id=job.get("coordination_id"),
        task_label=job.get("task_label"),
        model=job.get("model"),
    )
    return {
        "job_id": new_job_id,
        "project_path": job["project_path"],
        "agent": job["agent"],
        "allow_edits": job["allow_edits"],
        "resume_session_id": resume_session_id,
    }


def _brief(text: str, n: int = 200) -> str:
    return " ".join((text or "").split())[:n]


def _output_preview(text: str, max_lines: int = 40, max_chars: int = 1600) -> str:
    text = (text or "").replace("\r\n", "\n").strip("\n")
    if not text.strip():
        return ""
    lines = text.split("\n")
    truncated = len(lines) > max_lines
    lines = lines[:max_lines]
    out = "\n".join(lines)
    if len(out) > max_chars:
        out = out[:max_chars]
        truncated = True
    if truncated:
        out += "\n… (output truncated)"
    return out


def _entry(kind: str, text: str) -> dict:
    return {"k": kind, "t": text}


def _native_interaction(value: dict) -> dict | None:
    questions = value.get("questions")
    if isinstance(questions, list) and questions:
        value = questions[0] if isinstance(questions[0], dict) else {}
    prompt = str(value.get("question") or value.get("prompt") or "").strip()
    if not prompt:
        return None
    header = str(value.get("header") or "").strip()
    if header:
        prompt = f"{header}: {prompt}"
    raw_options = value.get("options") if isinstance(value.get("options"), list) else []
    options = []
    for option in raw_options:
        label = option.get("label") if isinstance(option, dict) else option
        label = str(label or "").strip()
        if label:
            options.append(label)
    return {"kind": "question", "prompt": prompt, "options": options}



def _claude_events(event: dict, state: dict) -> list[dict]:
    etype = event.get("type")
    out: list[dict] = []
    session_id = event.get("session_id")
    if session_id and session_id != state.get("session_id"):
        state["session_id"] = session_id
        out.append(_entry("session", str(session_id)))
    if etype == "assistant":
        for block in event.get("message", {}).get("content", []):
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text" and block.get("text", "").strip():
                out.append(_entry("msg", block["text"].strip()))
            elif btype == "tool_use":
                name = block.get("name", "tool")
                inp = block.get("input", {}) if isinstance(block.get("input"), dict) else {}
                if name in ("AskUserQuestion", "request_user_input"):
                    interaction = _native_interaction(inp)
                    if interaction:
                        state["interaction"] = interaction
                        out.append(_entry("interaction", interaction["prompt"]))
                elif name == "Bash" and inp.get("command"):
                    out.append(_entry("cmd", "$ " + inp["command"].strip()))
                elif name in ("Edit", "Write", "NotebookEdit") and (inp.get("file_path") or inp.get("path")):
                    out.append(_entry("tool", "± " + str(inp.get("file_path") or inp.get("path"))))
                else:
                    detail = inp.get("pattern") or inp.get("path") or inp.get("file_path") or inp.get("url") or ""
                    out.append(_entry("tool", f"{name} {_brief(str(detail), 160)}".rstrip()))
    elif etype == "user":
        for block in event.get("message", {}).get("content", []):
            if isinstance(block, dict) and block.get("type") == "tool_result":
                content = block.get("content", "")
                if isinstance(content, list):
                    content = " ".join(
                        b.get("text", "") for b in content if isinstance(b, dict)
                    )
                preview = _output_preview(str(content))
                if preview:
                    out.append(_entry("out", preview))
    elif etype == "result":
        state["result"] = event.get("result", "") or state.get("result", "")
        usage = event.get("usage", {}) or {}
        state["tokens"] = (
            (usage.get("input_tokens", 0) or 0)
            + (usage.get("output_tokens", 0) or 0)
            + (usage.get("cache_read_input_tokens", 0) or 0)
            + (usage.get("cache_creation_input_tokens", 0) or 0)
        )
    return out


async def dispatch_claude(
    project_path: str,
    prompt: str,
    allow_edits: bool,
    on_log,
    env: dict | None = None,
    resume_session_id: str | None = None,
    model: str | None = None,
) -> tuple[str, int]:
    binary = resolve_claude_binary()
    mode = "auto" if allow_edits else "plan"
    args = [
        binary, "-p",
        "--input-format", "text",
        "--output-format", "stream-json", "--verbose",
        "--permission-mode", mode,
        "--append-system-prompt", INTERACTION_INSTRUCTIONS,
        "--add-dir", project_path,
    ]
    if model:
        args.extend(["--model", model])
    if resume_session_id:
        args.extend(["--resume", resume_session_id])
    state: dict = {"result": "", "tokens": 0}

    def handle(line: str) -> None:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return
        for entry in _claude_events(event, state):
            on_log(entry)

    code, raw = await _stream(args, project_path, handle, env=env, stdin_text=prompt)
    result = state.get("result") or ""
    interaction = state.get("interaction")
    if interaction and not _INTERACTION_RE.search(result):
        marker = (
            "<agentmemorysync_interaction>"
            + json.dumps(interaction)
            + "</agentmemorysync_interaction>"
        )
        result = f"{result}\n\n{marker}".strip()
    if not result:
        result = _brief(raw, 400) or "(no result text returned)"
        if "login" in raw.lower() or "not logged in" in raw.lower():
            on_log(_entry("warn", "Claude Code isn't logged in for headless use. Run `claude` "
                          "once in a terminal and sign in, then retry."))
        elif code != 0:
            on_log(_entry("warn", "claude exited with code " + str(code)))
    return result, state.get("tokens", 0)



def _codex_events(event: dict, state: dict) -> list[dict]:
    etype = event.get("type")
    out: list[dict] = []
    if etype == "thread.started" and event.get("thread_id"):
        state["session_id"] = event["thread_id"]
        out.append(_entry("session", str(event["thread_id"])))
    if etype == "item.started":
        item = event.get("item", {})
        if item.get("type") == "command_execution" and item.get("command"):
            out.append(_entry("cmd", "$ " + item["command"].strip()))
    elif etype == "item.completed":
        item = event.get("item", {})
        it = item.get("type")
        if it == "agent_message":
            text = (item.get("text") or "").strip()
            if text:
                state["result"] = text
                out.append(_entry("msg", text))
        elif it == "reasoning":
            text = (item.get("text") or "").strip()
            if text:
                out.append(_entry("reason", text))
        elif it in ("command_execution", "local_shell_call"):
            preview = _output_preview(item.get("aggregated_output", ""))
            if preview:
                out.append(_entry("out", preview))
            code = item.get("exit_code")
            if code is not None:
                out.append(_entry("exit" if code == 0 else "warn", f"↳ exit {code}"))
        elif it == "file_change":
            changes = item.get("changes") or []
            paths = ", ".join(str(c.get("path", "")) for c in changes if isinstance(c, dict))
            out.append(_entry("tool", "± " + (paths or "file change")))
        elif it in ("mcp_tool_call", "web_search"):
            out.append(_entry("tool", it + " " + _brief(json.dumps(item), 120)))
        elif it in ("request_user_input", "user_input_request"):
            interaction = _native_interaction(item)
            if interaction:
                state["interaction"] = interaction
                out.append(_entry("interaction", interaction["prompt"]))
    elif etype == "turn.completed":
        usage = event.get("usage", {}) or {}
        state["tokens"] = (usage.get("input_tokens", 0) or 0) + (
            usage.get("output_tokens", 0) or 0
        )
    return out


async def dispatch_codex(
    project_path: str,
    prompt: str,
    allow_edits: bool,
    on_log,
    env: dict | None = None,
    resume_session_id: str | None = None,
    model: str | None = None,
) -> tuple[str, int]:
    binary = resolve_codex_binary()
    sandbox = "workspace-write" if allow_edits else "read-only"
    effective_prompt = f"{prompt}\n\n{INTERACTION_INSTRUCTIONS}"
    model_args = ["-c", 'model="' + model.replace('"', '\\"') + '"'] if model else []
    if resume_session_id:
        args = [
            binary, "exec", "resume",
            "-c", 'approval_policy="never"',
            *model_args,
            "--json", "--skip-git-repo-check",
            resume_session_id, "-",
        ]
    else:
        args = [
            binary, "exec", "-",
            "-C", project_path,
            "--json", "-s", sandbox,
            "-c", 'approval_policy="never"',
            *model_args,
            "--skip-git-repo-check",
        ]
    state: dict = {"result": "", "tokens": 0}

    def handle(line: str) -> None:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return
        for entry in _codex_events(event, state):
            on_log(entry)

    code, raw = await _stream(
        args, project_path, handle, env=env, stdin_text=effective_prompt
    )
    result = state.get("result") or ""
    interaction = state.get("interaction")
    if interaction and not _INTERACTION_RE.search(result):
        marker = (
            "<agentmemorysync_interaction>"
            + json.dumps(interaction)
            + "</agentmemorysync_interaction>"
        )
        result = f"{result}\n\n{marker}".strip()
    if not result:
        result = _brief(raw, 400) or "(no result text returned)"
        if code != 0:
            on_log(_entry("warn", "codex exited with code " + str(code)))
    return result, state.get("tokens", 0)


async def dispatch_local_openai(
    base_url: str,
    model: str,
    headers: dict,
    prompt: str,
    on_log,
) -> tuple[str, int]:
    import httpx

    url = f"{base_url}/v1/chat/completions"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": INTERACTION_INSTRUCTIONS},
            {"role": "user", "content": prompt},
        ],
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    text_parts: list[str] = []
    buffer = ""
    usage_tokens = 0
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, read=300.0)) as client:
            async with client.stream("POST", url, headers=headers, json=payload) as resp:
                if resp.status_code != 200:
                    body = (await resp.aread()).decode("utf-8", "replace")
                    raise RuntimeError(
                        f"Local model endpoint {url} returned {resp.status_code}: {_brief(body, 300)}"
                    )
                async for line in resp.aiter_lines():
                    line = line.strip()
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[len("data:"):].strip()
                    if data == "[DONE]":
                        break
                    try:
                        event = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    usage = event.get("usage")
                    if usage:
                        usage_tokens = (usage.get("prompt_tokens", 0) or 0) + (
                            usage.get("completion_tokens", 0) or 0
                        )
                    choices = event.get("choices") or []
                    if not choices:
                        continue
                    piece = (choices[0].get("delta") or {}).get("content") or ""
                    if piece:
                        text_parts.append(piece)
                        buffer += piece
                        if len(buffer) >= 300:
                            on_log(_entry("msg", buffer))
                            buffer = ""
    except httpx.ConnectError as exc:
        raise RuntimeError(f"Could not reach local model endpoint {url}. Is it running?") from exc
    if buffer:
        on_log(_entry("msg", buffer))
    result = "".join(text_parts).strip() or "(no result text returned)"
    if not usage_tokens:
        usage_tokens = (len(result) + 3) // 4
    return result, usage_tokens


def _extract_interaction(result: str) -> tuple[str, dict | None]:
    match = _INTERACTION_RE.search(result or "")
    if match:
        try:
            value = json.loads(match.group(1))
        except (json.JSONDecodeError, TypeError):
            value = None
        if isinstance(value, dict) and str(value.get("prompt", "")).strip():
            clean = ((result or "")[: match.start()] + (result or "")[match.end() :]).strip()
            kind = str(value.get("kind", "question")).lower()
            if kind not in ("approval", "question", "confirmation"):
                kind = "question"
            options = value.get("options") if isinstance(value.get("options"), list) else []
            options = [str(option).strip()[:100] for option in options if str(option).strip()]
            options = list(dict.fromkeys(options))[:8]
            if not options and kind == "approval":
                options = ["Approve", "Deny"]
            elif not options and kind == "confirmation":
                options = ["Confirm", "Cancel"]
            return clean, {
                "kind": kind,
                "prompt": str(value["prompt"]).strip(),
                "options": options,
            }
    if _APPROVAL_FALLBACK_RE.search(result or ""):
        return (result or "").strip(), {
            "kind": "approval",
            "prompt": (result or "").strip(),
            "options": ["Approve", "Deny"],
        }
    natural = _extract_natural_option_interaction(result or "")
    if natural:
        return (result or "").strip(), natural
    return result, None


def _extract_natural_option_interaction(result: str) -> dict | None:
    text = result.strip()
    if not text.endswith("?"):
        return None
    question = next(
        (line.strip() for line in reversed(text.splitlines()) if line.strip()),
        "",
    )
    if not _ACTIONABLE_QUESTION_RE.search(question):
        return None

    candidates: list[tuple[str, str]] = []
    for line in text.splitlines():
        match = _MARKDOWN_OPTION_RE.match(line)
        if match:
            candidates.append((match.group("marker"), match.group("body").strip()))
    numbered = [item for item in candidates if item[0][0].isdigit()]
    if numbered:
        candidates = numbered

    options: list[str] = []
    for _marker, option in candidates:
        bold = re.match(r"\*\*(.+?)\*\*", option)
        if bold:
            label = bold.group(1)
        else:
            label = re.split(r"\s+[—–-]\s+|:\s+", option, maxsplit=1)[0]
        label = re.sub(r"[*_`]", "", label).strip()[:100]
        if label and label not in options:
            options.append(label)
    if len(options) < 2:
        return None

    prompt = text if len(text) <= 4000 else "…" + text[-3999:]
    return {"kind": "question", "prompt": prompt, "options": options[:8]}


async def _run_dispatch_turn(
    job_id: str,
    project_path: str,
    agent: str,
    prompt: str,
    allow_edits: bool,
    resume_session_id: str | None = None,
) -> None:
    job = get_dispatch_job(job_id)
    activity_count = int((job or {}).get("activity_count") or 0)

    def on_log(entry) -> None:
        nonlocal activity_count
        if isinstance(entry, dict):
            if entry.get("k") == "session":
                update_dispatch_session(job_id, str(entry.get("t", "")))
                update_dispatch_progress(job_id, 8, "Agent connected", activity_count)
                return
            append_dispatch_log(job_id, json.dumps(entry))
            kind = str(entry.get("k") or "info")
            if kind not in ("done", "warn", "interaction"):
                activity_count += 1
                label = {
                    "cmd": "Running a command",
                    "out": "Reviewing command output",
                    "exit": "Command finished",
                    "tool": "Using project tools",
                    "reason": "Analyzing the task",
                    "msg": "Reporting progress",
                    "user": "Processing your response",
                }.get(kind, "Agent is working")
                progress = min(90, 8 + int((activity_count ** 0.7) * 8))
                update_dispatch_progress(job_id, progress, label, activity_count)
        else:
            append_dispatch_log(job_id, json.dumps(_entry("info", str(entry))))

    snapshot = (job or {}).get("context_snapshot") or ""
    effective_prompt = (
        f"{snapshot}\n\n{prompt}" if snapshot and not resume_session_id else prompt
    )
    env = dict(os.environ)
    env[CONTEXT_PREINJECTED_ENV] = "1"

    action = "Resuming" if resume_session_id else "Deploying"
    update_dispatch_progress(
        job_id,
        max(5, int((job or {}).get("progress") or 0)),
        "Resuming agent" if resume_session_id else "Starting agent",
        activity_count,
    )
    selected_model = (job or {}).get("model") or get_agent_model(agent)
    on_log(_entry("info", f"{action} {agent} ({'edits allowed' if allow_edits else 'read-only'})…"))
    try:
        adapter = get_agent_adapter(agent, "dispatch")
        result, tokens = await adapter.dispatch(
            project_path,
            effective_prompt,
            allow_edits,
            on_log,
            env=env,
            resume_session_id=resume_session_id,
            model=selected_model,
        )
        if snapshot and not resume_session_id:
            from telemetry import record_context_injection

            record_context_injection(
                project_path, agent, job_id, snapshot, route="dashboard"
            )
        result, assignments = _extract_recategorization(result)
        if assignments:
            applied = _apply_recategorization(project_path, assignments)
            on_log(_entry("info", f"Applied {applied}/{len(assignments)} category reassignments."))
            result = f"{result}\n\nApplied {applied}/{len(assignments)} category reassignments.".strip()
        result, interaction = _extract_interaction(result)
        if interaction:
            create_dispatch_interaction(
                uuid.uuid4().hex,
                job_id,
                project_path,
                agent,
                interaction["kind"],
                interaction["prompt"],
                interaction["options"],
            )
            on_log(_entry("interaction", interaction["prompt"]))
            update_dispatch_job(job_id, "waiting", result, tokens=tokens)
            return
        on_log(_entry("done", f"Done · {tokens:,} tokens" if tokens else "Done"))
        update_dispatch_job(job_id, "done", result, tokens=tokens)
        record_event(project_path, agent, job_id, "dispatch", result[:400])
    except Exception as exc:
        on_log(_entry("warn", str(exc)))
        update_dispatch_job(job_id, "error", str(exc))


async def run_dispatch_job(
    job_id: str,
    project_path: str,
    agent: str,
    prompt: str,
    allow_edits: bool,
    resume_session_id: str | None = None,
) -> None:
    await _run_dispatch_turn(
        job_id, project_path, agent, prompt, allow_edits, resume_session_id=resume_session_id
    )


async def resume_dispatch_job(job_id: str, response: str) -> None:
    job = get_dispatch_job(job_id)
    if not job:
        return
    session_id = job.get("session_id")
    update_dispatch_job(job_id, "running", job.get("result_text") or "")
    append_dispatch_log(
        job_id,
        json.dumps(_entry("user", "Dashboard response: " + response)),
    )
    continuation = (
        "The user answered the pending dashboard interaction as follows:\n\n"
        f"{response}\n\nContinue the original task now."
    )
    if not session_id:
        continuation = (
            "Continue this dashboard task in a fresh agent session.\n\n"
            f"Original task:\n{job['prompt']}\n\n"
            f"The previous agent paused with:\n{job.get('result_text') or '(no additional text)'}\n\n"
            f"The user answered:\n{response}\n\n"
            "Complete the original task now."
        )
    await _run_dispatch_turn(
        job_id,
        job["project_path"],
        job["agent"],
        continuation,
        job["allow_edits"],
        resume_session_id=session_id,
    )


def start_dispatch_job(project_path: str, agent: str, prompt: str, allow_edits: bool) -> str:
    get_agent_adapter(agent, "dispatch")
    job_id = uuid.uuid4().hex
    snapshot = get_context(project_path) or ""
    create_dispatch_job(job_id, project_path, agent, prompt, allow_edits, context_snapshot=snapshot)
    return job_id


def start_coordinated_jobs(source_job: dict, tasks: list[dict]) -> dict:
    coordination_id = uuid.uuid4().hex
    created = []
    source_result = (source_job.get("result_text") or "").strip()
    if len(source_result) > 6000:
        source_result = source_result[:6000] + "\n[Source result truncated]"
    source_prompt = (source_job.get("prompt") or "").strip()
    if len(source_prompt) > 3000:
        source_prompt = source_prompt[:3000] + "\n[Source prompt truncated]"

    for task in tasks:
        label = task["label"].strip()
        task_prompt = task["prompt"].strip()
        prompt = (
            "You are one worker in a coordinated follow-up to an existing dashboard deployment.\n"
            "Complete only the assigned task below. Other agents may be working in the same "
            "repository, so inspect the current working tree before editing, do not undo unrelated "
            "changes, and report the files and tests you touched. Do not expand into the other "
            "workers' assignments.\n\n"
            f"Source deployment task:\n{source_prompt or '(not available)'}\n\n"
            f"Source deployment result:\n{source_result or '(not available)'}\n\n"
            f"Your assignment ({label}):\n{task_prompt}"
        )
        job_id = uuid.uuid4().hex
        create_dispatch_job(
            job_id,
            source_job["project_path"],
            task["agent"],
            prompt,
            task["allow_edits"],
            context_snapshot=source_job.get("context_snapshot") or "",
            parent_job_id=source_job["id"],
            coordination_id=coordination_id,
            task_label=label,
            model=task.get("model") or None,
        )
        created.append(
            {
                "job_id": job_id,
                "project_path": source_job["project_path"],
                "agent": task["agent"],
                "prompt": prompt,
                "allow_edits": task["allow_edits"],
            }
        )
    return {"coordination_id": coordination_id, "jobs": created}


async def run_coordinated_jobs(jobs: list[dict]) -> None:
    await asyncio.gather(
        *(
            run_dispatch_job(
                job["job_id"],
                job["project_path"],
                job["agent"],
                job["prompt"],
                job["allow_edits"],
            )
            for job in jobs
        )
    )

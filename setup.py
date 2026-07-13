import os
import shutil
import sys

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
GENERATED_DIR = os.path.join(REPO_ROOT, "config", "generated")

ROOT_TOKEN = "__AGENT_MEMORY_SYNC_ROOT__"
PYTHON_TOKEN = "__AGENT_MEMORY_SYNC_PYTHON__"

TEMPLATES = [
    ("claude_settings_snippet.json", "claude_settings.json"),
    ("codex_hooks_snippet.toml", "codex_hooks.toml"),
    ("codex_mcp_snippet.toml", "codex_mcp.toml"),
]


def resolve_python() -> str:
    candidates = (
        os.path.join(REPO_ROOT, ".venv", "Scripts", "python.exe"),
        os.path.join(REPO_ROOT, ".venv", "bin", "python"),
    )
    for venv_python in candidates:
        if os.path.isfile(venv_python):
            return venv_python
    print(
        "WARNING: no .venv found at "
        f"{os.path.join(REPO_ROOT, '.venv')}. Falling back to the interpreter running this "
        "script. Create a venv and install requirements.txt into it for a "
        "more reliable hook/MCP command.",
        file=sys.stderr,
    )
    return sys.executable


def render(python_exe: str) -> None:
    os.makedirs(GENERATED_DIR, exist_ok=True)
    root_forward = REPO_ROOT.replace("\\", "/")
    python_forward = python_exe.replace("\\", "/")

    for template_name, output_name in TEMPLATES:
        src = os.path.join(REPO_ROOT, "config", template_name)
        if not os.path.isfile(src):
            continue
        with open(src, "r", encoding="utf-8") as f:
            content = f.read()
        content = content.replace(ROOT_TOKEN, root_forward).replace(
            PYTHON_TOKEN, python_forward
        )
        dst = os.path.join(GENERATED_DIR, output_name)
        with open(dst, "w", encoding="utf-8") as f:
            f.write(content)
        print(f"Wrote {dst}")


def main() -> None:
    python_exe = resolve_python()
    render(python_exe)

    print()
    print("Next steps (nothing above touched your global config):")
    print(
        "  1. Claude Code: merge config/generated/claude_settings.json into\n"
        "     %USERPROFILE%\\.claude\\settings.json"
    )
    print(
        "  2. Codex, pick ONE based on your install (see README's\n"
        "     'which Codex do I have?' section):\n"
        "     - Hook-capable Codex CLI: merge config/generated/codex_hooks.toml\n"
        "       into %USERPROFILE%\\.codex\\config.toml (or hooks.json)\n"
        "     - MCP-capable Codex (has existing [mcp_servers.*] entries already):\n"
        "       merge config/generated/codex_mcp.toml into\n"
        "       %USERPROFILE%\\.codex\\config.toml"
    )
    print("  3. Restart both CLIs, run the dashboard, create your admin account.")

    if shutil.which("py") is None and shutil.which("python") is None:
        print(
            "\nNote: neither 'py' nor 'python' resolves on PATH in this shell. "
            "that's fine, the generated configs use the venv's absolute "
            "python.exe path directly."
        )


if __name__ == "__main__":
    main()

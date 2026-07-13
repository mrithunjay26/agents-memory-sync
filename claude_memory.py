import os


def encode_claude_project_path(path: str) -> str:
    normalized = os.path.normpath(os.path.abspath(path))
    return normalized.replace(":", "-").replace("\\", "-").replace("/", "-")


def claude_projects_dir() -> str:
    return os.path.join(os.path.expanduser("~"), ".claude", "projects")


def resolve_claude_project_dir(project_path: str) -> str:
    projects = claude_projects_dir()
    encoded = encode_claude_project_path(project_path)
    exact = os.path.join(projects, encoded)
    if os.path.isdir(exact):
        return exact
    if os.path.isdir(projects):
        canonical = os.path.normcase(encoded)
        for name in os.listdir(projects):
            if os.path.normcase(name) == canonical:
                return os.path.join(projects, name)
    return exact

from __future__ import annotations

import subprocess


def recent_commits_for_file(root: str, file_path: str, limit: int = 10) -> list[dict]:
    """`git log` entries touching `file_path`. Empty outside a git repo, on
    any git error, or if git isn't installed. Never raises."""
    limit = max(1, min(50, int(limit)))
    try:
        result = subprocess.run(
            [
                "git", "log", f"-n{limit}", "--date=iso-strict",
                "--pretty=format:%H\x1f%an\x1f%ad\x1f%s", "--", file_path,
            ],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode != 0 or not result.stdout.strip():
        return []
    commits = []
    for line in result.stdout.splitlines():
        parts = line.split("\x1f")
        if len(parts) == 4:
            commit_hash, author, date, subject = parts
            commits.append({"commit": commit_hash[:12], "author": author, "date": date, "subject": subject})
    return commits

import subprocess

import code_search


def _write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_recent_commits_for_file_reads_real_git_log(tmp_path):
    repo = tmp_path
    subprocess.run(["git", "init"], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "a@b.c"], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "Tester"], cwd=repo, capture_output=True, check=True)
    _write(repo / "file.txt", "one\n")
    subprocess.run(["git", "add", "file.txt"], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "add file"], cwd=repo, capture_output=True, check=True)

    commits = code_search.recent_commits_for_file(str(repo), "file.txt")

    assert len(commits) == 1
    assert commits[0]["subject"] == "add file"
    assert commits[0]["author"] == "Tester"


def test_recent_commits_for_file_outside_git_repo_returns_empty(tmp_path):
    assert code_search.recent_commits_for_file(str(tmp_path), "file.txt") == []

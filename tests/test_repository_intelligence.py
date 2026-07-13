from pathlib import Path

import repository_intelligence as repository


def _project(tmp_path: Path) -> Path:
    root = tmp_path / "sample-project"
    root.mkdir()
    (root / "service.py").write_text(
        '''"""Small service layer."""

class Base:
    pass

class Service(Base):
    """Runs application work."""

    def run(self, value: int) -> int:
        return helper(value)

def helper(value: int) -> int:
    return value
''',
        encoding="utf-8",
    )
    (root / "test_service.py").write_text(
        "from service import Service\n\n"
        "def test_run():\n"
        "    assert Service().run(1) == 1\n",
        encoding="utf-8",
    )
    (root / "README.md").write_text(
        "# Sample\n\nRuns application work. archived-only-marker\n", encoding="utf-8"
    )
    return root


def test_indexes_symbols_relationships_search_and_hierarchy(tmp_path):
    root = _project(tmp_path)
    assert repository.get_index_status(str(root))["indexed"] is False

    result = repository.index_repository(str(root))

    assert result["file_count"] == 3
    assert result["symbol_count"] == 5
    assert result["relationship_count"] > 0
    assert result["updated"] == 3

    matches = repository.search_code(str(root), "application work")
    assert matches[0]["path"] in {"README.md", "service.py"}
    assert matches[0]["git"]["last_commit"] is None

    service = repository.explain_symbol(str(root), "Service")
    assert service is not None
    assert service["path"] == "service.py"
    assert service["kind"] == "class"
    assert any(link["kind"] == "inherits" and link["target"] == "Base" for link in service["outgoing"])

    helper_calls = repository.find_references(str(root), "helper", kind="call")
    assert [(item["path"], item["source_symbol"]) for item in helper_calls] == [
        ("service.py", "Service.run")
    ]
    test_links = repository.find_references(str(root), "Service", kind="tests")
    assert test_links[0]["path"] == "test_service.py"

    architecture = repository.get_repository_map(str(root))
    assert architecture["languages"] == {"Markdown": 1, "Python": 2}
    assert architecture["symbol_kinds"]["test"] == 1
    assert "3 files" in architecture["summary"]


def test_incremental_index_updates_changed_files_and_removes_deleted_files(tmp_path):
    root = _project(tmp_path)
    repository.index_repository(str(root))

    unchanged = repository.index_repository(str(root))
    assert unchanged["updated"] == 0
    assert unchanged["unchanged"] == 3

    (root / "service.py").write_text(
        "def replacement():\n    return 'new searchable phrase'\n", encoding="utf-8"
    )
    (root / "README.md").unlink()
    changed = repository.index_repository(str(root))

    assert changed["updated"] == 1
    assert changed["unchanged"] == 1
    assert changed["removed"] == 1
    assert changed["file_count"] == 2
    assert repository.explain_symbol(str(root), "Service") is None
    assert repository.search_code(str(root), "new searchable phrase")[0]["path"] == "service.py"
    assert repository.search_code(str(root), "archived-only-marker") == []


def test_python_parse_error_still_produces_a_searchable_file(tmp_path):
    root = tmp_path / "broken-project"
    root.mkdir()
    (root / "broken.py").write_text(
        "def incomplete(:\n    marker = 'recoverable evidence'\n", encoding="utf-8"
    )

    result = repository.index_repository(str(root))

    assert result["error_count"] == 1
    assert result["file_count"] == 1
    matches = repository.search_code(str(root), "recoverable evidence")
    assert matches[0]["path"] == "broken.py"
    assert "Structural analysis unavailable" in matches[0]["summary"]

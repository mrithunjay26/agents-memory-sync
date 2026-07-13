from __future__ import annotations

import ast
import hashlib
import os
import re
import subprocess
from collections import Counter, defaultdict
from pathlib import Path, PurePosixPath
from typing import Any

import store


MAX_INDEXED_FILE_BYTES = 1_000_000
MAX_SEARCH_LIMIT = 50
IGNORED_DIRECTORIES = {
    ".git",
    ".hg",
    ".svn",
    ".idea",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "vendor",
}
LANGUAGES = {
    ".c": "C",
    ".cc": "C++",
    ".cpp": "C++",
    ".cs": "C#",
    ".css": "CSS",
    ".go": "Go",
    ".h": "C/C++ header",
    ".hpp": "C++ header",
    ".html": "HTML",
    ".java": "Java",
    ".js": "JavaScript",
    ".jsx": "JavaScript JSX",
    ".json": "JSON",
    ".md": "Markdown",
    ".php": "PHP",
    ".ps1": "PowerShell",
    ".py": "Python",
    ".rb": "Ruby",
    ".rs": "Rust",
    ".sh": "Shell",
    ".sql": "SQL",
    ".toml": "TOML",
    ".ts": "TypeScript",
    ".tsx": "TypeScript JSX",
    ".txt": "Text",
    ".xml": "XML",
    ".yaml": "YAML",
    ".yml": "YAML",
}
SPECIAL_TEXT_FILES = {
    ".dockerignore",
    ".editorconfig",
    ".gitignore",
    "dockerfile",
    "license",
    "makefile",
    "readme",
}


def _run_git(root: str, *args: str, timeout: int = 30) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", root, *args],
            capture_output=True,
            check=False,
            timeout=timeout,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.decode("utf-8", errors="replace")


def _is_supported(path: str) -> bool:
    pure = PurePosixPath(path)
    return (
        pure.suffix.casefold() in LANGUAGES
        or pure.name.casefold() in SPECIAL_TEXT_FILES
    )


def _discover_files(root: str) -> list[str]:
    tracked = _run_git(
        root, "ls-files", "--cached", "--others", "--exclude-standard", "-z"
    )
    if tracked is not None:
        candidates = tracked.split("\0")
    else:
        candidates = []
        for current, directories, files in os.walk(root):
            directories[:] = [name for name in directories if name not in IGNORED_DIRECTORIES]
            for name in files:
                candidates.append(os.path.relpath(os.path.join(current, name), root))

    selected = []
    for candidate in candidates:
        relative = candidate.replace("\\", "/").lstrip("./")
        if not relative or not _is_supported(relative):
            continue
        if any(part in IGNORED_DIRECTORIES for part in PurePosixPath(relative).parts):
            continue
        full_path = os.path.join(root, *PurePosixPath(relative).parts)
        try:
            if os.path.isfile(full_path) and os.path.getsize(full_path) <= MAX_INDEXED_FILE_BYTES:
                selected.append(relative)
        except OSError:
            continue
    return sorted(set(selected))


def _git_metadata(root: str) -> tuple[str | None, dict[str, dict[str, str]]]:
    head = (_run_git(root, "rev-parse", "HEAD") or "").strip() or None
    output = _run_git(
        root,
        "log",
        "--format=--AMS--%H%x1f%an%x1f%aI",
        "--name-only",
        "--no-renames",
        timeout=60,
    )
    if not output:
        return head, {}

    current: tuple[str, str, str] | None = None
    metadata: dict[str, dict[str, str]] = {}
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if line.startswith("--AMS--"):
            fields = line[len("--AMS--") :].split("\x1f", 2)
            current = tuple(fields) if len(fields) == 3 else None
        elif line and current:
            path = line.replace("\\", "/")
            if path not in metadata:
                metadata[path] = {
                    "last_commit": current[0],
                    "last_author": current[1],
                    "last_changed_at": current[2],
                }
    return head, metadata


def _expr_name(node: ast.AST | None) -> str:
    if node is None:
        return ""
    try:
        return ast.unparse(node)
    except (AttributeError, ValueError):
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            prefix = _expr_name(node.value)
            return f"{prefix}.{node.attr}" if prefix else node.attr
        return type(node).__name__


def _first_sentence(text: str | None, limit: int = 180) -> str:
    compact = " ".join((text or "").split())
    if not compact:
        return ""
    sentence = re.split(r"(?<=[.!?])\s+", compact, maxsplit=1)[0]
    return sentence[:limit].rstrip()


class _RelationshipVisitor(ast.NodeVisitor):
    def __init__(self, source: str | None, is_test: bool):
        self.source = source
        self.is_test = is_test
        self.relationships: list[dict[str, Any]] = []

    def _add(self, kind: str, target: str, line: int) -> None:
        target = target.strip()
        if target:
            self.relationships.append(
                {"source": self.source, "kind": kind, "target": target, "line": line}
            )

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self._add("import", alias.name, node.lineno)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        prefix = "." * node.level + (node.module or "")
        for alias in node.names:
            self._add("import", f"{prefix}.{alias.name}".strip("."), node.lineno)

    def visit_Call(self, node: ast.Call) -> None:
        target = _expr_name(node.func)
        self._add("call", target, node.lineno)
        if self.is_test:
            self._add("tests", target, node.lineno)
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Load):
            self._add("reference", node.id, node.lineno)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        return

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        return

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        return


def _python_structure(content: str, relative_path: str) -> tuple[list[dict], list[dict], str]:
    tree = ast.parse(content, filename=relative_path)
    symbols: list[dict[str, Any]] = []
    relationships: list[dict[str, Any]] = []
    module_doc = ast.get_docstring(tree, clean=True)

    module_visitor = _RelationshipVisitor(None, relative_path.startswith("test") or "/test" in relative_path)
    for statement in tree.body:
        if not isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            module_visitor.visit(statement)
    relationships.extend(module_visitor.relationships)

    def collect(body: list[ast.stmt], parents: list[str]) -> None:
        for node in body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            qualified = ".".join([*parents, node.name])
            parent = ".".join(parents) or None
            is_class = isinstance(node, ast.ClassDef)
            is_test = node.name.startswith("test_") or node.name.startswith("Test")
            if is_class:
                kind = "test_class" if is_test else "class"
                signature = "(" + ", ".join(_expr_name(base) for base in node.bases) + ")"
                for base in node.bases:
                    relationships.append(
                        {
                            "source": qualified,
                            "kind": "inherits",
                            "target": _expr_name(base),
                            "line": node.lineno,
                        }
                    )
            else:
                nested_kind = "method" if parents else "function"
                kind = "test" if is_test else nested_kind
                prefix = "async " if isinstance(node, ast.AsyncFunctionDef) else ""
                signature = prefix + "(" + ast.unparse(node.args) + ")"
            docstring = ast.get_docstring(node, clean=True)
            summary = _first_sentence(docstring) or f"{kind.replace('_', ' ')} {qualified}"
            symbols.append(
                {
                    "name": node.name,
                    "qualified_name": qualified,
                    "kind": kind,
                    "start_line": node.lineno,
                    "end_line": getattr(node, "end_lineno", node.lineno),
                    "signature": signature,
                    "docstring": docstring,
                    "summary": summary,
                    "parent_qualified_name": parent,
                }
            )

            visitor = _RelationshipVisitor(qualified, is_test or kind == "test_class")
            for statement in node.body:
                visitor.visit(statement)
            relationships.extend(visitor.relationships)
            collect(node.body, [*parents, node.name])

    collect(tree.body, [])
    return symbols, relationships, _first_sentence(module_doc)


def _file_summary(
    relative_path: str,
    language: str,
    symbols: list[dict],
    relationships: list[dict],
    module_summary: str,
) -> str:
    pieces = [f"{language} file {relative_path}."]
    if module_summary:
        pieces.append(module_summary)
    if symbols:
        featured = ", ".join(symbol["qualified_name"] for symbol in symbols[:8])
        pieces.append(f"Defines {featured}{'…' if len(symbols) > 8 else ''}.")
    kinds = Counter(relationship["kind"] for relationship in relationships)
    if kinds:
        pieces.append(
            "Relationships: " + ", ".join(f"{count} {kind}" for kind, count in sorted(kinds.items())) + "."
        )
    return " ".join(pieces)


def _read_text(path: str) -> str | None:
    try:
        data = Path(path).read_bytes()
    except OSError:
        return None
    if b"\0" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("utf-8", errors="replace")


def _resolve_relationships(conn, project_path: str) -> None:
    symbol_rows = conn.execute(
        "SELECT id, name, qualified_name FROM repo_symbols WHERE project_path = ?",
        (project_path,),
    ).fetchall()
    exact = {qualified: symbol_id for symbol_id, _name, qualified in symbol_rows}
    by_name: dict[str, list[int]] = defaultdict(list)
    for symbol_id, name, _qualified in symbol_rows:
        by_name[name].append(symbol_id)

    rows = conn.execute(
        "SELECT id, target FROM repo_relationships WHERE project_path = ?",
        (project_path,),
    ).fetchall()
    for relationship_id, target in rows:
        cleaned = target.removesuffix("()")
        target_id = exact.get(cleaned)
        if target_id is None:
            candidates = by_name.get(cleaned.rsplit(".", 1)[-1], [])
            if len(candidates) == 1:
                target_id = candidates[0]
        conn.execute(
            "UPDATE repo_relationships SET target_symbol_id = ? WHERE id = ?",
            (target_id, relationship_id),
        )


def index_repository(project_path: str) -> dict[str, Any]:
    root = store.find_project_root(project_path)
    if not os.path.isdir(root):
        raise ValueError(f"Repository path does not exist: {project_path}")

    discovered = _discover_files(root)
    discovered_set = set(discovered)
    commit_hash, history = _git_metadata(root)
    indexed_at = store._now()
    updated = skipped = removed = error_count = 0
    errors: list[dict[str, str]] = []

    conn = store._connect()
    try:
        existing_rows = conn.execute(
            "SELECT id, path, content_hash FROM repo_files WHERE project_path = ?",
            (root,),
        ).fetchall()
        existing = {path: (file_id, content_hash) for file_id, path, content_hash in existing_rows}

        for relative_path in discovered:
            full_path = os.path.join(root, *PurePosixPath(relative_path).parts)
            content = _read_text(full_path)
            if content is None:
                error_count += 1
                errors.append({"path": relative_path, "error": "not readable as text"})
                continue
            content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
            git = history.get(relative_path, {})
            current = existing.get(relative_path)
            if current and current[1] == content_hash:
                conn.execute(
                    "UPDATE repo_files SET last_commit = ?, last_author = ?, "
                    "last_changed_at = ? WHERE id = ?",
                    (
                        git.get("last_commit"),
                        git.get("last_author"),
                        git.get("last_changed_at"),
                        current[0],
                    ),
                )
                skipped += 1
                continue

            language = LANGUAGES.get(PurePosixPath(relative_path).suffix.casefold(), "Text")
            symbols: list[dict] = []
            relationships: list[dict] = []
            module_summary = ""
            if language == "Python":
                try:
                    symbols, relationships, module_summary = _python_structure(content, relative_path)
                except (SyntaxError, ValueError) as exc:
                    error_count += 1
                    errors.append({"path": relative_path, "error": str(exc)})
                    module_summary = "Structural analysis unavailable because the file did not parse."
            summary = _file_summary(relative_path, language, symbols, relationships, module_summary)
            if current:
                file_id = current[0]
                conn.execute("DELETE FROM repo_relationships WHERE source_file_id = ?", (file_id,))
                conn.execute("DELETE FROM repo_symbols WHERE file_id = ?", (file_id,))
                conn.execute(
                    "UPDATE repo_files SET language = ?, content_hash = ?, size_bytes = ?, "
                    "line_count = ?, content = ?, summary = ?, last_commit = ?, last_author = ?, "
                    "last_changed_at = ?, indexed_at = ? WHERE id = ?",
                    (
                        language,
                        content_hash,
                        len(content.encode("utf-8")),
                        len(content.splitlines()),
                        content,
                        summary,
                        git.get("last_commit"),
                        git.get("last_author"),
                        git.get("last_changed_at"),
                        indexed_at,
                        file_id,
                    ),
                )
            else:
                cursor = conn.execute(
                    "INSERT INTO repo_files (project_path, path, language, content_hash, "
                    "size_bytes, line_count, content, summary, last_commit, last_author, "
                    "last_changed_at, indexed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        root,
                        relative_path,
                        language,
                        content_hash,
                        len(content.encode("utf-8")),
                        len(content.splitlines()),
                        content,
                        summary,
                        git.get("last_commit"),
                        git.get("last_author"),
                        git.get("last_changed_at"),
                        indexed_at,
                    ),
                )
                file_id = cursor.lastrowid

            symbol_ids: dict[str, int] = {}
            for symbol in symbols:
                cursor = conn.execute(
                    "INSERT INTO repo_symbols (project_path, file_id, name, qualified_name, "
                    "kind, start_line, end_line, signature, docstring, summary, "
                    "parent_qualified_name) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        root,
                        file_id,
                        symbol["name"],
                        symbol["qualified_name"],
                        symbol["kind"],
                        symbol["start_line"],
                        symbol["end_line"],
                        symbol["signature"],
                        symbol["docstring"],
                        symbol["summary"],
                        symbol["parent_qualified_name"],
                    ),
                )
                symbol_ids[symbol["qualified_name"]] = cursor.lastrowid
            for relationship in relationships:
                conn.execute(
                    "INSERT INTO repo_relationships (project_path, source_file_id, "
                    "source_symbol_id, kind, target, line) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        root,
                        file_id,
                        symbol_ids.get(relationship["source"]),
                        relationship["kind"],
                        relationship["target"],
                        relationship["line"],
                    ),
                )
            updated += 1

        for relative_path, (file_id, _content_hash) in existing.items():
            if relative_path in discovered_set:
                continue
            conn.execute("DELETE FROM repo_relationships WHERE source_file_id = ?", (file_id,))
            conn.execute("DELETE FROM repo_symbols WHERE file_id = ?", (file_id,))
            conn.execute("DELETE FROM repo_files WHERE id = ?", (file_id,))
            removed += 1

        _resolve_relationships(conn, root)
        file_count = conn.execute(
            "SELECT COUNT(*) FROM repo_files WHERE project_path = ?", (root,)
        ).fetchone()[0]
        symbol_count = conn.execute(
            "SELECT COUNT(*) FROM repo_symbols WHERE project_path = ?", (root,)
        ).fetchone()[0]
        relationship_count = conn.execute(
            "SELECT COUNT(*) FROM repo_relationships WHERE project_path = ?", (root,)
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO repo_index_state (project_path, indexed_at, commit_hash, file_count, "
            "symbol_count, relationship_count, error_count) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(project_path) DO UPDATE SET indexed_at = excluded.indexed_at, "
            "commit_hash = excluded.commit_hash, file_count = excluded.file_count, "
            "symbol_count = excluded.symbol_count, relationship_count = excluded.relationship_count, "
            "error_count = excluded.error_count",
            (root, indexed_at, commit_hash, file_count, symbol_count, relationship_count, error_count),
        )
        conn.commit()
    finally:
        conn.close()

    return {
        "project_path": root,
        "indexed_at": indexed_at,
        "commit_hash": commit_hash,
        "file_count": file_count,
        "symbol_count": symbol_count,
        "relationship_count": relationship_count,
        "updated": updated,
        "unchanged": skipped,
        "removed": removed,
        "error_count": error_count,
        "errors": errors[:20],
        "structural_languages": ["Python"],
    }


def get_index_status(project_path: str) -> dict[str, Any]:
    root = store.find_project_root(project_path)
    conn = store._connect()
    try:
        row = conn.execute(
            "SELECT indexed_at, commit_hash, file_count, symbol_count, relationship_count, "
            "error_count FROM repo_index_state WHERE project_path = ?",
            (root,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return {"project_path": root, "indexed": False}
    return {
        "project_path": root,
        "indexed": True,
        "indexed_at": row[0],
        "commit_hash": row[1],
        "file_count": row[2],
        "symbol_count": row[3],
        "relationship_count": row[4],
        "error_count": row[5],
    }


def _ensure_index(project_path: str) -> str:
    root = store.find_project_root(project_path)
    index_repository(root)
    return root


def search_code(project_path: str, query: str, limit: int = 10) -> list[dict[str, Any]]:
    root = _ensure_index(project_path)
    query = " ".join((query or "").split()).strip()
    if not query:
        return []
    limit = max(1, min(MAX_SEARCH_LIMIT, int(limit)))
    terms = list(dict.fromkeys(re.findall(r"[\w./:\\-]+", query.casefold())))[:24]
    if not terms:
        return []
    match_query = " OR ".join(f'"{term}"' for term in terms)

    conn = store._connect()
    try:
        rows = conn.execute(
            "SELECT f.id, f.path, f.language, f.summary, f.content, f.last_commit, "
            "f.last_author, f.last_changed_at FROM repo_files_fts "
            "JOIN repo_files f ON f.id = repo_files_fts.rowid "
            "WHERE repo_files_fts MATCH ? AND f.project_path = ? "
            "ORDER BY bm25(repo_files_fts) LIMIT ?",
            (match_query, root, min(250, limit * 20)),
        ).fetchall()
        symbol_rows = conn.execute(
            "SELECT s.name, s.qualified_name, s.kind, s.start_line, f.path "
            "FROM repo_symbols s JOIN repo_files f ON f.id = s.file_id "
            "WHERE s.project_path = ?",
            (root,),
        ).fetchall()
    finally:
        conn.close()

    symbols_by_file: dict[str, list[dict[str, Any]]] = defaultdict(list)
    folded_query = query.casefold()
    for name, qualified, kind, line, path in symbol_rows:
        if folded_query in name.casefold() or folded_query in qualified.casefold():
            symbols_by_file[path].append(
                {"name": name, "qualified_name": qualified, "kind": kind, "line": line}
            )

    ranked = []
    for file_id, path, language, summary, content, commit, author, changed_at in rows:
        folded = content.casefold()
        positions = [folded.find(term) for term in terms if term in folded]
        exact = folded.find(folded_query)
        at = exact if exact >= 0 else (min(positions) if positions else 0)
        start = max(0, at - 300)
        snippet = content[start : start + 1200]
        if start:
            snippet = "…" + snippet
        if start + 1200 < len(content):
            snippet += "…"
        coverage = sum(term in folded or term in path.casefold() for term in terms)
        ranked.append(
            (
                (int(exact >= 0), coverage, int(path.casefold() == folded_query), -file_id),
                {
                    "path": path,
                    "language": language,
                    "summary": summary,
                    "snippet": snippet,
                    "matching_symbols": symbols_by_file.get(path, []),
                    "git": {
                        "last_commit": commit,
                        "last_author": author,
                        "last_changed_at": changed_at,
                    },
                },
            )
        )
    ranked.sort(key=lambda item: item[0], reverse=True)
    return [result for _score, result in ranked[:limit]]


def explain_symbol(project_path: str, symbol: str) -> dict[str, Any] | None:
    root = _ensure_index(project_path)
    needle = (symbol or "").strip()
    if not needle:
        return None
    conn = store._connect()
    try:
        rows = conn.execute(
            "SELECT s.id, s.name, s.qualified_name, s.kind, s.start_line, s.end_line, "
            "s.signature, s.docstring, s.summary, s.parent_qualified_name, f.path, "
            "f.last_commit, f.last_author, f.last_changed_at FROM repo_symbols s "
            "JOIN repo_files f ON f.id = s.file_id WHERE s.project_path = ? AND "
            "(s.qualified_name = ? OR s.name = ? OR s.qualified_name LIKE ?) "
            "ORDER BY CASE WHEN s.qualified_name = ? THEN 0 WHEN s.name = ? THEN 1 ELSE 2 END, "
            "length(s.qualified_name), f.path LIMIT 10",
            (root, needle, needle, f"%.{needle}", needle, needle),
        ).fetchall()
        if not rows:
            return None
        row = rows[0]
        outgoing = conn.execute(
            "SELECT kind, target, line FROM repo_relationships "
            "WHERE source_symbol_id = ? ORDER BY line, kind LIMIT 100",
            (row[0],),
        ).fetchall()
        incoming = conn.execute(
            "SELECT r.kind, r.line, f.path, s.qualified_name FROM repo_relationships r "
            "JOIN repo_files f ON f.id = r.source_file_id "
            "LEFT JOIN repo_symbols s ON s.id = r.source_symbol_id "
            "WHERE r.project_path = ? AND (r.target_symbol_id = ? OR r.target = ?) "
            "ORDER BY f.path, r.line LIMIT 100",
            (root, row[0], row[1]),
        ).fetchall()
    finally:
        conn.close()
    return {
        "name": row[1],
        "qualified_name": row[2],
        "kind": row[3],
        "path": row[10],
        "start_line": row[4],
        "end_line": row[5],
        "signature": row[6],
        "docstring": row[7],
        "summary": row[8],
        "parent": row[9],
        "git": {"last_commit": row[11], "last_author": row[12], "last_changed_at": row[13]},
        "outgoing": [
            {"kind": kind, "target": target, "line": line} for kind, target, line in outgoing
        ],
        "incoming": [
            {"kind": kind, "path": path, "source": source, "line": line}
            for kind, line, path, source in incoming
        ],
        "alternatives": [
            {"qualified_name": other[2], "kind": other[3], "path": other[10], "line": other[4]}
            for other in rows[1:]
        ],
    }


def find_references(
    project_path: str, symbol: str, kind: str | None = None, limit: int = 100
) -> list[dict[str, Any]]:
    root = _ensure_index(project_path)
    needle = (symbol or "").strip()
    if not needle:
        return []
    limit = max(1, min(500, int(limit)))
    allowed_kinds = {"call", "reference", "tests", "inherits", "import"}
    if kind is not None and kind not in allowed_kinds:
        raise ValueError(f"kind must be one of: {', '.join(sorted(allowed_kinds))}")

    conn = store._connect()
    try:
        symbol_ids = [
            row[0]
            for row in conn.execute(
                "SELECT id FROM repo_symbols WHERE project_path = ? AND "
                "(name = ? OR qualified_name = ? OR qualified_name LIKE ?)",
                (root, needle, needle, f"%.{needle}"),
            )
        ]
        rows = conn.execute(
            "SELECT r.kind, r.target, r.line, r.target_symbol_id, f.path, "
            "s.qualified_name FROM repo_relationships r "
            "JOIN repo_files f ON f.id = r.source_file_id "
            "LEFT JOIN repo_symbols s ON s.id = r.source_symbol_id "
            "WHERE r.project_path = ? ORDER BY f.path, r.line",
            (root,),
        ).fetchall()
    finally:
        conn.close()

    results = []
    for relation_kind, target, line, target_symbol_id, path, source in rows:
        target_name = target.rsplit(".", 1)[-1]
        if target_symbol_id not in symbol_ids and target != needle and target_name != needle:
            continue
        if kind and relation_kind != kind:
            continue
        results.append(
            {
                "kind": relation_kind,
                "target": target,
                "path": path,
                "line": line,
                "source_symbol": source,
            }
        )
        if len(results) >= limit:
            break
    return results


def get_repository_map(project_path: str, directory: str = "") -> dict[str, Any]:
    root = _ensure_index(project_path)
    directory = str(PurePosixPath(directory.replace("\\", "/"))).strip("./")
    if directory == ".":
        directory = ""
    prefix = f"{directory}/" if directory else ""

    conn = store._connect()
    try:
        files = conn.execute(
            "SELECT id, path, language, line_count, summary FROM repo_files "
            "WHERE project_path = ? AND path LIKE ? ORDER BY path",
            (root, f"{prefix}%"),
        ).fetchall()
        file_ids = {row[0] for row in files}
        symbols = conn.execute(
            "SELECT s.file_id, s.qualified_name, s.kind, s.start_line FROM repo_symbols s "
            "WHERE s.project_path = ? ORDER BY s.file_id, s.start_line",
            (root,),
        ).fetchall()
        relationships = conn.execute(
            "SELECT source_file_id, kind, target FROM repo_relationships "
            "WHERE project_path = ? AND kind IN ('import', 'inherits', 'tests')",
            (root,),
        ).fetchall()
    finally:
        conn.close()

    symbols = [row for row in symbols if row[0] in file_ids]
    relationships = [row for row in relationships if row[0] in file_ids]
    language_counts = Counter(row[2] for row in files)
    kind_counts = Counter(row[2] for row in symbols)
    directories: dict[str, dict[str, int]] = defaultdict(lambda: {"files": 0, "lines": 0})
    for _file_id, path, _language, line_count, _summary in files:
        relative = path[len(prefix) :] if prefix and path.startswith(prefix) else path
        child = relative.split("/", 1)[0] if "/" in relative else "."
        directories[child]["files"] += 1
        directories[child]["lines"] += line_count

    return {
        "project_path": root,
        "scope": directory or ".",
        "summary": (
            f"{len(files)} files, {sum(row[3] for row in files)} lines, "
            f"{len(symbols)} Python symbols, {len(relationships)} structural links."
        ),
        "languages": dict(sorted(language_counts.items())),
        "symbol_kinds": dict(sorted(kind_counts.items())),
        "children": [
            {"name": name, **counts} for name, counts in sorted(directories.items())
        ],
        "files": [
            {"path": path, "language": language, "lines": lines, "summary": summary}
            for _file_id, path, language, lines, summary in files[:200]
        ],
        "key_symbols": [
            {"qualified_name": qualified, "kind": kind, "line": line}
            for _file_id, qualified, kind, line in symbols[:200]
        ],
        "relationships": [
            {"kind": kind, "target": target}
            for _file_id, kind, target in relationships[:200]
        ],
        "truncated": len(files) > 200 or len(symbols) > 200 or len(relationships) > 200,
    }

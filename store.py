import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import embeddings


def _default_data_dir() -> str:
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA", os.path.expanduser("~"))
    elif sys.platform == "darwin":
        base = os.path.join(os.path.expanduser("~"), "Library", "Application Support")
    else:
        base = os.environ.get(
            "XDG_DATA_HOME", os.path.join(os.path.expanduser("~"), ".local", "share")
        )
    return os.path.join(base, "AgentMemorySync")


DEFAULT_DB_PATH = os.path.join(_default_data_dir(), "store.db")

DEFAULT_RECENT_LIMIT = 15
MIN_RECENT_LIMIT = 1
MAX_RECENT_LIMIT = 100
MAX_CONTEXT_SUMMARY_CHARS = 2000
MAX_NOTE_CHARS = 4000
MAX_RENDERED_CONTEXT_CHARS = 200_000
CONTEXT_NOTE_EVENT_TYPE = "context_note"
CONTEXT_CATEGORIES = (
    "decision",
    "constraint",
    "task",
    "artifact",
    "insight",
    "note",
    "activity",
)
LATEST_SCHEMA_VERSION = 14
DISPATCH_WORK_STATUSES = ("planned", "in_progress", "blocked", "completed")
SQLITE_BUSY_TIMEOUT_MS = 30000
USER_ROLES = ("admin", "member")
PROJECT_ACCESS_LEVELS = ("viewer", "editor", "operator")
MAX_RETENTION_DAYS = 3650
SEMANTIC_CANDIDATE_CAP = 300
SEMANTIC_TEXT_CHAR_CAP = 4000
SEMANTIC_MIN_SIMILARITY = 0.15
RRF_K = 60


def _db_path() -> str:
    return os.environ.get("AGENT_MEMORY_DB_PATH", DEFAULT_DB_PATH)


def db_path() -> str:
    return _db_path()


def _connect() -> sqlite3.Connection:
    path = _db_path()
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    try:
        conn.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
        _enable_wal(conn)
        _migrate(conn)
        return conn
    except Exception:
        conn.close()
        raise


def _connect_for_search() -> sqlite3.Connection | None:
    path = os.path.abspath(_db_path())
    if not os.path.isfile(path):
        return None

    base_uri = Path(path).as_uri()
    errors: list[sqlite3.OperationalError] = []
    for options in ("mode=ro", "mode=ro&immutable=1"):
        try:
            conn = sqlite3.connect(f"{base_uri}?{options}", uri=True, timeout=30)
            conn.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
            try:
                row = conn.execute(
                    "SELECT MAX(version) FROM schema_migrations"
                ).fetchone()
                version = row[0] or 0
            except sqlite3.OperationalError as exc:
                if "no such table" not in str(exc).lower():
                    conn.close()
                    raise
                version = 0
            if version >= 9:
                return conn
            conn.close()
            break
        except sqlite3.OperationalError as exc:
            errors.append(exc)

    try:
        return _connect()
    except sqlite3.OperationalError as exc:
        detail = str(exc)
        if errors:
            detail += f" (read-only open also failed: {errors[-1]})"
        raise sqlite3.OperationalError(
            f"Unable to open shared context database at {path}: {detail}"
        ) from exc


def _enable_wal(conn: sqlite3.Connection) -> None:
    deadline = time.monotonic() + (SQLITE_BUSY_TIMEOUT_MS / 1000)
    while True:
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() or time.monotonic() >= deadline:
                raise
            time.sleep(0.05)


def _add_column(conn: sqlite3.Connection, table: str, definition: str) -> None:
    column = definition.split()[0]
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")


def _migration_1_current_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_path TEXT NOT NULL,
            agent TEXT NOT NULL,
            session_id TEXT,
            event_type TEXT NOT NULL,
            summary TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_events_project ON events(project_path)"
    )
    for definition in (
        "context_included INTEGER NOT NULL DEFAULT 1",
        "context_pinned INTEGER NOT NULL DEFAULT 0",
        "context_summary TEXT",
        "context_updated_at TEXT",
    ):
        _add_column(conn, "events", definition)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS context_settings (
            project_path TEXT PRIMARY KEY,
            recent_limit INTEGER NOT NULL DEFAULT 15,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS file_touches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_path TEXT NOT NULL,
            agent TEXT NOT NULL,
            session_id TEXT,
            file_path TEXT NOT NULL,
            tool_name TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_touches_project_file "
        "ON file_touches(project_path, file_path)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS projects (
            project_path TEXT PRIMARY KEY,
            added_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS imported_sessions (
            session_id TEXT PRIMARY KEY,
            project_path TEXT NOT NULL,
            agent TEXT NOT NULL,
            imported_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS dispatch_jobs (
            id TEXT PRIMARY KEY,
            project_path TEXT NOT NULL,
            agent TEXT NOT NULL,
            prompt TEXT NOT NULL,
            allow_edits INTEGER NOT NULL,
            status TEXT NOT NULL,
            result_text TEXT,
            created_at TEXT NOT NULL,
            finished_at TEXT,
            tokens INTEGER DEFAULT 0
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_dispatch_project ON dispatch_jobs(project_path)"
    )
    for definition in (
        "tokens INTEGER DEFAULT 0",
        "context_snapshot TEXT",
    ):
        _add_column(conn, "dispatch_jobs", definition)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS dispatch_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT NOT NULL,
            line TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_dispatch_logs_job ON dispatch_logs(job_id, id)"
    )


def _migration_2_windows_path_casing(conn: sqlite3.Connection) -> None:
    if os.name == "nt":
        _normalize_project_casing(conn)


def _migration_3_context_categories(conn: sqlite3.Connection) -> None:
    _add_column(conn, "events", "context_category TEXT")


def _migration_4_native_context_tokens(conn: sqlite3.Connection) -> None:
    _add_column(conn, "events", "source_tokens INTEGER NOT NULL DEFAULT 0")


def _migration_5_agent_interactions(conn: sqlite3.Connection) -> None:
    _add_column(conn, "dispatch_jobs", "session_id TEXT")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS dispatch_interactions (
            id TEXT PRIMARY KEY,
            job_id TEXT NOT NULL,
            project_path TEXT NOT NULL,
            agent TEXT NOT NULL,
            kind TEXT NOT NULL,
            prompt TEXT NOT NULL,
            options_json TEXT NOT NULL,
            status TEXT NOT NULL,
            response TEXT,
            created_at TEXT NOT NULL,
            resolved_at TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_dispatch_interactions_project "
        "ON dispatch_interactions(project_path, status, created_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_dispatch_interactions_job "
        "ON dispatch_interactions(job_id, created_at)"
    )


def _migration_6_dispatch_operations(conn: sqlite3.Connection) -> None:
    for definition in (
        "progress INTEGER NOT NULL DEFAULT 0",
        "progress_label TEXT NOT NULL DEFAULT 'Queued'",
        "activity_count INTEGER NOT NULL DEFAULT 0",
        "updated_at TEXT",
        "work_status TEXT NOT NULL DEFAULT 'in_progress'",
        "replaces_job_id TEXT",
    ):
        _add_column(conn, "dispatch_jobs", definition)


def _compact_legacy_history(text: str, limit: int = 500) -> str:
    flat = " ".join((text or "").split())
    if len(flat) <= limit:
        return flat
    half = (limit - len("Transcript start:  | Transcript end: ")) // 2
    return f"Transcript start: {flat[:half]} | Transcript end: {flat[-half:]}"


def _migration_7_shared_corpus(conn: sqlite3.Connection) -> None:
    _add_column(conn, "events", "raw_context TEXT")
    _add_column(conn, "events", "context_tokens INTEGER NOT NULL DEFAULT 0")
    rows = conn.execute(
        "SELECT id, summary FROM events WHERE event_type = 'history'"
    ).fetchall()
    for event_id, context in rows:
        context = context or ""
        conn.execute(
            "UPDATE events SET raw_context = ?, context_tokens = ?, summary = ? WHERE id = ?",
            (context, (len(context) + 3) // 4, _compact_legacy_history(context), event_id),
        )


def _migration_8_agent_models(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_settings (
            agent_id TEXT PRIMARY KEY,
            model TEXT,
            updated_at TEXT NOT NULL
        )
        """
    )


def _events_searchable_expr(alias: str) -> str:
    return (
        f"COALESCE({alias}.context_summary, '') || ' ' || {alias}.summary || ' ' || "
        f"COALESCE({alias}.raw_context, '')"
    )


def _migration_9_fts5_corpus_search(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS events_fts USING fts5(
            searchable,
            content='events',
            content_rowid='id',
            tokenize="unicode61 tokenchars '-_./:\\'"
        )
        """
    )
    conn.execute(
        "INSERT INTO events_fts(rowid, searchable) "
        f"SELECT id, {_events_searchable_expr('events')} FROM events"
    )
    conn.execute(
        f"""
        CREATE TRIGGER IF NOT EXISTS events_fts_ai AFTER INSERT ON events BEGIN
            INSERT INTO events_fts(rowid, searchable)
            VALUES (new.id, {_events_searchable_expr('new')});
        END
        """
    )
    conn.execute(
        f"""
        CREATE TRIGGER IF NOT EXISTS events_fts_ad AFTER DELETE ON events BEGIN
            INSERT INTO events_fts(events_fts, rowid, searchable)
            VALUES ('delete', old.id, {_events_searchable_expr('old')});
        END
        """
    )
    conn.execute(
        f"""
        CREATE TRIGGER IF NOT EXISTS events_fts_au AFTER UPDATE ON events BEGIN
            INSERT INTO events_fts(events_fts, rowid, searchable)
            VALUES ('delete', old.id, {_events_searchable_expr('old')});
            INSERT INTO events_fts(rowid, searchable)
            VALUES (new.id, {_events_searchable_expr('new')});
        END
        """
    )


def _migration_10_coordinated_deployments(conn: sqlite3.Connection) -> None:
    for definition in (
        "parent_job_id TEXT",
        "coordination_id TEXT",
        "task_label TEXT",
        "model TEXT",
    ):
        _add_column(conn, "dispatch_jobs", definition)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_dispatch_coordination "
        "ON dispatch_jobs(coordination_id, created_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_dispatch_parent "
        "ON dispatch_jobs(parent_job_id, created_at)"
    )


def _migration_11_repository_intelligence(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS repo_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_path TEXT NOT NULL,
            path TEXT NOT NULL,
            language TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            size_bytes INTEGER NOT NULL,
            line_count INTEGER NOT NULL,
            content TEXT NOT NULL,
            summary TEXT NOT NULL,
            last_commit TEXT,
            last_author TEXT,
            last_changed_at TEXT,
            indexed_at TEXT NOT NULL,
            UNIQUE(project_path, path)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_repo_files_project_path "
        "ON repo_files(project_path, path)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS repo_symbols (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_path TEXT NOT NULL,
            file_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            qualified_name TEXT NOT NULL,
            kind TEXT NOT NULL,
            start_line INTEGER NOT NULL,
            end_line INTEGER NOT NULL,
            signature TEXT,
            docstring TEXT,
            summary TEXT NOT NULL,
            parent_qualified_name TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_repo_symbols_lookup "
        "ON repo_symbols(project_path, name, qualified_name)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_repo_symbols_file "
        "ON repo_symbols(file_id, start_line)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS repo_relationships (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_path TEXT NOT NULL,
            source_file_id INTEGER NOT NULL,
            source_symbol_id INTEGER,
            kind TEXT NOT NULL,
            target TEXT NOT NULL,
            target_symbol_id INTEGER,
            line INTEGER NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_repo_relationships_source "
        "ON repo_relationships(project_path, source_symbol_id, kind)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_repo_relationships_target "
        "ON repo_relationships(project_path, target, target_symbol_id, kind)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS repo_index_state (
            project_path TEXT PRIMARY KEY,
            indexed_at TEXT NOT NULL,
            commit_hash TEXT,
            file_count INTEGER NOT NULL,
            symbol_count INTEGER NOT NULL,
            relationship_count INTEGER NOT NULL,
            error_count INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    conn.execute(
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS repo_files_fts USING fts5(
            path,
            summary,
            content,
            content='repo_files',
            content_rowid='id',
            tokenize="unicode61 tokenchars '-_./:\\'"
        )
        """
    )
    conn.execute(
        "INSERT INTO repo_files_fts(rowid, path, summary, content) "
        "SELECT id, path, summary, content FROM repo_files"
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS repo_files_fts_ai AFTER INSERT ON repo_files BEGIN
            INSERT INTO repo_files_fts(rowid, path, summary, content)
            VALUES (new.id, new.path, new.summary, new.content);
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS repo_files_fts_ad AFTER DELETE ON repo_files BEGIN
            INSERT INTO repo_files_fts(repo_files_fts, rowid, path, summary, content)
            VALUES ('delete', old.id, old.path, old.summary, old.content);
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS repo_files_fts_au AFTER UPDATE ON repo_files BEGIN
            INSERT INTO repo_files_fts(repo_files_fts, rowid, path, summary, content)
            VALUES ('delete', old.id, old.path, old.summary, old.content);
            INSERT INTO repo_files_fts(rowid, path, summary, content)
            VALUES (new.id, new.path, new.summary, new.content);
        END
        """
    )


def _migration_12_enterprise_controls(conn: sqlite3.Connection) -> None:
    _add_column(conn, "users", "role TEXT NOT NULL DEFAULT 'admin'")
    _add_column(conn, "users", "active INTEGER NOT NULL DEFAULT 1")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS project_permissions (
            username TEXT NOT NULL,
            project_path TEXT NOT NULL,
            access_level TEXT NOT NULL,
            granted_by TEXT NOT NULL,
            granted_at TEXT NOT NULL,
            PRIMARY KEY (username, project_path)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_project_permissions_project "
        "ON project_permissions(project_path, username)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS enterprise_policies (
            project_path TEXT PRIMARY KEY,
            retention_days INTEGER NOT NULL DEFAULT 0,
            secret_redaction INTEGER NOT NULL DEFAULT 1,
            updated_by TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            last_enforced_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS audit_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            actor TEXT NOT NULL,
            action TEXT NOT NULL,
            project_path TEXT,
            target_type TEXT,
            target_id TEXT,
            details_json TEXT NOT NULL,
            previous_hash TEXT NOT NULL,
            entry_hash TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_audit_events_project_time "
        "ON audit_events(project_path, created_at)"
    )


def _migration_13_event_embeddings(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS event_embeddings (
            event_id INTEGER PRIMARY KEY REFERENCES events(id) ON DELETE CASCADE,
            model TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            vector BLOB NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )


def _migration_14_local_providers(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS local_providers (
            agent_id TEXT PRIMARY KEY,
            display_name TEXT NOT NULL,
            base_url TEXT NOT NULL,
            model TEXT NOT NULL,
            api_key_env TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )


MIGRATIONS = (
    (1, _migration_1_current_schema),
    (2, _migration_2_windows_path_casing),
    (3, _migration_3_context_categories),
    (4, _migration_4_native_context_tokens),
    (5, _migration_5_agent_interactions),
    (6, _migration_6_dispatch_operations),
    (7, _migration_7_shared_corpus),
    (8, _migration_8_agent_models),
    (9, _migration_9_fts5_corpus_search),
    (10, _migration_10_coordinated_deployments),
    (11, _migration_11_repository_intelligence),
    (12, _migration_12_enterprise_controls),
    (13, _migration_13_event_embeddings),
    (14, _migration_14_local_providers),
)


def _migrate(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL
        )
        """
    )
    for version, migration in MIGRATIONS:
        conn.execute("BEGIN IMMEDIATE")
        try:
            already_applied = conn.execute(
                "SELECT 1 FROM schema_migrations WHERE version = ?", (version,)
            ).fetchone()
            if already_applied:
                conn.commit()
                continue
            migration(conn)
            conn.execute(
                "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                (version, _now()),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def schema_version() -> int:
    conn = _connect()
    try:
        row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
        return row[0] or 0
    finally:
        conn.close()


def _normalize_project_casing(conn: sqlite3.Connection) -> None:
    for table in (
        "events",
        "file_touches",
        "dispatch_jobs",
        "imported_sessions",
    ):
        conn.execute(
            f"UPDATE {table} SET project_path = LOWER(project_path) "
            "WHERE project_path <> LOWER(project_path)"
        )
    conn.execute(
        "INSERT OR IGNORE INTO projects (project_path, added_at) "
        "SELECT LOWER(project_path), added_at FROM projects "
        "WHERE project_path <> LOWER(project_path)"
    )
    conn.execute("DELETE FROM projects WHERE project_path <> LOWER(project_path)")
    conn.execute(
        "INSERT OR IGNORE INTO context_settings "
        "(project_path, recent_limit, updated_at) "
        "SELECT LOWER(project_path), recent_limit, updated_at FROM context_settings "
        "WHERE project_path <> LOWER(project_path)"
    )
    conn.execute(
        "DELETE FROM context_settings WHERE project_path <> LOWER(project_path)"
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


_SECRET_PATTERNS = (
    (re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----.*?-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", re.DOTALL), "[REDACTED PRIVATE KEY]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[REDACTED AWS ACCESS KEY]"),
    (re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"), "[REDACTED GITHUB TOKEN]"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"), "[REDACTED API KEY]"),
    (re.compile(r"(?i)\b(authorization\s*:\s*bearer\s+)[A-Za-z0-9._~+/=-]{12,}"), r"\1[REDACTED]"),
    (
        re.compile(
            r"(?i)\b(password|passwd|pwd|api[_-]?key|access[_-]?token|auth[_-]?token|client[_-]?secret|secret)"
            r"(\s*[:=]\s*)(?:[\"']?)([^\s,;\"']{8,})"
        ),
        r"\1\2[REDACTED]",
    ),
)


def redact_secrets(text: str) -> tuple[str, int]:
    value = text or ""
    count = 0
    for pattern, replacement in _SECRET_PATTERNS:
        value, matches = pattern.subn(replacement, value)
        count += matches
    return value, count


def _redact_audit_details(value, key: str = ""):
    if isinstance(value, dict):
        return {str(item_key): _redact_audit_details(item_value, str(item_key))
                for item_key, item_value in value.items()}
    if isinstance(value, list):
        return [_redact_audit_details(item) for item in value]
    if isinstance(value, tuple):
        return [_redact_audit_details(item) for item in value]
    if isinstance(value, str):
        if re.search(r"(?i)(password|passwd|pwd|api.?key|token|secret)", key):
            return "[REDACTED]"
        return redact_secrets(value)[0]
    return value


def _redact_for_project(conn: sqlite3.Connection, project_path: str, text: str) -> str:
    row = conn.execute(
        "SELECT secret_redaction FROM enterprise_policies WHERE project_path = ?",
        (project_path,),
    ).fetchone()
    if row is not None and not row[0]:
        return text
    return redact_secrets(text)[0]


def _validate_user_role(role: str) -> str:
    role = (role or "").strip().lower()
    if role not in USER_ROLES:
        raise ValueError("role must be 'admin' or 'member'.")
    return role


def _validate_access_level(access_level: str) -> str:
    access_level = (access_level or "").strip().lower()
    if access_level not in PROJECT_ACCESS_LEVELS:
        raise ValueError("access_level must be viewer, editor, or operator.")
    return access_level


def list_users() -> list[dict]:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT username, role, active, created_at FROM users ORDER BY username"
        ).fetchall()
    finally:
        conn.close()
    return [
        {"username": row[0], "role": row[1], "active": bool(row[2]), "created_at": row[3]}
        for row in rows
    ]


def set_user_role(username: str, role: str, active: bool | None = None) -> dict:
    role = _validate_user_role(role)
    conn = _connect()
    try:
        sets = ["role = ?"]
        params: list[object] = [role]
        if active is not None:
            sets.append("active = ?")
            params.append(int(active))
        params.append(username)
        changed = conn.execute(
            f"UPDATE users SET {', '.join(sets)} WHERE username = ?", tuple(params)
        ).rowcount
        if not changed:
            raise LookupError("No such user.")
        conn.commit()
    finally:
        conn.close()
    return get_user_by_username(username)


def set_project_permission(
    username: str, project_path: str, access_level: str, granted_by: str
) -> dict:
    access_level = _validate_access_level(access_level)
    conn = _connect()
    try:
        if not conn.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
            raise LookupError("No such user.")
        if not conn.execute("SELECT 1 FROM projects WHERE project_path = ?", (project_path,)).fetchone():
            raise LookupError("No such tracked project.")
        conn.execute(
            "INSERT INTO project_permissions (username, project_path, access_level, granted_by, granted_at) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(username, project_path) DO UPDATE SET "
            "access_level = excluded.access_level, granted_by = excluded.granted_by, "
            "granted_at = excluded.granted_at",
            (username, project_path, access_level, granted_by, _now()),
        )
        conn.commit()
    finally:
        conn.close()
    return {"username": username, "project_path": project_path, "access_level": access_level}


def delete_project_permission(username: str, project_path: str) -> bool:
    conn = _connect()
    try:
        changed = conn.execute(
            "DELETE FROM project_permissions WHERE username = ? AND project_path = ?",
            (username, project_path),
        ).rowcount
        conn.commit()
        return bool(changed)
    finally:
        conn.close()


def list_project_permissions(project_path: str | None = None) -> list[dict]:
    conn = _connect()
    try:
        if project_path:
            rows = conn.execute(
                "SELECT username, project_path, access_level, granted_by, granted_at "
                "FROM project_permissions WHERE project_path = ? ORDER BY username",
                (project_path,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT username, project_path, access_level, granted_by, granted_at "
                "FROM project_permissions ORDER BY project_path, username"
            ).fetchall()
    finally:
        conn.close()
    return [dict(zip(("username", "project_path", "access_level", "granted_by", "granted_at"), row)) for row in rows]


def get_project_access(username: str, project_path: str) -> str | None:
    conn = _connect()
    try:
        user = conn.execute(
            "SELECT role, active FROM users WHERE username = ?", (username,)
        ).fetchone()
        if not user:
            return "operator" if not conn.execute("SELECT 1 FROM users LIMIT 1").fetchone() else None
        if not user[1]:
            return None
        if user[0] == "admin":
            return "operator"
        row = conn.execute(
            "SELECT access_level FROM project_permissions WHERE username = ? AND project_path = ?",
            (username, project_path),
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def get_enterprise_policy(project_path: str) -> dict:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT retention_days, secret_redaction, updated_by, updated_at, last_enforced_at "
            "FROM enterprise_policies WHERE project_path = ?", (project_path,)
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return {"project_path": project_path, "retention_days": 0, "secret_redaction": True,
                "updated_by": None, "updated_at": None, "last_enforced_at": None}
    return {"project_path": project_path, "retention_days": row[0], "secret_redaction": bool(row[1]),
            "updated_by": row[2], "updated_at": row[3], "last_enforced_at": row[4]}


def set_enterprise_policy(
    project_path: str, retention_days: int, secret_redaction: bool, updated_by: str
) -> dict:
    retention_days = int(retention_days)
    if retention_days < 0 or retention_days > MAX_RETENTION_DAYS:
        raise ValueError(f"retention_days must be between 0 and {MAX_RETENTION_DAYS}.")
    conn = _connect()
    try:
        if not conn.execute("SELECT 1 FROM projects WHERE project_path = ?", (project_path,)).fetchone():
            raise LookupError("No such tracked project.")
        conn.execute(
            "INSERT INTO enterprise_policies "
            "(project_path, retention_days, secret_redaction, updated_by, updated_at) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(project_path) DO UPDATE SET "
            "retention_days = excluded.retention_days, secret_redaction = excluded.secret_redaction, "
            "updated_by = excluded.updated_by, updated_at = excluded.updated_at",
            (project_path, retention_days, int(secret_redaction), updated_by, _now()),
        )
        conn.commit()
    finally:
        conn.close()
    return get_enterprise_policy(project_path)


def record_audit_event(
    actor: str, action: str, project_path: str | None = None,
    target_type: str | None = None, target_id: str | None = None,
    details: dict | None = None,
) -> dict:
    clean_details = json.dumps(
        _redact_audit_details(details or {}), sort_keys=True, separators=(",", ":")
    )
    created_at = _now()
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        previous = conn.execute("SELECT entry_hash FROM audit_events ORDER BY id DESC LIMIT 1").fetchone()
        previous_hash = previous[0] if previous else ""
        payload = "\x1f".join((previous_hash, actor, action, project_path or "", target_type or "",
                                 target_id or "", clean_details, created_at))
        entry_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        cursor = conn.execute(
            "INSERT INTO audit_events (actor, action, project_path, target_type, target_id, "
            "details_json, previous_hash, entry_hash, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (actor, action, project_path, target_type, target_id, clean_details,
             previous_hash, entry_hash, created_at),
        )
        conn.commit()
        event_id = cursor.lastrowid
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {"id": event_id, "entry_hash": entry_hash, "previous_hash": previous_hash}


def list_audit_events(project_path: str | None = None, limit: int = 10000) -> list[dict]:
    conn = _connect()
    try:
        if project_path:
            rows = conn.execute(
                "SELECT id, actor, action, project_path, target_type, target_id, details_json, "
                "previous_hash, entry_hash, created_at FROM audit_events WHERE project_path = ? "
                "ORDER BY id ASC LIMIT ?", (project_path, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, actor, action, project_path, target_type, target_id, details_json, "
                "previous_hash, entry_hash, created_at FROM audit_events ORDER BY id ASC LIMIT ?",
                (limit,),
            ).fetchall()
    finally:
        conn.close()
    keys = ("id", "actor", "action", "project_path", "target_type", "target_id", "details",
            "previous_hash", "entry_hash", "created_at")
    result = []
    for row in rows:
        values = list(row)
        values[6] = json.loads(values[6] or "{}")
        result.append(dict(zip(keys, values)))
    return result


def verify_audit_chain() -> dict:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT id, actor, action, project_path, target_type, target_id, details_json, "
            "previous_hash, entry_hash, created_at FROM audit_events ORDER BY id ASC"
        ).fetchall()
    finally:
        conn.close()
    expected_previous = ""
    for row in rows:
        payload = "\x1f".join((expected_previous, row[1], row[2], row[3] or "", row[4] or "",
                                 row[5] or "", row[6], row[9]))
        expected_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        if row[7] != expected_previous or row[8] != expected_hash:
            return {"valid": False, "event_count": len(rows), "broken_event_id": row[0]}
        expected_previous = row[8]
    return {"valid": True, "event_count": len(rows), "broken_event_id": None}


def purge_expired_data(project_path: str | None = None, now: datetime | None = None) -> dict:
    current = now or datetime.now(timezone.utc)
    conn = _connect()
    totals = {"events": 0, "file_touches": 0, "dispatch_jobs": 0}
    try:
        if project_path:
            policies = conn.execute(
                "SELECT project_path, retention_days FROM enterprise_policies WHERE project_path = ?",
                (project_path,),
            ).fetchall()
        else:
            policies = conn.execute(
                "SELECT project_path, retention_days FROM enterprise_policies WHERE retention_days > 0"
            ).fetchall()
        for project, days in policies:
            if days <= 0:
                continue
            cutoff = (current - timedelta(days=days)).isoformat(timespec="seconds")
            totals["events"] += conn.execute(
                "DELETE FROM events WHERE project_path = ? AND created_at < ?", (project, cutoff)
            ).rowcount
            totals["file_touches"] += conn.execute(
                "DELETE FROM file_touches WHERE project_path = ? AND created_at < ?", (project, cutoff)
            ).rowcount
            old_jobs = [row[0] for row in conn.execute(
                "SELECT id FROM dispatch_jobs WHERE project_path = ? AND created_at < ? "
                "AND status NOT IN ('running', 'waiting', 'canceling')", (project, cutoff)
            )]
            for job_id in old_jobs:
                conn.execute("DELETE FROM dispatch_logs WHERE job_id = ?", (job_id,))
                conn.execute("DELETE FROM dispatch_interactions WHERE job_id = ?", (job_id,))
            if old_jobs:
                placeholders = ",".join("?" for _ in old_jobs)
                totals["dispatch_jobs"] += conn.execute(
                    f"DELETE FROM dispatch_jobs WHERE id IN ({placeholders})", tuple(old_jobs)
                ).rowcount
            conn.execute("UPDATE enterprise_policies SET last_enforced_at = ? WHERE project_path = ?",
                         (_now(), project))
        conn.commit()
    finally:
        conn.close()
    return totals


def redact_stored_secrets(project_path: str) -> dict:
    conn = _connect()
    counts = {"events": 0, "dispatch_jobs": 0, "dispatch_logs": 0, "interactions": 0}
    try:
        for row in conn.execute(
            "SELECT id, summary, context_summary, raw_context FROM events WHERE project_path = ?",
            (project_path,),
        ).fetchall():
            values = [redact_secrets(value or "")[0] if value is not None else None for value in row[1:]]
            if tuple(values) != tuple(row[1:]):
                conn.execute("UPDATE events SET summary = ?, context_summary = ?, raw_context = ? WHERE id = ?",
                             (*values, row[0]))
                counts["events"] += 1
        for row in conn.execute(
            "SELECT id, prompt, result_text, context_snapshot FROM dispatch_jobs WHERE project_path = ?",
            (project_path,),
        ).fetchall():
            values = [redact_secrets(value or "")[0] if value is not None else None for value in row[1:]]
            if tuple(values) != tuple(row[1:]):
                conn.execute("UPDATE dispatch_jobs SET prompt = ?, result_text = ?, context_snapshot = ? WHERE id = ?",
                             (*values, row[0]))
                counts["dispatch_jobs"] += 1
        for row in conn.execute(
            "SELECT l.id, l.line FROM dispatch_logs l JOIN dispatch_jobs j ON j.id = l.job_id "
            "WHERE j.project_path = ?", (project_path,),
        ).fetchall():
            value = redact_secrets(row[1])[0]
            if value != row[1]:
                conn.execute("UPDATE dispatch_logs SET line = ? WHERE id = ?", (value, row[0]))
                counts["dispatch_logs"] += 1
        for row in conn.execute(
            "SELECT id, prompt, response FROM dispatch_interactions WHERE project_path = ?",
            (project_path,),
        ).fetchall():
            prompt = redact_secrets(row[1] or "")[0]
            response = redact_secrets(row[2] or "")[0] if row[2] is not None else None
            if (prompt, response) != (row[1], row[2]):
                conn.execute("UPDATE dispatch_interactions SET prompt = ?, response = ? WHERE id = ?",
                             (prompt, response, row[0]))
                counts["interactions"] += 1
        conn.commit()
    finally:
        conn.close()
    return counts


def find_project_root(cwd: str) -> str:

    path = os.path.abspath(cwd)
    while True:
        if os.path.isdir(os.path.join(path, ".git")):
            return os.path.normcase(path)
        parent = os.path.dirname(path)
        if parent == path:
            return os.path.normcase(os.path.abspath(cwd))
        path = parent


def record_event(
    project_path: str, agent: str, session_id: str, event_type: str, summary: str
) -> None:
    summary = summary.strip()
    if not summary:
        return
    conn = _connect()
    try:
        summary = _redact_for_project(conn, project_path, summary)
        conn.execute(
            "INSERT INTO events (project_path, agent, session_id, event_type, "
            "summary, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (project_path, agent, session_id, event_type, summary, _now()),
        )
        conn.commit()
    finally:
        conn.close()


def record_history_event(
    project_path: str,
    agent: str,
    session_id: str,
    context: str,
    source_tokens: int = 0,
    summary: str | None = None,
) -> bool:
    context = context.strip()
    if not context:
        return False
    conn = _connect()
    try:
        context = _redact_for_project(conn, project_path, context)
        if summary is not None:
            summary = _redact_for_project(conn, project_path, summary)
        row = conn.execute(
            "SELECT id FROM events WHERE project_path = ? AND agent = ? "
            "AND session_id = ? AND event_type = 'history' ORDER BY id DESC LIMIT 1",
            (project_path, agent, session_id),
        ).fetchone()
        tokens = max(0, int(source_tokens or 0))
        context_tokens = (len(context) + 3) // 4
        digest = (summary or _compact_legacy_history(context)).strip()
        if row:
            conn.execute(
                "UPDATE events SET summary = ?, raw_context = ?, source_tokens = ?, "
                "context_tokens = ? WHERE id = ?",
                (digest, context, tokens, context_tokens, row[0]),
            )
            inserted = False
        else:
            conn.execute(
                "INSERT INTO events (project_path, agent, session_id, event_type, "
                "summary, raw_context, source_tokens, context_tokens, created_at) "
                "VALUES (?, ?, ?, 'history', ?, ?, ?, ?, ?)",
                (project_path, agent, session_id, digest, context, tokens, context_tokens, _now()),
            )
            inserted = True
        conn.commit()
        return inserted
    finally:
        conn.close()


CONTEXT_HEADER = (
    "Shared project context for Claude Code and Codex (identical agent-neutral working set):"
)

TELEMETRY_EVENT_TYPE = "context_injected"


def _infer_context_category(event_type: str, summary: str) -> str:
    if event_type == CONTEXT_NOTE_EVENT_TYPE:
        return "note"
    normalized_type = event_type.lower().replace("-", "_")
    text = summary.lower()
    if any(word in normalized_type for word in ("decision", "plan", "handoff")) or any(
        marker in text for marker in ("decided ", "decision:", "we will ", "chose ")
    ):
        return "decision"
    if any(word in normalized_type for word in ("error", "conflict", "block")) or any(
        marker in text for marker in ("constraint:", "blocked", "must not", "cannot ")
    ):
        return "constraint"
    if any(word in normalized_type for word in ("task", "todo")) or any(
        marker in text for marker in ("todo:", "next step", "needs to", "follow up")
    ):
        return "task"
    if any(word in normalized_type for word in ("file", "artifact", "commit", "deploy")) or any(
        marker in text for marker in ("created ", "updated ", "implemented ", "wrote ")
    ):
        return "artifact"
    if any(word in normalized_type for word in ("research", "review", "finding")) or any(
        marker in text for marker in ("found ", "learned ", "observed ", "root cause")
    ):
        return "insight"
    return "activity"


def _validate_context_category(category: str) -> str:
    category = category.strip().lower()
    if category not in CONTEXT_CATEGORIES:
        raise ValueError(
            "category must be one of: " + ", ".join(CONTEXT_CATEGORIES) + "."
        )
    return category


def _format_context_line(agent: str, event_type: str, summary: str, created_at: str) -> str:
    return f"- [{created_at}] {agent} ({event_type}): {summary}"


def _render_context(
    pinned: list[tuple], recent: list[tuple], project_path: str = ""
) -> str:
    if not pinned and not recent:
        return ""
    lines = [
        CONTEXT_HEADER,
        "This snapshot is a starting point, not a budget: pulling more of it "
        "(or searching the raw corpus below) costs nothing you need to "
        "conserve, so use as much shared context as the task actually needs.",
    ]
    if project_path:
        search_cli = os.path.join(os.path.dirname(os.path.abspath(__file__)), "shared_context.py")
        def quoted(value: str) -> str:
            return '"' + value.replace('"', '\\"') + '"'

        lines.append(
            "Older raw evidence is shared too and is not size-limited the way "
            "this snapshot is. Either agent may search it with: "
            f'{quoted(sys.executable)} {quoted(search_cli)} search '
            f'--project {quoted(project_path)} --query "<terms>"'
        )
    if pinned:
        lines.append("")
        lines.append("Pinned:")
        lines.extend(_format_context_line(*row) for row in pinned)
        if recent:
            lines.append("")
            lines.append("Recent:")
    lines.extend(_format_context_line(*row) for row in recent)
    return "\n".join(lines)


def get_context_settings(project_path: str) -> dict:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT recent_limit FROM context_settings WHERE project_path = ?",
            (project_path,),
        ).fetchone()
    finally:
        conn.close()
    return {"recent_limit": row[0] if row else DEFAULT_RECENT_LIMIT}


def update_context_settings(project_path: str, recent_limit: int) -> dict:
    recent_limit = int(recent_limit)
    if not (MIN_RECENT_LIMIT <= recent_limit <= MAX_RECENT_LIMIT):
        raise ValueError(
            f"recent_limit must be between {MIN_RECENT_LIMIT} and {MAX_RECENT_LIMIT}."
        )
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO context_settings (project_path, recent_limit, updated_at) "
            "VALUES (?, ?, ?) ON CONFLICT(project_path) DO UPDATE SET "
            "recent_limit = excluded.recent_limit, updated_at = excluded.updated_at",
            (project_path, recent_limit, _now()),
        )
        conn.commit()
    finally:
        conn.close()
    return {"recent_limit": recent_limit}


def get_agent_models() -> dict[str, str | None]:
    conn = _connect()
    try:
        rows = conn.execute("SELECT agent_id, model FROM agent_settings").fetchall()
    finally:
        conn.close()
    return {agent_id: model for agent_id, model in rows if model}


def get_agent_model(agent_id: str) -> str | None:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT model FROM agent_settings WHERE agent_id = ?", (agent_id,)
        ).fetchone()
    finally:
        conn.close()
    return row[0] if row and row[0] else None


def set_agent_model(agent_id: str, model: str | None) -> dict:
    model = (model or "").strip() or None
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO agent_settings (agent_id, model, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(agent_id) DO UPDATE SET "
            "model = excluded.model, updated_at = excluded.updated_at",
            (agent_id, model, _now()),
        )
        conn.commit()
    finally:
        conn.close()
    return {"agent_id": agent_id, "model": model}


def _local_provider_row(row) -> dict:
    return {
        "agent_id": row[0],
        "display_name": row[1],
        "base_url": row[2],
        "model": row[3],
        "api_key_env": row[4],
        "created_at": row[5],
        "updated_at": row[6],
    }


def list_local_providers() -> list[dict]:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT agent_id, display_name, base_url, model, api_key_env, "
            "created_at, updated_at FROM local_providers ORDER BY created_at"
        ).fetchall()
    finally:
        conn.close()
    return [_local_provider_row(row) for row in rows]


def get_local_provider(agent_id: str) -> dict | None:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT agent_id, display_name, base_url, model, api_key_env, "
            "created_at, updated_at FROM local_providers WHERE agent_id = ?",
            (agent_id,),
        ).fetchone()
    finally:
        conn.close()
    return _local_provider_row(row) if row else None


def upsert_local_provider(
    agent_id: str,
    display_name: str,
    base_url: str,
    model: str,
    api_key_env: str | None = None,
) -> dict:
    now = _now()
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO local_providers "
            "(agent_id, display_name, base_url, model, api_key_env, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(agent_id) DO UPDATE SET "
            "display_name = excluded.display_name, base_url = excluded.base_url, "
            "model = excluded.model, api_key_env = excluded.api_key_env, "
            "updated_at = excluded.updated_at",
            (agent_id, display_name, base_url, model, api_key_env, now, now),
        )
        conn.commit()
    finally:
        conn.close()
    return get_local_provider(agent_id)


def delete_local_provider(agent_id: str) -> bool:
    conn = _connect()
    try:
        cur = conn.execute("DELETE FROM local_providers WHERE agent_id = ?", (agent_id,))
        conn.commit()
    finally:
        conn.close()
    return cur.rowcount > 0


def get_context_bundle(project_path: str, limit: int | None = None) -> dict:
    conn = _connect()
    try:
        settings_row = conn.execute(
            "SELECT recent_limit FROM context_settings WHERE project_path = ?",
            (project_path,),
        ).fetchone()
        recent_limit = limit if limit is not None else (
            settings_row[0] if settings_row else DEFAULT_RECENT_LIMIT
        )
        recent_limit = max(MIN_RECENT_LIMIT, min(MAX_RECENT_LIMIT, int(recent_limit)))

        rows = conn.execute(
            "SELECT id, agent, event_type, summary, context_summary, created_at, "
            "context_included, context_pinned, context_updated_at, context_category, "
            "source_tokens, context_tokens, raw_context FROM events "
            "WHERE project_path = ? AND event_type != ? ORDER BY id DESC",
            (project_path, TELEMETRY_EVENT_TYPE),
        ).fetchall()
    finally:
        conn.close()

    entries = []
    for (
        eid,
        agent,
        event_type,
        summary,
        context_summary,
        created_at,
        included,
        pinned,
        context_updated_at,
        context_category,
        source_tokens,
        context_tokens,
        raw_context,
    ) in rows:
        automatic_category = _infer_context_category(event_type, summary)
        entries.append(
            {
                "id": eid,
                "agent": agent,
                "event_type": event_type,
                "summary": summary,
                "context_summary": context_summary,
                "effective_summary": context_summary if context_summary is not None else summary,
                "created_at": created_at,
                "included": bool(included),
                "pinned": bool(pinned),
                "context_updated_at": context_updated_at,
                "category": context_category or automatic_category,
                "category_source": "manual" if context_category else "automatic",
                "source_tokens": source_tokens or 0,
                "context_tokens": context_tokens or (
                    (len(raw_context or summary) + 3) // 4
                ),
                "has_raw_context": bool(raw_context),
            }
        )

    included_entries = [e for e in entries if e["included"]]
    pinned_entries = [e for e in included_entries if e["pinned"]]
    pinned_ids = {e["id"] for e in pinned_entries}
    recent_activity = [
        e for e in included_entries if e["id"] not in pinned_ids
    ][:recent_limit]
    selected_ids = {e["id"] for e in recent_activity}
    recent_entries = [e for e in included_entries if e["id"] in selected_ids]
    excluded_entries = [e for e in entries if not e["included"]]

    pinned_render = list(reversed(pinned_entries))
    recent_render = list(reversed(recent_entries))

    def as_tuple(e):
        return (e["agent"], e["event_type"], e["effective_summary"], e["created_at"])

    preview = _render_context(
        [as_tuple(e) for e in pinned_render],
        [as_tuple(e) for e in recent_render],
        project_path,
    )
    truncated = len(preview) > MAX_RENDERED_CONTEXT_CHARS
    if truncated:
        marker = "\n\n[Context truncated at the configured safety limit.]"
        preview = preview[: MAX_RENDERED_CONTEXT_CHARS - len(marker)].rstrip() + marker
    included_count = len(pinned_render) + len(recent_render)

    return {
        "entries": entries,
        "pinned": pinned_render,
        "recent": recent_render,
        "excluded": excluded_entries,
        "preview": preview,
        "token_estimate": len(preview) // 4,
        "native_usage_tokens": sum(e["source_tokens"] for e in included_entries),
        "corpus_tokens": sum(e["context_tokens"] for e in included_entries),
        "active_source_tokens": sum(
            e["context_tokens"] for e in pinned_entries + recent_entries
        ),
        "active_native_usage_tokens": sum(
            e["source_tokens"] for e in pinned_entries + recent_entries
        ),
        "source_tokens": sum(e["context_tokens"] for e in included_entries),
        "archived_token_estimate": sum(
            len(e["effective_summary"]) for e in included_entries
        ) // 4,
        "counts": {
            "total": len(entries),
            "included": included_count,
            "pinned": len(pinned_render),
            "history": sum(1 for e in included_entries if e["event_type"] == "history"),
            "excluded": len(excluded_entries),
            "exclusive": 0,
        },
        "visible_to": ["claude-code", "codex"],
        "sharing_policy": {
            "mode": "pure_shared_context",
            "agent_specific_context": False,
            "provenance_controls_visibility": False,
            "working_set_identical_for_all_agents": True,
        },
        "settings": {"recent_limit": recent_limit},
        "categories": list(CONTEXT_CATEGORIES),
        "content_hash": hashlib.sha256(preview.encode("utf-8")).hexdigest(),
        "truncated": truncated,
    }


def get_context(project_path: str, limit: int | None = None) -> str:
    return get_context_bundle(project_path, limit=limit)["preview"]


def _event_embedding_vectors(
    conn: sqlite3.Connection, rows: list[tuple]
) -> dict[int, bytes]:
    if not rows:
        return {}
    ids = [event_id for event_id, _ in rows]
    placeholders = ",".join("?" for _ in ids)
    try:
        cached = {
            event_id: (content_hash, vector)
            for event_id, content_hash, vector in conn.execute(
                "SELECT event_id, content_hash, vector FROM event_embeddings "
                f"WHERE event_id IN ({placeholders}) AND model = ?",
                (*ids, embeddings.EMBEDDING_MODEL),
            ).fetchall()
        }
    except sqlite3.OperationalError:
        cached = {}

    result: dict[int, bytes] = {}
    to_store = []
    now = _now()
    for event_id, text in rows:
        fingerprint = embeddings.content_fingerprint(text)
        hit = cached.get(event_id)
        if hit and hit[0] == fingerprint:
            result[event_id] = hit[1]
            continue
        vector = embeddings.embed_text(text)
        result[event_id] = vector
        to_store.append((event_id, embeddings.EMBEDDING_MODEL, fingerprint, vector, now))

    if to_store:
        try:
            conn.executemany(
                "INSERT INTO event_embeddings "
                "(event_id, model, content_hash, vector, updated_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(event_id) DO UPDATE SET "
                "model = excluded.model, content_hash = excluded.content_hash, "
                "vector = excluded.vector, updated_at = excluded.updated_at",
                to_store,
            )
            conn.commit()
        except sqlite3.OperationalError:
            pass
    return result


def _reciprocal_rank_fusion(rank_lists: list[list[int]], k: int = RRF_K) -> dict[int, float]:
    scores: dict[int, float] = {}
    for ranks in rank_lists:
        for position, event_id in enumerate(ranks):
            scores[event_id] = scores.get(event_id, 0.0) + 1.0 / (k + position + 1)
    return scores


def search_shared_context(
    project_path: str, query: str, limit: int = 5, snippet_chars: int = 1200
) -> list[dict]:
    query = " ".join((query or "").split()).strip()
    if not query:
        return []
    limit = max(1, min(20, int(limit)))
    snippet_chars = max(200, min(4000, int(snippet_chars)))
    terms = re.findall(r"[\w./:\\-]+", query.casefold(), flags=re.UNICODE)
    stop_words = {
        "a", "an", "and", "are", "as", "at", "be", "by", "for", "from",
        "in", "is", "it", "of", "on", "or", "that", "the", "this", "to",
        "was", "were", "with",
    }
    useful_terms = list(dict.fromkeys(term for term in terms if term not in stop_words))
    if useful_terms:
        terms = useful_terms
    elif terms:
        terms = list(dict.fromkeys(terms))
    else:
        terms = [query.casefold()]
    terms = terms[:32]

    match_query = " OR ".join(f'"{term}"' for term in terms)
    candidate_limit = min(500, max(100, limit * 25))

    conn = _connect_for_search()
    if conn is None:
        return []
    try:
        lexical_rows = conn.execute(
            "SELECT e.id, e.agent, e.event_type, e.summary, e.context_summary, "
            "e.raw_context, e.created_at "
            "FROM events_fts JOIN events e ON e.id = events_fts.rowid "
            "WHERE events_fts MATCH ? AND e.project_path = ? AND e.event_type != ? "
            "AND e.context_included = 1 "
            "ORDER BY bm25(events_fts) LIMIT ?",
            (match_query, project_path, TELEMETRY_EVENT_TYPE, candidate_limit),
        ).fetchall()

        semantic_scan_rows = conn.execute(
            "SELECT e.id, e.agent, e.event_type, e.summary, e.context_summary, "
            "e.raw_context, e.created_at "
            "FROM events e "
            "WHERE e.project_path = ? AND e.event_type != ? "
            "AND e.context_included = 1 "
            "ORDER BY e.id DESC LIMIT ?",
            (project_path, TELEMETRY_EVENT_TYPE, SEMANTIC_CANDIDATE_CAP),
        ).fetchall()

        combined_rows: dict[int, tuple] = {row[0]: row for row in lexical_rows}
        for row in semantic_scan_rows:
            combined_rows.setdefault(row[0], row)

        embed_inputs = [
            (
                row[0],
                " ".join(part for part in (row[4], row[3], row[5]) if part)[
                    :SEMANTIC_TEXT_CHAR_CAP
                ],
            )
            for row in semantic_scan_rows
        ]
        vectors = _event_embedding_vectors(conn, embed_inputs)
    finally:
        conn.close()

    lexical_rank_ids = [row[0] for row in lexical_rows]
    semantic_rank_ids: list[int] = []
    if vectors:
        query_vector = embeddings.embed_text(query)
        similarities = sorted(
            (
                (embeddings.cosine_similarity(query_vector, vector), event_id)
                for event_id, vector in vectors.items()
            ),
            key=lambda item: item[0],
            reverse=True,
        )
        semantic_rank_ids = [
            event_id
            for similarity, event_id in similarities
            if similarity >= SEMANTIC_MIN_SIMILARITY
        ][:candidate_limit]

    fused_scores = _reciprocal_rank_fusion([lexical_rank_ids, semantic_rank_ids])

    ranked = []
    needle = query.casefold()
    for event_id, fused_score in fused_scores.items():
        _id, agent, event_type, summary, context_summary, raw_context, created_at = (
            combined_rows[event_id]
        )
        text = raw_context or context_summary or summary or ""
        searchable_text = " ".join(
            part for part in (context_summary, summary, raw_context) if part
        )
        folded = searchable_text.casefold()
        exact_phrase = needle in folded
        matched_terms = [term for term in terms if term in folded]
        occurrence_count = sum(folded.count(term) for term in matched_terms)

        snippet_folded = text.casefold()
        positions = [snippet_folded.find(term) for term in matched_terms]
        positions = [position for position in positions if position >= 0]
        at = snippet_folded.find(needle)
        if at < 0 and positions:
            at = min(positions)
        start = max(0, at - (snippet_chars // 3)) if at >= 0 else 0
        snippet = text[start : start + snippet_chars]
        if start:
            snippet = "…" + snippet
        if start + snippet_chars < len(text):
            snippet += "…"
        ranked.append(
            (
                (fused_score, int(exact_phrase), len(matched_terms), occurrence_count, event_id),
                {
                    "id": event_id,
                    "agent": agent,
                    "event_type": event_type,
                    "summary": summary,
                    "snippet": snippet,
                    "created_at": created_at,
                },
            )
        )
    ranked.sort(key=lambda item: item[0], reverse=True)
    return [result for _score, result in ranked[:limit]]


def update_context_event(
    project_path: str,
    event_id: int,
    *,
    included: bool | None = None,
    pinned: bool | None = None,
    context_summary: str | None = None,
    reset_summary: bool = False,
    category: str | None = None,
    reset_category: bool = False,
) -> None:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT project_path, event_type FROM events WHERE id = ?", (event_id,)
        ).fetchone()
        if not row or row[0] != project_path or row[1] == TELEMETRY_EVENT_TYPE:
            raise LookupError(f"No event {event_id} for project {project_path!r}.")

        sets: list[str] = []
        params: list = []
        if included is not None:
            sets.append("context_included = ?")
            params.append(1 if included else 0)
        if pinned is not None:
            sets.append("context_pinned = ?")
            params.append(1 if pinned else 0)
        if reset_summary:
            sets.append("context_summary = NULL")
            sets.append("context_updated_at = ?")
            params.append(_now())
        elif context_summary is not None:
            text = _redact_for_project(conn, project_path, context_summary.strip())
            if len(text) > MAX_CONTEXT_SUMMARY_CHARS:
                raise ValueError(
                    f"context_summary must be <= {MAX_CONTEXT_SUMMARY_CHARS} characters."
                )
            sets.append("context_summary = ?")
            params.append(text or None)
            sets.append("context_updated_at = ?")
            params.append(_now())
        if reset_category:
            sets.append("context_category = NULL")
            sets.append("context_updated_at = ?")
            params.append(_now())
        elif category is not None:
            sets.append("context_category = ?")
            params.append(_validate_context_category(category))
            sets.append("context_updated_at = ?")
            params.append(_now())
        if not sets:
            return
        params.append(event_id)
        conn.execute(f"UPDATE events SET {', '.join(sets)} WHERE id = ?", params)
        conn.commit()
    finally:
        conn.close()


def create_context_note(project_path: str, content: str, category: str = "note") -> int:
    content = content.strip()
    if not content:
        raise ValueError("Note content must not be empty.")
    if len(content) > MAX_NOTE_CHARS:
        raise ValueError(f"Note content must be <= {MAX_NOTE_CHARS} characters.")
    category = _validate_context_category(category)
    conn = _connect()
    try:
        content = _redact_for_project(conn, project_path, content)
        cur = conn.execute(
            "INSERT INTO events (project_path, agent, session_id, event_type, "
            "summary, created_at, context_category) VALUES (?, 'user', '', ?, ?, ?, ?)",
            (project_path, CONTEXT_NOTE_EVENT_TYPE, content, _now(), category),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def delete_context_note(project_path: str, event_id: int) -> None:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT project_path, event_type FROM events WHERE id = ?", (event_id,)
        ).fetchone()
        if not row or row[0] != project_path:
            raise LookupError(f"No event {event_id} for project {project_path!r}.")
        if row[1] != CONTEXT_NOTE_EVENT_TYPE:
            raise PermissionError(
                "Only manual context notes can be deleted; exclude raw events instead."
            )
        conn.execute("DELETE FROM events WHERE id = ?", (event_id,))
        conn.commit()
    finally:
        conn.close()


def record_file_touch(
    project_path: str, agent: str, session_id: str, file_path: str, tool_name: str
) -> None:
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO file_touches (project_path, agent, session_id, file_path, "
            "tool_name, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (project_path, agent, session_id, file_path, tool_name, _now()),
        )
        conn.commit()
    finally:
        conn.close()


def register_project(project_path: str) -> None:
    conn = _connect()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO projects (project_path, added_at) VALUES (?, ?)",
            (project_path, _now()),
        )
        conn.commit()
    finally:
        conn.close()


def is_session_imported(session_id: str) -> bool:
    conn = _connect()
    try:
        return (
            conn.execute(
                "SELECT 1 FROM imported_sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
            is not None
        )
    finally:
        conn.close()


def mark_session_imported(session_id: str, project_path: str, agent: str) -> None:
    conn = _connect()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO imported_sessions (session_id, project_path, agent, "
            "imported_at) VALUES (?, ?, ?, ?)",
            (session_id, project_path, agent, _now()),
        )
        conn.commit()
    finally:
        conn.close()


def list_projects() -> list[dict]:
    conn = _connect()
    try:
        rows = conn.execute(
            """
            SELECT project_path,
                   COUNT(*) AS event_count,
                   MAX(created_at) AS last_activity,
                   GROUP_CONCAT(DISTINCT agent) AS agents
            FROM events
            WHERE event_type != ?
            GROUP BY project_path
            """,
            (TELEMETRY_EVENT_TYPE,),
        ).fetchall()
        registered = conn.execute("SELECT project_path, added_at FROM projects").fetchall()
    finally:
        conn.close()

    projects = {
        r[0]: {
            "project_path": r[0],
            "event_count": r[1],
            "last_activity": r[2],
            "agents": sorted(set(r[3].split(","))) if r[3] else [],
        }
        for r in rows
    }
    for path, added_at in registered:
        if path not in projects:
            projects[path] = {
                "project_path": path,
                "event_count": 0,
                "last_activity": added_at,
                "agents": [],
            }
    return sorted(
        projects.values(), key=lambda p: p["last_activity"] or "", reverse=True
    )


def list_events(project_path: str | None = None, limit: int = 200, since_id: int = 0) -> list[dict]:
    conn = _connect()
    try:
        if project_path:
            rows = conn.execute(
                "SELECT id, project_path, agent, event_type, summary, created_at "
                "FROM events WHERE project_path = ? AND id > ? AND event_type != ? "
                "ORDER BY id DESC LIMIT ?",
                (project_path, since_id, TELEMETRY_EVENT_TYPE, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, project_path, agent, event_type, summary, created_at "
                "FROM events WHERE id > ? AND event_type != ? ORDER BY id DESC LIMIT ?",
                (since_id, TELEMETRY_EVENT_TYPE, limit),
            ).fetchall()
    finally:
        conn.close()
    return [
        {
            "id": r[0],
            "project_path": r[1],
            "agent": r[2],
            "event_type": r[3],
            "summary": r[4],
            "created_at": r[5],
        }
        for r in rows
    ]


def native_usage_tokens_as_of(project_path: str, cutoff_created_at: str) -> int:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT COALESCE(SUM(source_tokens), 0) FROM events "
            "WHERE project_path = ? AND event_type != ? AND context_included = 1 "
            "AND created_at <= ?",
            (project_path, TELEMETRY_EVENT_TYPE, cutoff_created_at),
        ).fetchone()
    finally:
        conn.close()
    return row[0] or 0


def list_context_injections(project_path: str, limit: int = 10_000) -> list[dict]:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT agent, session_id, summary, created_at FROM events "
            "WHERE project_path = ? AND event_type = ? "
            "ORDER BY id DESC LIMIT ?",
            (project_path, TELEMETRY_EVENT_TYPE, limit),
        ).fetchall()
    finally:
        conn.close()
    return [
        {"agent": r[0], "session_id": r[1], "summary": r[2], "created_at": r[3]}
        for r in rows
    ]


def get_conflicts(project_path: str | None = None, window_minutes: int = 30) -> list[dict]:
    conn = _connect()
    try:
        if project_path:
            rows = conn.execute(
                "SELECT project_path, file_path, agent, tool_name, created_at "
                "FROM file_touches WHERE project_path = ? ORDER BY file_path, created_at",
                (project_path,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT project_path, file_path, agent, tool_name, created_at "
                "FROM file_touches ORDER BY project_path, file_path, created_at"
            ).fetchall()
    finally:
        conn.close()

    window = timedelta(minutes=window_minutes)
    conflicts = []
    group_key = None
    group: list[tuple] = []

    def flush(key, touches):
        if len(touches) < 2:
            return
        for i in range(len(touches)):
            for j in range(i + 1, len(touches)):
                agent_a, at_a = touches[i]
                agent_b, at_b = touches[j]
                if agent_a == agent_b:
                    continue
                if abs(datetime.fromisoformat(at_b) - datetime.fromisoformat(at_a)) <= window:
                    conflicts.append(
                        {
                            "project_path": key[0],
                            "file_path": key[1],
                            "agent_a": agent_a,
                            "agent_b": agent_b,
                            "touched_at_a": at_a,
                            "touched_at_b": at_b,
                        }
                    )
                    return

    for proj, file_path, agent, tool_name, created_at in rows:
        key = (proj, file_path)
        if key != group_key:
            if group_key is not None:
                flush(group_key, group)
            group_key, group = key, []
        group.append((agent, created_at))
    if group_key is not None:
        flush(group_key, group)

    conflicts.sort(key=lambda c: max(c["touched_at_a"], c["touched_at_b"]), reverse=True)
    return conflicts


def list_file_touches(project_path: str, file_path: str, limit: int = 20) -> list[dict]:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT agent, tool_name, created_at FROM file_touches "
            "WHERE project_path = ? AND file_path = ? "
            "ORDER BY created_at DESC, id DESC LIMIT ?",
            (project_path, file_path, max(1, min(200, int(limit)))),
        ).fetchall()
    finally:
        conn.close()
    return [
        {"agent": r[0], "tool_name": r[1], "created_at": r[2]} for r in rows
    ]


def has_any_user() -> bool:
    conn = _connect()
    try:
        return conn.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None
    finally:
        conn.close()


def create_user(username: str, password_hash: str, role: str = "admin") -> None:
    role = _validate_user_role(role)
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO users (username, password_hash, created_at, role, active) "
            "VALUES (?, ?, ?, ?, 1)",
            (username, password_hash, _now(), role),
        )
        conn.commit()
    finally:
        conn.close()


def get_user_by_username(username: str) -> dict | None:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT id, username, password_hash, created_at, role, active "
            "FROM users WHERE username = ?",
            (username,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    return {"id": row[0], "username": row[1], "password_hash": row[2],
            "created_at": row[3], "role": row[4], "active": bool(row[5])}


def create_dispatch_job(
    job_id: str,
    project_path: str,
    agent: str,
    prompt: str,
    allow_edits: bool,
    context_snapshot: str = "",
    work_status: str = "in_progress",
    replaces_job_id: str | None = None,
    parent_job_id: str | None = None,
    coordination_id: str | None = None,
    task_label: str | None = None,
    model: str | None = None,
) -> None:
    if work_status not in DISPATCH_WORK_STATUSES:
        raise ValueError(f"Invalid work status: {work_status}.")
    conn = _connect()
    try:
        prompt = _redact_for_project(conn, project_path, prompt)
        context_snapshot = _redact_for_project(conn, project_path, context_snapshot)
        now = _now()
        conn.execute(
            "INSERT INTO dispatch_jobs (id, project_path, agent, prompt, allow_edits, "
            "status, result_text, created_at, finished_at, context_snapshot, progress, "
            "progress_label, activity_count, updated_at, work_status, replaces_job_id, "
            "parent_job_id, coordination_id, task_label, model) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (job_id, project_path, agent, prompt, int(allow_edits), "running", None, now,
             None, context_snapshot, 2, "Queued", 0, now, work_status, replaces_job_id,
             parent_job_id, coordination_id, task_label, model),
        )
        conn.commit()
    finally:
        conn.close()


def update_dispatch_job(
    job_id: str, status: str, result_text: str = "", tokens: int = 0
) -> None:
    conn = _connect()
    try:
        project_row = conn.execute(
            "SELECT project_path FROM dispatch_jobs WHERE id = ?", (job_id,)
        ).fetchone()
        if project_row:
            result_text = _redact_for_project(conn, project_row[0], result_text)
        finished = status in ("done", "error", "canceled")
        progress_label = {
            "waiting": "Needs input",
            "done": "Completed",
            "error": "Failed",
            "canceled": "Canceled",
        }.get(status)
        conn.execute(
            "UPDATE dispatch_jobs SET status = ?, result_text = ?, finished_at = ?, "
            "tokens = COALESCE(tokens, 0) + ?, progress = CASE WHEN ? THEN 100 ELSE progress END, "
            "progress_label = COALESCE(?, progress_label), updated_at = ?, "
            "work_status = CASE WHEN ? = 'done' THEN 'completed' ELSE work_status END WHERE id = ?",
            (
                status,
                result_text,
                _now() if finished else None,
                tokens,
                int(finished),
                progress_label,
                _now(),
                status,
                job_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def update_dispatch_progress(job_id: str, progress: int, label: str, activity_count: int) -> None:
    conn = _connect()
    try:
        conn.execute(
            "UPDATE dispatch_jobs SET progress = ?, progress_label = ?, activity_count = ?, "
            "updated_at = ? WHERE id = ? AND status = 'running'",
            (max(0, min(99, int(progress))), label[:120], max(0, activity_count), _now(), job_id),
        )
        conn.commit()
    finally:
        conn.close()


def set_dispatch_canceling(job_id: str) -> bool:
    conn = _connect()
    try:
        changed = conn.execute(
            "UPDATE dispatch_jobs SET status = 'canceling', progress_label = 'Canceling', "
            "updated_at = ? WHERE id = ? AND status IN ('running', 'waiting')",
            (_now(), job_id),
        ).rowcount
        conn.commit()
        return bool(changed)
    finally:
        conn.close()


def update_dispatch_work_status(job_id: str, work_status: str) -> None:
    if work_status not in DISPATCH_WORK_STATUSES:
        raise ValueError(f"Invalid work status: {work_status}.")
    conn = _connect()
    try:
        changed = conn.execute(
            "UPDATE dispatch_jobs SET work_status = ?, updated_at = ? WHERE id = ?",
            (work_status, _now(), job_id),
        ).rowcount
        if not changed:
            raise LookupError("No such dispatch job.")
        conn.commit()
    finally:
        conn.close()


def append_dispatch_log(job_id: str, line: str) -> None:
    line = line.strip()
    if not line:
        return
    conn = _connect()
    try:
        project_row = conn.execute(
            "SELECT project_path FROM dispatch_jobs WHERE id = ?", (job_id,)
        ).fetchone()
        if project_row:
            line = _redact_for_project(conn, project_row[0], line)
        conn.execute(
            "INSERT INTO dispatch_logs (job_id, line, created_at) VALUES (?, ?, ?)",
            (job_id, line[:2000], _now()),
        )
        conn.commit()
    finally:
        conn.close()


def update_dispatch_session(job_id: str, session_id: str) -> None:
    if not session_id:
        return
    conn = _connect()
    try:
        conn.execute(
            "UPDATE dispatch_jobs SET session_id = ? WHERE id = ?",
            (session_id, job_id),
        )
        conn.commit()
    finally:
        conn.close()


def list_dispatch_logs(job_id: str, since_id: int = 0) -> list[dict]:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT id, line, created_at FROM dispatch_logs WHERE job_id = ? AND id > ? "
            "ORDER BY id ASC",
            (job_id, since_id),
        ).fetchall()
    finally:
        conn.close()
    return [{"id": r[0], "line": r[1], "created_at": r[2]} for r in rows]


def _dispatch_row_to_dict(row) -> dict:
    return {
        "id": row[0],
        "project_path": row[1],
        "agent": row[2],
        "prompt": row[3],
        "allow_edits": bool(row[4]),
        "status": row[5],
        "result_text": row[6],
        "created_at": row[7],
        "finished_at": row[8],
        "tokens": row[9] if len(row) > 9 and row[9] is not None else 0,
        "context_snapshot": row[10] if len(row) > 10 else None,
        "session_id": row[11] if len(row) > 11 else None,
        "progress": row[12] if len(row) > 12 and row[12] is not None else 0,
        "progress_label": row[13] if len(row) > 13 else "Queued",
        "activity_count": row[14] if len(row) > 14 and row[14] is not None else 0,
        "updated_at": row[15] if len(row) > 15 else None,
        "work_status": row[16] if len(row) > 16 and row[16] else "in_progress",
        "replaces_job_id": row[17] if len(row) > 17 else None,
        "parent_job_id": row[18] if len(row) > 18 else None,
        "coordination_id": row[19] if len(row) > 19 else None,
        "task_label": row[20] if len(row) > 20 else None,
        "model": row[21] if len(row) > 21 else None,
    }


_DISPATCH_COLS = (
    "id, project_path, agent, prompt, allow_edits, status, result_text, "
    "created_at, finished_at, tokens, context_snapshot, session_id, progress, "
    "progress_label, activity_count, updated_at, work_status, replaces_job_id, "
    "parent_job_id, coordination_id, task_label, model"
)


def get_dispatch_job(job_id: str) -> dict | None:
    conn = _connect()
    try:
        row = conn.execute(
            f"SELECT {_DISPATCH_COLS} FROM dispatch_jobs WHERE id = ?", (job_id,)
        ).fetchone()
    finally:
        conn.close()
    return _dispatch_row_to_dict(row) if row else None


def sum_dispatch_tokens(project_path: str, agent: str) -> int:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT COALESCE(SUM(tokens), 0) FROM dispatch_jobs WHERE project_path = ? AND agent = ?",
            (project_path, agent),
        ).fetchone()
    finally:
        conn.close()
    return int(row[0] or 0)


def list_dispatch_jobs(project_path: str, limit: int = 50) -> list[dict]:
    conn = _connect()
    try:
        rows = conn.execute(
            f"SELECT {_DISPATCH_COLS} FROM dispatch_jobs WHERE project_path = ? "
            "ORDER BY created_at DESC LIMIT ?",
            (project_path, limit),
        ).fetchall()
    finally:
        conn.close()
    return [_dispatch_row_to_dict(row) for row in rows]


def delete_dispatch_job(job_id: str) -> bool:
    conn = _connect()
    try:
        conn.execute("DELETE FROM dispatch_logs WHERE job_id = ?", (job_id,))
        conn.execute("DELETE FROM dispatch_interactions WHERE job_id = ?", (job_id,))
        changed = conn.execute("DELETE FROM dispatch_jobs WHERE id = ?", (job_id,)).rowcount
        conn.commit()
        return bool(changed)
    finally:
        conn.close()


def list_active_dispatch_jobs(limit: int = 50) -> list[dict]:
    conn = _connect()
    try:
        rows = conn.execute(
            f"SELECT {_DISPATCH_COLS} FROM dispatch_jobs "
            "WHERE status IN ('running', 'waiting', 'canceling') "
            "ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        conn.close()
    return [_dispatch_row_to_dict(row) for row in rows]


def create_dispatch_interaction(
    interaction_id: str,
    job_id: str,
    project_path: str,
    agent: str,
    kind: str,
    prompt: str,
    options: list[str] | None = None,
) -> dict:
    kind = kind if kind in ("approval", "question", "confirmation") else "question"
    clean_prompt = (prompt or "").strip()[:4000]
    if not clean_prompt:
        raise ValueError("Interaction prompt must not be empty.")
    clean_options = [str(option).strip()[:100] for option in (options or []) if str(option).strip()]
    clean_options = list(dict.fromkeys(clean_options))[:8]
    conn = _connect()
    try:
        clean_prompt = _redact_for_project(conn, project_path, clean_prompt)
        clean_options = [_redact_for_project(conn, project_path, option) for option in clean_options]
        conn.execute(
            "INSERT INTO dispatch_interactions "
            "(id, job_id, project_path, agent, kind, prompt, options_json, status, response, created_at, resolved_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', NULL, ?, NULL)",
            (
                interaction_id,
                job_id,
                project_path,
                agent,
                kind,
                clean_prompt,
                json.dumps(clean_options),
                _now(),
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return get_dispatch_interaction(interaction_id)


def _interaction_row_to_dict(row) -> dict:
    return {
        "id": row[0],
        "job_id": row[1],
        "project_path": row[2],
        "agent": row[3],
        "kind": row[4],
        "prompt": row[5],
        "options": json.loads(row[6] or "[]"),
        "status": row[7],
        "response": row[8],
        "created_at": row[9],
        "resolved_at": row[10],
    }


_INTERACTION_COLS = (
    "id, job_id, project_path, agent, kind, prompt, options_json, "
    "status, response, created_at, resolved_at"
)


def get_dispatch_interaction(interaction_id: str) -> dict | None:
    conn = _connect()
    try:
        row = conn.execute(
            f"SELECT {_INTERACTION_COLS} FROM dispatch_interactions WHERE id = ?",
            (interaction_id,),
        ).fetchone()
    finally:
        conn.close()
    return _interaction_row_to_dict(row) if row else None


def list_dispatch_interactions(
    project_path: str | None = None, pending_only: bool = False, limit: int = 100
) -> list[dict]:
    clauses = []
    params: list[object] = []
    if project_path:
        clauses.append("project_path = ?")
        params.append(project_path)
    if pending_only:
        clauses.append("status = 'pending'")
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    params.append(limit)
    conn = _connect()
    try:
        rows = conn.execute(
            f"SELECT {_INTERACTION_COLS} FROM dispatch_interactions{where} "
            "ORDER BY created_at DESC LIMIT ?",
            tuple(params),
        ).fetchall()
    finally:
        conn.close()
    return [_interaction_row_to_dict(row) for row in rows]


def resolve_dispatch_interaction(interaction_id: str, response: str) -> dict:
    clean_response = (response or "").strip()[:4000]
    if not clean_response:
        raise ValueError("Response must not be empty.")
    conn = _connect()
    try:
        project_row = conn.execute(
            "SELECT project_path FROM dispatch_interactions WHERE id = ?", (interaction_id,)
        ).fetchone()
        if project_row:
            clean_response = _redact_for_project(conn, project_row[0], clean_response)
        cursor = conn.execute(
            "UPDATE dispatch_interactions SET status = 'answered', response = ?, resolved_at = ? "
            "WHERE id = ? AND status = 'pending'",
            (clean_response, _now(), interaction_id),
        )
        if cursor.rowcount != 1:
            existing = conn.execute(
                "SELECT status FROM dispatch_interactions WHERE id = ?", (interaction_id,)
            ).fetchone()
            if not existing:
                raise LookupError("No such interaction.")
            raise ValueError("Interaction has already been answered.")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return get_dispatch_interaction(interaction_id)


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 3 or sys.argv[1] != "show":
        print("Usage: python store.py show <project_path>")
        raise SystemExit(1)
    print(get_context(find_project_root(sys.argv[2])) or "(no events recorded)")

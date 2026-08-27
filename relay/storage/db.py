"""SQLite connection management and schema migrations.

SPEC reference: §14 (Persistent Memory), §15 (Event Log); Appendix B.1.

Schema contract: ``relay.storage.models`` is the single source of truth.
Append-only history is enforced at the database level with triggers.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 2

_APPEND_ONLY_TABLES = ("event_log", "evidence_records")


def _append_only_triggers() -> list[str]:
    statements: list[str] = []
    for table in _APPEND_ONLY_TABLES:
        for action in ("UPDATE", "DELETE"):
            statements.append(
                f"CREATE TRIGGER IF NOT EXISTS {table}_no_{action.lower()} "
                f"BEFORE {action} ON {table} BEGIN "
                f"SELECT RAISE(ABORT, '{table} is append-only'); END;"
            )
    return statements


_V1_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE workspaces (
        identity_key TEXT UNIQUE,
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        path TEXT,
        kind TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE rooms (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        workspace_id TEXT REFERENCES workspaces(id),
        members_json TEXT NOT NULL DEFAULT '[]',
        active_task_id TEXT,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE tasks (
        id TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        state TEXT NOT NULL,
        room_id TEXT REFERENCES rooms(id),
        workspace_id TEXT REFERENCES workspaces(id),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE runs (
        id TEXT PRIMARY KEY,
        task_id TEXT REFERENCES tasks(id),
        agent TEXT NOT NULL,
        role TEXT NOT NULL,
        model TEXT,
        status TEXT NOT NULL,
        input_size INTEGER,
        output_size INTEGER,
        cost_usd REAL,
        started_at TEXT NOT NULL,
        ended_at TEXT
    )
    """,
    "CREATE INDEX idx_runs_status ON runs(status)",
    """
    CREATE TABLE messages (
        id TEXT PRIMARY KEY,
        sender TEXT NOT NULL,
        recipient TEXT,
        room_id TEXT REFERENCES rooms(id),
        task_id TEXT REFERENCES tasks(id),
        type TEXT NOT NULL,
        content TEXT NOT NULL,
        references_json TEXT NOT NULL DEFAULT '[]',
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE artifacts (
        id TEXT PRIMARY KEY,
        task_id TEXT REFERENCES tasks(id),
        run_id TEXT REFERENCES runs(id),
        kind TEXT NOT NULL,
        content_ref TEXT,
        content TEXT,
        created_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX idx_artifacts_run ON artifacts(run_id)",
    """
    CREATE TABLE decisions (
        id TEXT PRIMARY KEY,
        statement TEXT NOT NULL,
        rationale TEXT,
        proposed_by TEXT,
        supported_by_json TEXT NOT NULL DEFAULT '[]',
        challenged_by_json TEXT NOT NULL DEFAULT '[]',
        verified_by TEXT,
        accepted_by TEXT,
        alternatives_considered_json TEXT NOT NULL DEFAULT '[]',
        primary_objection TEXT,
        status TEXT NOT NULL,
        room_id TEXT REFERENCES rooms(id),
        task_id TEXT REFERENCES tasks(id),
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE approvals (
        id TEXT PRIMARY KEY,
        action TEXT NOT NULL,
        requested_by TEXT,
        reason TEXT,
        status TEXT NOT NULL,
        decided_by TEXT,
        decided_at TEXT,
        task_id TEXT REFERENCES tasks(id),
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE tool_runs (
        id TEXT PRIMARY KEY,
        parent_run_id TEXT REFERENCES runs(id),
        tool TEXT NOT NULL,
        arguments_json TEXT NOT NULL DEFAULT '{}',
        status TEXT NOT NULL,
        result_ref TEXT,
        error TEXT,
        started_at TEXT NOT NULL,
        ended_at TEXT
    )
    """,
    """
    CREATE TABLE evidence_records (
        id TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        task_id TEXT NOT NULL,
        run_id TEXT,
        tool_run_id TEXT,
        artifact_id TEXT,
        produced_by TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX idx_evidence_task_kind ON evidence_records(task_id, kind)",
    """
    CREATE TABLE event_log (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
        room_id TEXT,
        task_id TEXT,
        sender TEXT,
        recipient TEXT,
        type TEXT NOT NULL,
        content TEXT NOT NULL,
        references_json TEXT NOT NULL DEFAULT '[]',
        created_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX idx_event_task ON event_log(task_id)",
    *_append_only_triggers(),
)

_MIGRATIONS: dict[int, tuple[str, ...]] = {1: _V1_STATEMENTS}

#: App. C.6 seam — additive, nullable, provider-neutral harness-fact columns.
#: Historical rows are untouched; ``model`` remains the requested model.
_V2_STATEMENTS: tuple[str, ...] = (
    "ALTER TABLE runs ADD COLUMN resolved_model TEXT",
    "ALTER TABLE runs ADD COLUMN adapter_version TEXT",
    "ALTER TABLE runs ADD COLUMN backend TEXT",
    "ALTER TABLE runs ADD COLUMN external_session_ref TEXT",
)

_MIGRATIONS[2] = _V2_STATEMENTS


def connect(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def migrate(conn: sqlite3.Connection) -> int:
    """Apply each schema version atomically; returns resulting version."""
    current = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if current > SCHEMA_VERSION:
        msg = f"database schema v{current} is newer than this Relay build (v{SCHEMA_VERSION})"
        raise sqlite3.DatabaseError(msg)

    for version in range(current + 1, SCHEMA_VERSION + 1):
        try:
            conn.execute("BEGIN IMMEDIATE")
            for statement in _MIGRATIONS[version]:
                conn.execute(statement)
            conn.execute(f"PRAGMA user_version = {version}")
            conn.execute("COMMIT")
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        current = version
    return current

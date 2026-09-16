"""SQLite connection management and schema migrations.

SPEC reference: §14 (Persistent Memory), §15 (Event Log); Appendix B.1.

Schema contract: ``relay.storage.models`` is the single source of truth.
Append-only history is enforced at the database level with triggers.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from relay.storage.models import room_name_key

SCHEMA_VERSION = 9

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

#: P4.1 (App. D.5/D.11) — additive message addressing/blocking columns, and
#: ``messages`` joining the append-only trigger family at the DB layer. v1 is
#: shipped history and is not retro-edited: the message triggers are created
#: here, so fresh databases traverse v1→v2→v3 and converge to the same schema.
_V3_STATEMENTS: tuple[str, ...] = (
    "ALTER TABLE messages ADD COLUMN blocking INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE messages ADD COLUMN recipient_role TEXT",
    (
        "CREATE TRIGGER IF NOT EXISTS messages_no_update "
        "BEFORE UPDATE ON messages BEGIN "
        "SELECT RAISE(ABORT, 'messages is append-only'); END;"
    ),
    (
        "CREATE TRIGGER IF NOT EXISTS messages_no_delete "
        "BEFORE DELETE ON messages BEGIN "
        "SELECT RAISE(ABORT, 'messages is append-only'); END;"
    ),
)

_MIGRATIONS[3] = _V3_STATEMENTS

#: P4.2 (frozen plan D2) — additive ``Message.run_id`` authorship-provenance
#: column. Historical rows are untouched (NULL = pre-P4.2 claimed authorship);
#: validation of the linkage lives at the bus boundary, not in schema.
_V4_STATEMENTS: tuple[str, ...] = (
    "ALTER TABLE messages ADD COLUMN run_id TEXT",
)

_MIGRATIONS[4] = _V4_STATEMENTS

#: P4.3 (frozen plan D1, D2) — additive ``Message.reply_to_id`` reply-linkage
#: column, indexed lookup, and a partial unique index ensuring at most one
#: materialized reply per (reply_to_id, run_id) delivery pairing.
_V5_STATEMENTS: tuple[str, ...] = (
    "ALTER TABLE messages ADD COLUMN reply_to_id TEXT",
    "CREATE INDEX IF NOT EXISTS idx_messages_reply_to ON messages(reply_to_id)",
    (
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_unique_reply_run "
        "ON messages(reply_to_id, run_id) "
        "WHERE reply_to_id IS NOT NULL AND run_id IS NOT NULL"
    ),
)

_MIGRATIONS[5] = _V5_STATEMENTS

_MIGRATIONS[6] = (
    "ALTER TABLE messages ADD COLUMN stage_key TEXT",
    "ALTER TABLE event_log ADD COLUMN stage_key TEXT",
    "CREATE INDEX idx_messages_stage_blocking ON messages(room_id, task_id, stage_key, blocking)",
    "CREATE INDEX idx_event_stage_type ON event_log(room_id, task_id, stage_key, type)",
)


_MIGRATIONS[7] = (
    """CREATE TABLE protocol_executions (
        id TEXT PRIMARY KEY,
        execution_key TEXT NOT NULL,
        room_id TEXT,
        task_id TEXT,
        topic TEXT NOT NULL,
        definition_snapshot TEXT NOT NULL,
        definition_digest TEXT NOT NULL,
        bindings_snapshot TEXT NOT NULL,
        runner_version TEXT NOT NULL,
        created_at TEXT NOT NULL,
        CHECK (room_id IS NOT NULL OR task_id IS NOT NULL)
    )""",
    (
        "CREATE UNIQUE INDEX idx_execution_both ON protocol_executions"
        "(execution_key, room_id, task_id) WHERE room_id IS NOT NULL AND task_id IS NOT NULL"
    ),
    (
        "CREATE UNIQUE INDEX idx_execution_room ON protocol_executions"
        "(execution_key, room_id) WHERE room_id IS NOT NULL AND task_id IS NULL"
    ),
    (
        "CREATE UNIQUE INDEX idx_execution_task ON protocol_executions"
        "(execution_key, task_id) WHERE room_id IS NULL AND task_id IS NOT NULL"
    ),
    (
        "CREATE TRIGGER protocol_executions_no_update BEFORE UPDATE ON protocol_executions "
        "BEGIN SELECT RAISE(ABORT, 'protocol_executions is append-only'); END;"
    ),
    (
        "CREATE TRIGGER protocol_executions_no_delete BEFORE DELETE ON protocol_executions "
        "BEGIN SELECT RAISE(ABORT, 'protocol_executions is append-only'); END;"
    ),
)


_MIGRATIONS[8] = (
    "ALTER TABLE workspaces ADD COLUMN active_room_id TEXT REFERENCES rooms(id)",
    "ALTER TABLE rooms ADD COLUMN status TEXT NOT NULL DEFAULT 'open'",
    "ALTER TABLE rooms ADD COLUMN updated_at TEXT",
    "ALTER TABLE rooms ADD COLUMN closed_at TEXT",
    "ALTER TABLE rooms ADD COLUMN name_key TEXT",
    "UPDATE rooms SET updated_at = created_at WHERE updated_at IS NULL",
    "CREATE INDEX idx_rooms_workspace_status ON rooms(workspace_id, status)",
    (
        "CREATE TRIGGER rooms_name_key_required_insert BEFORE INSERT ON rooms "
        "WHEN NEW.workspace_id IS NOT NULL AND NEW.name_key IS NULL "
        "BEGIN SELECT RAISE(ABORT, 'workspace Room requires name_key'); END;"
    ),
    (
        "CREATE TRIGGER rooms_name_key_required_update BEFORE UPDATE OF workspace_id, name_key "
        "ON rooms WHEN NEW.workspace_id IS NOT NULL AND NEW.name_key IS NULL "
        "BEGIN SELECT RAISE(ABORT, 'workspace Room requires name_key'); END;"
    ),
)


def _finalize_v8(conn: sqlite3.Connection) -> None:
    """Repair legacy names before installing workspace-local uniqueness."""
    rows = conn.execute(
        "SELECT id, workspace_id, name, created_at FROM rooms "
        "WHERE workspace_id IS NOT NULL ORDER BY workspace_id, created_at, id"
    ).fetchall()
    reserved: dict[str, set[str]] = {}
    for row in rows:
        reserved.setdefault(str(row["workspace_id"]), set()).add(room_name_key(str(row["name"])))

    seen: dict[str, set[str]] = {}
    for row in rows:
        workspace_id = str(row["workspace_id"])
        used = seen.setdefault(workspace_id, set())
        name = str(row["name"])
        folded = room_name_key(name)
        if folded not in used:
            used.add(folded)
            continue
        suffix = 2
        while True:
            candidate = f"{name} ({suffix})"
            candidate_folded = room_name_key(candidate)
            if candidate_folded not in reserved[workspace_id] and candidate_folded not in used:
                break
            suffix += 1
        conn.execute("UPDATE rooms SET name = ? WHERE id = ?", [candidate, row["id"]])
        used.add(candidate_folded)

    for row in conn.execute("SELECT id, name FROM rooms WHERE workspace_id IS NOT NULL"):
        conn.execute(
            "UPDATE rooms SET name_key = ? WHERE id = ?",
            [room_name_key(str(row["name"])), row["id"]],
        )

    conn.execute(
        "CREATE UNIQUE INDEX idx_rooms_workspace_name_key "
        "ON rooms(workspace_id, name_key) WHERE workspace_id IS NOT NULL"
    )


_MIGRATION_FINALIZERS = {8: _finalize_v8}


#: P7.3 (App. D.3): Room-scoped canonical records — plan/decision/finding graph.
#: ADD COLUMN / CREATE TABLE only; historical rows are untouched. The
#: ``findings`` triggers are installed HERE, not in ``_V1_STATEMENTS``: a fresh
#: database traverses v1→v9 in order, and the v1 append-only helper only knows
#: the tables that exist at v1. The decision indexes/triggers make the
#: supersession contract fail-closed at the storage layer:
#: one promoted decision per reply, at most one successor per decision, and no
#: self-supersession.
_MIGRATIONS[9] = (
    "ALTER TABLE artifacts ADD COLUMN room_id TEXT REFERENCES rooms(id)",
    "CREATE INDEX IF NOT EXISTS idx_artifacts_room ON artifacts(room_id, kind)",
    "ALTER TABLE decisions ADD COLUMN references_json TEXT NOT NULL DEFAULT '[]'",
    "ALTER TABLE decisions ADD COLUMN source_reply_id TEXT",
    "ALTER TABLE decisions ADD COLUMN supersedes_decision_id TEXT",
    (
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_decisions_source_reply "
        "ON decisions(source_reply_id) WHERE source_reply_id IS NOT NULL"
    ),
    (
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_decisions_supersedes "
        "ON decisions(supersedes_decision_id) WHERE supersedes_decision_id IS NOT NULL"
    ),
    (
        "CREATE TRIGGER IF NOT EXISTS decisions_no_self_supersede_insert "
        "BEFORE INSERT ON decisions WHEN NEW.supersedes_decision_id IS NOT NULL "
        "AND NEW.supersedes_decision_id = NEW.id "
        "BEGIN SELECT RAISE(ABORT, 'a decision cannot supersede itself'); END;"
    ),
    (
        "CREATE TRIGGER IF NOT EXISTS decisions_no_self_supersede_update "
        "BEFORE UPDATE OF supersedes_decision_id ON decisions "
        "WHEN NEW.supersedes_decision_id IS NOT NULL AND NEW.supersedes_decision_id = NEW.id "
        "BEGIN SELECT RAISE(ABORT, 'a decision cannot supersede itself'); END;"
    ),
    """
    CREATE TABLE findings (
        id TEXT PRIMARY KEY,
        room_id TEXT NOT NULL REFERENCES rooms(id),
        task_id TEXT NOT NULL REFERENCES tasks(id),
        review_artifact_id TEXT NOT NULL REFERENCES artifacts(id),
        review_run_id TEXT NOT NULL REFERENCES runs(id),
        source_finding_id TEXT NOT NULL,
        severity TEXT NOT NULL,
        title TEXT NOT NULL,
        description TEXT NOT NULL,
        requested_change TEXT NOT NULL,
        validation_expectation TEXT NOT NULL,
        location_json TEXT,
        created_at TEXT NOT NULL
    )
    """,
    (
        "CREATE UNIQUE INDEX idx_findings_review_source "
        "ON findings(review_artifact_id, source_finding_id)"
    ),
    "CREATE INDEX idx_findings_room ON findings(room_id, created_at, id)",
    (
        "CREATE TRIGGER IF NOT EXISTS findings_no_update "
        "BEFORE UPDATE ON findings BEGIN "
        "SELECT RAISE(ABORT, 'findings is append-only'); END;"
    ),
    (
        "CREATE TRIGGER IF NOT EXISTS findings_no_delete "
        "BEFORE DELETE ON findings BEGIN "
        "SELECT RAISE(ABORT, 'findings is append-only'); END;"
    ),
)


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
            finalizer = _MIGRATION_FINALIZERS.get(version)
            if finalizer is not None:
                finalizer(conn)
            conn.execute(f"PRAGMA user_version = {version}")
            conn.execute("COMMIT")
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        current = version
    return current

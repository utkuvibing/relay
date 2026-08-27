"""Typed persistence over SQLite for Relay's domain records.

SPEC reference: §14; Appendix B.1-B.3.

The row↔model mapping is generated from ``relay.storage.models`` —
the vocabulary there is the schema contract. Container-typed fields are
stored as JSON columns (``<field>_json``); enums as their values;
datetimes as ISO-8601 strings.

Immutability policy: ``event_log`` and ``evidence_records`` reject
UPDATE/DELETE twice — here in Python (defense-in-depth, mirroring
``relay.core.state_machine``'s client-side filter) and at the database
level via triggers installed by :mod:`relay.storage.db`. Rows elsewhere
(state-bearing records such as Run, Task, Approval) mutate legitimately.
"""

from __future__ import annotations

import enum
import json
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from functools import cache
from types import TracebackType, UnionType
from typing import Any, Union, get_args, get_origin

import pydantic
import pydantic_core

from relay.core.evidence import EvidenceKind, validate_provenance
from relay.storage.models import (
    Approval,
    Artifact,
    ArtifactKind,
    Decision,
    EventLogEntry,
    EvidenceRecord,
    Message,
    Room,
    Run,
    Task,
    ToolRun,
    Workspace,
)

__all__ = [
    "MODEL_TABLES",
    "ImmutableHistoryError",
    "SqliteEvidenceStore",
    "SqliteRelayStore",
]

#: Aggregate classes persisted, each mapped to its SPEC §14 table.
#: Workspaces carry one DB-only column outside this mapping
#: (``identity_key`` — the canonical filesystem identity used by
#: idempotent ``relay init``; see :meth:`SqliteRelayStore.register_identity`).
MODEL_TABLES: dict[type[pydantic.BaseModel], str] = {
    Workspace: "workspaces",
    Room: "rooms",
    Task: "tasks",
    Run: "runs",
    Message: "messages",
    Artifact: "artifacts",
    Decision: "decisions",
    Approval: "approvals",
    ToolRun: "tool_runs",
    EvidenceRecord: "evidence_records",
    EventLogEntry: "event_log",
}

_APPEND_ONLY_TABLES = frozenset({"event_log", "evidence_records"})

_PRIMITIVES = (str, int, float, bool)


def _pk_column(model_cls: type[pydantic.BaseModel]) -> str:
    """The natural key column for a model; ``sequence`` for the event log."""
    return "sequence" if model_cls is EventLogEntry else "id"


class ImmutableHistoryError(RuntimeError):
    """A historical table (event log, evidence) cannot be mutated."""


# --------------------------------------------------------------------------
# Row ↔ model codec, driven by model annotations.
# --------------------------------------------------------------------------


@cache
def _codec(model_cls: type[pydantic.BaseModel]) -> tuple[tuple[str, str, Any], ...]:
    """Return ``(field_name, column_name, annotation)`` triples."""
    triples: list[tuple[str, str, Any]] = []
    for name, field in model_cls.model_fields.items():
        triples.append((name, _column_for(name, field.annotation), field.annotation))
    return tuple(triples)


def _column_for(name: str, annotation: Any) -> str:
    base = annotation
    while True:
        origin = get_origin(base)
        if origin in (Union, UnionType):
            args = [a for a in get_args(base) if a is not type(None)]
            if len(args) != len(get_args(base)):
                base = args[0] if args else type(None)
                continue
            break
        break
    origin = get_origin(base)
    if origin in (list, dict, set) or (
        isinstance(base, type) and issubclass(base, pydantic.BaseModel)
    ):
        return f"{name}_json"
    return name


def _is_optional(annotation: Any) -> bool:
    return get_origin(annotation) in (Union, UnionType) and type(None) in get_args(annotation)


def _bare(annotation: Any) -> Any:
    """Strip Optional[] wrappers, leaving the innermost single annotation."""
    base = annotation
    while get_origin(base) in (Union, UnionType):
        args = [a for a in get_args(base) if a is not type(None)]
        if len(args) != 1:
            return base
        base = args[0]
    return base


def _encode_field(annotation: Any, value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, _PRIMITIVES):
        return value
    return json.dumps(pydantic_core.to_jsonable_python(value))


@cache
def _type_adapter(annotation: Any) -> pydantic.TypeAdapter:
    return pydantic.TypeAdapter(annotation)


def _decode_field(annotation: Any, raw: Any) -> Any:
    if raw is None:
        if _is_optional(annotation):
            return None
        raise ValueError(f"non-optional column got NULL: {annotation}")
    base = _bare(annotation)
    if isinstance(base, type) and issubclass(base, enum.Enum):
        return base(raw)
    if base is datetime:
        decoded = datetime.fromisoformat(raw)
        if decoded.tzinfo is None:
            decoded = decoded.replace(tzinfo=UTC)
        return decoded
    if base in _PRIMITIVES:
        return base(raw)
    return _type_adapter(annotation).validate_python(json.loads(raw))


# --------------------------------------------------------------------------
# Store facade
# --------------------------------------------------------------------------


class _Transaction:
    """Explicit ``BEGIN IMMEDIATE`` context; one level deep by design."""

    def __init__(self, store: SqliteRelayStore) -> None:
        self.store = store

    def __enter__(self) -> SqliteRelayStore:
        if self.store._in_transaction:
            msg = "nested transactions are not supported"
            raise RuntimeError(msg)
        self.store._in_transaction = True
        self.store.conn.execute("BEGIN IMMEDIATE")
        return self.store

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.store._in_transaction = False
        if exc_type is None:
            self.store.conn.execute("COMMIT")
        else:
            self.store.conn.execute("ROLLBACK")


class SqliteRelayStore:
    """Append-first typed persistence over one Relay SQLite database."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if version < 1:
            msg = "database has no schema; call relay.storage.db.migrate() first"
            raise RuntimeError(msg)

    # -- transactions -------------------------------------------------------

    _in_transaction = False

    def transaction(self) -> _Transaction:
        return _Transaction(self)

    # -- generic record operations ------------------------------------------

    def save_model(self, record: Any) -> Any:
        codec = _codec(type(record))
        columns = [col for _, col, _ in codec]
        values = [_encode_field(ann, getattr(record, fname)) for fname, _, ann in codec]
        placeholders = ", ".join("?" for _ in columns)
        cursor = self.conn.execute(
            f"INSERT INTO {MODEL_TABLES[type(record)]} ({', '.join(columns)}) "
            f"VALUES ({placeholders})",
            values,
        )
        if type(record) is EventLogEntry:
            # ``sequence`` is DB-assigned on insert (models.py contract); the
            # facade reads it back exactly like EventLogWriter does.
            return record.model_copy(update={"sequence": cursor.lastrowid})
        return record

    def update_model(self, record: Any) -> Any:
        table = MODEL_TABLES[type(record)]
        if table in _APPEND_ONLY_TABLES:
            raise ImmutableHistoryError(f"'{table}' is append-only")
        codec = _codec(type(record))
        assignments = ", ".join(f"{col} = ?" for _, col, _ in codec)
        values = [_encode_field(ann, getattr(record, fname)) for fname, _, ann in codec]
        cursor = self.conn.execute(
            f"UPDATE {table} SET {assignments} WHERE {_pk_column(type(record))} = ?",
            [*values, getattr(record, _pk_column(type(record)))],
        )
        if cursor.rowcount == 0:
            msg = f"{type(record).__name__} '{record.id}' not found"
            raise KeyError(msg)
        return record

    @staticmethod
    def _refuse_history_mutation(table: str) -> None:
        if table in _APPEND_ONLY_TABLES:
            raise ImmutableHistoryError(f"'{table}' is append-only")

    def load_model(self, model_cls: type, record_id: str | int) -> Any | None:
        return next(
            self._iter_rows(model_cls, f"WHERE {_pk_column(model_cls)} = ?", [record_id]),
            None,
        )

    def all_models(
        self,
        model_cls: type,
        clause: str = "",
        params: list[Any] | None = None,
        order_by: str = "rowid ASC",
        limit: int | None = None,
    ) -> Iterator[Any]:
        return self._iter_rows(model_cls, clause, params or [], order_by, limit)

    def _iter_rows(
        self,
        model_cls: type,
        clause: str,
        params: list[Any],
        order_by: str = "rowid ASC",
        limit: int | None = None,
    ) -> Iterator[Any]:
        codec = _codec(model_cls)
        sql = (
            f"SELECT rowid AS _seq, {', '.join(col for _, col, _ in codec)} "
            f"FROM {MODEL_TABLES[model_cls]}"
        )
        if clause:
            sql += f" {clause}"
        if order_by:
            sql += f" ORDER BY {order_by}"
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        for row in self.conn.execute(sql, params):
            fields = {fname: _decode_field(ann, row[col]) for fname, col, ann in codec}
            if model_cls is EventLogEntry:
                # ``sequence`` is DB-assigned on insert (models.py contract);
                # here it is read back from the AUTOINCREMENT primary key.
                fields["sequence"] = row["_seq"]
            yield model_cls.model_validate(fields)

    def delete_model(self, record: Any) -> None:
        table = MODEL_TABLES[type(record)]
        if table in _APPEND_ONLY_TABLES:
            raise ImmutableHistoryError(f"'{table}' is append-only")
        self.conn.execute(f"DELETE FROM {table} WHERE id = ?", [record.id])

    def counts(self) -> dict[str, int]:
        tables = sorted({*MODEL_TABLES.values()})
        return {
            table: int(self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in tables
        }

    # -- aggregate conveniences ----------------------------------------------

    def register_identity(self, workspace: Workspace, identity_key: str) -> None:
        """Bind the canonical Windows-safe workspace key to its row.

        ``identity_key`` is derived with ``normcase(realpath(...))`` and is
        DB metadata, deliberately absent from the domain vocabulary; the
        human-readable path remains ``Workspace.path``.
        """
        self.conn.execute(
            "UPDATE workspaces SET identity_key = ? WHERE id = ?",
            [identity_key, workspace.id],
        )

    def workspace_for_identity(self, identity_key: str) -> Workspace | None:
        row = self.conn.execute(
            "SELECT id FROM workspaces WHERE identity_key = ?", [identity_key]
        ).fetchone()
        return None if row is None else self.load_model(Workspace, row["id"])

    def artifacts_for_run(self, run_id: str, kind: ArtifactKind | None = None) -> list[Artifact]:
        clause = "WHERE run_id = ? AND kind = ?" if kind else "WHERE run_id = ?"
        params = [run_id] if not kind else [run_id, kind.value]
        return list(self.all_models(Artifact, clause, params))

    def recent_runs(self, limit: int = 10) -> list[Run]:
        return list(self.all_models(Run, order_by="started_at DESC, rowid DESC", limit=limit))


# --------------------------------------------------------------------------
# Evidence protocol implementation (App. A.1 seam swap)
# --------------------------------------------------------------------------


class SqliteEvidenceStore:
    """SQLite-backed ``EvidenceStore`` replacing the in-memory reference.

    Provenance is validated at this boundary exactly like
    ``InMemoryEvidenceStore``; identical records become proof here. A
    duplicate id with differing content is rejected — history rows are
    immutable. Mutation/deletion attempts are additionally blocked by
    database triggers regardless of this API surface.
    """

    def __init__(self, store: SqliteRelayStore) -> None:
        self._store = store

    def record(self, evidence: EvidenceRecord) -> EvidenceRecord:
        validate_provenance(evidence)
        existing = self._store.load_model(EvidenceRecord, evidence.id)
        if existing is not None:
            if existing == evidence:
                return evidence
            msg = f"evidence record '{evidence.id}' already exists with different content"
            raise ValueError(msg)
        self._store.save_model(evidence)
        return evidence

    def records_for_task(
        self,
        task_id: str,
        kind: EvidenceKind | None = None,
    ) -> tuple[EvidenceRecord, ...]:
        if kind is None:
            clause = "WHERE task_id = ?"
            params: list[Any] = [task_id]
        else:
            clause = "WHERE task_id = ? AND kind = ?"
            params = [task_id, kind.value]
        return tuple(self._store.all_models(EvidenceRecord, clause, params))

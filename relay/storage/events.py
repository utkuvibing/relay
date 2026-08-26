"""Append-only system-event log (SPEC §15, Appendix A.2/B.1).

Entries are written inside the caller's current transaction — the
orchestrator's two-phase run persistence depends on this so that a Run,
its artifacts, and their lifecycle markers commit atomically.

``sequence`` is assigned by SQLite's AUTOINCREMENT on insert; callers
never supply one (``relay.storage.models`` contract). Sequences are
unique and strictly increasing but **not** gapless: a rolled-back
transaction consumes AUTOINCREMENT ids, which is expected.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

from relay.storage.models import EventLogEntry, EventType


class EventLogWriter:
    """Appends :class:`EventLogEntry` records and reads them back."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def record(self, entry: EventLogEntry) -> EventLogEntry:
        """Insert one entry within the caller's transaction.

        Returns the entry with its DB-assigned ``sequence`` filled in.
        """
        if entry.sequence is not None:
            msg = "event sequence is assigned by the store on insert"
            raise ValueError(msg)
        cursor = self._conn.execute(
            "INSERT INTO event_log "
            "(room_id, task_id, sender, recipient, type, content, "
            "references_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                entry.room_id,
                entry.task_id,
                entry.sender,
                entry.recipient,
                entry.type.value,
                entry.content,
                json.dumps(entry.references),
                _as_utc_string(entry.created_at),
            ],
        )
        return entry.model_copy(update={"sequence": cursor.lastrowid})

    def tail(self, limit: int = 20) -> list[EventLogEntry]:
        """Most recent entries first (highest sequence is newest)."""
        rows = self._conn.execute(
            "SELECT * FROM event_log ORDER BY sequence DESC LIMIT ?", [int(limit)]
        ).fetchall()
        return [_entry_from_row(row) for row in rows]

    def all(self) -> list[EventLogEntry]:
        """Every entry, oldest first."""
        rows = self._conn.execute("SELECT * FROM event_log ORDER BY sequence ASC").fetchall()
        return [_entry_from_row(row) for row in rows]


def _entry_from_row(row: sqlite3.Row) -> EventLogEntry:
    return EventLogEntry(
        sequence=int(row["sequence"]),
        room_id=row["room_id"],
        task_id=row["task_id"],
        sender=row["sender"],
        recipient=row["recipient"],
        type=EventType(row["type"]),
        content=row["content"],
        references=json.loads(row["references_json"]),
        created_at=datetime.fromisoformat(row["created_at"]),
    )


def _as_utc_string(value: datetime) -> str:
    value = value if value.tzinfo else value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()

"""P7.1 persistent Room lifecycle and stable-seat behavior."""

import sqlite3

import pytest

from relay.core.rooms import RoomError, RoomLifecycle
from relay.core.state_machine import TaskState
from relay.storage.db import _MIGRATIONS, connect, migrate
from relay.storage.events import EventLogWriter
from relay.storage.models import EventType, Room, RoomStatus, Task, Workspace
from relay.storage.store import SqliteRelayStore


@pytest.fixture
def room_store(tmp_path):
    conn = connect(tmp_path / "rooms.db")
    migrate(conn)
    store = SqliteRelayStore(conn)
    workspace = store.save_model(Workspace(id="workspace", name="demo"))
    yield store, workspace, RoomLifecycle(store, EventLogWriter(conn))
    conn.close()


def test_create_snapshots_roles_and_becomes_current(room_store):
    store, workspace, lifecycle = room_store
    room = lifecycle.create(
        workspace,
        "Architecture",
        {"planner": "gpt", "reviewer": "claude"},
        {"gpt", "claude"},
    )

    assert [(seat.role, seat.agent) for seat in room.members] == [
        ("planner", "gpt"),
        ("reviewer", "claude"),
    ]
    assert store.load_model(Workspace, workspace.id).active_room_id == room.id
    events = EventLogWriter(store.conn).all()
    assert [event.type for event in events] == [EventType.ROOM_CREATED]


def test_close_and_resume_preserve_task_and_history(room_store):
    store, workspace, lifecycle = room_store
    room = lifecycle.create(workspace, "Build", {"planner": "gpt"}, {"gpt"})
    task = store.save_model(Task(title="still active", room_id=room.id))
    workspace = store.load_model(Workspace, workspace.id)

    closed = lifecycle.close(workspace, room)
    assert closed.status is RoomStatus.CLOSED
    assert store.load_model(Task, task.id).state is TaskState.CREATED
    assert store.load_model(Workspace, workspace.id).active_room_id is None

    reopened = lifecycle.resume(store.load_model(Workspace, workspace.id), closed)
    assert reopened.status is RoomStatus.OPEN
    assert reopened.closed_at is None
    assert store.load_model(Workspace, workspace.id).active_room_id == room.id


def test_bind_validates_role_and_agent_without_mutation(room_store):
    store, workspace, lifecycle = room_store
    room = lifecycle.create(workspace, "Team", {"planner": "gpt"}, {"gpt", "claude"})
    baseline = store.counts()
    for role, agent in (("invented", "gpt"), ("reviewer", "missing")):
        with pytest.raises(RoomError):
            lifecycle.bind(room, role, agent, {"gpt", "claude"})
        assert store.counts() == baseline
        assert store.load_model(Room, room.id) == room

    added = lifecycle.bind(room, "reviewer", "claude", {"gpt", "claude"})
    rebound = lifecycle.bind(added, "reviewer", "gpt", {"gpt", "claude"})
    assert {seat.role: seat.agent for seat in rebound.members} == {
        "planner": "gpt",
        "reviewer": "gpt",
    }
    counts = store.counts()
    assert lifecycle.bind(rebound, "reviewer", "gpt", {"gpt", "claude"}) == rebound
    assert store.counts() == counts


def test_create_rejects_empty_roles_and_duplicate_name(room_store):
    store, workspace, lifecycle = room_store
    with pytest.raises(RoomError, match="no roles"):
        lifecycle.create(workspace, "Empty", {}, {"gpt"})
    lifecycle.create(workspace, "Design", {"planner": "gpt"}, {"gpt"})
    before = store.counts()
    with pytest.raises(RoomError, match="already exists"):
        lifecycle.create(
            store.load_model(Workspace, workspace.id),
            "design",
            {"planner": "gpt"},
            {"gpt"},
        )
    assert store.counts() == before


def test_create_rolls_back_room_workspace_and_event_together(room_store):
    store, workspace, _ = room_store

    class FailingWriter:
        def record(self, entry):
            raise RuntimeError("injected event failure")

    lifecycle = RoomLifecycle(store, FailingWriter())
    before = store.counts()
    with pytest.raises(RuntimeError, match="injected"):
        lifecycle.create(workspace, "Atomic", {"planner": "gpt"}, {"gpt"})
    assert store.counts() == before
    assert store.load_model(Workspace, workspace.id).active_room_id is None


def test_v8_migration_disambiguates_legacy_names_without_changing_ids(tmp_path):
    conn = connect(tmp_path / "legacy.db")
    for version in range(1, 8):
        for statement in _MIGRATIONS[version]:
            conn.execute(statement)
    conn.execute("PRAGMA user_version = 7")
    conn.execute(
        "INSERT INTO workspaces (id, name, kind, created_at) VALUES "
        "('w', 'workspace', 'folder', '2025-01-01T00:00:00+00:00')"
    )
    for room_id, name, created in (
        ("a", "Design", "2025-01-01T00:00:00+00:00"),
        ("b", "design", "2025-01-02T00:00:00+00:00"),
        ("c", "Design (2)", "2025-01-03T00:00:00+00:00"),
    ):
        conn.execute(
            "INSERT INTO rooms (id, name, workspace_id, members_json, created_at) "
            "VALUES (?, ?, 'w', '[]', ?)",
            [room_id, name, created],
        )

    assert migrate(conn) == 8
    rows = conn.execute("SELECT id, name, status FROM rooms ORDER BY id").fetchall()
    assert [tuple(row) for row in rows] == [
        ("a", "Design", "open"),
        ("b", "design (3)", "open"),
        ("c", "Design (2)", "open"),
    ]
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO rooms "
            "(id, name, workspace_id, members_json, status, updated_at, created_at) "
            "VALUES ('d', 'DESIGN', 'w', '[]', 'open', ?, ?)",
            ["2025-01-04T00:00:00+00:00", "2025-01-04T00:00:00+00:00"],
        )
    conn.close()

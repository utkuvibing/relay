"""P7.1 persistent Room lifecycle and stable-seat behavior."""

import sqlite3

import pytest

from relay.core.bus import ConversationBus
from relay.core.rooms import RoomError, RoomLifecycle, RoomSeatResolver
from relay.core.state_machine import TaskState
from relay.storage.db import _MIGRATIONS, SCHEMA_VERSION, connect, migrate
from relay.storage.events import EventLogWriter
from relay.storage.models import (
    EventType,
    Message,
    MessageType,
    Room,
    RoomStatus,
    Task,
    Workspace,
    room_name_key,
)
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


def test_room_seat_resolver_snapshots_binding_for_one_exchange(room_store):
    store, workspace, lifecycle = room_store
    room = lifecycle.create(
        workspace,
        "Review",
        {"reviewer": "claude"},
        {"claude", "gpt"},
    )
    resolver = RoomSeatResolver(room)

    rebound = lifecycle.bind(room, "reviewer", "gpt", {"claude", "gpt"})
    old_exchange = ConversationBus(store, EventLogWriter(store.conn), resolver).send(
        Message(
            sender="human:utku",
            recipient_role="reviewer",
            room_id=room.id,
            type=MessageType.CLARIFICATION_REQUEST,
            content="Is this a blocker?",
        )
    )
    new_exchange = ConversationBus(
        store,
        EventLogWriter(store.conn),
        RoomSeatResolver(rebound),
    ).send(
        Message(
            sender="human:utku",
            recipient_role="reviewer",
            room_id=room.id,
            type=MessageType.CLARIFICATION_REQUEST,
            content="What changed?",
        )
    )

    assert old_exchange.recipient == "claude"
    assert old_exchange.recipient_role == "reviewer"
    assert new_exchange.recipient == "gpt"


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


def test_lifecycle_is_idempotent_and_persists_after_database_reopen(tmp_path):
    path = tmp_path / "persistent.db"
    conn = connect(path)
    migrate(conn)
    store = SqliteRelayStore(conn)
    workspace = store.save_model(Workspace(id="workspace", name="demo"))
    lifecycle = RoomLifecycle(store, EventLogWriter(conn))
    room = lifecycle.create(workspace, "Persistent", {"planner": "gpt"}, {"gpt"})
    workspace = store.load_model(Workspace, workspace.id)
    counts = store.counts()
    assert lifecycle.resume(workspace, room) == room
    assert store.counts() == counts
    closed = lifecycle.close(workspace, room)
    counts = store.counts()
    assert lifecycle.close(store.load_model(Workspace, workspace.id), closed) == closed
    assert store.counts() == counts
    conn.close()

    reopened_conn = connect(path)
    reopened_store = SqliteRelayStore(reopened_conn)
    assert reopened_store.load_model(Room, room.id) == closed
    assert reopened_store.load_model(Workspace, workspace.id).active_room_id is None
    reopened_conn.close()


def test_room_lookup_precedence_and_ambiguous_prefix(room_store):
    store, workspace, lifecycle = room_store
    first = lifecycle.create(
        workspace,
        "Alpha",
        {"planner": "gpt"},
        {"gpt"},
        room_id="shared-one",
    )
    workspace = store.load_model(Workspace, workspace.id)
    second = lifecycle.create(
        workspace,
        "Beta",
        {"planner": "gpt"},
        {"gpt"},
        room_id="shared-two",
    )
    assert lifecycle.resolve(workspace.id, first.id) == first
    assert lifecycle.resolve(workspace.id, "ALPHA") == first
    assert lifecycle.resolve(workspace.id, "shared-t") == second
    with pytest.raises(RoomError, match="ambiguous"):
        lifecycle.resolve(workspace.id, "shared-")


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


def test_close_resume_and_bind_roll_back_with_their_event(room_store):
    store, workspace, lifecycle = room_store
    room = lifecycle.create(workspace, "Atomic lifecycle", {"planner": "gpt"}, {"gpt"})

    class FailingWriter:
        def record(self, entry):
            raise RuntimeError("injected event failure")

    failing = RoomLifecycle(store, FailingWriter())
    workspace = store.load_model(Workspace, workspace.id)
    baseline = store.counts()
    with pytest.raises(RuntimeError, match="injected"):
        failing.bind(room, "reviewer", "gpt", {"gpt"})
    with pytest.raises(RuntimeError, match="injected"):
        failing.close(workspace, room)
    assert store.load_model(Room, room.id) == room
    assert store.load_model(Workspace, workspace.id) == workspace
    assert store.counts() == baseline

    closed = lifecycle.close(workspace, room)
    closed_workspace = store.load_model(Workspace, workspace.id)
    baseline = store.counts()
    with pytest.raises(RuntimeError, match="injected"):
        failing.resume(closed_workspace, closed)
    assert store.load_model(Room, room.id) == closed
    assert store.load_model(Workspace, workspace.id) == closed_workspace
    assert store.counts() == baseline


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

    assert migrate(conn) == SCHEMA_VERSION
    rows = conn.execute("SELECT id, name, status FROM rooms ORDER BY id").fetchall()
    assert [tuple(row) for row in rows] == [
        ("a", "Design", "open"),
        ("b", "design (3)", "open"),
        ("c", "Design (2)", "open"),
    ]
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO rooms "
            "(id, name, workspace_id, members_json, status, updated_at, created_at, name_key) "
            "VALUES ('d', 'DESIGN', 'w', '[]', 'open', ?, ?, ?)",
            [
                "2025-01-04T00:00:00+00:00",
                "2025-01-04T00:00:00+00:00",
                room_name_key("DESIGN"),
            ],
        )
    conn.close()


def test_room_name_uniqueness_uses_unicode_casefold(room_store):
    store, workspace, lifecycle = room_store
    lifecycle.create(workspace, "Straße", {"planner": "gpt"}, {"gpt"})
    with pytest.raises(RoomError, match="already exists"):
        lifecycle.create(
            store.load_model(Workspace, workspace.id),
            "STRASSE",
            {"planner": "gpt"},
            {"gpt"},
        )


def test_typed_room_rename_recomputes_name_key_atomically(room_store):
    store, workspace, lifecycle = room_store
    lifecycle.create(workspace, "Straße", {"planner": "gpt"}, {"gpt"})
    workspace = store.load_model(Workspace, workspace.id)
    other = lifecycle.create(workspace, "Other", {"planner": "gpt"}, {"gpt"})

    with pytest.raises(sqlite3.IntegrityError):
        store.update_model(other.model_copy(update={"name": "STRASSE"}))
    assert store.load_model(Room, other.id).name == "Other"

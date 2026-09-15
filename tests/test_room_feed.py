"""Room feed read-model: composition, dedupe, determinism (P4.1, plan D8).

SPEC reference: §27 Phase 4; App. D.1/D.11-P4. The feed is a pure
read-model over persisted records — zero mutations, no derived state.
"""

from datetime import UTC, datetime, timedelta

import pytest

from relay.core.bus import ConversationBus
from relay.core.room_feed import FeedEntry, RoomFeedIntegrityError, build_room_feed
from relay.storage.db import connect, migrate
from relay.storage.events import EventLogWriter
from relay.storage.models import (
    EventLogEntry,
    EventType,
    Message,
    MessageType,
    Room,
    Run,
)
from relay.storage.store import SqliteRelayStore

#: P4.2 (frozen plan D1): bare logical-agent senders prove authorship via a
#: real Run; ``_msg`` wires the seeded run id automatically.
_RUN_IDS: dict[str, str] = {}


@pytest.fixture(autouse=True)
def _authorship_runs(store):
    _RUN_IDS.clear()
    _RUN_IDS["claude"] = store.save_model(Run(agent="claude", role="reviewer")).id
    _RUN_IDS["codex"] = store.save_model(Run(agent="codex", role="implementer")).id
    yield
    _RUN_IDS.clear()


@pytest.fixture()
def db(tmp_path):
    conn = connect(tmp_path / "feed.sqlite3")
    migrate(conn)
    yield conn
    conn.close()


@pytest.fixture()
def store(db):
    return SqliteRelayStore(db)


@pytest.fixture()
def scope(store):
    """Real Room rows — the messages table carries FK constraints."""
    store.save_model(Room(id="room-1", name="feed-room"))
    store.save_model(Room(id="room-2", name="other-room"))


@pytest.fixture()
def bus(store, db, scope):
    return ConversationBus(store, EventLogWriter(db))


def _msg(**overrides) -> Message:
    base: dict[str, object] = {
        "sender": "claude",
        "recipient": "codex",
        "room_id": "room-1",
        "type": MessageType.NOTE,
        "content": "compatibility shim is intentional",
    }
    base.update(overrides)
    sender = base["sender"]
    if "run_id" not in overrides and isinstance(sender, str) and ":" not in sender:
        # P4.2 D1: auto-wire authorship provenance for bare agent senders.
        base["run_id"] = _RUN_IDS.get(sender)
    return Message(**base)


def _event(**overrides) -> EventLogEntry:
    base: dict[str, object] = {
        "room_id": "room-1",
        "type": EventType.ARTIFACT_CREATED,
        "content": "plan artifact minted",
    }
    base.update(overrides)
    return EventLogEntry(**base)


class TestFeedComposition:
    def test_composes_messages_and_events_chronologically(self, store, bus, db):
        t0 = datetime.now(UTC)
        bus.send(_msg(content="first", created_at=t0))
        EventLogWriter(db).record(
            _event(content="system did a thing", created_at=t0 + timedelta(seconds=1))
        )
        bus.send(_msg(content="third", created_at=t0 + timedelta(seconds=2)))

        feed = build_room_feed(store, "room-1")

        assert [entry.text for entry in feed] == ["first", "system did a thing", "third"]
        assert [entry.origin for entry in feed] == ["message", "event", "message"]
        assert feed[0].entry_id.startswith("message:")
        assert feed[1].entry_id.startswith("event:")
        assert feed[1].kind == "artifact_created"

    def test_message_sent_markers_are_deduped(self, store, bus, db):
        """The marker is provenance; the Message already represents the item."""
        saved = bus.send(_msg(content="only once"))
        markers = [
            e
            for e in EventLogWriter(db).all()
            if e.type is EventType.MESSAGE_SENT and f"message:{saved.id}" in e.references
        ]
        assert len(markers) == 1

        feed = build_room_feed(store, "room-1")
        assert len(feed) == 1
        assert feed[0].origin == "message"
        assert feed[0].text == "only once"

    def test_orphan_marker_is_an_integrity_failure(self, store, db):
        writer = EventLogWriter(db)
        writer.record(
            _event(
                type=EventType.MESSAGE_SENT,
                content="opinion from ghost to room",
                references=["message:does-not-exist", "room:room-1"],
            )
        )

        with pytest.raises(RoomFeedIntegrityError, match="missing Message"):
            build_room_feed(store, "room-1")

    def test_orphan_message_is_an_integrity_failure(self, store, scope):
        store.save_model(_msg(content="unmarked"))
        with pytest.raises(RoomFeedIntegrityError, match="no same-Room"):
            build_room_feed(store, "room-1")

    def test_cross_room_marker_is_an_integrity_failure(self, store, db, scope):
        other = store.save_model(_msg(room_id="room-2", content="other"))
        EventLogWriter(db).record(
            _event(
                type=EventType.MESSAGE_SENT,
                references=[f"message:{other.id}", "room:room-1"],
            )
        )
        with pytest.raises(RoomFeedIntegrityError, match="contradictory Room scope"):
            build_room_feed(store, "room-1")

    def test_duplicate_marker_is_an_integrity_failure(self, store, bus, db):
        message = bus.send(_msg(content="once"))
        EventLogWriter(db).record(
            _event(
                type=EventType.MESSAGE_SENT,
                references=[f"message:{message.id}", "room:room-1"],
            )
        )
        with pytest.raises(RoomFeedIntegrityError, match="duplicate"):
            build_room_feed(store, "room-1")

    def test_marker_with_multiple_message_refs_is_an_integrity_failure(self, store, db, scope):
        first = store.save_model(_msg(content="first"))
        second = store.save_model(_msg(content="second"))
        EventLogWriter(db).record(
            _event(
                type=EventType.MESSAGE_SENT,
                references=[f"message:{first.id}", f"message:{second.id}", "room:room-1"],
            )
        )
        with pytest.raises(RoomFeedIntegrityError, match="exactly one"):
            build_room_feed(store, "room-1")

    def test_scoped_to_one_room(self, store, bus):
        bus.send(_msg(room_id="room-2", content="other room"))
        bus.send(_msg(content="this room"))

        feed = build_room_feed(store, "room-1")
        assert [entry.text for entry in feed] == ["this room"]

    def test_source_classes_follow_producer_conventions(self, store, bus):
        t0 = datetime.now(UTC)
        bus.send(_msg(sender="human:utku", created_at=t0))
        bus.send(_msg(sender="relay:review", created_at=t0 + timedelta(seconds=1)))
        bus.send(_msg(sender="claude", created_at=t0 + timedelta(seconds=2)))

        feed = build_room_feed(store, "room-1")
        assert [entry.source for entry in feed] == ["human", "relay", "agent"]

    def test_feed_entry_projects_reply_to_id(self, store, bus):
        t0 = datetime.now(UTC)
        parent = bus.send(_msg(content="parent", created_at=t0))
        bus.send(
            _msg(
                sender="codex",
                recipient="claude",
                reply_to_id=parent.id,
                content="reply",
                created_at=t0 + timedelta(seconds=1),
            )
        )

        feed = build_room_feed(store, "room-1")
        assert len(feed) == 2
        assert feed[0].reply_to_id is None
        assert feed[1].reply_to_id == parent.id


class TestFeedDeterminism:
    def test_event_sequence_is_authoritative_for_identical_timestamps(self, store, bus, db):
        stamp = datetime.now(UTC)
        for i in range(3):
            bus.send(_msg(content=f"m{i}", created_at=stamp))
        EventLogWriter(db).record(_event(content="event at same time", created_at=stamp))

        first = build_room_feed(store, "room-1")
        second = build_room_feed(store, "room-1")

        assert first == second  # pure and deterministic across calls
        texts = [entry.text for entry in first]
        assert texts == ["m0", "m1", "m2", "event at same time"]
        assert [entry.sequence for entry in first] == sorted(entry.sequence for entry in first)

    def test_timestamps_do_not_override_event_sequence(self, store, bus, db):
        bus.send(_msg(content="first by sequence", created_at=datetime.now(UTC)))
        EventLogWriter(db).record(
            _event(
                content="second by sequence",
                created_at=datetime.now(UTC) - timedelta(hours=1),
            )
        )

        feed = build_room_feed(store, "room-1")
        assert [entry.text for entry in feed] == ["first by sequence", "second by sequence"]


class TestFeedIsPure:
    def test_zero_mutations_and_stable_result(self, store, bus, db):
        bus.send(_msg())
        EventLogWriter(db).record(_event())
        baseline = store.counts()

        first = build_room_feed(store, "room-1")
        assert store.counts() == baseline  # no writes, no derived state persisted

        second = build_room_feed(store, "room-1")
        assert first == second
        assert all(isinstance(entry, FeedEntry) for entry in first)

    def test_empty_room_yields_empty_feed(self, store):
        assert build_room_feed(store, "missing-room") == []


class TestCanonicalRecordProjection:
    """P7.3 (App. D.3): canonical Room records render at their marker sequence."""

    def _freeze(self, tmp_path, db, store):
        from tests.room_helpers import (
            freeze,
            room_config,
            room_store,
        )

        fixture = room_store(tmp_path)
        outcome = freeze(fixture, room_config(), workspace_root=tmp_path)
        return fixture, outcome

    def test_frozen_plan_renders_once_at_its_marker(self, tmp_path):
        fixture, outcome = self._freeze(tmp_path, None, None)
        feed = build_room_feed(fixture.store, fixture.room.id)
        records = [entry for entry in feed if entry.origin == "record"]
        assert [entry.kind for entry in records] == ["plan_frozen"]
        entry = records[0]
        assert entry.text == "plan frozen: Plan"
        assert f"plan:{outcome.plan_artifact.id}" in entry.references
        assert entry.entry_id == f"record:{entry.sequence}"
        assert build_room_feed(fixture.store, fixture.room.id) == feed

    def test_decision_entries_keep_their_historical_state(self, tmp_path):
        from relay.agents.base import AgentRole
        from relay.core.room_records import promote_room_decision
        from relay.core.rooms import RoomSeatResolver
        from relay.storage.models import RoomDecisionPayload

        fixture, _outcome = self._freeze(tmp_path, None, None)
        store, writer, room = fixture.store, fixture.writer, fixture.room
        bus = ConversationBus(store, writer, RoomSeatResolver(room))

        def promote(payload: RoomDecisionPayload):
            parent = bus.send(
                Message(
                    sender="human:utku",
                    recipient_role=AgentRole.PLANNER.value,
                    room_id=room.id,
                    type=MessageType.PROPOSAL,
                    content="decide",
                )
            )
            run = store.save_model(
                Run(agent="gpt", role=AgentRole.PLANNER.value, status="succeeded")
            )
            reply = bus.send(
                Message(
                    sender="gpt",
                    recipient=parent.sender,
                    reply_to_id=parent.id,
                    run_id=run.id,
                    room_id=room.id,
                    type=MessageType.FINAL_POSITION,
                    content=payload.model_dump_json(),
                )
            )
            return promote_room_decision(store, writer, room, parent, reply)

        first = promote(
            RoomDecisionPayload(
                schema_version="relay.room_decision.v1", outcome="accept", statement="design A"
            )
        )
        assert first is not None
        second = promote(
            RoomDecisionPayload(
                schema_version="relay.room_decision.v1",
                outcome="accept",
                statement="design B",
                supersedes_decision_id=first.id,
            )
        )
        assert second is not None

        feed = build_room_feed(store, room.id)
        records = [entry for entry in feed if entry.origin == "record"]
        kinds = [entry.kind for entry in records]
        assert kinds == ["plan_frozen", "decision_accepted", "decision_accepted", "decision_superseded"]
        # The earlier acceptance keeps rendering its own state even though the
        # row is now SUPERSEDED.
        assert records[1].text == "accepted: design A"
        assert records[2].text == "accepted: design B"
        assert records[3].text == f"superseded by {second.id}"

    def test_orphan_room_plan_without_marker_fails_closed(self, tmp_path):
        from relay.storage.models import Artifact, ArtifactKind

        fixture, outcome = self._freeze(tmp_path, None, None)
        fixture.store.save_model(
            Artifact(
                kind=ArtifactKind.PLAN,
                room_id=fixture.room.id,
                task_id=outcome.task.id,
                content="# Orphan",
            )
        )
        with pytest.raises(RoomFeedIntegrityError, match="no ROOM_PLAN_FROZEN"):
            build_room_feed(fixture.store, fixture.room.id)

    def test_marker_without_decision_record_fails_closed(self, tmp_path):
        fixture, _outcome = self._freeze(tmp_path, None, None)
        fixture.writer.record(
            EventLogEntry(
                type=EventType.DECISION_ACCEPTED,
                room_id=fixture.room.id,
                sender="claude",
                content="forged",
                references=[f"room:{fixture.room.id}", "decision:ghost"],
            )
        )
        with pytest.raises(RoomFeedIntegrityError, match="foreign decision"):
            build_room_feed(fixture.store, fixture.room.id)

    def test_record_free_rooms_render_unchanged(self, store, bus):
        bus.send(_msg())
        feed = build_room_feed(store, "room-1")
        assert [entry.origin for entry in feed] == ["message"]
        assert all(entry.origin != "record" for entry in feed)

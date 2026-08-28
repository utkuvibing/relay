"""Room feed read-model: composition, dedupe, determinism (P4.1, plan D8).

SPEC reference: §27 Phase 4; App. D.1/D.11-P4. The feed is a pure
read-model over persisted records — zero mutations, no derived state.
"""

from datetime import UTC, datetime, timedelta

import pytest

from relay.core.bus import ConversationBus
from relay.core.room_feed import FeedEntry, build_room_feed
from relay.storage.db import connect, migrate
from relay.storage.events import EventLogWriter
from relay.storage.models import (
    EventLogEntry,
    EventType,
    Message,
    MessageType,
    Room,
)
from relay.storage.store import SqliteRelayStore


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
        bus.send(_msg(content="first"))
        EventLogWriter(db).record(_event(content="system did a thing"))
        bus.send(_msg(content="third"))

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

    def test_orphan_marker_falls_back_to_system_entry(self, store, db):
        """Defensive: a marker whose Message row is absent still renders."""
        writer = EventLogWriter(db)
        writer.record(
            _event(
                type=EventType.MESSAGE_SENT,
                content="opinion from ghost to room",
                references=["message:does-not-exist"],
            )
        )

        feed = build_room_feed(store, "room-1")
        assert len(feed) == 1
        assert feed[0].origin == "event"
        assert feed[0].source == "system"

    def test_scoped_to_one_room(self, store, bus):
        bus.send(_msg(room_id="room-2", content="other room"))
        bus.send(_msg(content="this room"))

        feed = build_room_feed(store, "room-1")
        assert [entry.text for entry in feed] == ["this room"]

    def test_source_classes_follow_producer_conventions(self, store, bus):
        bus.send(_msg(sender="human:utku"))
        bus.send(_msg(sender="relay:review"))
        bus.send(_msg(sender="claude"))

        feed = build_room_feed(store, "room-1")
        assert [entry.source for entry in feed] == ["human", "relay", "agent"]


class TestFeedDeterminism:
    def test_identical_timestamps_use_the_stable_tie_breaker(self, store, bus, db):
        """Plan D8: (timestamp, origin rank, entry id) — messages before
        events at equal timestamps, then the stable entry-id string."""
        stamp = datetime.now(UTC)
        same_time_messages = [
            Message(
                sender="claude",
                room_id="room-1",
                type=MessageType.NOTE,
                content=f"m{i}",
                created_at=stamp,
            )
            for i in range(3)
        ]
        with store.transaction():
            for message in same_time_messages:
                store.save_model(message)
            EventLogWriter(db).record(_event(content="event at same time", created_at=stamp))

        first = build_room_feed(store, "room-1")
        second = build_room_feed(store, "room-1")

        assert first == second  # pure and deterministic across calls
        # Message ids are random, so the id tie-break order within a rank is
        # arbitrary but STABLE — the contract is sort-key correctness, not m0 first.
        texts = [entry.text for entry in first]
        assert set(texts) == {"m0", "m1", "m2", "event at same time"}
        assert first[-1].text == "event at same time"  # rank: messages precede the event
        assert first[0].at == first[-1].at  # tie actually exercised
        message_entry_ids = [entry.entry_id for entry in first if entry.origin == "message"]
        assert message_entry_ids == sorted(message_entry_ids)

    def test_reordered_insertion_does_not_change_output_order(self, store, bus, db):
        early = datetime.now(UTC) - timedelta(hours=1)
        late_message = _msg(content="late message")
        with store.transaction():
            store.save_model(
                Message(
                    sender="claude",
                    room_id="room-1",
                    type=MessageType.NOTE,
                    content="early message",
                    created_at=early,
                )
            )
        EventLogWriter(db).record(_event(content="late event", created_at=datetime.now(UTC)))
        bus.send(late_message)

        feed = build_room_feed(store, "room-1")
        assert [entry.text for entry in feed] == [
            "early message",
            "late message",
            "late event",
        ]


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

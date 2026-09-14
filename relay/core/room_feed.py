"""Room feed read-model (SPEC §27 Phase 4; App. D.1/D.11-P4).

Pure, read-only composition of chronological Room history from persisted
records — making the Room feed technically possible in P4.1; the product
surface arrives later (P7). This is a READ-MODEL concern, deliberately
outside :mod:`relay.storage`: the store exposes raw scoped records; this
module owns the "what constitutes the feed" semantics.

Composition contract (P7.1):

* every Room Message maps one-to-one to a same-Room MESSAGE_SENT marker;
* the marker's event sequence orders the item, while the canonical Message
  supplies the rendered payload;
* every other Room-scoped system/lifecycle event renders at its sequence;
* zero mutations, no derived state persisted, fully offline-testable.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from relay.storage.models import EventLogEntry, EventType, Message
from relay.storage.store import SqliteRelayStore


@dataclass(frozen=True)
class FeedEntry:
    """One rendered Room-history item."""

    at: datetime
    sequence: int
    origin: str  # "message" | "event"
    source: str  # "agent" | "human" | "relay" | "system"
    kind: str  # MessageType value or EventType value
    sender: str | None
    recipient: str | None
    text: str
    references: tuple[str, ...]
    #: Stable tie-break id: "message:<id>" or "event:<sequence>".
    entry_id: str
    reply_to_id: str | None = None


class RoomFeedIntegrityError(RuntimeError):
    """Persisted Room messages and MESSAGE_SENT markers do not correspond."""


def _message_source(sender: str) -> str:
    """A.1 producer conventions rendered as feed source classes."""
    if sender.startswith("human:"):
        return "human"
    if sender.startswith("relay:"):
        return "relay"
    return "agent"


def _from_message(message: Message, marker: EventLogEntry) -> FeedEntry:
    if marker.sequence is None:
        raise RoomFeedIntegrityError("MESSAGE_SENT marker has no persisted sequence")
    return FeedEntry(
        at=message.created_at,
        sequence=marker.sequence,
        origin="message",
        source=_message_source(message.sender),
        kind=message.type.value,
        sender=message.sender,
        recipient=message.recipient,
        text=message.content,
        references=tuple(message.references),
        entry_id=f"message:{message.id}",
        reply_to_id=message.reply_to_id,
    )


def _from_event(event: EventLogEntry) -> FeedEntry:
    if event.sequence is None:
        raise RoomFeedIntegrityError("Room event has no persisted sequence")
    return FeedEntry(
        at=event.created_at,
        sequence=event.sequence,
        origin="event",
        source="system",
        kind=event.type.value,
        sender=event.sender,
        recipient=event.recipient,
        text=event.content,
        references=tuple(event.references),
        entry_id=f"event:{event.sequence}",
    )


def build_room_feed(store: SqliteRelayStore, room_id: str) -> list[FeedEntry]:
    """Validate and compose the canonical event-sequence Room feed."""
    room_messages = list(
        store.all_models(
            Message,
            "WHERE room_id = ?",
            [room_id],
            order_by="created_at ASC, rowid ASC",
        )
    )
    events = list(
        store.all_models(
            EventLogEntry,
            "WHERE room_id = ?",
            [room_id],
            order_by="sequence ASC",
        )
    )

    markers = [event for event in events if event.type is EventType.MESSAGE_SENT]
    messages_by_id = {message.id: message for message in room_messages}
    marker_by_message: dict[str, EventLogEntry] = {}

    for marker in markers:
        refs = [ref for ref in marker.references if ref.startswith("message:")]
        if len(refs) != 1:
            raise RoomFeedIntegrityError(
                f"MESSAGE_SENT marker {marker.sequence!r} must reference exactly one Message"
            )
        message_id = refs[0].removeprefix("message:")
        room_refs = [ref for ref in marker.references if ref.startswith("room:")]
        if room_refs != [f"room:{room_id}"]:
            raise RoomFeedIntegrityError(
                f"MESSAGE_SENT marker {marker.sequence!r} has contradictory Room references"
            )
        message = store.load_model(Message, message_id)
        if message is None:
            raise RoomFeedIntegrityError(
                f"MESSAGE_SENT marker {marker.sequence!r} references missing Message '{message_id}'"
            )
        if message.room_id != room_id or marker.room_id != room_id:
            raise RoomFeedIntegrityError(
                f"MESSAGE_SENT marker {marker.sequence!r} has contradictory Room scope"
            )
        if message_id in marker_by_message:
            raise RoomFeedIntegrityError(f"Message '{message_id}' has duplicate MESSAGE_SENT markers")
        marker_by_message[message_id] = marker

    orphan_ids = sorted(set(messages_by_id) - set(marker_by_message))
    if orphan_ids:
        raise RoomFeedIntegrityError(
            f"Room Message '{orphan_ids[0]}' has no same-Room MESSAGE_SENT marker"
        )
    if set(marker_by_message) != set(messages_by_id):
        raise RoomFeedIntegrityError("Room MESSAGE_SENT marker mapping is not one-to-one")

    entries: list[FeedEntry] = []
    for event in events:
        if event.type is EventType.MESSAGE_SENT:
            ref = next(ref for ref in event.references if ref.startswith("message:"))
            message = messages_by_id[ref.removeprefix("message:")]
            entries.append(_from_message(message, event))
        else:
            entries.append(_from_event(event))
    entries.sort(key=lambda entry: entry.sequence)
    return entries

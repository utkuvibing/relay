"""Room feed read-model (SPEC §27 Phase 4; App. D.1/D.11-P4).

Pure, read-only composition of chronological Room history from persisted
records — making the Room feed technically possible in P4.1; the product
surface arrives later (P7). This is a READ-MODEL concern, deliberately
outside :mod:`relay.storage`: the store exposes raw scoped records; this
module owns the "what constitutes the feed" semantics.

Composition contract (P4.1 plan D8):

* Message rows scoped to the room + EventLogEntry rows scoped to the room;
* MESSAGE_SENT markers are excluded when the corresponding Message is
  already represented (the marker is provenance, not a second feed item).
  Defensive fallback: a marker whose Message row is absent renders as a
  system entry rather than silently vanishing;
* deterministic ordering with an explicit stable tie-breaker:
  ``(timestamp, origin rank, entry id)`` — messages precede events at
  identical timestamps, then the stable ``message:<id>`` /
  ``event:<sequence>`` string breaks remaining ties;
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
    origin: str  # "message" | "event"
    source: str  # "agent" | "human" | "relay" | "system"
    kind: str  # MessageType value or EventType value
    sender: str | None
    recipient: str | None
    text: str
    references: tuple[str, ...]
    #: Stable tie-break id: "message:<id>" or "event:<sequence>".
    entry_id: str


_MESSAGE_ORIGIN_RANK = 0
_EVENT_ORIGIN_RANK = 1


def _message_source(sender: str) -> str:
    """A.1 producer conventions rendered as feed source classes."""
    if sender.startswith("human:"):
        return "human"
    if sender.startswith("relay:"):
        return "relay"
    return "agent"


def _from_message(message: Message) -> FeedEntry:
    return FeedEntry(
        at=message.created_at,
        origin="message",
        source=_message_source(message.sender),
        kind=message.type.value,
        sender=message.sender,
        recipient=message.recipient,
        text=message.content,
        references=tuple(message.references),
        entry_id=f"message:{message.id}",
    )


def _from_event(event: EventLogEntry) -> FeedEntry:
    return FeedEntry(
        at=event.created_at,
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
    """Compose the chronological feed for one room; pure read-only."""
    messages = list(
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

    represented = {f"message:{message.id}" for message in messages}

    entries = [_from_message(message) for message in messages]
    for event in events:
        if event.type is EventType.MESSAGE_SENT and any(
            reference in represented for reference in event.references
        ):
            continue  # the Message itself already represents this item
        entries.append(_from_event(event))

    entries.sort(key=lambda entry: (entry.at, _origin_rank(entry), entry.entry_id))
    return entries


def _origin_rank(entry: FeedEntry) -> int:
    return _MESSAGE_ORIGIN_RANK if entry.origin == "message" else _EVENT_ORIGIN_RANK

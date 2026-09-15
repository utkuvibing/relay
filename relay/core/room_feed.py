"""Room feed read-model (SPEC §27 Phase 4; App. D.1/D.11-P4).

Pure, read-only composition of chronological Room history from persisted
records — making the Room feed technically possible in P4.1; the product
surface arrives later (P7). This is a READ-MODEL concern, deliberately
outside :mod:`relay.storage`: the store exposes raw scoped records; this
module owns the "what constitutes the feed" semantics.

Composition contract (P7.1/P7.3):

* every Room Message maps one-to-one to a same-Room MESSAGE_SENT marker;
* the marker's event sequence orders the item, while the canonical Message
  supplies the rendered payload;
* every Room-scoped canonical record (frozen plan, P6.4-revised plan, decision,
  finding) has exactly ONE entry marker, which renders the record once at its
  own sequence — never the marker text, and never a later state of the record
  (a decision's acceptance entry keeps rendering ``accepted`` after the row is
  superseded; the later ``DECISION_SUPERSEDED`` renders separately);
* supersession transitions (``DECISION_SUPERSEDED``) render at their own
  sequence and must agree with the canonical edge fields;
* every other Room-scoped system/lifecycle event renders directly;
* zero mutations, no derived state persisted, fully offline-testable.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from relay.storage.models import (
    Artifact,
    ArtifactKind,
    Decision,
    DecisionStatus,
    EventLogEntry,
    EventType,
    Finding,
    Message,
)
from relay.storage.store import SqliteRelayStore


@dataclass(frozen=True)
class FeedEntry:
    """One rendered Room-history item."""

    at: datetime
    sequence: int
    origin: str  # "message" | "event" | "record"
    source: str  # "agent" | "human" | "relay" | "system"
    kind: str  # MessageType value, EventType value, or canonical record kind
    sender: str | None
    recipient: str | None
    text: str
    references: tuple[str, ...]
    #: Stable tie-break id: "message:<id>", "event:<sequence>", or
    #: "record:<sequence>".
    entry_id: str
    reply_to_id: str | None = None


class RoomFeedIntegrityError(RuntimeError):
    """Persisted Room messages and MESSAGE_SENT markers do not correspond."""


#: P7.3: the entry markers of canonical Room records (exactly one per record).
_RECORD_ENTRY_MARKERS = frozenset(
    {
        EventType.ROOM_PLAN_FROZEN,
        EventType.ROOM_PLAN_REVISED,
        EventType.DECISION_ACCEPTED,
        EventType.DECISION_REJECTED,
        EventType.FINDING_RECORDED,
    }
)

#: P7.3: canonical-record transition markers (zero or more per record).
_RECORD_TRANSITION_MARKERS = frozenset({EventType.DECISION_SUPERSEDED})

_RECORD_MARKER_TYPES = _RECORD_ENTRY_MARKERS | _RECORD_TRANSITION_MARKERS

_MAX_TEXT_CHARS = 200


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


def _first_line(content: str) -> str:
    for line in content.splitlines():
        stripped = line.strip().lstrip("#").strip()
        if stripped:
            return stripped[:_MAX_TEXT_CHARS]
    return "(empty plan)"


def _refs(event: EventLogEntry, prefix: str) -> list[str]:
    return [ref[len(prefix) :] for ref in event.references if ref.startswith(prefix)]


def _record_entry(event: EventLogEntry, text: str, kind: str) -> FeedEntry:
    assert event.sequence is not None  # callers pass persisted events
    return FeedEntry(
        at=event.created_at,
        sequence=event.sequence,
        origin="record",
        source="relay",
        kind=kind,
        sender=event.sender,
        recipient=None,
        text=text,
        references=tuple(event.references),
        entry_id=f"record:{event.sequence}",
    )


def _record_entries(
    store: SqliteRelayStore, room_id: str, events: list[EventLogEntry]
) -> dict[int, FeedEntry]:
    """Validate and render the Room's canonical-record markers (P7.3).

    Fail-closed: every Room-scoped canonical record must have exactly one entry
    marker, every marker must reference exactly one record of its class, and
    every transition marker must agree with the canonical supersession edge.
    """

    plans = {
        artifact.id: artifact
        for artifact in store.all_models(
            Artifact,
            "WHERE room_id = ? AND kind = ?",
            [room_id, ArtifactKind.PLAN.value],
            order_by="rowid ASC",
        )
    }
    decisions = {
        decision.id: decision
        for decision in store.all_models(
            Decision, "WHERE room_id = ?", [room_id], order_by="rowid ASC"
        )
    }
    findings = {
        finding.id: finding
        for finding in store.all_models(
            Finding, "WHERE room_id = ?", [room_id], order_by="rowid ASC"
        )
    }

    entries: dict[int, FeedEntry] = {}
    marked: set[str] = set()
    for event in events:
        if event.type not in _RECORD_MARKER_TYPES:
            continue
        if event.sequence is None:
            raise RoomFeedIntegrityError("canonical record marker has no persisted sequence")
        if event.type is EventType.DECISION_SUPERSEDED:
            entries[event.sequence] = _supersession_entry(event, decisions)
            continue
        record_ref, entry = _entry_for_marker(event, plans, decisions, findings)
        if record_ref in marked:
            raise RoomFeedIntegrityError(
                f"canonical Room record '{record_ref}' has duplicate entry markers"
            )
        marked.add(record_ref)
        entries[event.sequence] = entry

    for plan_id in plans:
        if f"plan:{plan_id}" not in marked:
            raise RoomFeedIntegrityError(
                f"Room plan artifact '{plan_id}' has no ROOM_PLAN_FROZEN/ROOM_PLAN_REVISED marker"
            )
    for decision_id in decisions:
        if f"decision:{decision_id}" not in marked:
            raise RoomFeedIntegrityError(
                f"Room decision '{decision_id}' has no DECISION_ACCEPTED/REJECTED marker"
            )
    for finding_id in findings:
        if f"finding:{finding_id}" not in marked:
            raise RoomFeedIntegrityError(
                f"Room finding '{finding_id}' has no FINDING_RECORDED marker"
            )
    return entries


def _entry_for_marker(
    event: EventLogEntry,
    plans: dict[str, Artifact],
    decisions: dict[str, Decision],
    findings: dict[str, Finding],
) -> tuple[str, FeedEntry]:
    if event.type in (EventType.ROOM_PLAN_FROZEN, EventType.ROOM_PLAN_REVISED):
        plan_refs = _refs(event, "plan:")
        if len(plan_refs) != 1:
            raise RoomFeedIntegrityError(
                f"{event.type.value} marker {event.sequence!r} must reference exactly one plan"
            )
        plan = plans.get(plan_refs[0])
        if plan is None:
            raise RoomFeedIntegrityError(
                f"{event.type.value} marker {event.sequence!r} references a foreign plan"
            )
        if event.type is EventType.ROOM_PLAN_FROZEN:
            if len(_refs(event, "artifact:")) != 1 or len(_refs(event, "message:")) != 1:
                raise RoomFeedIntegrityError(
                    f"ROOM_PLAN_FROZEN marker {event.sequence!r} has a contradictory shape"
                )
            return f"plan:{plan.id}", _record_entry(
                event, f"plan frozen: {_first_line(plan.content or '')}", "plan_frozen"
            )
        superseded = _refs(event, "supersedes_plan:")
        if len(superseded) != 1 or superseded[0] not in plans:
            raise RoomFeedIntegrityError(
                f"ROOM_PLAN_REVISED marker {event.sequence!r} contradicts its predecessor"
            )
        if len(_refs(event, "decision:")) != 1:
            raise RoomFeedIntegrityError(
                f"ROOM_PLAN_REVISED marker {event.sequence!r} has no decision provenance"
            )
        return f"plan:{plan.id}", _record_entry(
            event, f"plan revised: {_first_line(plan.content or '')}", "plan_revised"
        )
    if event.type in (EventType.DECISION_ACCEPTED, EventType.DECISION_REJECTED):
        decision_refs = _refs(event, "decision:")
        if len(decision_refs) != 1:
            raise RoomFeedIntegrityError(
                f"{event.type.value} marker {event.sequence!r} must reference exactly one decision"
            )
        decision = decisions.get(decision_refs[0])
        if decision is None:
            raise RoomFeedIntegrityError(
                f"{event.type.value} marker {event.sequence!r} references a foreign decision"
            )
        state = "accepted" if event.type is EventType.DECISION_ACCEPTED else "rejected"
        return f"decision:{decision.id}", _record_entry(
            event, f"{state}: {decision.statement[:_MAX_TEXT_CHARS]}", f"decision_{state}"
        )
    finding_refs = _refs(event, "finding:")
    if len(finding_refs) != 1:
        raise RoomFeedIntegrityError(
            f"FINDING_RECORDED marker {event.sequence!r} must reference exactly one finding"
        )
    finding = findings.get(finding_refs[0])
    if finding is None:
        raise RoomFeedIntegrityError(
            f"FINDING_RECORDED marker {event.sequence!r} references a foreign finding"
        )
    return f"finding:{finding.id}", _record_entry(
        event,
        f"{finding.severity.value}: {finding.title[:_MAX_TEXT_CHARS]}",
        "finding",
    )


def _supersession_entry(
    event: EventLogEntry, decisions: dict[str, Decision]
) -> FeedEntry:
    successors = _refs(event, "decision:")
    superseded = _refs(event, "supersedes_decision:")
    if len(successors) != 1 or len(superseded) != 1:
        raise RoomFeedIntegrityError(
            f"DECISION_SUPERSEDED marker {event.sequence!r} has a contradictory shape"
        )
    successor = decisions.get(successors[0])
    predecessor = decisions.get(superseded[0])
    if successor is None or predecessor is None:
        raise RoomFeedIntegrityError(
            f"DECISION_SUPERSEDED marker {event.sequence!r} references a foreign decision"
        )
    if successor.supersedes_decision_id != predecessor.id:
        raise RoomFeedIntegrityError(
            f"DECISION_SUPERSEDED marker {event.sequence!r} contradicts the canonical edge"
        )
    if predecessor.status is not DecisionStatus.SUPERSEDED:
        raise RoomFeedIntegrityError(
            f"DECISION_SUPERSEDED marker {event.sequence!r} supersedes a live decision"
        )
    return _record_entry(
        event, f"superseded by {successor.id}", "decision_superseded"
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

    record_entries = _record_entries(store, room_id, events)

    entries: list[FeedEntry] = []
    for event in events:
        if event.type is EventType.MESSAGE_SENT:
            ref = next(ref for ref in event.references if ref.startswith("message:"))
            message = messages_by_id[ref.removeprefix("message:")]
            entries.append(_from_message(message, event))
        elif event.sequence is not None and event.sequence in record_entries:
            entries.append(record_entries[event.sequence])
        else:
            entries.append(_from_event(event))
    entries.sort(key=lambda entry: entry.sequence)
    return entries

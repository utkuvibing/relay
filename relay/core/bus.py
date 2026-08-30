"""Conversation bus core (SPEC §9, §27 Phase 4; App. D.5–D.8, D.11-P4).

The bus is the single public write path for inter-agent conversation
traffic: it validates, then persists a ``Message`` row and its
``MESSAGE_SENT`` marker in ONE transaction. Marker content carries bounded
metadata only — never the conversation payload — so agent conversation
appears in the event log exclusively as markers referencing Message
records (App. A.2).

Addressing semantics (P4.1, frozen plan D3): ``recipient`` always stores
the RESOLVED logical-agent identity; ``recipient_role`` preserves the
original role address when the sender addressed a role. Role resolution
enters as an injected :class:`RoleResolver` seam — core never imports the
adapter registry (import-direction architecture test). Unresolved or
ambiguous roles are rejected BEFORE persistence; nothing partial is ever
written.

Authority boundary (App. D.8): the bus holds only the store, the event
writer, and an optional resolver. It can only ever insert Message rows
and MESSAGE_SENT markers — conversation may not mutate task state,
evidence, approvals, decisions, or policy. This is proven structurally
(``tests/test_architecture.py``) and behaviorally (``tests/test_bus.py``).

P4.1 closeout invariants (plan rev 3): broadcast (``recipient=None``
without a role) is denied outright — P5 may permit it under explicit
policy; self-send (sender == resolved recipient) is denied after role
resolution; ``MessageType.SYSTEM`` is reserved for ``relay:*`` senders.

P4.2 (frozen plan D1) — strict authorship: a bare logical-agent sender
MUST carry ``run_id`` linking the Run that authored the message, and the
bus validates ``run.agent == sender`` against the store; ``human:``/
``relay:`` senders must NOT carry ``run_id`` (authorship provenance is
agent-only — prefix senders cite runs via generic ``references``).
Sender strings alone are claimed, never proven, authorship. All
rejections happen BEFORE persistence.

Reply pairing / unresolved-blocking queries are NOT here: P4.3 owns that
semantics and the reply-linkage representation. ``references`` stay
generic semantic references in this phase.
"""

from __future__ import annotations

from typing import Protocol

from relay.storage.events import EventLogWriter
from relay.storage.models import EventLogEntry, EventType, Message, MessageType, Run
from relay.storage.store import SqliteRelayStore

#: Frozen content bound (P4.1 plan D12): enforced at the bus boundary as a
#: typed rejection BEFORE persistence — no truncation, no silent capping.
#: Deliberately not a DB CHECK constraint: a tunable must not be frozen
#: into schema.
MESSAGE_CONTENT_CAP_CHARS = 50_000

#: App. D.5: blocking-ness is metadata, legal only on the clarification
#: request and the D.5 "blocker" renderings (challenge/proposal/finding).
#: ``note`` can never block; no other type may carry the flag.
_BLOCKING_CAPABLE_TYPES = frozenset(
    {
        MessageType.CLARIFICATION_REQUEST,
        MessageType.CHALLENGE,
        MessageType.PROPOSAL,
        MessageType.REVIEW_FINDING,
    }
)

_ALLOWED_SENDER_PREFIXES = ("human:", "relay:")

#: P4.1 (plan D15): only relay:* senders may author SYSTEM messages.
_SYSTEM_SENDER_PREFIX = "relay:"


class MessageRejected(ValueError):
    """Typed pre-persistence rejection: nothing was written to the store."""


class RoleResolver(Protocol):
    """Resolution seam for role-addressed sends (P4.1; real resolver is P4.2).

    ``resolve_role`` maps a role address to the concrete logical-agent
    identity that will receive the message, or ``None`` when unresolved.
    Resolvers must be deterministic; an ambiguous role is a resolution
    failure (``None`` or a raised exception) and is rejected.
    """

    def resolve_role(self, role: str) -> str | None: ...

    def knows_agent(self, name: str) -> bool: ...


def _identity_is_valid(name: str) -> bool:
    """A logical identity is non-empty and whitespace-free."""
    return bool(name) and not any(character.isspace() for character in name)


class ConversationBus:
    """Validates and persists typed inter-agent messages; reads them back."""

    def __init__(
        self,
        store: SqliteRelayStore,
        writer: EventLogWriter,
        resolver: RoleResolver | None = None,
    ) -> None:
        self._store = store
        self._writer = writer
        self._resolver = resolver

    # -- write path ----------------------------------------------------------

    def send(self, message: Message) -> Message:
        """Validate, then persist message + MESSAGE_SENT marker atomically.

        Returns the persisted message with its final addressing (role-
        addressed sends carry the resolved identity in ``recipient``).
        Every rejection happens BEFORE persistence: a rejected send leaves
        the store byte-identical.
        """
        validated = self._validate(message)
        with self._store.transaction():
            saved = self._store.save_model(validated)
            self._writer.record(self._marker_for(saved))
        return saved

    def _validate(self, message: Message) -> Message:
        """Full G2 validation matrix; returns the message to persist."""
        if message.room_id is None and message.task_id is None:
            raise MessageRejected("message must carry a room_id and/or a task_id")

        self._validate_sender(message.sender)
        self._validate_authorship(message)

        if message.recipient_role is not None:
            if not _identity_is_valid(message.recipient_role):
                raise MessageRejected(f"invalid role address {message.recipient_role!r}")
            if message.recipient is not None:
                raise MessageRejected(
                    "recipient_role is resolved by the bus — do not pre-fill recipient"
                )
            resolved = self._resolve_role(message.recipient_role)
            message = message.model_copy(update={"recipient": resolved})
        elif message.recipient is not None:
            if ":" in message.recipient:
                # recipient is a RESOLVED logical-agent identity (D3); the
                # human:/relay: producer conventions are sender-side.
                raise MessageRejected(
                    f"recipient must be a bare logical-agent identity, got {message.recipient!r}"
                )
            if not _identity_is_valid(message.recipient):
                raise MessageRejected(f"invalid recipient {message.recipient!r}")

        # P4.1 (plan D13): broadcast is denied outright — every message must
        # carry a resolved recipient. P5 may later permit broadcast under
        # explicit communication policy.
        if message.recipient is None and message.recipient_role is None:
            raise MessageRejected(
                "broadcast is denied in P4.1 — address a logical agent directly or via role"
            )

        # P4.1 (plan D14): self-send is denied. Checked AFTER role resolution
        # so a role mapping back onto the sender is caught too. human:/relay:
        # senders are not logical-agent identities — the check does not apply.
        if ":" not in message.sender and message.recipient == message.sender:
            raise MessageRejected(
                f"self-send denied: sender and recipient are the same logical agent "
                f"({message.sender!r})"
            )

        # P4.1 (plan D15): MessageType.SYSTEM is reserved for relay:* senders.
        if message.type is MessageType.SYSTEM and not message.sender.startswith(
            _SYSTEM_SENDER_PREFIX
        ):
            raise MessageRejected(
                f"MessageType.SYSTEM is reserved for '{_SYSTEM_SENDER_PREFIX}*' senders; "
                f"sender {message.sender!r} may not author it"
            )

        if message.blocking and message.type not in _BLOCKING_CAPABLE_TYPES:
            raise MessageRejected(f"message type '{message.type.value}' cannot carry blocking=True")

        if not message.content.strip():
            raise MessageRejected("message content must not be empty")
        if len(message.content) > MESSAGE_CONTENT_CAP_CHARS:
            raise MessageRejected(
                f"message content exceeds the {MESSAGE_CONTENT_CAP_CHARS} "
                "character bound — rejected, not truncated"
            )

        for reference in message.references:
            if not isinstance(reference, str) or not reference.strip():
                raise MessageRejected("references must be non-empty strings")

        return message

    def _validate_sender(self, sender: str) -> None:
        if not _identity_is_valid(sender):
            raise MessageRejected(f"invalid sender {sender!r}")
        if ":" in sender:
            if not sender.startswith(_ALLOWED_SENDER_PREFIXES):
                raise MessageRejected(
                    f"sender {sender!r} must be a bare logical-agent identity "
                    f"or use the {' / '.join(_ALLOWED_SENDER_PREFIXES)} conventions"
                )
            return
        if self._resolver is not None and not self._resolver.knows_agent(sender):
            raise MessageRejected(f"unknown logical agent sender {sender!r}")

    def _validate_authorship(self, message: Message) -> None:
        """P4.2 (frozen plan D1): strict authorship provenance.

        A bare logical-agent sender MUST carry ``run_id`` and the linked Run
        must exist with ``run.agent == sender``. ``human:``/``relay:`` senders
        must NOT carry ``run_id`` — authorship proof is agent-only. Every
        rejection happens BEFORE persistence.
        """
        if ":" in message.sender:
            if message.run_id is not None:
                raise MessageRejected(
                    f"prefix sender {message.sender!r} must not carry run_id — "
                    "authorship provenance is agent-only; cite runs via "
                    "generic references instead"
                )
            return
        if message.run_id is None:
            raise MessageRejected(
                f"bare logical-agent sender {message.sender!r} requires run "
                "provenance: set run_id to the Run that authored this message "
                "(sender strings alone are claimed, not proven, authorship)"
            )
        run = self._store.load_model(Run, message.run_id)
        if run is None:
            raise MessageRejected(f"run_id {message.run_id!r} does not resolve to a Run")
        if run.agent != message.sender:
            raise MessageRejected(
                f"authorship mismatch: run {message.run_id!r} belongs to agent "
                f"{run.agent!r}, not sender {message.sender!r}"
            )

    def _resolve_role(self, role: str) -> str:
        if self._resolver is None:
            raise MessageRejected(
                f"role-addressed message to '{role}' rejected: no resolver configured"
            )
        try:
            resolved = self._resolver.resolve_role(role)
        except Exception as exc:
            raise MessageRejected(
                f"role resolution failed for '{role}': {type(exc).__name__}"
            ) from exc
        if resolved is None or not _identity_is_valid(resolved):
            raise MessageRejected(f"unresolved role address '{role}'")
        return resolved

    @staticmethod
    def _marker_for(saved: Message) -> EventLogEntry:
        """MESSAGE_SENT marker: bounded metadata + provenance refs, no payload."""
        target = saved.recipient or "room"
        role_note = f" via role '{saved.recipient_role}'" if saved.recipient_role else ""
        blocking_note = " (blocking)" if saved.blocking else ""
        scope_refs = []
        if saved.room_id:
            scope_refs.append(f"room:{saved.room_id}")
        if saved.task_id:
            scope_refs.append(f"task:{saved.task_id}")
        return EventLogEntry(
            room_id=saved.room_id,
            task_id=saved.task_id,
            sender=saved.sender,
            recipient=saved.recipient,
            type=EventType.MESSAGE_SENT,
            content=(
                f"{saved.type.value} from {saved.sender} to {target}{role_note}{blocking_note}"
            ),
            references=[f"message:{saved.id}", *scope_refs],
        )

    # -- read path -----------------------------------------------------------

    def messages_for_task(self, task_id: str) -> list[Message]:
        """Chronological messages scoped to a task."""
        return list(
            self._store.all_models(
                Message,
                "WHERE task_id = ?",
                [task_id],
                order_by="created_at ASC, rowid ASC",
            )
        )

    def messages_for_room(self, room_id: str) -> list[Message]:
        """Chronological messages scoped to a room."""
        return list(
            self._store.all_models(
                Message,
                "WHERE room_id = ?",
                [room_id],
                order_by="created_at ASC, rowid ASC",
            )
        )

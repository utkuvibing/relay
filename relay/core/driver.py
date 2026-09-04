"""Bounded multi-agent conversation driver (P4.4 — SPEC §27 Phase 4; App. C.7, D.11-P4).

Chains Relay-mediated deliveries into a multi-hop conversation: a caller-stable
spec names an ordered participant sequence, the driver composes each hop's
message on the conversation bus, hands it to the family-blind delivery spine
(:class:`~relay.core.delivery.MessageDelivery.deliver_and_reply`), and forwards
each materialized answer to the next participant — hub style, with the driver
visible in the ledger as ``relay:driver`` (frozen plan D1). The traversal is
structurally bounded: the participant sequence is finite, every hop is capped
at ``MAX_HOP_RETRIES`` attempts, and nothing waits or polls. There is no
unbounded loop. A failed blocking clarification is never retried as a NOTE —
the hop stops HOP_FAILED and the request stays canonically unanswered (D6,
pre-merge amendment). Termination reasons are exactly SEQUENCE_EXHAUSTED,
HOP_FAILED, and DELIVERY_PENDING — an unfinished provider run is honestly
"initiated, outcome pending" and a later same-key re-entry resolves it; the
driver never waits, polls, or fabricates outcomes (frozen plan D6/D7).

Deterministic identity (frozen plan D13): every driver-authored Message
(kinds ``seed``/``forward``/``retry``) carries an id derived by UUIDv5 over a
frozen canonical JSON identity vector — conversation key, exact scope, hop and
attempt indexes, message kind, the causal predecessor id, and the participant
identity with NULL-strict role provenance. Message content is deliberately
never an identity input: same inputs + different content collide on the id and
are caught by the semantic-equality verification as corruption. Creation is
concurrency-safe without any schema change — racing creators collide on the
existing ``messages.id`` PRIMARY KEY, the loser reloads the committed row and
either reuses it (exact semantic match) or refuses typed. A resumed foreign
seed adopts the canonical key ``foreign:<seed.id>``, so one foreign seed can
never be resumed into two parallel driver conversations. Message-id shape or
UUID version is NEVER ownership evidence: ownership is
``sender == DRIVER_SENDER`` plus successful re-derivation under the presented
key (frozen plan D14).

Traffic scoping (discharging a P4.2 open item): every driver-authored message
carries exactly the spec's ``room_id``/``task_id`` scope. Recipient-side
context policy (discharging the other): each participant receives the
deterministic delivery envelope plus the single forwarded-answer reference via
``context_refs`` pass-through — no transcript assembly, no D.10
reconstruction (P7).

Authority boundary (App. D.8): the driver authors only Message rows (via the
bus) and causes delivery Runs/events (via the delivery service). Task state,
evidence, approvals, decisions, and policy stay byte-identical — proven
structurally (``tests/test_architecture.py``) and behaviorally
(``tests/test_driver.py``).
"""

from __future__ import annotations

import enum
import json
import sqlite3
import uuid
from dataclasses import dataclass

from relay.agents.base import AgentRole
from relay.core.bus import ConversationBus, RoleResolver
from relay.core.delivery import (
    DeliveryPendingRefusal,
    DeliveryReplyOutcome,
    DuplicateDeliveryRefusal,
    MessageDelivery,
)
from relay.core.policy import (
    BlockingBudgetExhausted,
    BudgetExhausted,
    CommunicationPolicyGate,
    CommunicationPolicyRefusal,
    PolicyEnvelope,
)
from relay.storage.events import EventLogWriter
from relay.storage.models import Message, MessageType
from relay.storage.store import SqliteRelayStore

__all__ = [
    "DRIVER_SENDER",
    "FOREIGN_KEY_PREFIX",
    "MAX_CONVERSATION_KEY_CHARS",
    "MAX_HOP_RETRIES",
    "MAX_SPEC_PARTICIPANTS",
    "ConversationDriver",
    "ConversationKeyMismatchRefusal",
    "ConversationResult",
    "ConversationSpec",
    "DriverHop",
    "DriverIdentityRefusal",
    "DriverRefusal",
    "MessageKind",
    "ParticipantAddress",
    "PolicyRefusal",
    "ResumeSeedRefusal",
    "SpecRefusal",
    "StopReason",
    "foreign_conversation_key",
]

#: The driver is a ``relay:*`` component (App. A.1 producer convention); its
#: messages never carry ``run_id`` (P4.2 D1 — prefix senders are agent-authorship-free).
DRIVER_SENDER = "relay:driver"

#: Hard structural bounds (frozen plan D3): code constants, deliberately not
#: configuration — budgets and communication policy belong to P5.
MAX_SPEC_PARTICIPANTS = 8
MAX_HOP_RETRIES = 1

#: Caller-stable conversation-key rules (frozen plan D13/D14).
MAX_CONVERSATION_KEY_CHARS = 256
FOREIGN_KEY_PREFIX = "foreign:"

#: Frozen canonical-identity version tag and derivation namespace. Both are
#: immutable once shipped: changing either orphans every driver-derived id.
IDENTITY_VERSION = "relay.driver.identity.v1"
IDENTITY_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "https://relay.local/driver-identity/v1")


class MessageKind(str, enum.Enum):
    """Which traversal role a driver-authored message plays (frozen plan D5)."""

    SEED = "seed"
    FORWARD = "forward"
    RETRY = "retry"


class StopReason(str, enum.Enum):
    """Why a driver traversal stopped (frozen plan D7 plus P5.1 budget stop)."""

    SEQUENCE_EXHAUSTED = "sequence_exhausted"
    HOP_FAILED = "hop_failed"
    DELIVERY_PENDING = "delivery_pending"
    BUDGET_EXHAUSTED = "budget_exhausted"


class DriverRefusal(RuntimeError):
    """Typed driver refusal: nothing was persisted, no agent was invoked."""


class SpecRefusal(DriverRefusal):
    """The conversation spec is invalid — validation happens before any write."""


class PolicyRefusal(SpecRefusal):
    """The participant chain violates the configured communication policy."""


class ResumeSeedRefusal(DriverRefusal):
    """The seed proposed to :meth:`ConversationDriver.resume` is unusable."""


class ConversationKeyMismatchRefusal(DriverRefusal):
    """The presented conversation key does not own the resumed seed."""


class DriverIdentityRefusal(DriverRefusal):
    """A row already exists at a derived deterministic id with different
    semantic content — corruption or identity collision (frozen plan D13)."""


@dataclass(frozen=True)
class ParticipantAddress:
    """Typed participant addressing (frozen plan D3): exactly one arm set.

    ``agent`` addresses a configured logical agent directly; ``role`` is a
    role address in the ``AgentRole`` vocabulary (never a string-prefix
    encoding). The exactly-one-of invariant is enforced at spec validation.
    """

    agent: str | None = None
    role: AgentRole | None = None


@dataclass(frozen=True)
class ConversationSpec:
    """Caller-stable description of one bounded conversation (frozen plan D3).

    ``conversation_key`` names the logical conversation: the same key with the
    same canonical inputs re-derives the same driver message ids, which is what
    makes crash re-entry and re-invocation converge; a different key is a
    different conversation even when everything else is identical. Scope is
    pinned by ``room_id`` (+ optional ``task_id``) — all driver traffic carries
    exactly that scope. ``seed_content``/``seed_type``/``seed_blocking`` are
    START-ONLY: :meth:`ConversationDriver.resume` adopts the persisted seed
    instead and refuses any of them set.
    """

    conversation_key: str
    room_id: str
    participants: tuple[ParticipantAddress, ...]
    task_id: str | None = None
    seed_content: str | None = None
    seed_type: MessageType = MessageType.NOTE
    seed_blocking: bool = False


@dataclass(frozen=True)
class DriverHop:
    """One completed-or-terminated hop (frozen plan D8).

    ``forward`` and ``outcome`` describe the TERMINAL attempt (the message
    that was answered or finally failed — the seed itself for an unretried
    hop 1); ``attempts`` is the total attempt count (1 or ``MAX_HOP_RETRIES``
    + 1). Intermediate failed messages remain in the ledger, cited by the
    retry's references.
    """

    forward: Message
    outcome: DeliveryReplyOutcome
    attempts: int


@dataclass(frozen=True)
class ConversationResult:
    """Deterministic outcome of one driver traversal (frozen plan D8)."""

    seed: Message
    hops: tuple[DriverHop, ...]
    stop_reason: StopReason
    final_answer: Message | None
    refusal: CommunicationPolicyRefusal | None = None


def foreign_conversation_key(seed_id: str) -> str:
    """Canonical conversation key for resuming a foreign (non-driver) seed.

    Derived from the immutable seed identity, so the binding between a foreign
    seed and its driver conversation exists without any schema state: a second
    driver conversation on the same seed would need the same key and would
    re-derive the same downstream ids — converging, never forking (D14).
    """
    return f"{FOREIGN_KEY_PREFIX}{seed_id}"


def canonical_identity(
    *,
    conversation_key: str,
    room_id: str,
    task_id: str | None,
    hop_index: int,
    attempt_index: int,
    kind: MessageKind,
    prev_message_id: str | None,
    recipient_role: str | None,
    resolved_recipient: str,
) -> str:
    """Freeze the canonical identity vector into its byte-stable JSON form.

    Fixed field order, real JSON ``null`` for absent nullable values, and
    ``ensure_ascii`` escaping make the encoding injective over the fixed-shape
    vector: no separator character, literal ``"-"``, or Unicode payload can
    make two distinct tuples serialize alike (frozen plan D13).
    """
    vector: list[object] = [
        IDENTITY_VERSION,
        conversation_key,
        room_id,
        task_id,
        hop_index,
        attempt_index,
        kind.value,
        prev_message_id,
        recipient_role,
        resolved_recipient,
    ]
    return json.dumps(vector, ensure_ascii=True, separators=(",", ":"))


def derive_message_id(
    *,
    conversation_key: str,
    room_id: str,
    task_id: str | None,
    hop_index: int,
    attempt_index: int,
    kind: MessageKind,
    prev_message_id: str | None,
    recipient_role: str | None,
    resolved_recipient: str,
) -> str:
    """Derive the deterministic Message id for one logical driver message."""
    canonical = canonical_identity(
        conversation_key=conversation_key,
        room_id=room_id,
        task_id=task_id,
        hop_index=hop_index,
        attempt_index=attempt_index,
        kind=kind,
        prev_message_id=prev_message_id,
        recipient_role=recipient_role,
        resolved_recipient=resolved_recipient,
    )
    return uuid.uuid5(IDENTITY_NAMESPACE, canonical).hex


class ConversationDriver:
    """Execute bounded multi-hop conversations over the conversation layer.

    Consumes the bus (message composition/authoring), the delivery service
    (family-blind, at-most-once, crash-safe runs), and the role resolver
    (participant routing) — never the adapter registry, never the canonical
    state machine (App. D.8; import-direction architecture test).
    """

    def __init__(
        self,
        store: SqliteRelayStore,
        writer: EventLogWriter,
        bus: ConversationBus,
        delivery: MessageDelivery,
        resolver: RoleResolver,
        policy: CommunicationPolicyGate | None = None,
    ) -> None:
        self._store = store
        self._writer = writer
        self._bus = bus
        self._delivery = delivery
        self._resolver = resolver
        self._policy = policy

    # -- spec validation (frozen plan D3/D4) ---------------------------------

    def _validate_spec(self, spec: ConversationSpec, *, for_start: bool) -> None:
        key = spec.conversation_key
        if not isinstance(key, str) or not key.strip():
            raise SpecRefusal("conversation_key is required and must be a non-empty string")
        if len(key) > MAX_CONVERSATION_KEY_CHARS:
            raise SpecRefusal(f"conversation_key exceeds {MAX_CONVERSATION_KEY_CHARS} characters")
        if for_start and key.startswith(FOREIGN_KEY_PREFIX):
            raise SpecRefusal(
                f"conversation_key must not begin with the reserved '{FOREIGN_KEY_PREFIX}' "
                "prefix — it names foreign-seed conversations (frozen plan D14)"
            )
        if not isinstance(spec.room_id, str) or not spec.room_id.strip():
            raise SpecRefusal("room_id is required — driver traffic is scope-pinned")
        if not spec.participants:
            raise SpecRefusal("at least one participant is required")
        if len(spec.participants) > MAX_SPEC_PARTICIPANTS:
            raise SpecRefusal(
                f"participant sequence exceeds MAX_SPEC_PARTICIPANTS "
                f"({len(spec.participants)} > {MAX_SPEC_PARTICIPANTS})"
            )
        for index, participant in enumerate(spec.participants):
            if (participant.agent is None) == (participant.role is None):
                raise SpecRefusal(
                    f"participant {index}: exactly one of 'agent' or 'role' must be set"
                )
        for index, participant in enumerate(spec.participants):
            self._resolved_participant(participant, index)
        self._validate_policy_chain(spec)
        if for_start:
            if spec.seed_content is None or not spec.seed_content.strip():
                raise SpecRefusal("start() requires non-empty seed_content")
        else:
            if spec.seed_content is not None:
                raise SpecRefusal("resume() adopts the persisted seed — seed_content must be unset")
            if spec.seed_type is not MessageType.NOTE or spec.seed_blocking:
                raise SpecRefusal(
                    "seed_type/seed_blocking are start-only — the persisted seed "
                    "is authoritative on resume"
                )

    def _validate_policy_chain(self, spec: ConversationSpec) -> None:
        """Validate every role-to-role forward before the first write."""
        if self._policy is None:
            return
        for index in range(1, len(spec.participants)):
            previous = spec.participants[index - 1]
            current = spec.participants[index]
            sender = previous.role or AgentRole.PARTICIPANT
            recipient = current.role or AgentRole.PARTICIPANT
            envelope = PolicyEnvelope(
                sender=sender,
                recipient=recipient,
                type=MessageType.NOTE,
                blocking=False,
                room_id=spec.room_id,
                task_id=spec.task_id,
            )
            try:
                self._policy.check_edge(envelope)
            except CommunicationPolicyRefusal as exc:
                raise PolicyRefusal(
                    f"participant chain hop {index + 1} is not permitted by communication "
                    f"policy: {exc}"
                ) from exc

    def _resolved_participant(
        self, participant: ParticipantAddress, index: int
    ) -> tuple[str, str | None]:
        """Resolve one participant to ``(resolved_agent, recipient_role | None)``.

        The pair is the canonical participant identity: NULL-strict role
        provenance distinguishes a direct address from a role address that
        resolves to the same agent (frozen plan D13).
        """
        if participant.agent is not None:
            if not self._resolver.knows_agent(participant.agent):
                raise SpecRefusal(
                    f"participant {index}: unknown logical agent {participant.agent!r}"
                )
            return participant.agent, None
        assert participant.role is not None
        role_value = participant.role.value
        resolved = self._resolver.resolve_role(role_value)
        if resolved is None or not resolved or any(ch.isspace() for ch in resolved):
            raise SpecRefusal(f"participant {index}: unresolved role address {role_value!r}")
        return resolved, role_value

    # -- deterministic composition + create-or-reuse (frozen plan D13) -------

    def _compose_driver_message(
        self,
        spec: ConversationSpec,
        *,
        kind: MessageKind,
        hop_index: int,
        attempt_index: int,
        prev_message_id: str | None,
        participant: tuple[str, str | None],
        content: str,
        references: list[str],
        message_type: MessageType,
        blocking: bool,
    ) -> Message:
        """Compose one driver-authored message with its deterministic id."""
        resolved_recipient, recipient_role = participant
        message_id = derive_message_id(
            conversation_key=spec.conversation_key,
            room_id=spec.room_id,
            task_id=spec.task_id,
            hop_index=hop_index,
            attempt_index=attempt_index,
            kind=kind,
            prev_message_id=prev_message_id,
            recipient_role=recipient_role,
            resolved_recipient=resolved_recipient,
        )
        return Message(
            id=message_id,
            sender=DRIVER_SENDER,
            # Role-addressed sends leave recipient empty — the bus resolves it
            # and persists the resolved identity alongside recipient_role.
            recipient=None if recipient_role is not None else resolved_recipient,
            recipient_role=recipient_role,
            room_id=spec.room_id,
            task_id=spec.task_id,
            type=message_type,
            content=content,
            blocking=blocking,
            references=list(references),
        )

    def _send_driver_message(self, message: Message, resolved_recipient: str) -> Message:
        """Persist via the bus with create-or-reuse on the deterministic id.

        A PRIMARY-KEY collision means a creator raced us (crash re-entry,
        concurrent re-invocation) or the row already exists. Reload by the
        derived id: absent → the failure was unrelated, re-raise honestly;
        present → exact semantic equality or a typed corruption refusal
        (frozen plan D13). ``resolved_recipient`` is the driver's own
        resolution — for role-addressed messages the persisted row carries the
        bus-resolved identity while the composed message leaves ``recipient``
        empty for the bus to fill.
        """
        existing = self._store.load_model(Message, message.id)
        if existing is not None:
            self._verify_identity(existing, message, resolved_recipient)
            return existing
        try:
            return self._bus.send(message)
        except BlockingBudgetExhausted:
            existing = self._store.load_model(Message, message.id)
            if existing is None:
                raise
            self._verify_identity(existing, message, resolved_recipient)
            return existing
        except sqlite3.IntegrityError:
            existing = self._store.load_model(Message, message.id)
            if existing is None:
                raise
            self._verify_identity(existing, message, resolved_recipient)
            return existing

    def _driver_message_exists(
        self,
        spec: ConversationSpec,
        *,
        kind: MessageKind,
        hop_index: int,
        attempt_index: int,
        prev_message_id: str | None,
        participant: tuple[str, str | None],
    ) -> bool:
        """Check for an exact deterministic row before spending a new turn."""
        resolved_recipient, recipient_role = participant
        message_id = derive_message_id(
            conversation_key=spec.conversation_key,
            room_id=spec.room_id,
            task_id=spec.task_id,
            hop_index=hop_index,
            attempt_index=attempt_index,
            kind=kind,
            prev_message_id=prev_message_id,
            recipient_role=recipient_role,
            resolved_recipient=resolved_recipient,
        )
        return self._store.load_model(Message, message_id) is not None

    @staticmethod
    def _verify_identity(existing: Message, intended: Message, resolved_recipient: str) -> None:
        """Exact semantic equality at a derived id (frozen plan D13).

        NULL-strict on role provenance and scope; ordered reference equality;
        byte-equal content. The persisted ``recipient`` is compared against the
        driver's own resolution because role-addressed composition leaves it
        to the bus. ``run_id`` must be absent on both sides — driver messages
        never carry authorship runs (D1), and the bus would have rejected a
        prefix sender that did.
        """
        mismatch = (
            existing.sender != intended.sender
            or existing.recipient != resolved_recipient
            or existing.recipient_role != intended.recipient_role
            or existing.type != intended.type
            or existing.blocking != intended.blocking
            or existing.content != intended.content
            or existing.references != intended.references
            or existing.room_id != intended.room_id
            or existing.task_id != intended.task_id
            or existing.reply_to_id is not None
            or existing.run_id is not None
        )
        if mismatch:
            raise DriverIdentityRefusal(
                f"deterministic id '{existing.id}' already exists with different "
                "content/addressing/scope/references/type — identity collision "
                "(frozen plan D13)"
            )

    @staticmethod
    def _reply_type_for(parent_type: MessageType) -> MessageType:
        """Frozen reply-type discipline (frozen plan D12): never inferred."""
        if parent_type is MessageType.CLARIFICATION_REQUEST:
            return MessageType.CLARIFICATION_RESPONSE
        return MessageType.NOTE

    # -- traversal (frozen plan D5/D6/D7) ------------------------------------

    async def start(self, spec: ConversationSpec) -> ConversationResult:
        """Compose the seed and traverse the participant sequence from hop 1.

        The seed is a driver-authored message (kind ``seed``, hop 1, attempt 1)
        carrying the spec's scope and start-only seed fields. Its deterministic
        id makes re-invocation with the same key converge: a crashed or repeated
        ``start()`` reuses the committed seed row (exact semantic match) instead
        of forking a second conversation (frozen plan D13).
        """
        self._validate_spec(spec, for_start=True)
        first = self._resolved_participant(spec.participants[0], 0)
        seed = self._compose_driver_message(
            spec,
            kind=MessageKind.SEED,
            hop_index=1,
            attempt_index=1,
            prev_message_id=None,
            participant=first,
            content=spec.seed_content or "",
            references=[],
            message_type=spec.seed_type,
            blocking=spec.seed_blocking,
        )
        persisted_seed = self._send_driver_message(seed, first[0])
        return await self._traverse(spec, persisted_seed)

    async def resume(self, spec: ConversationSpec, seed_message_id: str) -> ConversationResult:
        """Adopt an existing persisted Message as the immutable root and traverse.

        The seed may be driver-composed (crash/re-entry recovery for
        ``start()``) or foreign (a human- or system-authored message — the
        human-entry path). The persisted seed is authoritative for type,
        blocking, and content; start-only spec fields must be unset (D3).
        Ownership of the seed is decided by the sender + re-derivation contract,
        never by id shape (frozen plan D14), and traversal begins at hop 1:
        the seed itself is delivered through ``deliver_and_reply`` exactly like
        a composed seed (frozen plan D5).
        """
        self._validate_spec(spec, for_start=False)
        seed = self._store.load_model(Message, seed_message_id)
        if seed is None:
            raise ResumeSeedRefusal(f"seed message '{seed_message_id}' does not exist")
        if seed.room_id != spec.room_id or seed.task_id != spec.task_id:
            raise ResumeSeedRefusal(
                f"seed scope (room={seed.room_id!r}, task={seed.task_id!r}) does not "
                f"match spec scope (room={spec.room_id!r}, task={spec.task_id!r})"
            )
        recipient = seed.recipient
        if recipient is None or ":" in recipient:
            raise ResumeSeedRefusal(
                f"seed recipient {recipient!r} is not a deliverable bare logical agent"
            )
        first = self._resolved_participant(spec.participants[0], 0)
        if recipient != first[0]:
            raise ResumeSeedRefusal(
                f"seed recipient {recipient!r} does not match the resolved first "
                f"participant {first[0]!r}"
            )
        self._verify_seed_ownership(spec, seed)
        return await self._traverse(spec, seed)

    def _verify_seed_ownership(self, spec: ConversationSpec, seed: Message) -> None:
        """D14 (7): driver-owned seeds re-derive; foreign seeds use the
        canonical ``foreign:<seed.id>`` key. Never UUID shape."""
        if seed.sender == DRIVER_SENDER:
            expected_id = derive_message_id(
                conversation_key=spec.conversation_key,
                room_id=seed.room_id,
                task_id=seed.task_id,
                hop_index=1,
                attempt_index=1,
                kind=MessageKind.SEED,
                prev_message_id=None,
                recipient_role=seed.recipient_role,
                resolved_recipient=seed.recipient or "",
            )
            if expected_id != seed.id:
                raise ConversationKeyMismatchRefusal(
                    f"seed '{seed.id}' was not composed under conversation key "
                    f"{spec.conversation_key!r} — re-derivation mismatch"
                )
            return
        expected_key = foreign_conversation_key(seed.id)
        if spec.conversation_key != expected_key:
            raise ConversationKeyMismatchRefusal(
                f"foreign seed '{seed.id}' is resumable only under the canonical "
                f"key {expected_key!r}, got {spec.conversation_key!r}"
            )

    async def _traverse(self, spec: ConversationSpec, seed: Message) -> ConversationResult:
        """Walk the participant sequence; hop 1 is the seed (frozen plan D5).

        Bounded by construction: one iteration per participant, at most
        ``MAX_HOP_RETRIES + 1`` delivery attempts per iteration, no other loop.
        Each forward (hop ≥ 2) is composed only after the previous hop's answer
        has been obtained or recovered.
        """
        hops: list[DriverHop] = []
        final_answer: Message | None = None

        for hop_index, address in enumerate(spec.participants, start=1):
            participant = self._resolved_participant(address, hop_index - 1)
            if hop_index == 1:
                terminal = seed
            else:
                assert final_answer is not None
                forward_exists = self._driver_message_exists(
                    spec,
                    kind=MessageKind.FORWARD,
                    hop_index=hop_index,
                    attempt_index=1,
                    prev_message_id=final_answer.id,
                    participant=participant,
                )
                if not forward_exists:
                    refusal = self._turn_budget_preflight(spec)
                    if refusal is not None:
                        return self._stop(
                            seed,
                            hops,
                            StopReason.BUDGET_EXHAUSTED,
                            final_answer,
                            refusal=refusal,
                        )
                terminal = self._compose_and_send_forward(
                    spec, hop_index=hop_index, answer=final_answer, participant=participant
                )

            attempts = 1
            outcome = await self._attempt_delivery(terminal)
            if isinstance(outcome, BudgetExhausted):
                return self._stop(
                    seed,
                    hops,
                    StopReason.BUDGET_EXHAUSTED,
                    final_answer,
                    refusal=outcome,
                )
            if outcome is None:
                return self._stop(seed, hops, StopReason.DELIVERY_PENDING, final_answer)

            # D6 (rev 4 pre-merge amendment): a failed blocking clarification
            # is never retried as a NOTE — the retry's reply would be a NOTE,
            # replacing the canonical clarification_request →
            # clarification_response answering pair (P4.3 D10) and corrupting
            # the unanswered semantics. The hop stops HOP_FAILED and the
            # request stays honestly in ``unanswered_blocking_messages``.
            # Bounded new-Message retries stand for ordinary NOTE parents.
            retryable = terminal.type is not MessageType.CLARIFICATION_REQUEST
            if outcome.reply is None and retryable and attempts <= MAX_HOP_RETRIES:
                failed_terminal = terminal
                failed_outcome = outcome
                failed_run_id = outcome.ask.run.id
                retry_exists = self._driver_message_exists(
                    spec,
                    kind=MessageKind.RETRY,
                    hop_index=hop_index,
                    attempt_index=MAX_HOP_RETRIES + 1,
                    prev_message_id=terminal.id,
                    participant=participant,
                )
                if not retry_exists:
                    refusal = self._turn_budget_preflight(spec)
                    if refusal is not None:
                        hops.append(DriverHop(forward=terminal, outcome=outcome, attempts=attempts))
                        return self._stop(
                            seed,
                            hops,
                            StopReason.BUDGET_EXHAUSTED,
                            final_answer,
                            refusal=refusal,
                        )
                terminal = self._compose_and_send_retry(
                    spec,
                    hop_index=hop_index,
                    failed=terminal,
                    failed_run_id=failed_run_id,
                    participant=participant,
                )
                attempts += 1
                outcome = await self._attempt_delivery(terminal)
                if isinstance(outcome, BudgetExhausted):
                    hops.append(
                        DriverHop(
                            forward=failed_terminal,
                            outcome=failed_outcome,
                            attempts=attempts - 1,
                        )
                    )
                    return self._stop(
                        seed,
                        hops,
                        StopReason.BUDGET_EXHAUSTED,
                        final_answer,
                        refusal=outcome,
                    )
                if outcome is None:
                    return self._stop(seed, hops, StopReason.DELIVERY_PENDING, final_answer)

            if outcome.reply is None:
                hops.append(DriverHop(forward=terminal, outcome=outcome, attempts=attempts))
                return self._stop(seed, hops, StopReason.HOP_FAILED, final_answer)

            final_answer = outcome.reply
            hops.append(DriverHop(forward=terminal, outcome=outcome, attempts=attempts))

        return self._stop(seed, hops, StopReason.SEQUENCE_EXHAUSTED, final_answer)

    @staticmethod
    def _stop(
        seed: Message,
        hops: list[DriverHop],
        stop_reason: StopReason,
        final_answer: Message | None,
        refusal: CommunicationPolicyRefusal | None = None,
    ) -> ConversationResult:
        return ConversationResult(
            seed=seed,
            hops=tuple(hops),
            stop_reason=stop_reason,
            final_answer=final_answer,
            refusal=refusal,
        )

    def _turn_budget_preflight(self, spec: ConversationSpec) -> CommunicationPolicyRefusal | None:
        """Read-only admission before composing a forward or retry."""
        if self._policy is None:
            return None
        try:
            self._policy.check_turn_budget(spec.room_id, spec.task_id)
        except BudgetExhausted as exc:
            return exc
        return None

    async def _attempt_delivery(
        self, message: Message
    ) -> DeliveryReplyOutcome | BudgetExhausted | None:
        """One delivery attempt; ``None`` means the run is still pending.

        A pending delivery (RUNNING run behind a committed binding marker) is
        crash-safe pending re-entry, never retried here (frozen plan D6). A
        ``DuplicateDeliveryRefusal`` surfaces only when a concurrent initiator's
        Tx1 committed between this entry's marker check and its own Tx1 — from
        the loser's perspective the delivery is likewise already initiated and
        its outcome pending, so it maps onto the same stop semantics.
        """
        try:
            return await self._delivery.deliver_and_reply(
                message.id, reply_type=self._reply_type_for(message.type)
            )
        except (DeliveryPendingRefusal, DuplicateDeliveryRefusal):
            return None
        except BudgetExhausted as exc:
            return exc

    def _compose_and_send_forward(
        self,
        spec: ConversationSpec,
        *,
        hop_index: int,
        answer: Message,
        participant: tuple[str, str | None],
    ) -> Message:
        """Hub forward (frozen plan D2): verbatim answer + one provenance ref."""
        forward = self._compose_driver_message(
            spec,
            kind=MessageKind.FORWARD,
            hop_index=hop_index,
            attempt_index=1,
            prev_message_id=answer.id,
            participant=participant,
            content=answer.content,
            references=[f"message:{answer.id}"],
            message_type=MessageType.NOTE,
            blocking=False,
        )
        return self._send_driver_message(forward, participant[0])

    def _compose_and_send_retry(
        self,
        spec: ConversationSpec,
        *,
        hop_index: int,
        failed: Message,
        failed_run_id: str,
        participant: tuple[str, str | None],
    ) -> Message:
        """Bounded retry (frozen plan D6): a NEW message citing the failed attempt."""
        retry = self._compose_driver_message(
            spec,
            kind=MessageKind.RETRY,
            hop_index=hop_index,
            attempt_index=MAX_HOP_RETRIES + 1,
            prev_message_id=failed.id,
            participant=participant,
            content=failed.content,
            references=[f"message:{failed.id}", f"run:{failed_run_id}"],
            message_type=MessageType.NOTE,
            blocking=False,
        )
        return self._send_driver_message(retry, participant[0])

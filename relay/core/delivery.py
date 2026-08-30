"""Relay-mediated message delivery (P4.2 — SPEC §27 Phase 4; App. D.8/D.11-P4).

Binds a persisted, append-only ``Message`` to a concrete recipient Run and
executes that run through the crash-safe, family-blind spine
(:func:`~relay.core.orchestrator.run_ask`) — API-backed and harness-backed
recipients flow through the identical path (heterogeneous day one, App. C.7
P4).

Frozen contracts implemented here (plan rev 3):

* **D7 — delivery service:** load → typed refusals (absent message, non-bare
  recipient, bogus role, unbuildable recipient — nothing persisted) →
  ``AgentRequest`` (role = addressed role, else ``PARTICIPANT``; task/room
  copied; ``context_refs = message.references`` verbatim; prompt = the
  deterministic D15 envelope) → harness recipients bound to a per-delivery
  READ_ONLY instance (D8) → ``run_ask`` with the Tx1 binding hook.
* **D8 — delivery grant:** harness delivery ALWAYS runs on an explicit
  ``READ_ONLY_ACCESS`` instance — the configured profile is downgraded in a
  copy, a missing profile becomes a fresh explicit READ_ONLY profile. If the
  harness cannot honor READ_ONLY, the fail-closed pre-spawn grant translation
  refuses typed and delivery NEVER falls back to the configured grant (one
  attempt, honest failure).
* **D9 — no reply persistence:** delivery writes NO Message rows; the
  recipient's output lands as the spine's ``run_output`` artifact.
* **D10 — binding marker:** ``MESSAGE_DELIVERED`` commits atomically inside
  the delivery run's pre-provider Tx1 (same transaction as ``Run(RUNNING)`` +
  ``run_input`` + ``AGENT_RUN_STARTED``); it asserts a BINDING, never
  success; failed/timeout runs retain it.
* **D13 — at-most-once initiation, unconditional:** the duplicate check and
  the marker insert share the single Tx1 ``BEGIN IMMEDIATE`` boundary, so
  every re-initiation attempt for a delivered Message is a typed refusal with
  zero store delta — after success, failure, and the crash-pending window
  alike. No escape hatch exists in P4.2; redelivery/retry semantics are
  P4.4+ work.
* **D15 — deterministic envelope:** fixed field order (sender, type,
  blocking, content); byte-for-byte assertable; no timestamps, ids, or
  transcript replay; semantic references ride ``AgentRequest.context_refs``
  (no D.10 reconstruction here).

Authority boundary (App. D.8): delivery can only ever create Runs, run
artifacts, run lifecycle events, and MESSAGE_DELIVERED markers — never task
state, evidence, approvals, or decisions. Proven structurally
(``tests/test_architecture.py``) and behaviorally (``tests/test_delivery.py``).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from relay.agents.base import Agent, AgentRequest, AgentRole
from relay.core.agent_factory import AgentFactory
from relay.core.orchestrator import AskOutcome, run_ask
from relay.storage.events import EventLogWriter
from relay.storage.models import EventLogEntry, EventType, Message, Run
from relay.storage.store import SqliteRelayStore

__all__ = [
    "DELIVERY_SENDER",
    "DeliveryOutcome",
    "DeliveryRefusal",
    "DuplicateDeliveryRefusal",
    "MessageDelivery",
]

#: Producer convention (App. A.1): the delivery machinery is a relay:*
#: component authoring the binding markers.
DELIVERY_SENDER = "relay:delivery"

#: Frozen deterministic envelope (frozen plan D15): fixed field order,
#: byte-for-byte assertable, no timestamps/ids, no transcript replay.
#: ``references`` deliberately ride ``AgentRequest.context_refs`` instead.
_DELIVERY_ENVELOPE = (
    "You received a message via the Relay conversation bus.\n"
    "FROM: {sender}\n"
    "TYPE: {message_type}\n"
    "BLOCKING: {blocking}\n"
    "\n"
    "MESSAGE:\n{content}"
)


class DeliveryRefusal(RuntimeError):
    """Typed pre-run refusal: delivery never initiated, nothing persisted."""


class DuplicateDeliveryRefusal(DeliveryRefusal):
    """At-most-once initiation (frozen plan D13): this Message is already
    bound to a Run; re-initiation is refused unconditionally in P4.2."""


@dataclass(frozen=True)
class DeliveryOutcome:
    """The delivery binding plus whatever the spine recorded.

    ``ask.response is None`` means the delivery run FAILED (error is set and
    the marker is retained); ``ask.error is None`` means the recipient run
    succeeded.
    """

    message: Message
    ask: AskOutcome


class MessageDelivery:
    """Deliver one persisted message into one recipient agent run."""

    def __init__(
        self,
        store: SqliteRelayStore,
        writer: EventLogWriter,
        factory: AgentFactory,
    ) -> None:
        self._store = store
        self._writer = writer
        self._factory = factory

    # -- public path ---------------------------------------------------------

    async def deliver(self, message_id: str) -> DeliveryOutcome:
        """Bind ``message_id`` to a fresh recipient run and execute it.

        Pre-run refusals (absent message, non-bare recipient, bogus role,
        unbuildable recipient) and duplicate-initiation vetoes are typed
        exceptions with ZERO store delta. Run failures are NOT refusals —
        the spine records them honestly and the binding marker is retained.
        """
        message = self._store.load_model(Message, message_id)
        if message is None:
            raise DeliveryRefusal(f"message '{message_id}' does not exist")

        recipient = message.recipient
        if recipient is None or ":" in recipient:
            raise DeliveryRefusal(
                f"message '{message_id}' has no deliverable recipient: "
                f"{recipient!r} — recipients are bare logical-agent identities"
            )
        role = self._role_for(message)

        try:
            agent = self._factory.build(recipient)
        except Exception as exc:
            raise DeliveryRefusal(
                f"recipient '{recipient}' cannot be built: {_refusal_reason(exc)}"
            ) from exc

        model = self._factory.model_of(recipient)
        request = AgentRequest(
            prompt=self._envelope(message),
            role=role,
            task_id=message.task_id,
            room_id=message.room_id,
            # D15 pass-through: semantic references stay canonical on the
            # Message row; the raw list rides the existing request channel.
            context_refs=list(message.references),
        )

        ask = await run_ask(
            self._store,
            self._writer,
            self._read_only_variant(agent),
            request,
            model=model,
            agent_name=recipient,
            pre_provider=self._binding_hook(message),
        )
        return DeliveryOutcome(message=message, ask=ask)

    # -- read model ----------------------------------------------------------

    def deliveries_for_message(self, message_id: str) -> tuple[EventLogEntry, ...]:
        """MESSAGE_DELIVERED markers binding this message to runs (read-only).

        The P4.4 driver's loop-detection/observability helper — never an
        override path (frozen plan D13: none exists in P4.2).
        """
        ref = f"message:{message_id}"
        return tuple(
            entry
            for entry in self._store.all_models(
                EventLogEntry,
                "WHERE type = ?",
                [EventType.MESSAGE_DELIVERED.value],
                order_by="sequence ASC",
            )
            if ref in entry.references
        )

    # -- internals -----------------------------------------------------------

    @staticmethod
    def _role_for(message: Message) -> AgentRole:
        """D11: addressed role wins; direct deliveries speak as PARTICIPANT."""
        if message.recipient_role is None:
            return AgentRole.PARTICIPANT
        try:
            return AgentRole(message.recipient_role)
        except ValueError:
            raise DeliveryRefusal(
                f"recipient role {message.recipient_role!r} is not a valid "
                "AgentRole — delivery refused"
            ) from None

    @staticmethod
    def _envelope(message: Message) -> str:
        """D15: deterministic, bounded, fixed field order."""
        return _DELIVERY_ENVELOPE.format(
            sender=message.sender,
            message_type=message.type.value,
            blocking="true" if message.blocking else "false",
            content=message.content,
        )

    def _read_only_variant(self, agent: Agent) -> Agent:
        """D8: harness delivery ALWAYS binds an explicit READ_ONLY instance.

        Configured profile → copied with ``grant=READ_ONLY_ACCESS``
        (executable/args/timeout preserved); missing profile → a fresh
        explicit READ_ONLY profile. NEVER the configured or adapter-default
        write grant, and no fallback when the harness refuses READ_ONLY —
        the fail-closed pre-spawn grant translation refuses typed and this
        is the only attempt. Deliberately NOT unified with the planner
        variant (``orchestrator._planner_for``), which falls back to the
        adapter default when no profile exists — frozen P3.1 behavior.
        """
        from relay.harness.runtime import HarnessAgent
        from relay.harness.types import ExecutionGrantKind

        if not isinstance(agent, HarnessAgent):
            return agent
        if agent._profile is not None:
            profile = agent._profile.model_copy(
                update={"grant": ExecutionGrantKind.READ_ONLY_ACCESS}
            )
        else:
            from relay.context.config import HarnessAgentConfig

            profile = HarnessAgentConfig(grant=ExecutionGrantKind.READ_ONLY_ACCESS)
        return type(agent)(
            settings=agent._settings,
            profile=profile,
            workspace_root=agent._workspace_root,
        )

    def _binding_hook(self, message: Message) -> object:
        """D10/D13/D14 Tx1 hook: bind atomically, veto duplicates.

        Runs INSIDE the delivery run's pre-provider Tx1 (single
        ``BEGIN IMMEDIATE`` boundary): the duplicate check observes every
        committed marker, and the new marker commits with the run row —
        concurrent initiations are serialized by the SQLite write lock.
        """

        def bind(run: Run, _input_artifact: object) -> Iterable[EventLogEntry]:
            if self.deliveries_for_message(message.id):
                raise DuplicateDeliveryRefusal(
                    f"message '{message.id}' is already bound to a run — "
                    "at-most-once delivery initiation (frozen plan D13); "
                    "redelivery/retry semantics are P4.4+ work"
                )
            return [self._marker_for(message, run)]

        return bind

    @staticmethod
    def _marker_for(message: Message, run: Run) -> EventLogEntry:
        """D10 binding marker: bounded metadata, message+run+scope refs."""
        role_note = f" via role '{message.recipient_role}'" if message.recipient_role else ""
        references = [f"message:{message.id}", f"run:{run.id}"]
        if message.room_id:
            references.append(f"room:{message.room_id}")
        if message.task_id:
            references.append(f"task:{message.task_id}")
        return EventLogEntry(
            room_id=message.room_id,
            task_id=message.task_id,
            sender=DELIVERY_SENDER,
            recipient=message.recipient,
            type=EventType.MESSAGE_DELIVERED,
            content=(
                f"{message.type.value} from {message.sender} to "
                f"{message.recipient}{role_note} bound to run {run.id}"
            ),
            references=references,
        )


def _refusal_reason(exc: Exception) -> str:
    """Public-contract errors keep their message; anything else stays type-only.

    Registry vocabulary never appears here: ``RegistryAgentFactory``
    normalizes its refusals to ``ConfigError`` before they reach this path,
    so delivery depends only on the AgentFactory seam and neutral errors
    (frozen plan D6 — core must not import the adapter registry).
    """
    from relay.agents.errors import AgentError, AgentNotConfigured
    from relay.context.config import ConfigError

    if isinstance(exc, (AgentError, AgentNotConfigured, ConfigError)):
        return str(exc)
    return f"unexpected factory failure ({type(exc).__name__})"

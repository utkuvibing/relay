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
  success; failed/timeout runs retain it. Room-scoped deliveries re-check the
  persisted Room's OPEN fence in that transaction before the binding commits.
* **D13 — at-most-once initiation, unconditional:** the duplicate check and
  the marker insert share the single Tx1 ``BEGIN IMMEDIATE`` boundary, so
  every re-initiation attempt for a delivered Message is a typed refusal with
  zero store delta — after success, failure, and the crash-pending window
  alike. No escape hatch exists in P4.2; redelivery/retry semantics are
  P4.4+ work.
* **P7.4 — session continuation fallback:** when a delivery carried a
  resume ref and the harness POSITIVELY rejected it (typed
  ``SessionResumeUnavailable`` — never arbitrary provider failure), the same
  initiation continues exactly once on a fresh run prompted from canonical
  context. The fallback run binds via a ``MESSAGE_DELIVERY_FALLBACK``
  marker (distinct type: initiation accounting still counts one
  ``MESSAGE_DELIVERED``), and reply materialization/recovery resolve
  through it to the fallback run. Eligibility is durable: the spine records
  ``RUN_SESSION_RESUME_REJECTED`` in the failed run's own failure
  transaction, so a crash before the fallback's Tx1 leaves the continuation
  still owed — recovery runs it exactly once (a second binding is vetoed
  inside the fallback run's Tx1 and resolved to the committed winner).
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

import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from relay.agents.base import Agent, AgentRequest, AgentResponse, AgentRole
from relay.core.agent_factory import AgentFactory
from relay.core.bus import (
    DEFAULT_MAX_THREAD_DEPTH,
    ConversationBus,
    ReplyRejected,
    RoundTripLimitExceeded,
)
from relay.core.orchestrator import AskOutcome, run_ask
from relay.core.policy import (
    REPLY_ADMISSION_REFERENCE_PREFIX,
    CommunicationPolicyGate,
    PolicyEnvelope,
    principal_for_sender,
    reply_admission_reference,
)
from relay.core.protocols import ProtocolDefinition, StageContext
from relay.core.rooms import require_open_room
from relay.core.stage_policy import StageContextRefusal, stage_admission
from relay.storage.events import EventLogWriter
from relay.storage.models import (
    Artifact,
    ArtifactKind,
    EventLogEntry,
    EventType,
    Message,
    MessageType,
    Run,
    RunStatus,
)
from relay.storage.store import SqliteRelayStore

__all__ = [
    "DELIVERY_SENDER",
    "DeliveryOutcome",
    "DeliveryPendingRefusal",
    "DeliveryRefusal",
    "DeliveryReplyOutcome",
    "DuplicateDeliveryRefusal",
    "DuplicateFallbackRefusal",
    "InvalidReplyTypeRefusal",
    "MessageDelivery",
    "ThreadDepthRefusal",
    "latest_session_ref",
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


class DeliveryPendingRefusal(DeliveryRefusal):
    """Delivery run is currently pending or incomplete; re-initiation is refused."""


class DuplicateFallbackRefusal(DeliveryRefusal):
    """Exactly one fresh fallback may bind to a delivered message (P7.4).

    Raised inside the fallback run's Tx1 when a ``MESSAGE_DELIVERY_FALLBACK``
    binding already exists — a concurrent continuation won the write-lock
    race. The veto rolls the loser's staged run back atomically, so no
    second fallback run is ever persisted.
    """


class InvalidReplyTypeRefusal(DeliveryRefusal, ValueError):
    """Explicit reply_type required or invalid reply_type for delivery."""


class ThreadDepthRefusal(DeliveryRefusal, RoundTripLimitExceeded):
    """Prospective reply exceeds thread depth ceiling or has cyclic ancestry."""


@dataclass(frozen=True)
class DeliveryOutcome:
    """The delivery binding plus whatever the spine recorded.

    ``ask.response is None`` means the delivery run FAILED (error is set and
    the marker is retained); ``ask.error is None`` means the recipient run
    succeeded.
    """

    message: Message
    ask: AskOutcome


@dataclass(frozen=True)
class DeliveryReplyOutcome:
    """Outcome of a deliver_and_reply call.

    ``reply`` is None if the recipient run failed or was refused.
    """

    message: Message
    ask: AskOutcome
    reply: Message | None = None


def latest_session_ref(
    store: SqliteRelayStore, room_id: str, agent_name: str, role: str
) -> str | None:
    """Newest persisted ``external_session_ref`` for one Room SEAT (P7.4).

    Seat-scoped, not agent-scoped: one configured agent may occupy several
    Room roles, and each seat owns a distinct external conversation. The
    scan walks delivery markers (``MESSAGE_DELIVERED`` plus the P7.4
    ``MESSAGE_DELIVERY_FALLBACK`` continuation markers) in reverse sequence
    order and accepts a marker only when canonical provenance lines up:

    * the marker's ``message:`` ref resolves to a Message addressed to this
      seat — ``recipient_role == role`` AND resolved ``recipient ==
      agent_name`` (the bus's persisted role→agent resolution);
    * the marker's ``run:`` ref resolves to a Run whose ``agent``/``role``
      agree — defense against a marker bound to a stale or corrupted row;
    * that Run carries a non-empty ``external_session_ref``.

    Anything else is skipped, never inherited across roles. Pure read —
    no writes, no handle-shape validation (callers validate via the
    adapter's ``resume_arguments`` before use).
    """
    delivered = EventType.MESSAGE_DELIVERED.value
    fallback = EventType.MESSAGE_DELIVERY_FALLBACK.value
    for marker in store.all_models(
        EventLogEntry,
        "WHERE type IN (?, ?) AND room_id = ?",
        [delivered, fallback, room_id],
        order_by="sequence DESC",
    ):
        if marker.recipient != agent_name:
            continue
        message_id: str | None = None
        run_id: str | None = None
        for ref in marker.references:
            if ref.startswith("message:"):
                message_id = ref[len("message:") :]
            elif ref.startswith("run:"):
                run_id = ref[len("run:") :]
        if message_id is None or run_id is None:
            continue
        message = store.load_model(Message, message_id)
        if (
            message is None
            or message.recipient_role != role
            or message.recipient != agent_name
        ):
            continue
        run = store.load_model(Run, run_id)
        if (
            run is not None
            and run.agent == agent_name
            and run.role == role
            and run.external_session_ref
        ):
            return run.external_session_ref
    return None


class MessageDelivery:
    """Deliver one persisted message into one recipient agent run."""

    def __init__(
        self,
        store: SqliteRelayStore,
        writer: EventLogWriter,
        factory: AgentFactory,
        bus: ConversationBus | None = None,
        policy: CommunicationPolicyGate | None = None,
        *,
        stage_context: StageContext | None = None,
        protocol: ProtocolDefinition | None = None,
    ) -> None:
        self._store = store
        self._writer = writer
        self._factory = factory
        self._prepared_recipients: dict[str, tuple[Agent, str | None]] = {}
        self._bus = bus if bus is not None else ConversationBus(
            store, writer, policy=policy, stage_context=stage_context, protocol=protocol
        )
        self._policy = policy if policy is not None else self._bus.policy
        self._stage = stage_admission(stage_context, protocol, self._policy)
        bus_stage = getattr(self._bus, "_stage", None)
        if (self._stage is not None or bus_stage is not None) and (
            self._stage is None or bus_stage is None
            or self._stage.context != bus_stage.context
            or self._stage.definition != bus_stage.definition
            or self._policy is not self._bus.policy
        ):
            raise StageContextRefusal("delivery and bus must share stage context, definition, gate")

    # -- public path ---------------------------------------------------------

    def prepare_recipient(self, recipient: str) -> None:
        """Construct and retain the exact read-only agent used by the next delivery."""
        self._prepared_recipients[recipient] = self._construct_recipient(recipient)

    def recipient_for_inspection(self, recipient: str) -> Agent | None:
        """The delivery-bound recipient instance for read-only capability checks.

        Returns the prepared recipient when one was staged via
        :meth:`prepare_recipient` (the SAME instance the next delivery will
        pop — capability/persist-flag inspection must never probe a second
        independently-constructed agent). When nothing was prepared the
        recipient is constructed on demand; an unbuildable recipient yields
        ``None`` (the subsequent delivery will surface the typed refusal).
        """
        prepared = self._prepared_recipients.get(recipient)
        if prepared is not None:
            return prepared[0]
        try:
            agent, _model = self._construct_recipient(recipient)
        except DeliveryRefusal:
            return None
        return agent

    async def deliver(
        self,
        message_id: str,
        *,
        prompt_suffix: str = "",
        fallback_prompt_suffix: str | None = None,
        resume_session_ref: str | None = None,
        extra_context_refs: list[str] | None = None,
    ) -> DeliveryOutcome:
        """Bind ``message_id`` to a fresh recipient run and execute it.

        Pre-run refusals (absent message, non-bare recipient, bogus role,
        unbuildable recipient) and duplicate-initiation vetoes are typed
        exceptions with ZERO store delta. Run failures are NOT refusals —
        the spine records them honestly and the binding marker is retained.

        ``prompt_suffix`` rides OUTSIDE the frozen D15 envelope (the P6.4
        appendix pattern); it defaults to empty, so ordinary delivery prompts
        stay byte-identical. ``resume_session_ref`` rides
        ``AgentRequest.metadata`` to SESSION_RESUME-capable harness agents;
        it defaults to None (honest fresh run, byte-identical behavior).
        ``fallback_prompt_suffix`` is the suffix for the one-time fresh run
        used ONLY when a resume attempt is positively rejected (P7.4); it
        defaults to ``prompt_suffix`` — callers supplying a resume ref
        should supply both so a fallback prompt never claims a resume that
        did not happen. ``extra_context_refs`` appends caller-derived
        provenance (P7.4 Room context) after the verbatim message
        references; it defaults to empty so existing
        ``context_refs == message.references`` assertions hold.
        """
        return await self._deliver(
            message_id,
            admitted_reply_type=None,
            prompt_suffix=prompt_suffix,
            fallback_prompt_suffix=fallback_prompt_suffix,
            resume_session_ref=resume_session_ref,
            extra_context_refs=extra_context_refs,
        )

    async def _deliver(
        self,
        message_id: str,
        *,
        admitted_reply_type: MessageType | None,
        prompt_suffix: str = "",
        fallback_prompt_suffix: str | None = None,
        resume_session_ref: str | None = None,
        extra_context_refs: list[str] | None = None,
    ) -> DeliveryOutcome:
        """Initiate delivery, optionally binding a pre-admitted reply type."""
        message = self._store.load_model(Message, message_id)
        if message is None:
            raise DeliveryRefusal(f"message '{message_id}' does not exist")

        self._check_stage_record(message)
        if self._stage is not None:
            envelope = self._bus.policy_envelope(message, self._bus.validate_authorship(message))
            self._stage.check_edge(envelope)
            assert self._policy is not None  # stage admission implies a stage gate
            self._policy.check_edge(envelope)

        recipient = message.recipient
        if recipient is None or ":" in recipient:
            raise DeliveryRefusal(
                f"message '{message_id}' has no deliverable recipient: "
                f"{recipient!r} — recipients are bare logical-agent identities"
            )
        role = self._role_for(message)

        prepared = self._prepared_recipients.pop(recipient, None)
        agent, model = prepared if prepared is not None else self._construct_recipient(recipient)
        metadata: dict[str, object] = {}
        if resume_session_ref is not None:
            self._validate_resume_ref(agent, recipient, resume_session_ref)
            metadata["resume_session_ref"] = resume_session_ref
        request = AgentRequest(
            prompt=self._envelope(message) + prompt_suffix,
            role=role,
            task_id=message.task_id,
            room_id=message.room_id,
            # D15 pass-through: semantic references stay canonical on the
            # Message row; the raw list rides the existing request channel.
            # P7.4 appends caller-derived Room-context provenance after the
            # verbatim references (empty by default — existing assertions hold).
            context_refs=[*message.references, *(extra_context_refs or [])],
            metadata=metadata,
        )

        ask = await run_ask(
            self._store,
            self._writer,
            agent,
            request,
            model=model,
            agent_name=recipient,
            pre_provider=self._binding_hook(message, admitted_reply_type),
        )
        if (
            resume_session_ref is not None
            and ask.response is None
            and _is_session_resume_rejection(ask.cause)
        ):
            # P7.4 honest fallback (App. D.10): the harness POSITIVELY
            # rejected the continuation handle — the typed cause, never a
            # text match, and only when a resume ref was actually sent.
            # Exactly one fresh run follows, prompted from the canonical
            # records already reconstructed for this delivery; arbitrary
            # provider failures never reach this branch. The fresh run is
            # bound by a MESSAGE_DELIVERY_FALLBACK marker inside ITS Tx1 —
            # the same initiation, so at-most-once accounting still sees
            # exactly one MESSAGE_DELIVERED binding for this message.
            fallback_request = request.model_copy(
                update={
                    "prompt": self._envelope(message)
                    + (
                        fallback_prompt_suffix
                        if fallback_prompt_suffix is not None
                        else prompt_suffix
                    ),
                    "metadata": {},
                }
            )
            try:
                ask = await run_ask(
                    self._store,
                    self._writer,
                    agent,
                    fallback_request,
                    model=model,
                    agent_name=recipient,
                    pre_provider=self._fallback_hook(message, ask.run),
                )
            except DuplicateFallbackRefusal as exc:
                # A concurrent recovery bound the owed fallback between our
                # failed resume attempt and this run's Tx1 — the veto rolled
                # our staged run back, so exactly one fallback exists. The
                # continuation is in progress elsewhere; re-entry resolves it.
                raise DeliveryPendingRefusal(
                    f"message '{message.id}' continuation was bound to a "
                    "fallback run by a concurrent recovery — re-invoke to "
                    "resolve its outcome"
                ) from exc
        return DeliveryOutcome(message=message, ask=ask)

    def _construct_recipient(self, recipient: str) -> tuple[Agent, str | None]:
        try:
            agent = self._read_only_variant(self._factory.build(recipient))
            model = self._factory.model_of(recipient)
        except Exception as exc:
            raise DeliveryRefusal(
                f"recipient '{recipient}' cannot be built: {_refusal_reason(exc)}"
            ) from exc
        return agent, model

    @staticmethod
    def _validate_resume_ref(agent: Agent, recipient: str, session_ref: str) -> None:
        """Pre-Tx1 resume validation (P7.4): capability + shape, zero delta.

        Only harness agents declaring SESSION_RESUME may resume, and the
        handle must survive the adapter's own ``resume_arguments`` shape
        check. Anything else is a typed refusal BEFORE the binding Tx1 —
        never a silent fresh run.
        """
        from relay.harness.capabilities import HarnessCapability
        from relay.harness.errors import UnsupportedCapability
        from relay.harness.runtime import HarnessAgent

        if not isinstance(agent, HarnessAgent):
            raise DeliveryRefusal(
                f"recipient '{recipient}' cannot resume an external session: "
                "API-family agents always run fresh from canonical records"
            )
        if HarnessCapability.SESSION_RESUME not in agent.capabilities_set():
            raise DeliveryRefusal(
                f"recipient '{recipient}' does not declare session_resume — "
                "honest fresh run required"
            )
        try:
            agent.resume_arguments(session_ref)
        except UnsupportedCapability as exc:
            raise DeliveryRefusal(f"invalid session reference: {exc}") from exc

    async def deliver_and_reply(
        self,
        message_id: str,
        *,
        reply_type: MessageType | None = None,
        max_thread_depth: int = DEFAULT_MAX_THREAD_DEPTH,
        prompt_suffix: str = "",
        fallback_prompt_suffix: str | None = None,
        resume_session_ref: str | None = None,
        extra_context_refs: list[str] | None = None,
    ) -> DeliveryReplyOutcome:
        """P4.3 (frozen plan D12-D15): deliver message and materialize reply idempotently.

        Enforces:
        - Parent message deliverability (bare logical-agent recipient).
        - Preflight validation of prospective reply_type and thread depth / ancestry
          BEFORE delivery initiation or provider execution.
        - Reuses existing run if delivery already occurred (crash recovery for both
          SUCCEEDED and FAILED runs, preserving sanitized failure error).
        - P7.4 crash window: a FAILED run carrying canonical
          ``RUN_SESSION_RESUME_REJECTED`` eligibility but no committed
          fallback binding still owes the same initiation's one-time fresh
          run — recovery executes it (never a second initiation, never a
          second fallback, never a retry of arbitrary failures).
        - Dual provenance verification: run.agent == reply.sender AND causal
          MESSAGE_DELIVERED(message, run) binding.
        - Reconstructs full AskOutcome(run=run, response=recovered_response) on recovery.
        - Repeated calls return existing reply with zero store delta.
        - Uniqueness constraint prevents duplicate reply generation under concurrency.
        """
        message = self._store.load_model(Message, message_id)
        if message is None:
            raise DeliveryRefusal(f"message '{message_id}' does not exist")

        self._check_stage_record(message)

        recipient = message.recipient
        if recipient is None or ":" in recipient:
            raise DeliveryRefusal(
                f"message '{message_id}' has no deliverable recipient: "
                f"{recipient!r} — recipients are bare logical-agent identities"
            )

        # Preflight 1: resolve/validate prospective reply_type BEFORE delivery initiation
        actual_reply_type = self._resolve_reply_type(message.type, reply_type)

        # Preflight 2: validate prospective thread depth & cycle safety BEFORE delivery initiation
        self._preflight_thread_depth(message, max_thread_depth)

        deliveries = self.deliveries_for_message(message_id)
        if not deliveries and self._policy is not None:
            delivery_role = self._role_for(message)
            self._check_reply_edge(message, actual_reply_type, delivery_role)
        if deliveries:
            if self._stage is not None:
                if len(deliveries) != 1:
                    raise StageContextRefusal("stage request has contradictory delivery bindings")
                self._stage.check_record(deliveries[0])
            marker = deliveries[0]
            run_id = None
            for ref in marker.references:
                if ref.startswith("run:"):
                    run_id = ref[4:]
                    break
            if run_id is None:
                raise DeliveryRefusal(
                    f"corrupt delivery marker seq={marker.sequence!r}: missing run reference"
                )

            run = self._store.load_model(Run, run_id)
            if run is None:
                raise DeliveryRefusal(f"delivery run '{run_id}' not found in store")

            if self._stage is not None and (
                run.agent != message.recipient or run.role != self._role_for(message).value
                or run.task_id != message.task_id or marker.sender != DELIVERY_SENDER
                or marker.recipient != message.recipient
                or [ref for ref in marker.references if ref.startswith("run:")] != [f"run:{run.id}"]
                or [ref for ref in marker.references if ref.startswith("message:")]
                != [f"message:{message.id}"]
            ):
                raise StageContextRefusal("stage delivery binding has contradictory provenance")

            # Causal provenance: verify marker references parent message and run
            if (
                f"message:{message.id}" not in marker.references
                or f"run:{run.id}" not in marker.references
            ):
                raise DeliveryRefusal("corrupt causal provenance: marker references mismatch")

            if run.status is RunStatus.RUNNING:
                raise DeliveryPendingRefusal(
                    f"delivery run '{run.id}' for message '{message.id}' is still in progress or incomplete"
                )

            if run.status is RunStatus.FAILED:
                # P7.4: a rejected resume attempt may have continued on a
                # fresh fallback run — recover THAT run's outcome so the
                # honest reply materializes instead of a stale failure.
                fallback_run = self._fallback_run_for(message.id, run.id)
                if fallback_run is None and self._resume_rejection_proven(run):
                    # Crash window: the RUN_SESSION_RESUME_REJECTED marker
                    # committed atomically with the FAILED update, but the
                    # process died before the continuation's Tx1. The same
                    # initiation's one-time fresh fallback is still owed —
                    # run it now. A concurrent recovery binding resolves to
                    # the winner's run, never a second fallback.
                    fallback_run = await self._continue_fallback(
                        message,
                        run,
                        recipient,
                        prompt_suffix=prompt_suffix,
                        fallback_prompt_suffix=fallback_prompt_suffix,
                        extra_context_refs=extra_context_refs,
                    )
                if fallback_run is None:
                    error = self._error_for_failed_run(run.id, run.agent)
                    return DeliveryReplyOutcome(
                        message=message,
                        ask=AskOutcome(run=run, error=error),
                        reply=None,
                    )
                run = fallback_run
                if run.status is RunStatus.RUNNING:
                    raise DeliveryPendingRefusal(
                        f"fallback delivery run '{run.id}' for message "
                        f"'{message.id}' is still in progress or incomplete"
                    )
                if run.status is RunStatus.FAILED:
                    error = self._error_for_failed_run(run.id, run.agent)
                    return DeliveryReplyOutcome(
                        message=message,
                        ask=AskOutcome(run=run, error=error),
                        reply=None,
                    )
                if run.status is not RunStatus.SUCCEEDED:
                    raise DeliveryRefusal(
                        f"fallback delivery run '{run.id}' in unhandled status '{run.status.value}'"
                    )

            if run.status is not RunStatus.SUCCEEDED:
                raise DeliveryRefusal(
                    f"delivery run '{run.id}' in unhandled status '{run.status.value}'"
                )

            output_artifacts = self._store.artifacts_for_run(run.id, kind=ArtifactKind.RUN_OUTPUT)
            output_content = (output_artifacts[0].content or "") if output_artifacts else ""
            recovered_response = AgentResponse(
                output=output_content,
                agent=run.agent,
                role=self._role_for(message),
            )
            ask = AskOutcome(run=run, response=recovered_response)

            # Idempotency check: if reply already exists for this (message.id, run.id)
            existing_replies = [m for m in self._bus.replies_for(message.id) if m.run_id == run.id]
            if existing_replies:
                existing_reply = self._existing_reply_for_type(
                    existing_replies, actual_reply_type
                )
                self._marker_admits_reply_type(marker, actual_reply_type)
                self._check_stage_record(existing_reply)
                if self._stage is not None and (
                    existing_reply.sender != message.recipient
                    or existing_reply.recipient != message.sender or existing_reply.blocking
                ):
                    raise StageContextRefusal("existing stage reply contradicts its parent")
                return DeliveryReplyOutcome(message=message, ask=ask, reply=existing_reply)

            # Authorship verification
            if run.agent != message.recipient:
                raise DeliveryRefusal(
                    f"authorship violation: delivery run belongs to {run.agent!r}, not recipient {message.recipient!r}"
                )

            if self._policy is not None:
                self._check_recovery_reply_admission(
                    marker, message, actual_reply_type, self._role_for(message)
                )

            reply = self._build_reply(message, run, output_content, actual_reply_type)
            try:
                saved_reply = self._bus.send(reply, max_thread_depth=max_thread_depth)
            except sqlite3.IntegrityError:
                existing = [m for m in self._bus.replies_for(message.id) if m.run_id == run.id]
                if existing:
                    existing_reply = self._existing_reply_for_type(
                        existing, actual_reply_type
                    )
                    return DeliveryReplyOutcome(message=message, ask=ask, reply=existing_reply)
                raise

            return DeliveryReplyOutcome(message=message, ask=ask, reply=saved_reply)

        # Fresh delivery
        outcome = await self._deliver(
            message_id,
            admitted_reply_type=(
                actual_reply_type if self._policy is not None else None
            ),
            prompt_suffix=prompt_suffix,
            fallback_prompt_suffix=fallback_prompt_suffix,
            resume_session_ref=resume_session_ref,
            extra_context_refs=extra_context_refs,
        )
        if outcome.ask.response is None:
            return DeliveryReplyOutcome(message=message, ask=outcome.ask, reply=None)

        run = outcome.ask.run
        if run.agent != message.recipient:
            raise DeliveryRefusal(
                f"authorship violation: delivery run belongs to {run.agent!r}, not recipient {message.recipient!r}"
            )

        reply = self._build_reply(message, run, outcome.ask.response.output, actual_reply_type)
        try:
            saved_reply = self._bus.send(reply, max_thread_depth=max_thread_depth)
        except sqlite3.IntegrityError:
            existing = [m for m in self._bus.replies_for(message.id) if m.run_id == run.id]
            if existing:
                existing_reply = self._existing_reply_for_type(existing, actual_reply_type)
                return DeliveryReplyOutcome(
                    message=message, ask=outcome.ask, reply=existing_reply
                )
            raise

        return DeliveryReplyOutcome(message=message, ask=outcome.ask, reply=saved_reply)

    def _preflight_thread_depth(self, parent: Message, max_thread_depth: int) -> None:
        try:
            self._bus.preflight_reply_depth(parent, max_thread_depth=max_thread_depth)
        except RoundTripLimitExceeded as exc:
            raise ThreadDepthRefusal(str(exc)) from exc
        except ReplyRejected as exc:
            raise ThreadDepthRefusal(f"preflight thread ancestry check failed: {exc}") from exc

    def _check_reply_edge(
        self, message: Message, reply_type: MessageType, delivery_role: AgentRole
    ) -> None:
        """Admit a fresh delivery's prospective reply before provider I/O."""
        author_role: str | None = None
        if not message.sender.startswith(("human:", "relay:")):
            if message.run_id is None:
                raise DeliveryRefusal(
                    f"bare sender {message.sender!r} has no authorship Run; "
                    "policy reply admission requires persisted role provenance"
                )
            author_run = self._store.load_model(Run, message.run_id)
            if author_run is None:
                raise DeliveryRefusal(
                    f"authorship Run {message.run_id!r} for sender "
                    f"{message.sender!r} does not exist"
                )
            if author_run.agent != message.sender:
                raise DeliveryRefusal(
                    f"authorship Run {message.run_id!r} belongs to "
                    f"{author_run.agent!r}, not sender {message.sender!r}"
                )
            author_role = author_run.role
        try:
            recipient = principal_for_sender(message.sender, run_role=author_role)
        except (TypeError, ValueError) as exc:
            raise DeliveryRefusal(f"invalid sender policy provenance: {exc}") from exc
        envelope = PolicyEnvelope(
            sender=delivery_role,
            recipient=recipient,
            type=reply_type,
            blocking=False,
            room_id=message.room_id,
            task_id=message.task_id,
        )
        if self._stage is not None:
            self._stage.check_edge(envelope)
        # Both callers invoke this only when self._policy is not None.
        assert self._policy is not None
        self._policy.check_edge(envelope)

    def _check_stage_record(self, message: Message) -> None:
        if self._stage is not None:
            self._stage.check_record(message)
        elif message.stage_key is not None:
            raise StageContextRefusal("unscoped delivery refuses stage-tagged traffic")

    def _check_recovery_reply_admission(
        self,
        marker: EventLogEntry,
        message: Message,
        reply_type: MessageType,
        delivery_role: AgentRole,
    ) -> None:
        """Trust an exact prior admission or re-evaluate a standalone delivery."""
        if self._marker_admits_reply_type(marker, reply_type):
            return
        self._check_reply_edge(message, reply_type, delivery_role)

    @staticmethod
    def _marker_admits_reply_type(
        marker: EventLogEntry, reply_type: MessageType
    ) -> bool:
        """Return typed admission presence and reject contradictory marker claims."""
        admission_refs = [
            ref for ref in marker.references if ref.startswith(REPLY_ADMISSION_REFERENCE_PREFIX)
        ]
        if not admission_refs:
            return False
        expected = reply_admission_reference(reply_type)
        if admission_refs != [expected]:
            admitted = ", ".join(
                repr(ref.removeprefix(REPLY_ADMISSION_REFERENCE_PREFIX))
                for ref in admission_refs
            )
            raise DeliveryRefusal(
                f"delivery marker at sequence {marker.sequence!r} admitted reply type "
                f"{admitted}, not reply type {reply_type.value!r}"
            )
        return True

    @staticmethod
    def _existing_reply_for_type(
        existing_replies: list[Message], requested_reply_type: MessageType
    ) -> Message:
        """Return the idempotent reply only when it matches its admission."""
        existing_reply = existing_replies[0]
        if existing_reply.type is not requested_reply_type:
            raise DeliveryRefusal(
                f"existing reply type {existing_reply.type.value!r} does not match "
                f"requested reply type {requested_reply_type.value!r}"
            )
        if existing_reply.blocking:
            raise DeliveryRefusal(
                "existing delivery-bound reply must be non-blocking"
            )
        return existing_reply

    def _error_for_failed_run(self, run_id: str, agent_name: str) -> str:
        """Extract the sanitized failure error from AGENT_RUN_FINISHED event."""
        run_ref = f"run:{run_id}"
        for entry in self._store.all_models(
            EventLogEntry,
            "WHERE type = ?",
            [EventType.AGENT_RUN_FINISHED.value],
            order_by="sequence DESC",
        ):
            if run_ref in entry.references:
                prefix = f"agent '{agent_name}' failed: "
                if entry.content.startswith(prefix):
                    return entry.content[len(prefix) :]
                return entry.content
        return "delivery run failed"

    @staticmethod
    def _resolve_reply_type(
        parent_type: MessageType, reply_type: MessageType | None
    ) -> MessageType:
        if reply_type is not None:
            if reply_type is MessageType.SYSTEM:
                raise InvalidReplyTypeRefusal(
                    "MessageType.SYSTEM is reserved for relay-authored system messages"
                )
            return reply_type
        if parent_type is MessageType.CLARIFICATION_REQUEST:
            return MessageType.CLARIFICATION_RESPONSE
        raise InvalidReplyTypeRefusal(
            f"explicit reply_type required when replying to message of type '{parent_type.value}'"
        )

    @staticmethod
    def _build_reply(
        parent: Message,
        run: Run,
        content: str,
        reply_type: MessageType,
    ) -> Message:
        assert parent.recipient is not None
        return Message(
            stage_key=parent.stage_key,
            sender=parent.recipient,
            recipient=parent.sender,
            reply_to_id=parent.id,
            run_id=run.id,
            room_id=parent.room_id,
            task_id=parent.task_id,
            type=reply_type,
            content=content,
            blocking=False,
        )

    # -- read model ----------------------------------------------------------

    def _fallback_run_for(self, message_id: str, prior_run_id: str) -> Run | None:
        """Resolve the one-time fresh fallback run for a failed resume attempt.

        Finds the ``MESSAGE_DELIVERY_FALLBACK`` marker binding this message
        whose ``prior_run:`` ref is the failed resume run, then loads the
        ``run:`` ref. Returns ``None`` when no fallback was ever committed —
        the failed run's honest outcome stands.
        """
        message_ref = f"message:{message_id}"
        prior_ref = f"prior_run:{prior_run_id}"
        for entry in self._store.all_models(
            EventLogEntry,
            "WHERE type = ?",
            [EventType.MESSAGE_DELIVERY_FALLBACK.value],
            order_by="sequence ASC",
        ):
            if message_ref not in entry.references or prior_ref not in entry.references:
                continue
            run_id = next(
                (ref[len("run:") :] for ref in entry.references if ref.startswith("run:")),
                None,
            )
            return self._store.load_model(Run, run_id) if run_id is not None else None
        return None

    def _resume_rejection_proven(self, run: Run) -> bool:
        """Canonical proof the failed run's one-time fallback is still owed.

        ``RUN_SESSION_RESUME_REJECTED`` commits in the SAME transaction that
        settles the run FAILED (orchestrator failure path), so its presence
        is durable typed evidence — never error-text parsing — that the
        primary failure was specifically a positive session-continuation
        rejection. Absent the marker the failed delivery stays terminal:
        arbitrary provider failures are never retried, even after a crash.
        """
        run_ref = f"run:{run.id}"
        return any(
            run_ref in entry.references
            for entry in self._store.all_models(
                EventLogEntry,
                "WHERE type = ?",
                [EventType.RUN_SESSION_RESUME_REJECTED.value],
                order_by="sequence ASC",
            )
        )

    async def _continue_fallback(
        self,
        message: Message,
        prior_run: Run,
        recipient: str,
        *,
        prompt_suffix: str,
        fallback_prompt_suffix: str | None,
        extra_context_refs: list[str] | None,
    ) -> Run:
        """Run the owed one-time fresh fallback for a proven resume rejection.

        Returns the bound fallback run — this call's, or the winner's when a
        concurrent recovery committed the binding first (the Tx1 veto rolls
        our staged run back atomically and we resolve the committed one).
        The prompt is the same envelope plus the caller's fallback suffix —
        a genuinely fresh request: no resume metadata, never a false
        ``resumed:`` claim.
        """
        prepared = self._prepared_recipients.pop(recipient, None)
        agent, model = (
            prepared if prepared is not None else self._construct_recipient(recipient)
        )
        request = AgentRequest(
            prompt=self._envelope(message)
            + (fallback_prompt_suffix if fallback_prompt_suffix is not None else prompt_suffix),
            role=self._role_for(message),
            task_id=message.task_id,
            room_id=message.room_id,
            context_refs=[*message.references, *(extra_context_refs or [])],
            metadata={},
        )
        try:
            ask = await run_ask(
                self._store,
                self._writer,
                agent,
                request,
                model=model,
                agent_name=recipient,
                pre_provider=self._fallback_hook(message, prior_run),
            )
        except DuplicateFallbackRefusal:
            winner = self._fallback_run_for(message.id, prior_run.id)
            if winner is None:
                raise DeliveryRefusal(
                    f"corrupt delivery state: fallback veto for message "
                    f"'{message.id}' but no committed fallback binding exists"
                )
            return winner
        return ask.run

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
        if agent.profile is not None:
            profile = agent.profile.model_copy(
                update={"grant": ExecutionGrantKind.READ_ONLY_ACCESS}
            )
        else:
            from relay.context.config import HarnessAgentConfig

            profile = HarnessAgentConfig(grant=ExecutionGrantKind.READ_ONLY_ACCESS)
        return type(agent)(
            settings=agent.settings,
            profile=profile,
            workspace_root=agent.workspace_root,
        )

    def _binding_hook(
        self,
        message: Message,
        admitted_reply_type: MessageType | None,
    ) -> Callable[[Run, Artifact], Iterable[EventLogEntry]]:
        """D10/D13/D14/P7.2 Tx1 hook: fence and bind atomically.

        Runs INSIDE the delivery run's pre-provider Tx1 (single
        ``BEGIN IMMEDIATE`` boundary): the Room OPEN check and duplicate check
        observe committed state, and the new marker commits with the run row.
        Concurrent closes and delivery initiations serialize on the SQLite
        write lock; a refusal rolls back the entire staged run.
        """

        def bind(run: Run, _input_artifact: object) -> Iterable[EventLogEntry]:
            if message.room_id is not None:
                require_open_room(self._store, message.room_id)
            if self.deliveries_for_message(message.id):
                raise DuplicateDeliveryRefusal(
                    f"message '{message.id}' is already bound to a run — "
                    "at-most-once delivery initiation (frozen plan D13); "
                    "redelivery/retry semantics are P4.4+ work"
                )
            if self._policy is not None:
                self._policy.check_turn_budget(message.room_id, message.task_id)
                if self._stage is not None:
                    self._stage.check_turn_budget()
            return [self._marker_for(message, run, admitted_reply_type)]

        return bind

    def _fallback_hook(
        self,
        message: Message,
        prior_run: Run,
    ) -> Callable[[Run, Artifact], Iterable[EventLogEntry]]:
        """P7.4 Tx1 hook for the one-time fresh fallback run.

        Runs INSIDE the fallback run's pre-provider Tx1: re-asserts the
        Room OPEN fence (the Room may have closed between the two runs),
        vetoes a second fallback binding (``DuplicateFallbackRefusal`` —
        concurrent recoveries serialize on the write lock and the loser
        resolves to the committed winner), and commits the
        ``MESSAGE_DELIVERY_FALLBACK`` continuation marker atomically with
        the fallback run row. Deliberately NOT a re-initiation: no
        ``MESSAGE_DELIVERED`` duplicate check (the same initiation
        continues — ``deliveries_for_message`` still sees exactly one
        initiation binding) and no second turn-budget charge.
        """

        def bind(run: Run, _input_artifact: object) -> Iterable[EventLogEntry]:
            if message.room_id is not None:
                require_open_room(self._store, message.room_id)
            # One fallback per initiation, enforced on the write lock: two
            # recovery attempts racing to continue the same crashed
            # initiation serialize here — the loser sees the winner's
            # committed marker, refuses typed, and its staged run row rolls
            # back with the veto.
            if self._has_fallback_binding(message.id):
                raise DuplicateFallbackRefusal(
                    f"message '{message.id}' already has a continuation "
                    "fallback bound — one fresh run completes a rejected "
                    "resume, never two"
                )
            return [self._fallback_marker_for(message, run, prior_run)]

        return bind

    def _has_fallback_binding(self, message_id: str) -> bool:
        """Any committed MESSAGE_DELIVERY_FALLBACK binding for this message."""
        ref = f"message:{message_id}"
        return any(
            ref in entry.references
            for entry in self._store.all_models(
                EventLogEntry,
                "WHERE type = ?",
                [EventType.MESSAGE_DELIVERY_FALLBACK.value],
                order_by="sequence ASC",
            )
        )

    @staticmethod
    def _fallback_marker_for(
        message: Message,
        run: Run,
        prior_run: Run,
    ) -> EventLogEntry:
        """P7.4 continuation marker: same initiation, fresh fallback run."""
        role_note = f" via role '{message.recipient_role}'" if message.recipient_role else ""
        references = [
            f"message:{message.id}",
            f"run:{run.id}",
            f"prior_run:{prior_run.id}",
        ]
        if message.room_id:
            references.append(f"room:{message.room_id}")
        if message.task_id:
            references.append(f"task:{message.task_id}")
        return EventLogEntry(
            stage_key=message.stage_key,
            room_id=message.room_id,
            task_id=message.task_id,
            sender=DELIVERY_SENDER,
            recipient=message.recipient,
            type=EventType.MESSAGE_DELIVERY_FALLBACK,
            content=(
                f"{message.type.value} from {message.sender} to "
                f"{message.recipient}{role_note} continued on fresh run "
                f"{run.id} after session-continuation rejection on run "
                f"{prior_run.id}"
            ),
            references=references,
        )

    @staticmethod
    def _marker_for(
        message: Message,
        run: Run,
        admitted_reply_type: MessageType | None = None,
    ) -> EventLogEntry:
        """D10 binding marker: bounded metadata, message+run+scope refs."""
        role_note = f" via role '{message.recipient_role}'" if message.recipient_role else ""
        references = [f"message:{message.id}", f"run:{run.id}"]
        if message.room_id:
            references.append(f"room:{message.room_id}")
        if message.task_id:
            references.append(f"task:{message.task_id}")
        if admitted_reply_type is not None:
            references.append(reply_admission_reference(admitted_reply_type))
        return EventLogEntry(
            stage_key=message.stage_key,
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


def _is_session_resume_rejection(cause: object) -> bool:
    """Positive session-continuation rejection classification (P7.4).

    The fallback applies ONLY to the typed ``SessionResumeUnavailable``
    cause — a signal the harness/runtime emits exclusively when it
    positively identified the supplied resume ref as unusable. Arbitrary
    provider failures (rate limits, transport, auth, config) never carry
    this type and are never retried. Kept import-lazy so core stays
    harness-agnostic at module load.
    """
    from relay.harness.errors import SessionResumeUnavailable

    return isinstance(cause, SessionResumeUnavailable)


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

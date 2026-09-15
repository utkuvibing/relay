"""Bounded micro-interactions inside build stages (P6.4).

SPEC §27 Phase 6 tail; App. D.4–D.9, D.11-P6.

A build implement/fix/review run is one-shot — Relay cannot mediate inside
a run — so the ONLY way a stage run reaches another participant is to end
its whole output with one strict ``relay.stage_signal.v1`` object instead
of its normal output. Relay persists the signal as a task-scoped bus
``Message`` (``run_id`` authorship), resolves blocking signals through the
existing ``MessageDelivery.deliver_and_reply``, promotes
``challenge``/``proposal`` answers into ``Decision`` records (and, when
accepted with ``plan_effect: supersede``, a superseding ``PLAN`` artifact +
``relay.plan_revision.v1`` link), and re-derives every step from durable
ledger state. Stage completion still resolves exclusively from persisted
evidence: conversation is coordination input, never authority.

Import discipline: ``delivery`` is imported lazily inside
``resolve_open_signal`` — it depends on ``orchestrator`` (the ``run_ask``
spine), which itself imports this module for parsing/derivation helpers.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, NoReturn, cast

from pydantic import ValidationError

from relay.agents.base import AgentRole
from relay.core.bus import ConversationBus, RoleResolver
from relay.core.evidence import EvidenceKind, EvidenceStore
from relay.core.policy import (
    BlockingBudgetExhausted,
    CommunicationPolicyRefusal,
    MessageRejected,
    PolicyEnvelope,
    TurnBudgetExhausted,
)
from relay.core.reviews import canonical_json
from relay.storage.events import EventLogWriter
from relay.storage.models import (
    Artifact,
    ArtifactKind,
    BuildEscalationPayload,
    Decision,
    DecisionStatus,
    EventLogEntry,
    EventType,
    EvidenceRecord,
    Message,
    MessageType,
    PlannerDecisionPayload,
    PlanRevisionPayload,
    Run,
    RunStatus,
    SignalInvalidPayload,
    StageSignalPayload,
    Task,
)
from relay.storage.store import SqliteRelayStore

if TYPE_CHECKING:  # pragma: no cover - typing only
    from relay.core.delivery import MessageDelivery

__all__ = [
    "SIGNAL_MESSAGE_TYPES",
    "SIGNAL_SCHEMA",
    "SIGNAL_SENDER",
    "InvalidSignal",
    "OpenSignal",
    "SignalContractError",
    "SignalDeliveryContext",
    "SignalResolution",
    "SignalServices",
    "blocking_messages_authored_by",
    "canonical_replies_for",
    "check_signal_legal",
    "compose_signal_message",
    "escalation_exists",
    "exchange_appendix",
    "invalid_signal_for_run_output",
    "notes_appendix",
    "parse_stage_signal",
    "persist_escalation",
    "persist_signal_diagnostic",
    "resolve_open_signal",
    "send_note_signal",
    "signal_appendix",
    "signal_diagnostic_for_run",
    "signal_for_run_output",
    "signal_is_blocking",
    "signal_message_type",
]

#: Strict emitted contract version; any other ``relay.stage_signal.*``
#: version string is an intended-but-unsupported signal (``bad_schema_version``).
SIGNAL_SCHEMA = "relay.stage_signal.v1"
_SIGNAL_SCHEMA_PREFIX = "relay.stage_signal."

#: Producer convention for signal-machinery-authored events (App. A.1).
SIGNAL_SENDER = "relay:signals"

_MAX_SIGNAL_CHARS = 20_000
_MAX_SIGNAL_DEPTH = 8
_MAX_REF_CHARS = 512
_MAX_EXCHANGE_PART_CHARS = 4_000
_MAX_EXCHANGES = 8
_MAX_NOTES = 8
_MAX_NOTE_CHARS = 2_000

_KIND_MESSAGE_TYPE: dict[str, MessageType] = {
    "clarification_request": MessageType.CLARIFICATION_REQUEST,
    "challenge": MessageType.CHALLENGE,
    "proposal": MessageType.PROPOSAL,
    "note": MessageType.NOTE,
}
_KIND_BLOCKING: dict[str, bool] = {
    "clarification_request": True,
    "challenge": True,
    "proposal": True,
    "note": False,
}
#: Canonical answering reply type per blocking signal kind (P4.3 pairing).
_KIND_REPLY_TYPE: dict[str, MessageType] = {
    "clarification_request": MessageType.CLARIFICATION_RESPONSE,
    "challenge": MessageType.FINAL_POSITION,
    "proposal": MessageType.FINAL_POSITION,
}
#: Parent MessageType -> canonical answer type (ledger derivation).
_REPLY_TYPE_BY_PARENT: dict[MessageType, MessageType] = {
    MessageType.CLARIFICATION_REQUEST: MessageType.CLARIFICATION_RESPONSE,
    MessageType.CHALLENGE: MessageType.FINAL_POSITION,
    MessageType.PROPOSAL: MessageType.FINAL_POSITION,
}
#: MessageTypes a signal may carry (for ledger provenance checks).
SIGNAL_MESSAGE_TYPES: frozenset[MessageType] = frozenset(_KIND_MESSAGE_TYPE.values())

#: Per emitting stage-role legality (the fixer's role is IMPLEMENTER).
#: Reviewer ``note`` is deliberately absent (deferred — no composite
#: reviewer envelope exists in this slice).
_STAGE_SIGNAL_LEGALITY: dict[str, dict[str, frozenset[AgentRole]]] = {
    AgentRole.IMPLEMENTER.value: {
        "clarification_request": frozenset({AgentRole.PLANNER}),
        "proposal": frozenset({AgentRole.PLANNER}),
        "note": frozenset({AgentRole.PLANNER, AgentRole.REVIEWER}),
    },
    AgentRole.REVIEWER.value: {
        "clarification_request": frozenset({AgentRole.IMPLEMENTER}),
        "challenge": frozenset({AgentRole.PLANNER}),
    },
}

#: Relay-authored appendix to a challenge/proposal message body: the frozen
#: D15 delivery envelope cannot carry the reply contract, so it rides inside
#: the message content, visibly delimited as relay-authored.
_DECISION_REPLY_CONTRACT = (
    "\n\n---\n"
    "[relay:stage-signal] Reply with EXACTLY one JSON object and nothing else:\n"
    '{"schema_version":"relay.planner_decision.v1","outcome":"accept"|"reject",'
    '"plan_effect":"unchanged"|"supersede","statement":"...","rationale":"...",'
    '"revised_plan":"..."}\n'
    "supersede requires a non-empty revised_plan and is accept-only; "
    "unchanged forbids revised_plan; reject keeps the plan unchanged."
)


#: ``relay.build.escalation.v1`` reason vocabulary.
EscalationReason = Literal[
    "policy_refused",
    "budget_exhausted",
    "unresolved_role",
    "self_send",
    "delivery_failed",
    "delivery_pending",
    "turn_budget_exhausted",
    #: P7.3: a Room-bound task's micro-exchange hit the Room's OPEN fence
    #: (P7.1 traffic fence) — the task parks until the human resumes the Room.
    "room_closed",
]


class SignalContractError(ValueError):
    """Safe, typed refusal for a malformed or stage-illegal stage signal."""

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        super().__init__(message or code)


def _reject(code: str, message: str | None = None) -> NoReturn:
    raise SignalContractError(code, message)


def _object_without_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            _reject("duplicate_key")
        result[key] = value
    return result


def _reject_constant(_: str) -> None:
    _reject("nonfinite_json")


def _json_depth(value: object) -> int:
    stack: list[tuple[object, int]] = [(value, 1)]
    deepest = 1
    while stack:
        current, depth = stack.pop()
        deepest = max(deepest, depth)
        if depth > _MAX_SIGNAL_DEPTH:
            _reject("json_too_deep")
        if isinstance(current, dict):
            for item in cast("dict[object, object]", current).values():
                stack.append((item, depth + 1))
        elif isinstance(current, list):
            for item in cast("list[object]", current):
                stack.append((item, depth + 1))
    return deepest


def _loads_strict(text: str) -> object:
    try:
        value = json.loads(
            text,
            object_pairs_hook=_object_without_duplicates,
            parse_constant=_reject_constant,
        )
    except SignalContractError:
        raise
    except RecursionError as exc:
        raise SignalContractError("json_too_deep") from exc
    except json.JSONDecodeError as exc:
        raise SignalContractError("malformed") from exc
    _json_depth(value)
    return value


def parse_strict_json(text: str) -> object:
    """Strict JSON decode for canonical reply contracts (P7.3).

    Duplicate keys, non-finite constants and over-deep documents are refused;
    malformed input raises :class:`SignalContractError`.
    """
    return _loads_strict(text)


def parse_stage_signal(text: str) -> StageSignalPayload | None:
    """Parse a run's whole output as a stage signal.

    Returns ``None`` for ordinary output (pre-P6.4 behavior preserved).
    Raises :class:`SignalContractError` when the output *intends* to be a
    signal — a JSON value carrying a ``relay.stage_signal.*`` schema marker,
    or unparseable text containing that marker — but fails strict
    validation. Intended-but-invalid output must never fall through to
    normal handling; callers persist a diagnostic and park the stage.
    """

    stripped = text.strip()
    if not stripped:
        return None
    intended = _SIGNAL_SCHEMA_PREFIX in stripped
    if len(stripped) > _MAX_SIGNAL_CHARS:
        if intended:
            _reject("oversized")
        return None
    try:
        value = _loads_strict(stripped)
    except SignalContractError:
        if intended:
            raise
        return None
    if not isinstance(value, dict):
        if intended:
            _reject("malformed")
        return None
    version = cast("dict[str, object]", value).get("schema_version")
    if not (isinstance(version, str) and version.startswith(_SIGNAL_SCHEMA_PREFIX)):
        return None
    if version != SIGNAL_SCHEMA:
        _reject("bad_schema_version")
    try:
        return StageSignalPayload.model_validate(value)
    except ValidationError as exc:
        raise SignalContractError("invalid_signal") from exc


def check_signal_legal(signal: StageSignalPayload, run_role: str) -> None:
    """Stage legality: kind must be emitted by this role and ``to_role`` a
    permitted target. Violations are stage-contract failures (``code``)."""

    legal = _STAGE_SIGNAL_LEGALITY.get(run_role)
    if legal is None or signal.kind not in legal:
        _reject("kind_not_permitted")
    try:
        target = AgentRole(signal.to_role)
    except ValueError:
        _reject("bad_role")
    if target not in legal[signal.kind]:
        _reject("role_not_permitted")


def signal_is_blocking(signal: StageSignalPayload) -> bool:
    return _KIND_BLOCKING[signal.kind]


def signal_message_type(signal: StageSignalPayload) -> MessageType:
    return _KIND_MESSAGE_TYPE[signal.kind]


def signal_for_run_output(store: SqliteRelayStore, run: Run) -> StageSignalPayload | None:
    """Parse the run's persisted RUN_OUTPUT as a signal.

    Returns ``None`` for ordinary output AND for intended-but-invalid
    signals — the diagnostic for the latter is persisted by the stage
    function at detection time, and the ledger treats the run as a consumed
    attempt thereafter.
    """

    outputs = store.artifacts_for_run(run.id, kind=ArtifactKind.RUN_OUTPUT)
    if len(outputs) != 1:
        return None
    try:
        return parse_stage_signal(outputs[0].content or "")
    except SignalContractError:
        return None


def invalid_signal_for_run_output(store: SqliteRelayStore, run: Run) -> str | None:
    """The contract-error code when the run's persisted RUN_OUTPUT is an
    intended-but-invalid stage signal — else ``None``.

    Intended-invalid means EITHER strict parse/schema failure OR a legal
    parse that violates the run role's signal legality — exactly what the
    stage functions diagnose at detection time. Derivation must never
    collapse this to ordinary output: a crash between the RUN_OUTPUT commit
    and the ``relay.build.signal.invalid.v1`` diagnostic persist would
    otherwise let resume treat the run as a consumed no-op and mint a
    fresh attempt — the fail-closed violation the ledger recovers from via
    the ``recover_signal`` action.
    """

    outputs = store.artifacts_for_run(run.id, kind=ArtifactKind.RUN_OUTPUT)
    if len(outputs) != 1:
        return None
    try:
        signal = parse_stage_signal(outputs[0].content or "")
    except SignalContractError as exc:
        return exc.code
    if signal is None:
        return None
    try:
        check_signal_legal(signal, run.role)
    except SignalContractError as exc:
        return exc.code
    return None


def blocking_messages_authored_by(
    store: SqliteRelayStore, task_id: str, run_id: str
) -> list[Message]:
    """Blocking task-scoped messages authored by one run, oldest first."""
    return list(
        store.all_models(
            Message,
            "WHERE run_id = ? AND task_id = ? AND blocking = 1",
            [run_id, task_id],
            order_by="rowid ASC",
        )
    )


def canonical_replies_for(store: SqliteRelayStore, message: Message) -> list[Message]:
    """Answering replies for a blocking signal message (P4.3 pair semantics).

    A reply is canonical iff it links to the parent, is authored by the
    resolved recipient back to the sender, is non-blocking, and carries the
    parent type's answer type (``clarification_response`` for requests,
    ``final_position`` for challenge/proposal). Multiple canonical replies
    for one parent are ledger corruption — callers refuse on ``len > 1``.
    """

    expected = _REPLY_TYPE_BY_PARENT.get(message.type)
    if expected is None:
        return []
    return [
        reply
        for reply in store.all_models(
            Message,
            "WHERE reply_to_id = ?",
            [message.id],
            order_by="rowid ASC",
        )
        if reply.type is expected
        and reply.sender == message.recipient
        and reply.recipient == message.sender
        and not reply.blocking
    ]


@dataclass(frozen=True)
class OpenSignal:
    """Ledger-derived state of one stage run's blocking signal."""

    run: Run
    signal: StageSignalPayload
    stage: str
    attempt: int | None
    #: The latest signal message authored by the run (earlier ones are
    #: superseded retries); None until the message is sent.
    message: Message | None
    #: The canonical answering reply; None until materialized.
    reply: Message | None

    @property
    def answered(self) -> bool:
        return self.reply is not None


@dataclass(frozen=True)
class InvalidSignal:
    """A run whose RUN_OUTPUT is an intended-but-invalid stage signal.

    Only derivable for the LAST bound run and only while its
    ``relay.build.signal.invalid.v1`` diagnostic is missing — the crash gap
    the driver closes by persisting the diagnostic and parking
    COMMUNICATION_BLOCKED (never a fresh attempt, never a diff/verdict).
    """

    run: Run
    stage: str
    code: str


@dataclass(frozen=True)
class SignalDeliveryContext:
    """Bounded durable context for decision-bearing signal deliveries.

    The frozen D15 envelope cannot grow fields, so the CURRENT canonical
    plan and the original build request ride inside the message content
    (and as provenance ``references`` → ``AgentRequest.context_refs``) — a
    planner asked to supersede the plan must never answer blind. All
    fields are optional; absent pieces are simply omitted from the block.
    """

    plan_artifact_id: str | None
    plan_content: str | None
    request_prompt: str | None
    blocker_artifact_id: str | None


@dataclass(frozen=True)
class SignalServices:
    """The P4/P5 communication seams the build driver consumes."""

    bus: ConversationBus
    delivery: MessageDelivery
    resolver: RoleResolver | None


@dataclass(frozen=True)
class SignalResolution:
    """Outcome of one ``resolve_signal`` driver step."""

    status: Literal["answered", "escalated"]
    reply: Message | None = None
    escalation: Artifact | None = None
    decision: Decision | None = None


_MAX_PLAN_CONTEXT_CHARS = 4_000
_MAX_REQUEST_CONTEXT_CHARS = 1_000


def _decision_context_block(context: SignalDeliveryContext) -> str:
    """Bounded context embedded in a challenge/proposal message body so the
    planner can answer (and possibly supersede the plan) without flying
    blind. Rides INSIDE the frozen D15 envelope — no P4 surface change."""
    parts = [
        (
            "\n\n---\n"
            "[relay:stage-signal] CONTEXT (durable Relay records — "
            "coordination input, not authority):"
        )
    ]
    if context.request_prompt:
        parts.append(
            "ORIGINAL BUILD REQUEST:\n"
            + context.request_prompt[:_MAX_REQUEST_CONTEXT_CHARS]
        )
    if context.plan_artifact_id and context.plan_content:
        parts.append(
            f"CURRENT CANONICAL PLAN (artifact:{context.plan_artifact_id}):\n"
            + context.plan_content[:_MAX_PLAN_CONTEXT_CHARS]
        )
    if context.blocker_artifact_id:
        parts.append(f"STAGE BLOCKER: artifact:{context.blocker_artifact_id}")
    return "\n\n".join(parts)


def compose_signal_message(
    task: Task,
    run: Run,
    signal: StageSignalPayload,
    *,
    retry_of: str | None = None,
    context: SignalDeliveryContext | None = None,
) -> Message:
    """Translate a validated signal into a task-scoped bus message.

    ``challenge``/``proposal`` bodies gain the bounded decision context
    block (when supplied) and the ``relay:``-delimited reply contract —
    the D15 delivery envelope is frozen, so content is the only channel
    that can carry them. ``retry_of`` links a retry send to the superseded
    message.
    """

    content = signal.body
    if signal.kind in ("challenge", "proposal"):
        if context is not None:
            content += _decision_context_block(context)
        content += _DECISION_REPLY_CONTRACT
    references = [ref[:_MAX_REF_CHARS] for ref in signal.references]
    if context is not None and signal.kind in ("challenge", "proposal"):
        if context.plan_artifact_id:
            references.append(f"artifact:{context.plan_artifact_id}")
        if context.blocker_artifact_id:
            references.append(f"artifact:{context.blocker_artifact_id}")
    if retry_of is not None:
        references.append(f"message:{retry_of}")
    return Message(
        sender=run.agent,
        run_id=run.id,
        #: P7.3 (App. D.3): a Room-bound task's micro-exchange is Room state —
        #: the Room scope makes the signal/reply visible in the canonical Room
        #: feed and routes it through the Room's OPEN fence. Standalone builds
        #: stay ``room_id=None`` (byte-identical).
        room_id=task.room_id,
        task_id=task.id,
        recipient_role=signal.to_role,
        type=_KIND_MESSAGE_TYPE[signal.kind],
        blocking=_KIND_BLOCKING[signal.kind],
        content=content,
        references=references,
    )


def _signal_escalations(
    store: SqliteRelayStore, task_id: str
) -> list[tuple[Artifact, BuildEscalationPayload]]:
    records: list[tuple[Artifact, BuildEscalationPayload]] = []
    for artifact in store.all_models(
        Artifact,
        "WHERE task_id = ? AND kind = ?",
        [task_id, ArtifactKind.REPORT.value],
    ):
        content = artifact.content or ""
        if '"relay.build.escalation.v1"' not in content:
            continue
        try:
            records.append((artifact, BuildEscalationPayload.model_validate_json(content)))
        except ValidationError:
            continue
    return records


def escalation_exists(store: SqliteRelayStore, task_id: str, *, signal_message_id: str) -> bool:
    """True when an escalation record already names this signal message."""
    return any(
        record.signal_message_id == signal_message_id
        for _, record in _signal_escalations(store, task_id)
    )


def persist_escalation(
    store: SqliteRelayStore,
    writer: EventLogWriter,
    task: Task,
    *,
    stage: str,
    attempt: int | None,
    signal_message_id: str | None,
    run_id: str | None,
    reason: EscalationReason,
    detail: str,
) -> Artifact:
    """Persist ``relay.build.escalation.v1`` once per open signal.

    Dedupe: an existing record naming the same signal message (or, before a
    message exists, the same run+reason) is returned unmodified — repeated
    ``relay continue`` attempts never multiply records.
    """

    for artifact, record in _signal_escalations(store, task.id):
        if signal_message_id is not None and record.signal_message_id == signal_message_id:
            return artifact
        if (
            signal_message_id is None
            and record.signal_message_id is None
            and record.run_id == run_id
            and record.reason == reason
        ):
            return artifact
    payload = BuildEscalationPayload(
        schema_version="relay.build.escalation.v1",
        task_id=task.id,
        stage=stage,
        attempt=attempt,
        signal_message_id=signal_message_id,
        run_id=run_id,
        reason=reason,
        detail=detail[:4_000],
    )
    artifact = Artifact(
        kind=ArtifactKind.REPORT,
        task_id=task.id,
        run_id=run_id,
        content=canonical_json(payload),
    )
    with store.transaction():
        store.save_model(artifact)
        writer.record(
            EventLogEntry(
                type=EventType.ARTIFACT_CREATED,
                task_id=task.id,
                sender=SIGNAL_SENDER,
                content=f"stage signal escalated: {reason}",
                references=[
                    ref
                    for ref in (
                        f"task:{task.id}",
                        f"artifact:{artifact.id}",
                        f"message:{signal_message_id}" if signal_message_id else None,
                        f"run:{run_id}" if run_id else None,
                    )
                    if ref is not None
                ],
            )
        )
    return artifact


def signal_diagnostic_for_run(
    store: SqliteRelayStore, task_id: str, run_id: str
) -> Artifact | None:
    """The persisted ``relay.build.signal.invalid.v1`` diagnostic for a run."""
    for artifact in store.all_models(
        Artifact,
        "WHERE task_id = ? AND kind = ?",
        [task_id, ArtifactKind.REPORT.value],
    ):
        content = artifact.content or ""
        if '"relay.build.signal.invalid.v1"' not in content:
            continue
        try:
            payload = SignalInvalidPayload.model_validate_json(content)
        except ValidationError:
            continue
        if payload.run_id == run_id:
            return artifact
    return None


def persist_signal_diagnostic(
    store: SqliteRelayStore,
    writer: EventLogWriter,
    task: Task,
    run: Run,
    *,
    stage: str,
    code: str,
) -> Artifact:
    """Persist ``relay.build.signal.invalid.v1`` for an intended-but-invalid
    signal output — the run is consumed but the stage parks honestly.

    Idempotent per (task, run): the crash-gap recovery path may observe a
    diagnostic already committed before the process died.
    """

    existing = signal_diagnostic_for_run(store, task.id, run.id)
    if existing is not None:
        return existing
    outputs = store.artifacts_for_run(run.id, kind=ArtifactKind.RUN_OUTPUT)
    output_artifact_id = outputs[0].id if len(outputs) == 1 else None
    payload = SignalInvalidPayload(
        schema_version="relay.build.signal.invalid.v1",
        task_id=task.id,
        run_id=run.id,
        stage=stage,
        code=code,
        output_artifact_id=output_artifact_id,
    )
    artifact = Artifact(
        kind=ArtifactKind.REPORT,
        task_id=task.id,
        run_id=run.id,
        content=canonical_json(payload),
    )
    with store.transaction():
        store.save_model(artifact)
        writer.record(
            EventLogEntry(
                type=EventType.ARTIFACT_CREATED,
                task_id=task.id,
                sender=SIGNAL_SENDER,
                content=f"stage signal rejected: {code}",
                references=[
                    ref
                    for ref in (
                        f"task:{task.id}",
                        f"artifact:{artifact.id}",
                        f"run:{run.id}",
                        f"artifact:{output_artifact_id}" if output_artifact_id else None,
                    )
                    if ref is not None
                ],
            )
        )
    return artifact


def send_note_signal(
    store: SqliteRelayStore,
    writer: EventLogWriter,
    services: SignalServices,
    task: Task,
    run: Run,
    signal: StageSignalPayload,
    *,
    stage: str,
    attempt: int | None,
) -> Artifact | None:
    """Best-effort send of a non-blocking ``note`` signal.

    A note never parks the stage: on refusal the exchange is dropped and a
    durable escalation record captures WHY (the note is coordination input,
    not authority — losing it must not halt the build).
    """

    def escalate(reason: EscalationReason, detail: str) -> Artifact:
        return persist_escalation(
            store,
            writer,
            task,
            stage=stage,
            attempt=attempt,
            signal_message_id=None,
            run_id=run.id,
            reason=reason,
            detail=detail,
        )

    resolved = (
        services.resolver.resolve_role(signal.to_role)
        if services.resolver is not None
        else None
    )
    if resolved is None:
        return escalate(
            "unresolved_role",
            f"note to role '{signal.to_role}' resolves to no configured agent",
        )
    if resolved == run.agent:
        return escalate(
            "self_send", f"note to role '{signal.to_role}' resolves to the emitting agent"
        )
    try:
        services.bus.send(compose_signal_message(task, run, signal))
    except (CommunicationPolicyRefusal, MessageRejected) as exc:
        return escalate("policy_refused", str(exc))
    return None


def _decision_promoted(store: SqliteRelayStore, task_id: str, message_id: str) -> bool:
    ref = f"message:{message_id}"
    return any(
        ref in entry.references
        for entry in store.all_models(
            EventLogEntry,
            "WHERE type = ? AND task_id = ?",
            [EventType.DECISION_PROPOSED.value, task_id],
        )
    )


def _promote_planner_decision(
    store: SqliteRelayStore,
    writer: EventLogWriter,
    evidence: EvidenceStore,
    task: Task,
    message: Message,
    reply: Message,
    *,
    current_plan_artifact_id: str | None,
) -> Decision | None:
    """Promote a parseable ``relay.planner_decision.v1`` reply — atomically.

    A valid decision always mints the ``Decision`` row + its
    ``DECISION_PROPOSED``/``DECISION_ACCEPTED|REJECTED`` events; an accepted
    ``plan_effect: supersede`` additionally mints the new ``PLAN`` artifact,
    the ``relay.plan_revision.v1`` link, and ``PLAN_PRODUCED`` evidence in the
    SAME transaction. Unparseable replies promote nothing — the raw answer
    still flows into the continuation prompt as coordination input.
    """

    if message.type not in (MessageType.CHALLENGE, MessageType.PROPOSAL):
        return None
    if _decision_promoted(store, task.id, message.id):
        return None
    try:
        payload = PlannerDecisionPayload.model_validate(_loads_strict(reply.content))
    except (SignalContractError, ValidationError):
        return None

    accepted = payload.outcome == "accept"
    supersede = (
        accepted
        and payload.plan_effect == "supersede"
        and current_plan_artifact_id is not None
    )
    decision = Decision(
        statement=payload.statement,
        rationale=payload.rationale,
        proposed_by=message.sender,
        accepted_by=reply.sender if accepted else None,
        status=DecisionStatus.ACCEPTED if accepted else DecisionStatus.REJECTED,
        #: P7.3 (App. D.3): a Room-bound task's promoted decision is Room state,
        #: with durable promotion provenance. Standalone builds keep both unset.
        room_id=task.room_id,
        source_reply_id=reply.id,
        task_id=task.id,
    )
    exchange_refs = [
        f"task:{task.id}",
        f"decision:{decision.id}",
        f"message:{message.id}",
        f"message:{reply.id}",
    ]
    if task.room_id is not None:
        exchange_refs.insert(0, f"room:{task.room_id}")
    with store.transaction():
        store.save_model(decision)
        writer.record(
            EventLogEntry(
                type=EventType.DECISION_PROPOSED,
                room_id=task.room_id,
                task_id=task.id,
                sender=message.sender,
                recipient=reply.sender,
                content=f"decision proposed via {message.type.value}: {payload.statement[:120]}",
                references=exchange_refs,
            )
        )
        writer.record(
            EventLogEntry(
                type=(
                    EventType.DECISION_ACCEPTED if accepted else EventType.DECISION_REJECTED
                ),
                room_id=task.room_id,
                task_id=task.id,
                sender=reply.sender,
                recipient=message.sender,
                content=(
                    f"decision {'accepted' if accepted else 'rejected'} by "
                    f"agent '{reply.sender}'"
                ),
                references=exchange_refs,
            )
        )
        if supersede:
            assert payload.revised_plan is not None  # payload invariant
            plan_artifact = store.save_model(
                Artifact(
                    kind=ArtifactKind.PLAN,
                    room_id=task.room_id,
                    task_id=task.id,
                    run_id=reply.run_id,
                    content=payload.revised_plan,
                )
            )
            revision = store.save_model(
                Artifact(
                    kind=ArtifactKind.REPORT,
                    room_id=task.room_id,
                    task_id=task.id,
                    run_id=reply.run_id,
                    content=canonical_json(
                        PlanRevisionPayload(
                            schema_version="relay.plan_revision.v1",
                            task_id=task.id,
                            plan_artifact_id=plan_artifact.id,
                            supersedes_plan_artifact_id=current_plan_artifact_id or "",
                            decision_id=decision.id,
                            signal_message_id=message.id,
                            reply_message_id=reply.id,
                            author_run_id=reply.run_id or "",
                        )
                    ),
                )
            )
            evidence.record(
                EvidenceRecord(
                    kind=EvidenceKind.PLAN_PRODUCED,
                    task_id=task.id,
                    run_id=reply.run_id,
                    artifact_id=plan_artifact.id,
                    produced_by=f"agent:{reply.sender}",
                )
            )
            for artifact in (plan_artifact, revision):
                writer.record(
                    EventLogEntry(
                        type=EventType.ARTIFACT_CREATED,
                        room_id=task.room_id,
                        task_id=task.id,
                        sender=SIGNAL_SENDER,
                        content=f"plan revision minted via {message.type.value}",
                        references=[f"task:{task.id}", f"artifact:{artifact.id}"],
                    )
                )
            if task.room_id is not None:
                #: P7.3: a Room-bound task's revised plan becomes the Room's
                #: canonical tip — the feed's entry marker for the new plan.
                #: The human-freeze marker (ROOM_PLAN_FROZEN) is never reused.
                writer.record(
                    EventLogEntry(
                        type=EventType.ROOM_PLAN_REVISED,
                        room_id=task.room_id,
                        task_id=task.id,
                        sender=SIGNAL_SENDER,
                        content=(
                            "plan revised by decision "
                            f"{decision.id}: {_first_line(payload.revised_plan)}"
                        ),
                        references=[
                            f"room:{task.room_id}",
                            f"task:{task.id}",
                            f"plan:{plan_artifact.id}",
                            f"supersedes_plan:{current_plan_artifact_id}",
                            f"decision:{decision.id}",
                            f"message:{message.id}",
                            f"message:{reply.id}",
                        ],
                    )
                )
    return decision


def _first_line(content: str, limit: int = 200) -> str:
    """The plan's first non-empty line, bounded — feed display text."""
    for line in content.splitlines():
        stripped = line.strip().lstrip("#").strip()
        if stripped:
            return stripped[:limit]
    return "(empty plan)"


def _room_closed_detail(task: Task, exc: Exception) -> str:
    """Human-actionable detail for a Room-fence refusal (P7.3)."""
    return (
        f"Room traffic for task '{task.id}' is fenced: {exc} — resume the Room "
        "(relay room resume <room>) and run 'relay continue' again"
    )


async def resolve_open_signal(
    store: SqliteRelayStore,
    writer: EventLogWriter,
    evidence: EvidenceStore,
    services: SignalServices,
    task: Task,
    open_signal: OpenSignal,
    *,
    signal_context: SignalDeliveryContext | None = None,
) -> SignalResolution:
    """Advance one open blocking signal one step, resuming from ledger state.

    Steps: send the signal message (if absent) -> deliver and materialize
    the canonical reply (or escalate and park) -> promote a parseable
    planner decision. Every step is idempotent; a crash between them is
    recovered by re-derivation on the next invocation.
    """

    from relay.core.delivery import (  # lazy: delivery -> orchestrator -> this module
        DeliveryPendingRefusal,
        DeliveryRefusal,
    )
    from relay.core.rooms import ClosedRoomError

    current_plan_artifact_id = (
        signal_context.plan_artifact_id if signal_context is not None else None
    )

    def escalate(
        reason: EscalationReason, detail: str, *, message_id: str | None = None
    ) -> SignalResolution:
        artifact = persist_escalation(
            store,
            writer,
            task,
            stage=open_signal.stage,
            attempt=open_signal.attempt,
            signal_message_id=message_id,
            run_id=open_signal.run.id,
            reason=reason,
            detail=detail,
        )
        return SignalResolution(status="escalated", escalation=artifact)

    def send(retry_of: str | None = None) -> Message | SignalResolution:
        resolved = (
            services.resolver.resolve_role(open_signal.signal.to_role)
            if services.resolver is not None
            else None
        )
        if resolved is None:
            return escalate(
                "unresolved_role",
                f"role '{open_signal.signal.to_role}' resolves to no configured agent",
            )
        if resolved == open_signal.run.agent:
            return escalate(
                "self_send",
                f"role '{open_signal.signal.to_role}' resolves to the emitting agent",
            )
        message = compose_signal_message(
            task,
            open_signal.run,
            open_signal.signal,
            retry_of=retry_of,
            context=signal_context,
        )
        try:
            return services.bus.send(message)
        except ClosedRoomError as exc:
            # P7.3: the Room's OPEN fence refused the traffic. ``bus.send``
            # validates and fences inside its own transaction, so nothing was
            # persisted — park with a durable, human-actionable escalation.
            return escalate("room_closed", _room_closed_detail(task, exc))
        except BlockingBudgetExhausted as exc:
            return escalate("budget_exhausted", str(exc))
        except TurnBudgetExhausted as exc:
            return escalate("turn_budget_exhausted", str(exc))
        except (CommunicationPolicyRefusal, MessageRejected) as exc:
            return escalate("policy_refused", str(exc))

    message = open_signal.message
    if message is None:
        sent = send()
        if isinstance(sent, SignalResolution):
            return sent
        message = sent

    reply = open_signal.reply
    if reply is None:
        # Delivery phase — recover whatever state the ledger holds.
        deliveries = services.delivery.deliveries_for_message(message.id)
        if len(deliveries) > 1:
            return escalate(
                "delivery_failed",
                f"multiple MESSAGE_DELIVERED markers for message {message.id}",
                message_id=message.id,
            )
        if deliveries:
            delivery_entry = deliveries[0]
            delivery_run = next(
                (
                    store.load_model(Run, ref[4:])
                    for ref in delivery_entry.references
                    if ref.startswith("run:")
                ),
                None,
            )
            if delivery_run is None:
                return escalate(
                    "delivery_failed",
                    "MESSAGE_DELIVERED marker carries no resolvable run ref",
                    message_id=message.id,
                )
            if delivery_run.status is RunStatus.RUNNING:
                return escalate(
                    "delivery_pending",
                    f"delivery run {delivery_run.id} still in flight — settle with "
                    "`relay continue --settle-interrupted` if it is a zombie",
                    message_id=message.id,
                )
            if delivery_run.status in (RunStatus.FAILED, RunStatus.CANCELLED):
                if not escalation_exists(store, task.id, signal_message_id=message.id):
                    # First observation: park so the human can inspect.
                    return escalate(
                        "delivery_failed",
                        f"delivery run {delivery_run.id} ended {delivery_run.status.value} "
                        "with no reply — `relay continue` retries the exchange once",
                        message_id=message.id,
                    )
                # Escalation already on record: retry once with a superseding
                # send bound to the same signal (at-most-once delivery means
                # the failed message itself can never be delivered again).
                retried = send(retry_of=message.id)
                if isinstance(retried, SignalResolution):
                    return retried
                message = retried
            # SUCCEEDED falls through: deliver_and_reply recovers/materializes.

        try:
            outcome = await services.delivery.deliver_and_reply(
                message.id,
                reply_type=_KIND_REPLY_TYPE[open_signal.signal.kind],
            )
        except DeliveryPendingRefusal as exc:
            return escalate("delivery_pending", str(exc), message_id=message.id)
        except ClosedRoomError as exc:
            # P7.3: the delivery Tx1 fence refused the staged run — no Run, no
            # artifacts, no MESSAGE_DELIVERED marker survive the rollback.
            return escalate(
                "room_closed", _room_closed_detail(task, exc), message_id=message.id
            )
        except BlockingBudgetExhausted as exc:
            return escalate("budget_exhausted", str(exc), message_id=message.id)
        except TurnBudgetExhausted as exc:
            return escalate("turn_budget_exhausted", str(exc), message_id=message.id)
        except CommunicationPolicyRefusal as exc:
            return escalate("policy_refused", str(exc), message_id=message.id)
        except (DeliveryRefusal, MessageRejected) as exc:
            return escalate("delivery_failed", str(exc), message_id=message.id)
        if outcome.reply is None:
            return escalate(
                "delivery_failed",
                "delivery run failed before producing a reply",
                message_id=message.id,
            )
        reply = outcome.reply

    decision = _promote_planner_decision(
        store,
        writer,
        evidence,
        task,
        message,
        reply,
        current_plan_artifact_id=current_plan_artifact_id,
    )
    return SignalResolution(status="answered", reply=reply, decision=decision)


def _deliverable(
    services: SignalServices,
    task_id: str,
    sender_role: AgentRole,
    sender_agent: str,
    kind: str,
    to_role: AgentRole,
) -> bool:
    """True iff a signal of ``kind`` to ``to_role`` is currently executable:
    the role resolves to a DISTINCT configured agent and policy admits both
    the send edge and the reply edge (plus blocking budgets for blocking
    kinds). Advertised capabilities must never dead-end on dispatch.
    """

    resolved = (
        services.resolver.resolve_role(to_role.value)
        if services.resolver is not None
        else None
    )
    if resolved is None or resolved == sender_agent:
        return False
    gate = services.bus.policy
    if gate is None:
        return True
    blocking = _KIND_BLOCKING[kind]
    try:
        gate.check_edge(
            PolicyEnvelope(
                sender=sender_role,
                recipient=to_role,
                type=_KIND_MESSAGE_TYPE[kind],
                blocking=blocking,
                room_id=None,
                task_id=task_id,
            )
        )
    except CommunicationPolicyRefusal:
        return False
    if blocking:
        reply_type = _KIND_REPLY_TYPE[kind]
        try:
            gate.check_edge(
                PolicyEnvelope(
                    sender=to_role,
                    recipient=sender_role,
                    type=reply_type,
                    blocking=False,
                    room_id=None,
                    task_id=task_id,
                )
            )
            gate.check_blocking_budget(None, task_id)
            gate.check_turn_budget(None, task_id)
        except CommunicationPolicyRefusal:
            return False
    return True


_KIND_BLURB: dict[str, str] = {
    "clarification_request": "ask a blocking question; the answer resumes this stage",
    "challenge": "a blocking challenge; the answer resumes this stage",
    "proposal": "a blocking proposal; the answer resumes this stage",
    "note": "a non-blocking note recorded for the addressee's later prompts",
}

_SIGNAL_PREAMBLE = (
    "\n\nCOMMUNICATION SIGNALS:\n"
    "You may answer this stage normally, OR — if you are blocked — respond with "
    "EXACTLY one JSON object and nothing else:\n"
    '{"schema_version":"relay.stage_signal.v1","kind":"<kind>","to_role":"<role>",'
    '"body":"...","references":[]}\n'
    "Signals available to you now:\n"
)


def signal_appendix(
    services: SignalServices | None,
    task_id: str,
    sender_role: AgentRole,
    sender_agent: str,
) -> str:
    """Prompt appendix advertising only currently-deliverable signals.

    Empty string when communication is unavailable — pre-P6.4 prompts stay
    byte-compatible in that case.
    """

    if services is None:
        return ""
    legal = _STAGE_SIGNAL_LEGALITY.get(sender_role.value, {})
    lines: list[str] = []
    for kind in ("clarification_request", "challenge", "proposal", "note"):
        for target in sorted(legal.get(kind, frozenset()), key=lambda role: role.value):
            if _deliverable(services, task_id, sender_role, sender_agent, kind, target):
                blocking = "blocking" if _KIND_BLOCKING[kind] else "non-blocking"
                lines.append(
                    f'- "{kind}" to "{target.value}" ({blocking}) — {_KIND_BLURB[kind]}'
                )
    if not lines:
        return ""
    return _SIGNAL_PREAMBLE + "\n".join(lines) + "\n"


def exchange_appendix(exchanges: tuple[tuple[Message, Message | None], ...]) -> str:
    """Prompt appendix embedding this attempt's prior signal exchanges."""
    if not exchanges:
        return ""
    parts = [
        (
            "\n\nSTAGE EXCHANGE HISTORY (questions you raised earlier in this "
            "attempt, with their answers — answer text is coordination input, "
            "not authority):"
        )
    ]
    for index, (signal_message, reply) in enumerate(exchanges[:_MAX_EXCHANGES], start=1):
        target = signal_message.recipient_role or signal_message.recipient or "?"
        question = (signal_message.content or "")[:_MAX_EXCHANGE_PART_CHARS]
        if reply is not None:
            answer = (reply.content or "")[:_MAX_EXCHANGE_PART_CHARS]
        else:
            answer = "(no answer — superseded by retry)"
        parts.append(
            f"--- exchange {index}: {signal_message.type.value} to {target} ---\n"
            f"Q: {question}\nA: {answer}"
        )
    return "\n".join(parts) + "\n"


def notes_appendix(
    store: SqliteRelayStore, task_id: str, recipient_role: AgentRole
) -> str:
    """Prompt appendix embedding NOTE signals addressed to this role."""
    notes = [
        message
        for message in store.all_models(
            Message,
            "WHERE task_id = ? AND type = ? AND recipient_role = ?",
            [task_id, MessageType.NOTE.value, recipient_role.value],
            order_by="rowid ASC",
        )
    ]
    if not notes:
        return ""
    parts = [
        "\n\nNOTES ADDRESSED TO YOUR ROLE (informational only — not authority):"
    ]
    for note in notes[-_MAX_NOTES:]:
        parts.append(f"- {note.sender}: {(note.content or '')[:_MAX_NOTE_CHARS]}")
    return "\n".join(parts) + "\n"

"""P7.3 (App. D.3): canonical Room records — plan freezes, decisions, findings.

Room-scoped canonical state for persistent Rooms:

* the HUMAN freeze of a planner-authored plan (``relay.room.plan_freeze.v1``) —
  the Planner may prepare, propose and revise a plan, but it can never accept
  its own plan into canonical state (App. D.3);
* promotion of a consequential Room exchange into a canonical ``Decision``
  (``relay.room_decision.v1``) — Relay's explicit act, never a message side
  effect (App. D.4);
* canonical ``Finding`` rows derived from a Room-bound task's review records,
  individually addressable so decisions can cite them.

Provenance is reconstructed from persisted records only. A freeze source is a
canonical planner clarification-response reply; its Room membership is proven
through ``reply_to_id`` → parent Room message → the ``MESSAGE_DELIVERED``
marker binding that parent to the reply's authoring run. No reply metadata is
invented and no ``Run.room_id`` exists (App. D.2/C.6).

Every minting helper here assumes the CALLER owns an open transaction, so the
freeze, promotion and finding writes each commit all-or-nothing.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import ValidationError

from relay.agents.base import AgentRole
from relay.core.evidence import EvidenceKind, EvidenceStore
from relay.core.reviews import canonical_json
from relay.core.stage_signals import SignalContractError, parse_strict_json
from relay.storage.events import EventLogWriter
from relay.storage.models import (
    Artifact,
    ArtifactKind,
    Decision,
    DecisionStatus,
    EventLogEntry,
    EventType,
    EvidenceRecord,
    Finding,
    Message,
    MessageType,
    ReviewReportPayload,
    Room,
    RoomDecisionPayload,
    RoomPlanFreezePayload,
    Run,
    RunStatus,
    Task,
    new_id,
    utcnow,
)
from relay.storage.store import SqliteRelayStore

__all__ = [
    "ROOM_DECISION_CONTRACT",
    "FrozenPlanMint",
    "FrozenPlanSource",
    "RoomRecordRefusal",
    "build_review_findings",
    "decision_for_reply",
    "freeze_for_source",
    "mint_frozen_plan",
    "promote_room_decision",
    "resolve_freeze_source",
    "room_plan_freeze_event",
    "room_plan_revised_event",
]

#: App. A.1 producer convention for Relay-owned canonical record writes. Kept
#: local on purpose: importing ``relay.core.delivery`` here would close an
#: import cycle (delivery → orchestrator → this module).
ROOM_RECORDS_SENDER = "relay:rooms"

#: The P4.2 delivery-binding marker producer (App. A.1).
_DELIVERY_SENDER = "relay:delivery"

#: Bounded supersession-chain walk — a longer chain is corruption, not depth.
_MAX_SUPERSESSION_DEPTH = 64

#: The strict reply contract advertised ONLY on ``relay room decide`` (P7.3).
#: It rides outside the frozen D15 envelope — the P6.4 appendix pattern — and
#: never appears on ordinary ``relay room ask`` discussion prompts.
ROOM_DECISION_CONTRACT = (
    "\n\n---\n"
    "[relay:room-decision] This exchange is a CONSEQUENTIAL Room decision.\n"
    "Answer with EXACTLY one JSON object and nothing else:\n"
    '{"schema_version":"relay.room_decision.v1","outcome":"accept|reject",'
    '"statement":"...","rationale":null,"references":[],"supersedes_decision_id":null}\n'
    "references may cite canonical records of this Room (plan:<id>, finding:<id>, "
    "decision:<id>, message:<id>). Only an accept outcome may carry "
    "supersedes_decision_id, and only a currently ACCEPTED decision may be "
    "superseded. Relay promotes the object into canonical Room state; ordinary "
    "prose promotes nothing.\n"
)


class RoomRecordRefusal(ValueError):
    """A canonical Room record cannot be minted; nothing is persisted."""

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        super().__init__(message or code)


@dataclass(frozen=True)
class FrozenPlanSource:
    """The validated canonical planner reply a human freeze adopts."""

    reply: Message
    parent: Message
    run: Run


@dataclass(frozen=True)
class FrozenPlanMint:
    """The records one freeze (or supersession) mints for a Room."""

    plan_artifact: Artifact
    freeze_record: Artifact


def _require(condition: bool, code: str, message: str) -> None:
    if not condition:
        raise RoomRecordRefusal(code, message)


def _delivered_by(store: SqliteRelayStore, parent: Message, run: Run) -> bool:
    """True when a MESSAGE_DELIVERED marker binds ``parent`` to ``run``."""
    for marker in store.all_models(
        EventLogEntry,
        "WHERE type = ?",
        [EventType.MESSAGE_DELIVERED.value],
        order_by="sequence ASC",
    ):
        if f"message:{parent.id}" not in marker.references:
            continue
        if (
            f"run:{run.id}" in marker.references
            and marker.sender == _DELIVERY_SENDER
            and marker.room_id == parent.room_id
        ):
            return True
    return False


def resolve_freeze_source(
    store: SqliteRelayStore, room: Room, message_id: str
) -> FrozenPlanSource:
    """Validate the canonical planner discussion reply a human may freeze.

    Fail-closed: every contradiction is a typed refusal with zero store delta.
    Only a normal Planner discussion answer (``clarification_request`` →
    ``clarification_response``) qualifies — ``relay room decide`` output
    (``proposal`` → ``final_position``) is a consequential decision surface and
    can never be frozen as a plan.
    """

    reply = store.load_model(Message, message_id)
    _require(reply is not None, "unknown_source", f"message '{message_id}' does not exist")
    assert reply is not None  # narrowed for type checkers
    _require(
        reply.room_id == room.id,
        "foreign_source",
        "the freeze source is not a message of this Room",
    )
    _require(
        reply.reply_to_id is not None,
        "not_a_reply",
        "the freeze source must be a canonical reply to a Room request",
    )
    _require(
        reply.run_id is not None,
        "no_authorship",
        "the freeze source reply has no authoring run",
    )
    assert reply.reply_to_id is not None and reply.run_id is not None
    _require(not reply.blocking, "blocking_source", "the freeze source reply must not block")
    _require(
        ":" not in reply.sender,
        "non_agent_source",
        "the freeze source must be authored by a logical agent",
    )

    parent = store.load_model(Message, reply.reply_to_id)
    _require(parent is not None, "missing_parent", "the freeze source reply has no parent")
    assert parent is not None  # narrowed for type checkers
    _require(
        parent.room_id == room.id,
        "foreign_parent",
        "the freeze source parent is not a message of this Room",
    )
    _require(
        parent.task_id is None,
        "task_scoped_parent",
        "the freeze source must come from Room discussion, not task-scoped signal traffic",
    )
    _require(
        parent.recipient_role == AgentRole.PLANNER.value,
        "not_planner_request",
        "the freeze source must answer a request addressed to the planner seat",
    )
    _require(
        parent.recipient == reply.sender,
        "sender_mismatch",
        "the freeze source reply was not authored by the addressed seat",
    )
    _require(
        parent.type is MessageType.CLARIFICATION_REQUEST,
        "not_discussion",
        "only a normal Planner discussion reply can be frozen "
        "(clarification_request -> clarification_response)",
    )
    _require(
        reply.type is MessageType.CLARIFICATION_RESPONSE,
        "not_discussion_reply",
        "the freeze source must be a clarification_response reply",
    )
    canonical = [
        candidate
        for candidate in store.all_models(
            Message, "WHERE reply_to_id = ?", [parent.id], order_by="rowid ASC"
        )
        if candidate.type is MessageType.CLARIFICATION_RESPONSE
        and candidate.sender == parent.recipient
        and candidate.recipient == parent.sender
        and not candidate.blocking
    ]
    _require(
        len(canonical) == 1 and canonical[0].id == reply.id,
        "not_canonical_reply",
        "the freeze source is not the canonical answer to its parent",
    )

    run = store.load_model(Run, reply.run_id)
    _require(run is not None, "missing_run", "the freeze source reply's run does not exist")
    assert run is not None  # narrowed for type checkers
    _require(run.agent == reply.sender, "run_mismatch", "the reply's run belongs to another agent")
    _require(
        run.role == AgentRole.PLANNER.value,
        "not_planner_run",
        "the reply's run did not speak as the planner",
    )
    _require(
        run.status is RunStatus.SUCCEEDED,
        "run_unsuccessful",
        "the reply's run did not succeed",
    )
    _require(
        _delivered_by(store, parent, run),
        "unbound_delivery",
        "the parent Room message is not bound to the reply's run by a delivery marker",
    )
    return FrozenPlanSource(reply=reply, parent=parent, run=run)


def freeze_for_source(
    store: SqliteRelayStore, room_id: str, source_message_id: str
) -> Artifact | None:
    """The freeze record already naming ``source_message_id``, if any."""
    for artifact in store.all_models(
        Artifact,
        "WHERE room_id = ? AND kind = ?",
        [room_id, ArtifactKind.REPORT.value],
        order_by="rowid ASC",
    ):
        content = artifact.content or ""
        if '"relay.room.plan_freeze.v1"' not in content:
            continue
        try:
            payload = RoomPlanFreezePayload.model_validate_json(content)
        except ValidationError:
            continue
        if payload.source_message_id == source_message_id:
            return artifact
    return None


def plan_first_line(content: str, limit: int = 200) -> str:
    """The plan's first non-empty line, bounded — the frozen task title."""
    for line in content.splitlines():
        stripped = line.strip().lstrip("#").strip()
        if stripped:
            return stripped[:limit]
    return "frozen Room plan"


def mint_frozen_plan(
    store: SqliteRelayStore,
    writer: EventLogWriter,
    evidence: EvidenceStore,
    room: Room,
    task: Task,
    source: FrozenPlanSource,
    *,
    frozen_by: str,
    supersedes_plan_artifact_id: str | None = None,
) -> FrozenPlanMint:
    """Mint the frozen ``PLAN`` + freeze record + ``PLAN_PRODUCED`` evidence.

    The CALLER owns the transaction. Provenance is honest by construction: the
    plan artifact and the evidence record both point at the planner run whose
    reply authored the plan (App. A.1), and the freeze record carries the
    human's acceptance plus the optional supersession edge.
    """

    plan_artifact = store.save_model(
        Artifact(
            kind=ArtifactKind.PLAN,
            room_id=room.id,
            task_id=task.id,
            run_id=source.run.id,
            content=source.reply.content,
        )
    )
    freeze_record = store.save_model(
        Artifact(
            kind=ArtifactKind.REPORT,
            room_id=room.id,
            task_id=task.id,
            content=canonical_json(
                RoomPlanFreezePayload(
                    schema_version="relay.room.plan_freeze.v1",
                    room_id=room.id,
                    task_id=task.id,
                    plan_artifact_id=plan_artifact.id,
                    supersedes_plan_artifact_id=supersedes_plan_artifact_id,
                    source_message_id=source.reply.id,
                    source_run_id=source.run.id,
                    frozen_by=frozen_by,
                )
            ),
        )
    )
    evidence.record(
        EvidenceRecord(
            kind=EvidenceKind.PLAN_PRODUCED,
            task_id=task.id,
            run_id=source.run.id,
            artifact_id=plan_artifact.id,
            produced_by=f"agent:{source.run.agent}",
        )
    )
    writer.record(
        EventLogEntry(
            type=EventType.ARTIFACT_CREATED,
            room_id=room.id,
            task_id=task.id,
            sender=ROOM_RECORDS_SENDER,
            content="canonical Room plan artifact frozen",
            references=[
                f"room:{room.id}",
                f"task:{task.id}",
                f"artifact:{plan_artifact.id}",
            ],
        )
    )
    writer.record(
        EventLogEntry(
            type=EventType.EVIDENCE_RECORDED,
            room_id=room.id,
            task_id=task.id,
            sender=ROOM_RECORDS_SENDER,
            content=f"{EvidenceKind.PLAN_PRODUCED.value} recorded for task",
            references=[f"task:{task.id}", f"run:{source.run.id}"],
        )
    )
    return FrozenPlanMint(plan_artifact=plan_artifact, freeze_record=freeze_record)


def room_plan_freeze_event(
    room: Room,
    task: Task,
    mint: FrozenPlanMint,
    source: FrozenPlanSource,
    *,
    frozen_by: str,
) -> EventLogEntry:
    """The ``ROOM_PLAN_FROZEN`` entry marker for one frozen plan (P7.3 feed)."""
    references = [
        f"room:{room.id}",
        f"task:{task.id}",
        f"plan:{mint.plan_artifact.id}",
        f"artifact:{mint.freeze_record.id}",
        f"message:{source.reply.id}",
        f"run:{source.run.id}",
    ]
    payload = RoomPlanFreezePayload.model_validate_json(mint.freeze_record.content or "")
    if payload.supersedes_plan_artifact_id is not None:
        references.append(f"supersedes_plan:{payload.supersedes_plan_artifact_id}")
    return EventLogEntry(
        type=EventType.ROOM_PLAN_FROZEN,
        room_id=room.id,
        task_id=task.id,
        sender=ROOM_RECORDS_SENDER,
        content=f"plan frozen by {frozen_by}: {plan_first_line(source.reply.content)}",
        references=references,
    )


def room_plan_revised_event(
    store: SqliteRelayStore,
    task: Task,
    *,
    plan_artifact_id: str,
    supersedes_plan_artifact_id: str,
    decision_id: str,
    signal_message_id: str,
    reply_message_id: str,
) -> EventLogEntry:
    """The ``ROOM_PLAN_REVISED`` entry marker for a P6.4 revision (P7.3 feed).

    Emitted only when the revising P6.4 exchange belongs to a Room-bound task;
    a human freeze never produces this marker (App. D.3 keeps the two
    acceptance paths distinguishable).
    """
    plan = store.load_model(Artifact, plan_artifact_id)
    assert task.room_id is not None
    return EventLogEntry(
        type=EventType.ROOM_PLAN_REVISED,
        room_id=task.room_id,
        task_id=task.id,
        sender=ROOM_RECORDS_SENDER,
        content=(
            "plan revised by decision "
            f"{decision_id}: {plan_first_line((plan.content if plan else '') or '')}"
        ),
        references=[
            f"room:{task.room_id}",
            f"task:{task.id}",
            f"plan:{plan_artifact_id}",
            f"supersedes_plan:{supersedes_plan_artifact_id}",
            f"decision:{decision_id}",
            f"message:{signal_message_id}",
            f"message:{reply_message_id}",
        ],
    )


def decision_for_reply(store: SqliteRelayStore, reply_id: str) -> Decision | None:
    """The decision already promoted from ``reply_id`` (durable idempotence)."""
    return next(
        (
            decision
            for decision in store.all_models(
                Decision, "WHERE source_reply_id = ?", [reply_id], order_by="rowid ASC"
            )
        ),
        None,
    )


def parse_room_decision(text: str) -> RoomDecisionPayload | None:
    """Strictly parse a reply as a decision payload, or ``None``.

    Output that does not carry the ``relay.room_decision.v1`` marker is
    ordinary conversation; a marker-carrying payload that fails strict
    validation promotes nothing (the exchange itself remains canonical).
    """
    if "relay.room_decision.v1" not in text:
        return None
    try:
        decoded: object = parse_strict_json(text)
    except SignalContractError:
        return None
    if not isinstance(decoded, dict):
        return None
    try:
        return RoomDecisionPayload.model_validate(decoded)
    except ValidationError:
        return None


def _reference_resolves(store: SqliteRelayStore, room_id: str, reference: str) -> bool:
    prefix, _, value = reference.partition(":")
    if not value:
        return False
    if prefix == "plan":
        artifact = store.load_model(Artifact, value)
        return (
            artifact is not None
            and artifact.kind is ArtifactKind.PLAN
            and artifact.room_id == room_id
        )
    if prefix == "finding":
        finding = store.load_model(Finding, value)
        return finding is not None and finding.room_id == room_id
    if prefix == "decision":
        decision = store.load_model(Decision, value)
        return decision is not None and decision.room_id == room_id
    if prefix == "message":
        message = store.load_model(Message, value)
        return message is not None and message.room_id == room_id
    return False


def _supersession_predecessor(
    store: SqliteRelayStore, room: Room, decision_id: str
) -> Decision | None:
    """The ACCEPTED, un-superseded, cycle-free predecessor, or ``None``."""
    predecessor = store.load_model(Decision, decision_id)
    if predecessor is None or predecessor.room_id != room.id:
        return None
    if predecessor.status is not DecisionStatus.ACCEPTED:
        return None
    if any(
        decision.supersedes_decision_id == predecessor.id
        for decision in store.all_models(Decision, "WHERE supersedes_decision_id = ?", [predecessor.id])
    ):
        return None
    seen = {predecessor.id}
    cursor = predecessor
    for _ in range(_MAX_SUPERSESSION_DEPTH):
        if cursor.supersedes_decision_id is None:
            return predecessor
        ancestor = store.load_model(Decision, cursor.supersedes_decision_id)
        if ancestor is None or ancestor.room_id != room.id or ancestor.id in seen:
            return None
        seen.add(ancestor.id)
        cursor = ancestor
    return None


def promote_room_decision(
    store: SqliteRelayStore,
    writer: EventLogWriter,
    room: Room,
    parent: Message,
    reply: Message,
) -> Decision | None:
    """Promote a strict decision reply into canonical Room state.

    Returns the promoted decision (or the one already promoted from this
    reply), or ``None`` when the reply carries no valid decision — promotion is
    Relay's explicit act and never a message side effect (App. D.4). Only a
    newly ACCEPTED decision may supersede, and only a currently ACCEPTED
    predecessor may be superseded.
    """

    existing = decision_for_reply(store, reply.id)
    if existing is not None:
        return existing
    payload = parse_room_decision(reply.content)
    if payload is None:
        return None
    if not all(_reference_resolves(store, room.id, ref) for ref in payload.references):
        return None
    predecessor: Decision | None = None
    if payload.supersedes_decision_id is not None:
        predecessor = _supersession_predecessor(store, room, payload.supersedes_decision_id)
        if predecessor is None:
            return None

    accepted = payload.outcome == "accept"
    with store.transaction():
        if decision_for_reply(store, reply.id) is not None:
            return decision_for_reply(store, reply.id)
        decision = store.save_model(
            Decision(
                statement=payload.statement,
                rationale=payload.rationale,
                proposed_by=parent.sender,
                accepted_by=reply.sender if accepted else None,
                status=DecisionStatus.ACCEPTED if accepted else DecisionStatus.REJECTED,
                room_id=room.id,
                references=list(payload.references),
                source_reply_id=reply.id,
                supersedes_decision_id=payload.supersedes_decision_id if accepted else None,
            )
        )
        exchange_refs = [
            f"room:{room.id}",
            f"decision:{decision.id}",
            f"message:{parent.id}",
            f"message:{reply.id}",
        ]
        writer.record(
            EventLogEntry(
                type=EventType.DECISION_PROPOSED,
                room_id=room.id,
                sender=parent.sender,
                recipient=reply.sender,
                content=f"decision proposed via Room exchange: {payload.statement[:120]}",
                references=exchange_refs,
            )
        )
        writer.record(
            EventLogEntry(
                type=(
                    EventType.DECISION_ACCEPTED if accepted else EventType.DECISION_REJECTED
                ),
                room_id=room.id,
                sender=reply.sender,
                recipient=parent.sender,
                content=(
                    f"decision {'accepted' if accepted else 'rejected'} by "
                    f"agent '{reply.sender}'"
                ),
                references=exchange_refs,
            )
        )
        if accepted and predecessor is not None:
            store.update_model(
                predecessor.model_copy(update={"status": DecisionStatus.SUPERSEDED})
            )
            writer.record(
                EventLogEntry(
                    type=EventType.DECISION_SUPERSEDED,
                    room_id=room.id,
                    sender=ROOM_RECORDS_SENDER,
                    content=f"decision {predecessor.id} superseded by {decision.id}",
                    references=[
                        f"room:{room.id}",
                        f"decision:{decision.id}",
                        f"supersedes_decision:{predecessor.id}",
                    ],
                )
            )
    return decision


def build_review_findings(
    store: SqliteRelayStore,
    task: Task,
    review_artifact: Artifact,
    review_run: Run,
    report: ReviewReportPayload,
) -> tuple[tuple[Finding, ...], tuple[EventLogEntry, ...]]:
    """Canonical Room findings for one review record (PURE construction).

    Only Room-bound tasks produce Room state; a standalone build mints nothing.
    The rows are returned UNSAVED so the caller persists them inside the same
    transaction as the review artifacts (``advance_task(..., models=...)``);
    rows already on record for ``(review_artifact_id, source_finding_id)`` are
    reused, never re-minted.
    """

    if task.room_id is None:
        return (), ()
    findings: list[Finding] = []
    events: list[EventLogEntry] = []
    for item in report.findings:
        existing = next(
            (
                row
                for row in store.all_models(
                    Finding,
                    "WHERE review_artifact_id = ? AND source_finding_id = ?",
                    [review_artifact.id, item.id],
                )
            ),
            None,
        )
        if existing is not None:
            findings.append(existing)
            continue
        finding = Finding(
            id=new_id(),
            room_id=task.room_id,
            task_id=task.id,
            review_artifact_id=review_artifact.id,
            review_run_id=review_run.id,
            source_finding_id=item.id,
            severity=item.severity,
            title=item.title,
            description=item.description,
            requested_change=item.requested_change,
            validation_expectation=item.validation_expectation,
            location=item.location,
            created_at=utcnow(),
        )
        findings.append(finding)
        events.append(
            EventLogEntry(
                type=EventType.FINDING_RECORDED,
                room_id=task.room_id,
                task_id=task.id,
                sender=ROOM_RECORDS_SENDER,
                content=f"{item.severity.value} finding: {item.title}",
                references=[
                    f"room:{task.room_id}",
                    f"task:{task.id}",
                    f"finding:{finding.id}",
                    f"artifact:{review_artifact.id}",
                ],
            )
        )
    return tuple(findings), tuple(events)

"""P7.3 (App. D.3): the canonical Room plan/decision/finding graph read-model.

Pure, read-only reconstruction of one Room's canonical history:

* ONE complete plan chain per Room-bound task, containing both edge kinds —
  human freezes (``relay.room.plan_freeze.v1``) and P6.4 plan revisions
  (``relay.plan_revision.v1``, mirrored by a ``ROOM_PLAN_REVISED`` marker);
* Room-scoped decisions with their promotion source and supersession edges;
* Room-scoped findings with their source review records.

Integrity is fail-closed and independently re-derived from raw canonical
records — this module never trusts that writers were correct (the
:mod:`relay.core.build_ledger` stance). A discontinuous plan chain, an orphan
freeze record, a supersession that contradicts the canonical fields, or a
Room-scoped decision without promotion provenance refuses the whole read.

Zero writes, zero invocations, no derived state persisted. Like
:mod:`relay.core.build_ledger` this is a canonical-record read-model: it reads
the evidence vocabulary to re-derive plan provenance. The conversation-layer
feed must NOT import it (``tests/test_architecture.py`` keeps the feed
authority-free); the feed reads raw rows itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, NoReturn

from pydantic import ValidationError

from relay.agents.base import AgentRole
from relay.core.decision_references import DecisionReferenceError, validate_decision_references
from relay.core.evidence import EvidenceKind
from relay.core.finding_integrity import FindingIntegrityError, resolve_finding_source
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
    PlanRevisionPayload,
    Room,
    RoomPlanFreezePayload,
    Run,
    RunStatus,
    Task,
)
from relay.storage.store import SqliteRelayStore

__all__ = [
    "RoomDecisionNode",
    "RoomFindingNode",
    "RoomGraph",
    "RoomGraphIntegrityError",
    "RoomPlanChain",
    "RoomPlanNode",
    "build_room_graph",
    "resolve_room_plan_chain",
]

#: App. A.1 producer conventions (kept local: importing delivery/room_records
#: here would pull canonical-authority modules into this read-model).
_DELIVERY_SENDER = "relay:delivery"
_PLAN_MARKER = "plan:"
_SUPERSEDES_PLAN_MARKER = "supersedes_plan:"
_DECISION_MARKER = "decision:"
_SUPERSEDES_DECISION_MARKER = "supersedes_decision:"

_MAX_CHAIN = 64


class RoomGraphIntegrityError(RuntimeError):
    """Persisted Room canonical records contradict each other."""


def _fail(detail: str) -> NoReturn:
    raise RoomGraphIntegrityError(detail)


@dataclass(frozen=True)
class RoomPlanNode:
    """One canonical plan in a Room-bound task's chain."""

    plan: Artifact
    #: ``frozen`` = human freeze; ``revised`` = P6.4 plan-changing decision.
    edge: Literal["frozen", "revised"]
    supersedes_plan_artifact_id: str | None
    freeze_record_id: str | None = None
    frozen_by: str | None = None
    source_message_id: str | None = None
    source_run_id: str | None = None
    decision_id: str | None = None
    signal_message_id: str | None = None
    reply_message_id: str | None = None

    @property
    def first_line(self) -> str:
        for line in (self.plan.content or "").splitlines():
            stripped = line.strip().lstrip("#").strip()
            if stripped:
                return stripped[:200]
        return "(empty plan)"


@dataclass(frozen=True)
class RoomPlanChain:
    """The canonical plan chain of one Room-bound task (root → tip)."""

    task_id: str
    nodes: tuple[RoomPlanNode, ...]

    @property
    def tip(self) -> Artifact:
        return self.nodes[-1].plan


@dataclass(frozen=True)
class RoomDecisionNode:
    """One Room-scoped decision with its durable provenance."""

    decision: Decision
    source_reply: Message
    superseded_by: str | None


@dataclass(frozen=True)
class RoomFindingNode:
    """One Room-scoped finding with its source review record."""

    finding: Finding
    review_artifact: Artifact
    review_run: Run


@dataclass(frozen=True)
class RoomGraph:
    """The complete canonical graph of one Room."""

    room: Room
    plans: tuple[RoomPlanChain, ...]
    decisions: tuple[RoomDecisionNode, ...]
    findings: tuple[RoomFindingNode, ...]


def _report_records(
    store: SqliteRelayStore, room_id: str, task_id: str, schema: str
) -> list[Artifact]:
    return [
        artifact
        for artifact in store.all_models(
            Artifact,
            "WHERE room_id = ? AND task_id = ? AND kind = ?",
            [room_id, task_id, ArtifactKind.REPORT.value],
            order_by="rowid ASC",
        )
        if schema in (artifact.content or "")
    ]


def _decode_freezes(
    store: SqliteRelayStore, room_id: str, task_id: str
) -> list[tuple[Artifact, RoomPlanFreezePayload]]:
    decoded: list[tuple[Artifact, RoomPlanFreezePayload]] = []
    for artifact in _report_records(store, room_id, task_id, '"relay.room.plan_freeze.v1"'):
        try:
            payload = RoomPlanFreezePayload.model_validate_json(artifact.content or "")
        except ValidationError:
            _fail(f"freeze record '{artifact.id}' is undecodable")
        if payload.room_id != room_id or payload.task_id != task_id:
            _fail(f"freeze record '{artifact.id}' names a different Room or task")
        if not payload.frozen_by.startswith("human:"):
            _fail(f"freeze record '{artifact.id}' was not frozen by a human")
        decoded.append((artifact, payload))
    return decoded


def _decode_revisions(
    store: SqliteRelayStore, room_id: str, task_id: str
) -> list[tuple[Artifact, PlanRevisionPayload]]:
    decoded: list[tuple[Artifact, PlanRevisionPayload]] = []
    for artifact in _report_records(store, room_id, task_id, '"relay.plan_revision.v1"'):
        try:
            payload = PlanRevisionPayload.model_validate_json(artifact.content or "")
        except ValidationError:
            _fail(f"plan revision record '{artifact.id}' is undecodable")
        if payload.task_id != task_id:
            _fail(f"plan revision record '{artifact.id}' names a different task")
        decoded.append((artifact, payload))
    return decoded


def _plan_produced(
    store: SqliteRelayStore, task_id: str, run_id: str, artifact_id: str
) -> bool:
    return any(
        record.kind is EvidenceKind.PLAN_PRODUCED
        and record.run_id == run_id
        and record.artifact_id == artifact_id
        for record in store.all_models(
            EvidenceRecord, "WHERE task_id = ?", [task_id], order_by="rowid ASC"
        )
    )


def _markers(
    store: SqliteRelayStore, room_id: str, event_type: EventType
) -> list[EventLogEntry]:
    return list(
        store.all_models(
            EventLogEntry,
            "WHERE room_id = ? AND type = ?",
            [room_id, event_type.value],
            order_by="sequence ASC",
        )
    )


def _single_marker(
    store: SqliteRelayStore,
    room_id: str,
    event_type: EventType,
    record_ref: str,
    detail: str,
) -> EventLogEntry:
    matches = [
        marker
        for marker in _markers(store, room_id, event_type)
        if record_ref in marker.references
    ]
    if len(matches) != 1:
        _fail(f"{detail} has {len(matches)} {event_type.value} markers (expected exactly one)")
    return matches[0]


def _require_human_freeze_source(
    store: SqliteRelayStore, room_id: str, payload: RoomPlanFreezePayload
) -> Message:
    """Re-derive the human freeze's source chain from persisted records."""
    reply = store.load_model(Message, payload.source_message_id)
    if reply is None or reply.room_id != room_id:
        _fail(f"freeze record '{payload.source_message_id}' source reply is missing or foreign")
    assert reply is not None  # narrowed for type checkers
    if reply.reply_to_id is None or reply.run_id != payload.source_run_id:
        _fail(f"freeze source reply '{reply.id}' contradicts its freeze record")
    parent = store.load_model(Message, reply.reply_to_id)
    if (
        parent is None
        or parent.room_id != room_id
        or parent.task_id is not None
        or parent.recipient_role != AgentRole.PLANNER.value
        or parent.recipient != reply.sender
        or parent.type is not MessageType.CLARIFICATION_REQUEST
        or reply.type is not MessageType.CLARIFICATION_RESPONSE
        or reply.blocking
    ):
        _fail(f"freeze source reply '{reply.id}' is not a canonical planner discussion answer")
    run = store.load_model(Run, payload.source_run_id)
    if (
        run is None
        or run.agent != reply.sender
        or run.role != AgentRole.PLANNER.value
        or run.status is not RunStatus.SUCCEEDED
    ):
        _fail(f"freeze source run '{payload.source_run_id}' is not a successful planner run")
    bound = any(
        f"message:{parent.id}" in marker.references
        and f"run:{payload.source_run_id}" in marker.references
        and marker.sender == _DELIVERY_SENDER
        and marker.room_id == room_id
        for marker in _markers(store, room_id, EventType.MESSAGE_DELIVERED)
    )
    if not bound:
        _fail(f"freeze source parent '{parent.id}' is not delivery-bound to its run")
    return reply


def _require_revision_provenance(
    store: SqliteRelayStore,
    room_id: str,
    task_id: str,
    payload: PlanRevisionPayload,
    successor: Artifact,
) -> None:
    """Re-derive one P6.4 revision edge from persisted records."""
    decision = store.load_model(Decision, payload.decision_id)
    if (
        decision is None
        or decision.room_id != room_id
        or decision.task_id != task_id
        or decision.status is not DecisionStatus.ACCEPTED
    ):
        _fail(f"plan revision '{successor.id}' names a decision that is not an accepted Room decision")
    signal = store.load_model(Message, payload.signal_message_id)
    reply = store.load_model(Message, payload.reply_message_id)
    if (
        signal is None
        or signal.room_id != room_id
        or signal.task_id != task_id
        or signal.type not in (MessageType.CHALLENGE, MessageType.PROPOSAL)
        or signal.recipient_role != AgentRole.PLANNER.value
    ):
        _fail(f"plan revision '{successor.id}' names an invalid signal message")
    if (
        reply is None
        or reply.room_id != room_id
        or reply.task_id != task_id
        or reply.type is not MessageType.FINAL_POSITION
        or reply.reply_to_id != payload.signal_message_id
    ):
        _fail(f"plan revision '{successor.id}' names an invalid reply message")
    if successor.run_id != reply.run_id or not _plan_produced(
        store, task_id, reply.run_id or "", successor.id
    ):
        _fail(f"plan revision '{successor.id}' is not bound to its authoring run's evidence")


def _chain_nodes(
    store: SqliteRelayStore, room_id: str, task_id: str
) -> tuple[RoomPlanNode, ...]:
    plans = list(
        store.all_models(
            Artifact,
            "WHERE room_id = ? AND task_id = ? AND kind = ?",
            [room_id, task_id, ArtifactKind.PLAN.value],
            order_by="rowid ASC",
        )
    )
    freezes = _decode_freezes(store, room_id, task_id)
    revisions = _decode_revisions(store, room_id, task_id)
    if not plans:
        if freezes or revisions:
            _fail(f"task '{task_id}' has plan records but no plan artifacts")
        return ()

    by_id = {plan.id: plan for plan in plans}
    freeze_by_plan: dict[str, tuple[Artifact, RoomPlanFreezePayload]] = {}
    for record, payload in freezes:
        if payload.plan_artifact_id not in by_id:
            _fail(f"freeze record '{record.id}' names a missing plan artifact")
        if payload.plan_artifact_id in freeze_by_plan:
            _fail(f"plan artifact '{payload.plan_artifact_id}' has two freeze records")
        freeze_by_plan[payload.plan_artifact_id] = (record, payload)
    revision_by_plan: dict[str, tuple[Artifact, PlanRevisionPayload]] = {}
    for record, payload in revisions:
        if payload.plan_artifact_id not in by_id or payload.supersedes_plan_artifact_id not in by_id:
            _fail(f"plan revision record '{record.id}' names a missing plan artifact")
        if payload.plan_artifact_id in revision_by_plan:
            _fail(f"plan artifact '{payload.plan_artifact_id}' has two revision records")
        revision_by_plan[payload.plan_artifact_id] = (record, payload)

    def node_for(plan_id: str) -> RoomPlanNode | None:
        frozen = freeze_by_plan.get(plan_id)
        if frozen is not None:
            record, payload = frozen
            return RoomPlanNode(
                plan=by_id[plan_id],
                edge="frozen",
                supersedes_plan_artifact_id=payload.supersedes_plan_artifact_id,
                freeze_record_id=record.id,
                frozen_by=payload.frozen_by,
                source_message_id=payload.source_message_id,
                source_run_id=payload.source_run_id,
            )
        revised = revision_by_plan.get(plan_id)
        if revised is not None:
            _record, revision = revised
            return RoomPlanNode(
                plan=by_id[plan_id],
                edge="revised",
                supersedes_plan_artifact_id=revision.supersedes_plan_artifact_id,
                decision_id=revision.decision_id,
                signal_message_id=revision.signal_message_id,
                reply_message_id=revision.reply_message_id,
                source_run_id=revision.author_run_id,
            )
        return None

    edges: dict[str, RoomPlanNode] = {}
    for plan_id in (*freeze_by_plan, *revision_by_plan):
        node = node_for(plan_id)
        assert node is not None  # built from the same mapping
        predecessor = node.supersedes_plan_artifact_id
        if predecessor is None:
            continue
        if predecessor not in by_id:
            _fail(f"plan artifact '{plan_id}' supersedes a missing plan artifact")
        if predecessor in edges:
            _fail(f"plan artifact '{predecessor}' is superseded twice")
        edges[predecessor] = node

    roots = [
        plan_id
        for plan_id, (_record, payload) in freeze_by_plan.items()
        if payload.supersedes_plan_artifact_id is None
    ]
    if len(roots) != 1:
        _fail(f"task '{task_id}' has {len(roots)} frozen plan roots (expected exactly one)")
    chain: list[RoomPlanNode] = []
    current = roots[0]
    seen: set[str] = set()
    for _ in range(_MAX_CHAIN):
        if current in seen:
            _fail(f"task '{task_id}' plan chain is cyclic at '{current}'")
        seen.add(current)
        node = node_for(current)
        if node is None:
            _fail(f"plan artifact '{current}' is outside the canonical chain")
        assert node is not None  # narrowed for type checkers
        chain.append(node)
        successor = edges.get(current)
        if successor is None:
            break
        current = successor.plan.id
    leftover = [plan.id for plan in plans if plan.id not in seen]
    if leftover:
        _fail(f"task '{task_id}' has plan artifacts outside the chain: {', '.join(leftover)}")

    for node in chain:
        if node.edge == "frozen":
            frozen = freeze_by_plan.get(node.plan.id)
            assert frozen is not None  # node construction guarantees it
            payload = frozen[1]
            _require_human_freeze_source(store, room_id, payload)
            if node.plan.run_id != payload.source_run_id or not _plan_produced(
                store, task_id, payload.source_run_id, node.plan.id
            ):
                _fail(f"frozen plan '{node.plan.id}' is not bound to its authoring run's evidence")
            _single_marker(
                store,
                room_id,
                EventType.ROOM_PLAN_FROZEN,
                f"{_PLAN_MARKER}{node.plan.id}",
                f"frozen plan '{node.plan.id}'",
            )
        else:
            revised = revision_by_plan.get(node.plan.id)
            assert revised is not None  # node construction guarantees it
            _require_revision_provenance(store, room_id, task_id, revised[1], node.plan)
            marker = _single_marker(
                store,
                room_id,
                EventType.ROOM_PLAN_REVISED,
                f"{_PLAN_MARKER}{node.plan.id}",
                f"revised plan '{node.plan.id}'",
            )
            if (
                f"{_SUPERSEDES_PLAN_MARKER}{node.supersedes_plan_artifact_id}"
                not in marker.references
            ):
                _fail(f"revised plan '{node.plan.id}' marker contradicts its predecessor")
    return tuple(chain)


def resolve_room_plan_chain(
    store: SqliteRelayStore, room_id: str, task_id: str
) -> RoomPlanChain:
    """The canonical plan chain of one Room-bound task (root → tip)."""
    return RoomPlanChain(task_id=task_id, nodes=_chain_nodes(store, room_id, task_id))


def _decision_nodes(store: SqliteRelayStore, room_id: str) -> tuple[RoomDecisionNode, ...]:
    decisions = list(
        store.all_models(
            Decision,
            "WHERE room_id = ?",
            [room_id],
            order_by="created_at ASC, rowid ASC",
        )
    )
    by_id = {decision.id: decision for decision in decisions}
    # Supersession edges are resolved in a first pass: a predecessor's node is
    # built before its successor exists in iteration order.
    successors: dict[str, str] = {}
    for decision in decisions:
        if decision.supersedes_decision_id is None:
            continue
        predecessor = by_id.get(decision.supersedes_decision_id)
        if predecessor is None:
            _fail(f"Room decision '{decision.id}' supersedes a foreign decision")
        if predecessor.id in successors:
            _fail(f"Room decision '{predecessor.id}' has two successors")
        successors[predecessor.id] = decision.id
    nodes: list[RoomDecisionNode] = []
    for decision in decisions:
        if decision.source_reply_id is None:
            _fail(f"Room decision '{decision.id}' has no promotion source (source_reply_id)")
        reply = store.load_model(Message, decision.source_reply_id)
        if reply is None or reply.room_id != room_id:
            _fail(f"Room decision '{decision.id}' names a missing or foreign promotion reply")
        assert reply is not None  # narrowed for type checkers
        try:
            validate_decision_references(store, decision)
        except DecisionReferenceError as exc:
            _fail(f"Room decision '{decision.id}' cites an invalid reference: {exc}")
        if decision.status is DecisionStatus.SUPERSEDED and decision.id not in successors:
            _fail(f"Room decision '{decision.id}' is superseded without a successor")
        if decision.supersedes_decision_id is not None:
            predecessor = by_id[decision.supersedes_decision_id]
            if predecessor.status is not DecisionStatus.SUPERSEDED:
                _fail(
                    f"Room decision '{decision.id}' supersedes a decision that is not superseded"
                )
            if decision.status is not DecisionStatus.ACCEPTED:
                _fail(f"Room decision '{decision.id}' is not accepted but supersedes another")
            marker = _single_marker(
                store,
                room_id,
                EventType.DECISION_SUPERSEDED,
                f"{_DECISION_MARKER}{decision.id}",
                f"supersession of '{predecessor.id}'",
            )
            if f"{_SUPERSEDES_DECISION_MARKER}{predecessor.id}" not in marker.references:
                _fail(f"supersession marker for '{predecessor.id}' contradicts the canonical edge")
        if decision.status is DecisionStatus.REJECTED and decision.accepted_by is not None:
            _fail(f"rejected Room decision '{decision.id}' carries an accepted_by producer")
        nodes.append(
            RoomDecisionNode(
                decision=decision,
                source_reply=reply,
                superseded_by=successors.get(decision.id),
            )
        )
    return tuple(nodes)


def _finding_nodes(store: SqliteRelayStore, room_id: str) -> tuple[RoomFindingNode, ...]:
    findings = list(
        store.all_models(
            Finding, "WHERE room_id = ?", [room_id], order_by="created_at ASC, rowid ASC"
        )
    )
    nodes: list[RoomFindingNode] = []
    for finding in findings:
        if finding.room_id != room_id:
            _fail(f"finding '{finding.id}' belongs to another Room")
        try:
            artifact, run = resolve_finding_source(store, finding)
        except FindingIntegrityError as exc:
            _fail(str(exc))
        nodes.append(RoomFindingNode(finding=finding, review_artifact=artifact, review_run=run))
    return tuple(nodes)


def build_room_graph(store: SqliteRelayStore, room_id: str) -> RoomGraph:
    """Reconstruct and validate one Room's canonical graph (read-only)."""
    room = store.load_model(Room, room_id)
    if room is None:
        _fail(f"Room '{room_id}' does not exist")
    assert room is not None  # narrowed for type checkers
    task_ids = [
        task.id
        for task in store.all_models(
            Task, "WHERE room_id = ?", [room_id], order_by="created_at ASC, rowid ASC"
        )
    ]
    chains: list[RoomPlanChain] = []
    for task_id in task_ids:
        chain = resolve_room_plan_chain(store, room_id, task_id)
        if chain.nodes:
            chains.append(chain)
    return RoomGraph(
        room=room,
        plans=tuple(chains),
        decisions=_decision_nodes(store, room_id),
        findings=_finding_nodes(store, room_id),
    )

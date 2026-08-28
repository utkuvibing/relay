"""Read-only task views for the CLI (P3.4): position, gaps, ledger.

SPEC reference: §20 (``relay inspect``), §15 (history rebuildable from the
event log), §27 Phase 3 (deterministic lifecycle).

Everything here is *derived* from stored records — ``Task.state``,
provenance-backed ``EvidenceRecord``s, ``EventLogEntry``s, approvals, and
artifacts. Rendering never mints evidence (App. A.1 discipline held at the
presentation layer), and this module performs zero store mutations:
observability is not an authority path (App. D.8 spirit).

The gap lists come from the state machine's own introspection API —
``available_transitions()`` ∘ ``missing_evidence_for()`` — so what the CLI
shows is exactly what the machine would demand, never a parallel opinion
about task progress.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from relay.core.evidence import EvidenceKind
from relay.core.state_machine import TaskState, TaskStateMachine
from relay.storage.models import (
    Approval,
    ApprovalStatus,
    Artifact,
    EventLogEntry,
    EventType,
    Task,
)

if TYPE_CHECKING:  # pragma: no cover
    from relay.core.evidence import EvidenceStore
    from relay.storage.models import EvidenceRecord
    from relay.storage.store import SqliteRelayStore

#: Event-content prefix written by the orchestrator's ``_advance`` (P3.1).
#: The format is orchestrator-owned and test-pinned; parsing degrades to
#: ``None`` on any future drift instead of guessing.
_TRANSITION_PREFIX = "task state: "


@dataclass(frozen=True)
class TransitionView:
    """One traversed edge, derived from the append-only event log."""

    from_state: str
    to_state: str
    sequence: int | None
    at: datetime


@dataclass(frozen=True)
class EdgeGapView:
    """One legal edge out of the current state, with its missing evidence.

    ``missing`` is empty when the machine would grant the edge right now
    (ungated edges, or gates already satisfied by stored records).
    """

    to_state: TaskState
    missing: tuple[EvidenceKind, ...]


@dataclass(frozen=True)
class TaskView:
    """Machine position for one task — the ``relay status`` active panel."""

    task: Task
    last_transition: TransitionView | None
    edges: tuple[EdgeGapView, ...]
    pending_approval: Approval | None
    is_terminal: bool


@dataclass(frozen=True)
class TaskLedger:
    """Full task ledger — the ``relay inspect`` payload."""

    view: TaskView
    transitions: tuple[TransitionView, ...]
    evidence: tuple[EvidenceRecord, ...]
    approvals: tuple[Approval, ...]
    artifacts: tuple[Artifact, ...]


def task_events(events: list[EventLogEntry], task_id: str) -> list[EventLogEntry]:
    """Task-scoped events via the reference vocabulary (``task:{id}``).

    Event rows do not populate the ``task_id`` column; the ``references``
    list is the binding — the same pattern run-scoped ``history --full``
    uses for ``run:{id}``.
    """
    marker = f"task:{task_id}"
    return [entry for entry in events if marker in entry.references]


def _parse_transition(entry: EventLogEntry) -> TransitionView | None:
    """Parse one ``STATE_TRANSITIONED`` event; unparsable content → ``None``."""
    if entry.type is not EventType.STATE_TRANSITIONED:
        return None
    if not entry.content.startswith(_TRANSITION_PREFIX):
        return None
    pair = entry.content[len(_TRANSITION_PREFIX) :].split(" -> ")
    if len(pair) != 2:
        return None
    return TransitionView(
        from_state=pair[0].strip(),
        to_state=pair[1].strip(),
        sequence=entry.sequence,
        at=entry.created_at,
    )


def _transitions_for(events: list[EventLogEntry], task_id: str) -> list[TransitionView]:
    parsed = (_parse_transition(entry) for entry in task_events(events, task_id))
    return [view for view in parsed if view is not None]


def build_task_view(
    *,
    task: Task,
    evidence_store: EvidenceStore,
    events: list[EventLogEntry],
    approvals: list[Approval] | None = None,
) -> TaskView:
    """Compose the machine-position view for one task. Read-only."""
    machine = TaskStateMachine(task_id=task.id, store=evidence_store, state=task.state)
    transitions = _transitions_for(events, task.id)
    edges = tuple(
        EdgeGapView(
            to_state=edge.target,
            missing=tuple(
                sorted(machine.missing_evidence_for(edge.target), key=lambda kind: kind.value)
            ),
        )
        for edge in machine.available_transitions()
    )
    pending: Approval | None = None
    for approval in approvals or []:
        if approval.task_id == task.id and approval.status is ApprovalStatus.PENDING:
            pending = approval
            break
    return TaskView(
        task=task,
        last_transition=transitions[-1] if transitions else None,
        edges=edges,
        pending_approval=pending,
        is_terminal=machine.is_terminal,
    )


def build_task_ledger(
    *,
    task: Task,
    store: SqliteRelayStore,
    evidence_store: EvidenceStore,
    events: list[EventLogEntry],
) -> TaskLedger:
    """Compose the full task ledger for ``relay inspect``. Read-only."""
    view = build_task_view(task=task, evidence_store=evidence_store, events=events)
    return TaskLedger(
        view=view,
        transitions=tuple(_transitions_for(events, task.id)),
        evidence=tuple(evidence_store.records_for_task(task.id)),
        approvals=tuple(a for a in store.all_models(Approval) if a.task_id == task.id),
        artifacts=tuple(
            store.all_models(
                Artifact,
                clause="WHERE task_id = ?",
                params=[task.id],
                order_by="created_at ASC, rowid ASC",
            )
        ),
    )


def ledger_payload(ledger: TaskLedger) -> dict[str, Any]:
    """JSON-safe ledger for ``relay inspect --json``."""
    view = ledger.view
    return {
        "task": view.task.model_dump(mode="json"),
        "edges": [
            {"to": edge.to_state.value, "missing": [kind.value for kind in edge.missing]}
            for edge in view.edges
        ],
        "last_transition": _transition_payload(view.last_transition),
        "transitions": [_transition_payload(t) for t in ledger.transitions],
        "evidence": [record.model_dump(mode="json") for record in ledger.evidence],
        "approvals": [approval.model_dump(mode="json") for approval in ledger.approvals],
        "artifacts": [artifact.model_dump(mode="json") for artifact in ledger.artifacts],
    }


def _transition_payload(view: TransitionView | None) -> dict[str, Any] | None:
    if view is None:
        return None
    return {
        "from": view.from_state,
        "to": view.to_state,
        "sequence": view.sequence,
        "at": view.at.isoformat(),
    }

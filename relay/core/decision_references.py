"""Scope-checked canonical references shared by decision writers and readers."""

from __future__ import annotations

from relay.core.evidence import EvidenceError, validate_provenance
from relay.core.finding_integrity import FindingIntegrityError, resolve_finding_source
from relay.storage.models import (
    Artifact,
    ArtifactKind,
    Decision,
    EvidenceRecord,
    Finding,
    Message,
    Run,
    Task,
    ToolRun,
)
from relay.storage.store import SqliteRelayStore

REFERENCE_TYPES = frozenset({"message", "finding", "plan", "artifact", "evidence", "decision"})


class DecisionReferenceError(ValueError):
    """A citation is missing, foreign, or inconsistent."""


def _scope(
    store: SqliteRelayStore,
    decision: Decision,
    task_id: str | None,
    room_id: str | None,
    *,
    evidence: bool = False,
) -> bool:
    if decision.task_id is not None:
        if task_id != decision.task_id:
            return False
        return room_id == decision.room_id or (evidence and decision.room_id is not None and room_id is None)
    if decision.room_id is None:
        return False
    if task_id is None:
        return room_id == decision.room_id
    task = store.load_model(Task, task_id)
    return task is not None and task.room_id == decision.room_id and (
        room_id == decision.room_id or (evidence and room_id is None)
    )


def validate_decision_reference(
    store: SqliteRelayStore,
    decision: Decision,
    reference: str,
    *,
    _seen: frozenset[str] = frozenset(),
) -> None:
    """Validate one explicit citation; no historical timestamp inference."""
    kind, separator, record_id = reference.partition(":")
    if separator != ":" or not record_id or kind not in REFERENCE_TYPES:
        raise DecisionReferenceError(f"invalid decision reference '{reference}'")
    if kind == "message":
        record = store.load_model(Message, record_id)
        valid = record is not None and _scope(store, decision, record.task_id, record.room_id)
    elif kind == "finding":
        record = store.load_model(Finding, record_id)
        valid = False
        if record is not None and _scope(store, decision, record.task_id, record.room_id):
            try:
                resolve_finding_source(store, record)
            except FindingIntegrityError as exc:
                raise DecisionReferenceError(str(exc)) from exc
            valid = True
    elif kind in ("plan", "artifact"):
        record = store.load_model(Artifact, record_id)
        valid = (
            record is not None
            and (kind != "plan" or record.kind is ArtifactKind.PLAN)
            and _scope(store, decision, record.task_id, record.room_id)
        )
    elif kind == "evidence":
        record = store.load_model(EvidenceRecord, record_id)
        if record is not None:
            try:
                validate_provenance(record)
            except EvidenceError as exc:
                raise DecisionReferenceError(
                    f"invalid evidence provenance for '{reference}': {exc}"
                ) from exc
        task = store.load_model(Task, record.task_id) if record is not None else None
        valid = False
        if record is not None and task is not None and _scope(store, decision, task.id, None, evidence=True):
            valid = True
            if record.run_id is not None:
                run = store.load_model(Run, record.run_id)
                valid = run is not None and run.task_id == record.task_id
            if valid and record.tool_run_id is not None:
                tool = store.load_model(ToolRun, record.tool_run_id)
                parent = store.load_model(Run, tool.parent_run_id) if tool and tool.parent_run_id else None
                valid = parent is not None and parent.task_id == record.task_id
            if valid and record.artifact_id is not None:
                artifact = store.load_model(Artifact, record.artifact_id)
                valid = artifact is not None and artifact.task_id == record.task_id
    else:
        record = store.load_model(Decision, record_id)
        valid = False
        if record is not None and record.id != decision.id and _scope(store, decision, record.task_id, record.room_id) and record.id not in _seen:
            valid = True
            for child in record.references:
                if child.startswith("decision:"):
                    validate_decision_reference(store, decision, child, _seen=_seen | {record.id})
    if not valid:
        raise DecisionReferenceError(f"missing, foreign, or cyclic decision reference '{reference}'")


def validate_decision_references(store: SqliteRelayStore, decision: Decision) -> None:
    if decision.task_id is not None:
        task = store.load_model(Task, decision.task_id)
        if task is None or task.room_id != decision.room_id:
            raise DecisionReferenceError("decision task and Room scope disagree")
    for reference in decision.references:
        validate_decision_reference(store, decision, reference)

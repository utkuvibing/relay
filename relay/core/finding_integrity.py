"""Canonical source checks shared by Room graphs and decision citations."""

from __future__ import annotations

from relay.storage.models import (
    Artifact,
    ArtifactKind,
    EventLogEntry,
    EventType,
    Finding,
    Run,
    Task,
)
from relay.storage.store import SqliteRelayStore


class FindingIntegrityError(ValueError):
    """A Finding does not have a valid Room review source."""


def resolve_finding_source(
    store: SqliteRelayStore, finding: Finding
) -> tuple[Artifact, Run]:
    """Resolve the exact review artifact and the run that authored it."""
    task = store.load_model(Task, finding.task_id)
    if task is None or task.room_id != finding.room_id:
        raise FindingIntegrityError(f"finding '{finding.id}' names a task outside its Room")
    artifact = store.load_model(Artifact, finding.review_artifact_id)
    if (
        artifact is None
        or artifact.kind is not ArtifactKind.REVIEW_FINDING
        or artifact.task_id != finding.task_id
        or artifact.room_id != finding.room_id
    ):
        raise FindingIntegrityError(f"finding '{finding.id}' names a foreign review artifact")
    run = store.load_model(Run, finding.review_run_id)
    if run is None or run.id != artifact.run_id:
        raise FindingIntegrityError(f"finding '{finding.id}' names a foreign review run")
    markers = [
        marker
        for marker in store.all_models(
            EventLogEntry,
            "WHERE room_id = ? AND type = ?",
            [finding.room_id, EventType.FINDING_RECORDED.value],
        )
        if f"finding:{finding.id}" in marker.references
    ]
    if (
        len(markers) != 1
        or markers[0].task_id != finding.task_id
        or f"artifact:{artifact.id}" not in markers[0].references
    ):
        raise FindingIntegrityError(f"finding '{finding.id}' has no valid review marker")
    return artifact, run

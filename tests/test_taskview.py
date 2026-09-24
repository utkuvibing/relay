"""Task-view builders (P3.4) — position, gaps, ledger.

Read-only derivation from the canonical store: gap lists come from the
state machine's own ``missing_evidence_for`` API (never a parallel opinion),
transition history from the append-only event log via the ``task:{id}``
reference vocabulary. The read-only guarantee itself is tested: building a
view mutates nothing.
"""

from __future__ import annotations

import pytest

from relay.cli.taskview import (
    build_task_ledger,
    build_task_view,
)
from relay.core.evidence import EvidenceKind
from relay.core.permissions import Action
from relay.core.state_machine import TaskState, TaskStateMachine
from relay.storage import connect, migrate
from relay.storage.events import EventLogWriter
from relay.storage.models import (
    Approval,
    Artifact,
    ArtifactKind,
    EventLogEntry,
    EventType,
    EvidenceRecord,
    Task,
)
from relay.storage.store import SqliteEvidenceStore, SqliteRelayStore


@pytest.fixture()
def db(tmp_path):
    conn = connect(tmp_path / "relay.sqlite3")
    migrate(conn)
    yield conn
    conn.close()


@pytest.fixture()
def store(db):
    return SqliteRelayStore(db)


@pytest.fixture()
def evidence(store):
    return SqliteEvidenceStore(store)


@pytest.fixture()
def writer(db):
    return EventLogWriter(db)


def _record(
    evidence: SqliteEvidenceStore,
    kind: EvidenceKind,
    task_id: str,
    produced_by: str = "relay:test",
    **kwargs,
) -> EvidenceRecord:
    return evidence.record(
        EvidenceRecord(kind=kind, task_id=task_id, produced_by=produced_by, **kwargs)
    )


def _advance(store, writer, evidence, task, target) -> Task:
    """Mirror the orchestrator's ``_advance``: transition + persist + event."""
    machine = TaskStateMachine(task_id=task.id, store=evidence, state=task.state)
    previous = machine.state
    machine.transition(target)
    updated = task.model_copy(update={"state": target})
    with store.transaction():
        store.update_model(updated)
        writer.record(
            EventLogEntry(
                type=EventType.STATE_TRANSITIONED,
                content=f"task state: {previous.value} -> {target.value}",
                references=[f"task:{task.id}"],
            )
        )
    return updated


def _view(store, evidence, writer, task, approvals=None):
    return build_task_view(
        task=task,
        evidence_store=evidence,
        events=writer.all(),
        approvals=approvals,
    )


class TestTaskViewGaps:
    def test_created_task_lists_context_gap(self, store, evidence, writer):
        task = store.save_model(Task(title="fresh"))
        view = _view(store, evidence, writer, task)

        assert view.is_terminal is False
        assert view.last_transition is None
        assert [(edge.to_state, edge.missing) for edge in view.edges] == [
            (TaskState.CONTEXT_READY, (EvidenceKind.CONTEXT_COLLECTED,))
        ]


class TestTransitionHistory:
    def test_events_bind_by_reference_not_task_id_column(self, store, evidence, writer):
        """Two tasks, interleaved transitions: each view sees only its own.

        Event rows carry the binding in ``references`` (the ``task_id``
        column is unset by the writer); the reference vocabulary is the
        contract the ledger relies on.
        """
        task_a = store.save_model(Task(title="a"))
        task_b = store.save_model(Task(title="b"))
        _record(evidence, EvidenceKind.CONTEXT_COLLECTED, task_a.id)
        task_a = _advance(store, writer, evidence, task_a, TaskState.CONTEXT_READY)
        _record(evidence, EvidenceKind.CONTEXT_COLLECTED, task_b.id)
        task_b = _advance(store, writer, evidence, task_b, TaskState.CONTEXT_READY)

        view_a = _view(store, evidence, writer, task_a)
        view_b = _view(store, evidence, writer, task_b)
        assert view_a.last_transition.from_state == "created"
        assert view_a.last_transition.to_state == "context_ready"
        assert view_b.last_transition.to_state == "context_ready"
        assert len({view_a.last_transition.sequence, view_b.last_transition.sequence}) == 2


class TestLedger:
    def test_ledger_scopes_everything_to_the_task(self, store, evidence, writer):
        task = store.save_model(Task(title="mine"))
        other = store.save_model(Task(title="other"))
        _record(evidence, EvidenceKind.CONTEXT_COLLECTED, task.id)
        _record(evidence, EvidenceKind.CONTEXT_COLLECTED, other.id)
        mine_artifact = store.save_model(
            Artifact(kind=ArtifactKind.PLAN, task_id=task.id, content="mine")
        )
        store.save_model(Artifact(kind=ArtifactKind.PLAN, task_id=other.id, content="other"))
        store.save_model(
            Approval(action=Action.EDIT_FILES, task_id=other.id, requested_by="relay:review")
        )
        _advance(store, writer, evidence, task, TaskState.CONTEXT_READY)

        ledger = build_task_ledger(
            task=task, store=store, evidence_store=evidence, events=writer.all()
        )
        assert [r.id for r in ledger.evidence] == [r.id for r in evidence.records_for_task(task.id)]
        assert [a.id for a in ledger.artifacts] == [mine_artifact.id]
        assert ledger.approvals == ()
        assert len(ledger.transitions) == 1


class TestReadOnly:
    def test_building_views_mutates_nothing(self, store, evidence, writer, db):
        task = store.save_model(Task(title="untouched"))
        _record(evidence, EvidenceKind.CONTEXT_COLLECTED, task.id)
        store.save_model(
            Approval(action=Action.EDIT_FILES, task_id=task.id, requested_by="relay:review")
        )
        _advance(store, writer, evidence, task, TaskState.CONTEXT_READY)

        tasks_before = store.counts()
        _view(store, evidence, writer, task, approvals=list(store.all_models(Approval)))
        build_task_ledger(task=task, store=store, evidence_store=evidence, events=writer.all())
        assert store.counts() == tasks_before

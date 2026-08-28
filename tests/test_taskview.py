"""Task-view builders (P3.4) — position, gaps, ledger.

Read-only derivation from the canonical store: gap lists come from the
state machine's own ``missing_evidence_for`` API (never a parallel opinion),
transition history from the append-only event log via the ``task:{id}``
reference vocabulary. The read-only guarantee itself is tested: building a
view mutates nothing.
"""

from __future__ import annotations

import json
from datetime import datetime

import pytest

from relay.cli.taskview import (
    build_task_ledger,
    build_task_view,
    ledger_payload,
    task_events,
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
        assert [(e.to_state, e.missing) for e in view.edges] == [
            (TaskState.CONTEXT_READY, (EvidenceKind.CONTEXT_COLLECTED,))
        ]

    def test_verify_blocked_task_reports_missing_tests_passed(self, store, evidence, writer):
        task = store.save_model(Task(title="blocked at verifying"))
        _record(evidence, EvidenceKind.CONTEXT_COLLECTED, task.id)
        task = _advance(store, writer, evidence, task, TaskState.CONTEXT_READY)
        _record(evidence, EvidenceKind.PLAN_PRODUCED, task.id, run_id="run-plan")
        task = _advance(store, writer, evidence, task, TaskState.PLAN_READY)
        task = _advance(store, writer, evidence, task, TaskState.IMPLEMENTING)
        _record(evidence, EvidenceKind.IMPLEMENTATION_PRODUCED, task.id, run_id="run-impl")
        task = _advance(store, writer, evidence, task, TaskState.IMPLEMENTED)
        task = _advance(store, writer, evidence, task, TaskState.VERIFYING)

        view = _view(store, evidence, writer, task)
        gaps = {e.to_state: e.missing for e in view.edges}
        # The progress edge is gated; the rework edge is always available.
        assert gaps[TaskState.REVIEWING] == (EvidenceKind.TESTS_PASSED,)
        assert gaps[TaskState.IMPLEMENTING] == ()
        assert view.last_transition.to_state == "verifying"

    def test_ungated_edge_renders_ready(self, store, evidence, writer):
        task = store.save_model(Task(title="plan ready", state=TaskState.PLAN_READY))
        view = _view(store, evidence, writer, task)
        # PLAN_READY -> IMPLEMENTING demands no evidence (D.3 implicit freeze).
        assert [(e.to_state, e.missing) for e in view.edges] == [(TaskState.IMPLEMENTING, ())]

    def test_done_is_terminal_with_no_edges(self, store, evidence, writer):
        task = store.save_model(Task(title="finished", state=TaskState.DONE))
        view = _view(store, evidence, writer, task)
        assert view.is_terminal is True
        assert view.edges == ()


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

    def test_history_walk_yields_ordered_transitions(self, store, evidence, writer):
        task = store.save_model(Task(title="walker"))
        _record(evidence, EvidenceKind.CONTEXT_COLLECTED, task.id)
        task = _advance(store, writer, evidence, task, TaskState.CONTEXT_READY)
        _record(evidence, EvidenceKind.PLAN_PRODUCED, task.id, run_id="run-plan")
        task = _advance(store, writer, evidence, task, TaskState.PLAN_READY)

        ledger = build_task_ledger(
            task=task, store=store, evidence_store=evidence, events=writer.all()
        )
        walks = [(t.from_state, t.to_state) for t in ledger.transitions]
        assert walks == [
            ("created", "context_ready"),
            ("context_ready", "plan_ready"),
        ]
        sequences = [t.sequence for t in ledger.transitions]
        assert sequences == sorted(sequences)
        assert all(isinstance(t.at, datetime) for t in ledger.transitions)

    def test_task_events_filters_on_reference_marker(self, writer):
        marker_entry = EventLogEntry(
            type=EventType.STATE_TRANSITIONED,
            content="task state: created -> context_ready",
            references=["task:abc", "run:r1"],
        )
        other = EventLogEntry(
            type=EventType.STATE_TRANSITIONED,
            content="task state: created -> context_ready",
            references=["task:zzz"],
        )
        writer.record(marker_entry)
        writer.record(other)
        events = writer.all()
        assert [e.references for e in task_events(events, "abc")] == [["task:abc", "run:r1"]]


class TestPendingApproval:
    def test_pending_approval_surfaced(self, store, evidence, writer):
        task = store.save_model(Task(title="gated", state=TaskState.APPROVAL_REQUIRED))
        approval = store.save_model(
            Approval(
                action=Action.EDIT_FILES,
                task_id=task.id,
                requested_by="relay:review",
                reason="completion",
            )
        )
        view = _view(store, evidence, writer, task, approvals=[approval])
        assert view.pending_approval is approval
        assert [(e.to_state, e.missing) for e in view.edges] == [
            (TaskState.DONE, (EvidenceKind.APPROVAL_GRANTED,))
        ]

    def test_decided_approval_no_longer_pending(self, store, evidence, writer):
        from relay.storage.models import ApprovalStatus

        task = store.save_model(Task(title="gated", state=TaskState.APPROVAL_REQUIRED))
        approval = store.save_model(
            Approval(
                action=Action.EDIT_FILES,
                task_id=task.id,
                requested_by="relay:review",
                status=ApprovalStatus.APPROVED,
                decided_by="kaya",
            )
        )
        view = _view(store, evidence, writer, task, approvals=[approval])
        assert view.pending_approval is None


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

    def test_ledger_payload_is_json_safe(self, store, evidence, writer):
        task = store.save_model(Task(title="json"))
        _record(evidence, EvidenceKind.CONTEXT_COLLECTED, task.id)
        store.save_model(Artifact(kind=ArtifactKind.REPORT, task_id=task.id, content="brief"))
        task = _advance(store, writer, evidence, task, TaskState.CONTEXT_READY)

        ledger = build_task_ledger(
            task=task, store=store, evidence_store=evidence, events=writer.all()
        )
        payload = ledger_payload(ledger)
        encoded = json.dumps(payload)  # must not raise
        assert payload["task"]["state"] == "context_ready"
        assert payload["edges"] == [{"to": "plan_ready", "missing": ["plan_produced"]}]
        assert payload["last_transition"]["to"] == "context_ready"
        assert payload["evidence"][0]["kind"] == "context_collected"
        assert payload["artifacts"][0]["content"] == "brief"
        assert encoded


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

"""The state machine is Relay's core promise (SPEC §6, §33):

a model cannot close a task; only recorded evidence can.
"""

import pytest

from relay.core.state_machine import (
    EvidenceKind,
    IllegalTransitionError,
    MissingEvidenceError,
    TaskState,
    TaskStateMachine,
)

#: The happy-path chain from SPEC §6 with the evidence each edge requires.
HAPPY_PATH: list[tuple[TaskState, set[EvidenceKind]]] = [
    (TaskState.CONTEXT_READY, {EvidenceKind.CONTEXT_COLLECTED}),
    (TaskState.PLAN_READY, {EvidenceKind.PLAN_PRODUCED}),
    (TaskState.IMPLEMENTING, set()),
    (TaskState.IMPLEMENTED, {EvidenceKind.IMPLEMENTATION_PRODUCED}),
    (TaskState.VERIFYING, set()),
    (TaskState.REVIEWING, {EvidenceKind.TESTS_PASSED}),
    (TaskState.APPROVAL_REQUIRED, {EvidenceKind.REVIEW_PASSED}),
    (TaskState.DONE, {EvidenceKind.APPROVAL_GRANTED}),
]


class TestHappyPath:
    def test_full_lifecycle_reaches_done(self):
        sm = TaskStateMachine()
        for target, evidence in HAPPY_PATH:
            sm.advance(target, evidence)
        assert sm.state is TaskState.DONE
        assert sm.is_terminal

    def test_each_step_requires_exactly_its_own_evidence(self):
        for i, (target, evidence) in enumerate(HAPPY_PATH):
            sm = TaskStateMachine(TaskState.CREATED)
            for prev_target, prev_evidence in HAPPY_PATH[:i]:
                sm.advance(prev_target, prev_evidence)
            # Presenting every OTHER kind of evidence must NOT unlock a
            # gated step; unguarded edges (empty evidence) are skipped.
            if evidence:
                decoys = set(EvidenceKind) - evidence
                with pytest.raises(MissingEvidenceError):
                    sm.advance(target, decoys)


class TestReworkLoops:
    def test_failed_tests_return_task_to_implementing(self):
        sm = TaskStateMachine(TaskState.VERIFYING)
        sm.advance(TaskState.IMPLEMENTING)  # FAIL → rework, no gate
        assert sm.state is TaskState.IMPLEMENTING

    def test_fix_required_returns_task_to_implementing(self):
        sm = TaskStateMachine(TaskState.REVIEWING)
        sm.advance(TaskState.IMPLEMENTING)  # FIX_REQUIRED → rework
        assert sm.state is TaskState.IMPLEMENTING

    def test_rework_can_reach_done_again(self):
        sm = TaskStateMachine(TaskState.VERIFYING)
        sm.advance(TaskState.IMPLEMENTING)
        sm.advance(TaskState.IMPLEMENTED, {EvidenceKind.IMPLEMENTATION_PRODUCED})
        sm.advance(TaskState.VERIFYING)
        sm.advance(TaskState.REVIEWING, {EvidenceKind.TESTS_PASSED})
        sm.advance(TaskState.APPROVAL_REQUIRED, {EvidenceKind.REVIEW_PASSED})
        sm.advance(TaskState.DONE, {EvidenceKind.APPROVAL_GRANTED})
        assert sm.state is TaskState.DONE


class TestGates:
    def test_done_blocked_without_approval_even_if_model_claims_complete(self):
        """SPEC §33 Reliability: a verbal 'PASS' is not verification."""
        sm = TaskStateMachine(TaskState.APPROVAL_REQUIRED)
        with pytest.raises(MissingEvidenceError) as excinfo:
            sm.advance(TaskState.DONE)
        assert EvidenceKind.APPROVAL_GRANTED in excinfo.value.missing
        assert sm.state is TaskState.APPROVAL_REQUIRED  # unchanged

    def test_reviewing_blocked_when_tests_failed(self):
        sm = TaskStateMachine(TaskState.VERIFYING)
        with pytest.raises(MissingEvidenceError):
            sm.advance(TaskState.REVIEWING)  # no TESTS_PASSED presented


class TestIllegalMoves:
    @pytest.mark.parametrize(
        ("current", "target"),
        [
            (TaskState.CREATED, TaskState.PLAN_READY),  # skipping context
            (TaskState.CREATED, TaskState.DONE),  # the fantasy shortcut
            (TaskState.IMPLEMENTING, TaskState.DONE),
            (TaskState.DONE, TaskState.CREATED),  # terminal stays terminal
        ],
    )
    def test_nonexistent_edges_are_refused(self, current, target):
        sm = TaskStateMachine(current)
        all_evidence = set(EvidenceKind)
        with pytest.raises(IllegalTransitionError):
            sm.advance(target, all_evidence)  # even ALL evidence can't help

    def test_can_advance_reports_without_raising(self):
        sm = TaskStateMachine(TaskState.CREATED)
        assert not sm.can_advance(TaskState.DONE, set(EvidenceKind))
        assert not sm.can_advance(TaskState.CONTEXT_READY)  # missing evidence
        assert sm.can_advance(TaskState.CONTEXT_READY, {EvidenceKind.CONTEXT_COLLECTED})

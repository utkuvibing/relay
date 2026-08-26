"""Deterministic task lifecycle owned by Relay — never by an LLM.

SPEC reference: §6 (State Machine), §33 (Success Criteria — Reliability).

A model saying "everything is complete" carries zero authority here.
A transition is granted only when the edge exists AND every piece of
evidence it requires has actually been produced and recorded.
"""

from __future__ import annotations

import enum
from collections.abc import Iterable
from dataclasses import dataclass


class TaskState(str, enum.Enum):
    """Lifecycle states of a software task (SPEC §6)."""

    CREATED = "created"
    CONTEXT_READY = "context_ready"
    PLAN_READY = "plan_ready"
    IMPLEMENTING = "implementing"
    IMPLEMENTED = "implemented"
    VERIFYING = "verifying"
    REVIEWING = "reviewing"
    APPROVAL_REQUIRED = "approval_required"
    DONE = "done"


class EvidenceKind(str, enum.Enum):
    """Verification artifacts Relay must hold before granting a transition.

    These correspond to hard gates (SPEC §6): tests pass, review passes,
    required approvals granted. An agent's claim is not evidence.
    """

    CONTEXT_COLLECTED = "context_collected"
    PLAN_PRODUCED = "plan_produced"
    IMPLEMENTATION_PRODUCED = "implementation_produced"
    TESTS_PASSED = "tests_passed"
    REVIEW_PASSED = "review_passed"
    APPROVAL_GRANTED = "approval_granted"


@dataclass(frozen=True)
class Transition:
    """A legal edge in the task graph, plus the evidence it demands."""

    target: TaskState
    required_evidence: frozenset[EvidenceKind] = frozenset()


#: All legal edges. Anything not listed here is illegal by construction.
TRANSITIONS: dict[TaskState, tuple[Transition, ...]] = {
    TaskState.CREATED: (
        Transition(TaskState.CONTEXT_READY, frozenset({EvidenceKind.CONTEXT_COLLECTED})),
    ),
    TaskState.CONTEXT_READY: (
        Transition(TaskState.PLAN_READY, frozenset({EvidenceKind.PLAN_PRODUCED})),
    ),
    TaskState.PLAN_READY: (
        Transition(TaskState.IMPLEMENTING),
    ),
    TaskState.IMPLEMENTING: (
        Transition(TaskState.IMPLEMENTED, frozenset({EvidenceKind.IMPLEMENTATION_PRODUCED})),
    ),
    TaskState.IMPLEMENTED: (
        Transition(TaskState.VERIFYING),
    ),
    # FAIL → back to implementation (SPEC §6 diagram).
    TaskState.VERIFYING: (
        Transition(TaskState.REVIEWING, frozenset({EvidenceKind.TESTS_PASSED})),
        Transition(TaskState.IMPLEMENTING),
    ),
    # FIX_REQUIRED → back to implementation (SPEC §6 diagram).
    TaskState.REVIEWING: (
        Transition(
            TaskState.APPROVAL_REQUIRED,
            frozenset({EvidenceKind.REVIEW_PASSED}),
        ),
        Transition(TaskState.IMPLEMENTING),
    ),
    # No task closes without explicit approval evidence.
    TaskState.APPROVAL_REQUIRED: (
        Transition(TaskState.DONE, frozenset({EvidenceKind.APPROVAL_GRANTED})),
    ),
    TaskState.DONE: (),
}


class StateMachineError(Exception):
    """Base class for task lifecycle violations."""


class IllegalTransitionError(StateMachineError):
    """No legal edge exists between the two states."""

    def __init__(self, current: TaskState, target: TaskState) -> None:
        self.current = current
        self.target = target
        super().__init__(f"Illegal transition: {current.value} -> {target.value}")


class MissingEvidenceError(StateMachineError):
    """The edge exists but required verification evidence is absent."""

    def __init__(
        self,
        current: TaskState,
        target: TaskState,
        missing: set[EvidenceKind],
    ) -> None:
        self.current = current
        self.target = target
        self.missing = missing
        names = ", ".join(sorted(e.value for e in missing))
        super().__init__(
            f"Transition {current.value} -> {target.value} blocked, missing evidence: {names}"
        )


class TaskStateMachine:
    """Per-task guard over the transition table.

    Holds the current state and grants or refuses transitions based on
    recorded evidence — never on agent claims.
    """

    def __init__(self, state: TaskState = TaskState.CREATED) -> None:
        self._state = state

    @property
    def state(self) -> TaskState:
        return self._state

    @property
    def is_terminal(self) -> bool:
        return not TRANSITIONS[self._state]

    def available_transitions(self) -> tuple[Transition, ...]:
        return TRANSITIONS[self._state]

    def can_advance(self, target: TaskState, evidence: Iterable[EvidenceKind] = ()) -> bool:
        try:
            self._check(target, evidence)
            return True
        except StateMachineError:
            return False

    def advance(
        self,
        target: TaskState,
        evidence: Iterable[EvidenceKind] = (),
    ) -> TaskState:
        """Move to ``target`` if the edge is legal and evidence suffices."""
        self._check(target, evidence)
        self._state = target
        return self._state

    def _check(self, target: TaskState, evidence: Iterable[EvidenceKind]) -> Transition:
        candidates = [t for t in TRANSITIONS[self._state] if t.target is target]
        if not candidates:
            raise IllegalTransitionError(self._state, target)

        transition = candidates[0]
        held = set(evidence)
        missing = transition.required_evidence - held
        if missing:
            raise MissingEvidenceError(self._state, target, missing)
        return transition

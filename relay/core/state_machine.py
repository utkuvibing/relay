"""Deterministic task lifecycle owned by Relay — never by an LLM.

SPEC reference: §6 (State Machine), §33 (Reliability); Appendix A (hardening).

Two pillars:

* **Evidence, not claims.** The machine accepts NO evidence arguments from
  callers. It reads provenance-backed ``EvidenceRecord`` objects from the
  ``EvidenceStore`` it is bound to at construction. An ``EvidenceKind``
  enum in an agent's hand is a claim; only a stored record is proof.

* **Policy-gated completion.** Human approval is mandatory only when policy
  requires it; the direct ``REVIEWING -> DONE`` path still demands review +
  verification evidence and proof that no approvals are pending.

A model saying "everything is complete" carries zero authority here.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass

from relay.core.evidence import EvidenceKind, EvidenceStore


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


# Re-exported for backward compatibility with Phase 0 consumers.
__all__ = [
    "EvidenceKind",
    "IllegalTransitionError",
    "MissingEvidenceError",
    "StateMachineError",
    "TaskState",
    "TaskStateMachine",
    "Transition",
]


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
    TaskState.PLAN_READY: (Transition(TaskState.IMPLEMENTING),),
    TaskState.IMPLEMENTING: (
        Transition(TaskState.IMPLEMENTED, frozenset({EvidenceKind.IMPLEMENTATION_PRODUCED})),
    ),
    TaskState.IMPLEMENTED: (Transition(TaskState.VERIFYING),),
    # FAIL → back to implementation (SPEC §6 diagram).
    TaskState.VERIFYING: (
        # Entering review demands recorded test proof.
        Transition(TaskState.REVIEWING, frozenset({EvidenceKind.TESTS_PASSED})),
        Transition(TaskState.IMPLEMENTING),
    ),
    # FIX_REQUIRED → back to implementation (SPEC §6 diagram).
    TaskState.REVIEWING: (
        # Gated path: a human must explicitly approve.
        Transition(
            TaskState.APPROVAL_REQUIRED,
            frozenset({EvidenceKind.REVIEW_PASSED}),
        ),
        # Direct path (SPEC Appendix A.3): allowed only when policy needs no
        # human sign-off — and even then it demands full verification:
        # recorded tests + recorded review + relay-attested empty approval queue.
        Transition(
            TaskState.DONE,
            frozenset(
                {
                    EvidenceKind.TESTS_PASSED,
                    EvidenceKind.REVIEW_PASSED,
                    EvidenceKind.NO_PENDING_APPROVALS,
                }
            ),
        ),
        Transition(TaskState.IMPLEMENTING),
    ),
    # Gated completion: no task closes without explicit human approval
    # recorded as evidence produced by `human:*`.
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
    """The edge exists but trusted evidence for its gates is not in the store."""

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
            f"Transition {current.value} -> {target.value} blocked, "
            f"missing stored evidence: {names}"
        )


class TaskStateMachine:
    """Per-task guard over the transition table.

    Bound to one ``task_id`` and one trusted ``EvidenceStore``. Transitions
    are granted only when every piece of evidence an edge requires has been
    *recorded in the store for this task* — never because a caller passed
    enum values around.

    The rework loops VERIFYING → IMPLEMENTING and REVIEWING → IMPLEMENTING
    stay ungated so tests-failed / fix-required cycles are always available.
    """

    def __init__(
        self,
        *,
        task_id: str,
        store: EvidenceStore,
        state: TaskState = TaskState.CREATED,
    ) -> None:
        self._task_id = task_id
        self._store = store
        self._state = state

    @property
    def task_id(self) -> str:
        return self._task_id

    @property
    def state(self) -> TaskState:
        return self._state

    @property
    def is_terminal(self) -> bool:
        return not TRANSITIONS[self._state]

    @property
    def store(self) -> EvidenceStore:
        return self._store

    def available_transitions(self) -> tuple[Transition, ...]:
        return TRANSITIONS[self._state]

    def transition(self, target: TaskState) -> Transition:
        """Move to ``target`` if the edge is legal and the store holds proof."""
        edge = self._resolve(target)
        self._state = target
        return edge

    def can_transition(self, target: TaskState) -> bool:
        """Pure check — reports feasibility without mutating state."""
        try:
            self._resolve(target)
            return True
        except StateMachineError:
            return False

    def missing_evidence_for(self, target: TaskState) -> set[EvidenceKind]:
        """Which kinds of stored evidence still block this edge?

        Raises only ``IllegalTransitionError`` (no such edge); an existing
        but blocked edge yields its gap set instead of raising.
        """
        edge = self._lookup_edge(target)
        return set(edge.required_evidence) - self._recorded_kinds()

    def _resolve(self, target: TaskState) -> Transition:
        edge = self._lookup_edge(target)
        missing = edge.required_evidence - self._recorded_kinds()
        if missing:
            raise MissingEvidenceError(self._state, target, missing)
        return edge

    def _lookup_edge(self, target: TaskState) -> Transition:
        candidates = [t for t in TRANSITIONS[self._state] if t.target is target]
        if not candidates:
            raise IllegalTransitionError(self._state, target)
        return candidates[0]

    def _recorded_kinds(self) -> frozenset[EvidenceKind]:
        # Defense in depth: filter by task binding client-side too, so a
        # misbehaving custom store cannot leak cross-task evidence in.
        return frozenset(
            record.kind
            for record in self._store.records_for_task(self._task_id)
            if record.task_id == self._task_id
        )

"""Verification evidence: the only thing that can move a task through gates.

SPEC reference: §6 (State Machine), §33 (Reliability); SPEC Appendix A.

Hardening rules (Phase 0):

* An ``EvidenceKind`` value in a caller's hand is a **claim**, not proof.
* Proof is an ``EvidenceRecord`` written into an ``EvidenceStore``, carrying
  the provenance its kind demands (which run, which tool run, who produced
  it).
* The state machine reads evidence exclusively from the store, scoped to
  its own task. It accepts no evidence from callers — so a model cannot
  forge a transition by asserting an enum.

Producer conventions (enforced by ``validate_provenance``):

* ``human:<name>``  — a human decision (only this may produce APPROVAL_GRANTED)
* ``relay:<component>`` — Relay itself (only this may produce NO_PENDING_APPROVALS)
* ``agent:<name>`` / adapter names — model-authored output

An agent literally cannot write approval or policy evidence: the store
refuses it before the state machine could ever see it.
"""

from __future__ import annotations

import enum
from collections.abc import Sequence
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:  # pragma: no cover
    from relay.storage.models import EvidenceRecord


class EvidenceKind(str, enum.Enum):
    """Verification artifacts Relay must hold before granting a transition.

    These correspond to hard gates (SPEC §6): tests pass, review passes,
    required approvals granted. An agent's textual claim is not evidence;
    a provenance-backed ``EvidenceRecord`` is.
    """

    CONTEXT_COLLECTED = "context_collected"
    PLAN_PRODUCED = "plan_produced"
    IMPLEMENTATION_PRODUCED = "implementation_produced"
    TESTS_PASSED = "tests_passed"
    REVIEW_PASSED = "review_passed"
    APPROVAL_GRANTED = "approval_granted"
    #: Recorded by Relay when policy allows completion and no approvals
    #: are pending. Enables the direct REVIEWING -> DONE path (SPEC App. A.3).
    NO_PENDING_APPROVALS = "no_pending_approvals"


#: Optional linkage fields each evidence kind MUST carry, on top of the
#: always-required ``produced_by``. Verification-grade kinds demand a
#: pointer to the run or tool execution that produced them.
PROVENANCE_REQUIREMENTS: dict[EvidenceKind, frozenset[str]] = {
    EvidenceKind.CONTEXT_COLLECTED: frozenset(),
    EvidenceKind.PLAN_PRODUCED: frozenset({"run_id"}),
    EvidenceKind.IMPLEMENTATION_PRODUCED: frozenset({"run_id"}),
    EvidenceKind.TESTS_PASSED: frozenset({"tool_run_id"}),
    EvidenceKind.REVIEW_PASSED: frozenset({"run_id"}),
    EvidenceKind.APPROVAL_GRANTED: frozenset(),
    EvidenceKind.NO_PENDING_APPROVALS: frozenset(),
}

#: Producer prefixes permitted to author each evidence kind. Empty frozenset
#: means any producer convention is acceptable.
PRODUCER_REQUIREMENTS: dict[EvidenceKind, frozenset[str]] = {
    EvidenceKind.APPROVAL_GRANTED: frozenset({"human:"}),
    EvidenceKind.NO_PENDING_APPROVALS: frozenset({"relay:"}),
}


class EvidenceError(Exception):
    """Base class for evidence-integrity violations."""


class MissingProvenanceError(EvidenceError):
    """Evidence lacks the linkage fields its kind demands."""

    def __init__(self, kind: EvidenceKind, missing_fields: set[str]) -> None:
        self.kind = kind
        self.missing_fields = missing_fields
        names = ", ".join(sorted(missing_fields))
        super().__init__(f"Evidence kind '{kind.value}' requires provenance fields: {names}")


class InvalidProducerError(EvidenceError):
    """Evidence was authored by a producer who may not attest this kind."""

    def __init__(self, kind: EvidenceKind, produced_by: str, allowed: frozenset[str]) -> None:
        self.kind = kind
        self.produced_by = produced_by
        self.allowed_prefixes = allowed
        prefixes = ", ".join(sorted(allowed))
        super().__init__(
            f"Evidence kind '{kind.value}' may only be produced by prefixes "
            f"[{prefixes}]; got '{produced_by}'"
        )


def validate_provenance(record: EvidenceRecord) -> None:
    """Raise unless ``record`` satisfies its kind's provenance contract.

    Called by every trustworthy store implementation on ``record()`` —
    forged or incomplete evidence must never enter the store at all.
    """
    missing = {
        field
        for field in PROVENANCE_REQUIREMENTS.get(record.kind, frozenset())
        if getattr(record, field) is None
    }
    if missing:
        raise MissingProvenanceError(record.kind, missing)

    allowed = PRODUCER_REQUIREMENTS.get(record.kind, frozenset())
    if allowed and not any(record.produced_by.startswith(p) for p in allowed):
        raise InvalidProducerError(record.kind, record.produced_by, allowed)


class EvidenceStore(Protocol):
    """Read/append access to trusted evidence. Append-only by contract.

    Implementations MUST call :func:`validate_provenance` on ``record``
    and MUST scope ``records_for_task`` strictly by ``task_id``.
    """

    def record(self, evidence: EvidenceRecord) -> EvidenceRecord:
        """Persist one evidence record; returns it unchanged."""
        ...

    def records_for_task(
        self,
        task_id: str,
        kind: EvidenceKind | None = None,
    ) -> Sequence[EvidenceRecord]:
        """All stored records for one task, optionally filtered by kind."""
        ...


class InMemoryEvidenceStore:
    """Reference ``EvidenceStore`` — process-local, append-only.

    The SQLite-backed store (Phase 1) replaces this at the seam without
    changing the state machine.
    """

    def __init__(self) -> None:
        self._records: list[EvidenceRecord] = []

    def record(self, evidence: EvidenceRecord) -> EvidenceRecord:
        validate_provenance(evidence)
        self._records.append(evidence)
        return evidence

    def records_for_task(
        self,
        task_id: str,
        kind: EvidenceKind | None = None,
    ) -> Sequence[EvidenceRecord]:
        return tuple(
            r for r in self._records if r.task_id == task_id and (kind is None or r.kind is kind)
        )

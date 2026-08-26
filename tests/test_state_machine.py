"""Hardened Phase 0 guarantees (SPEC §6, §33; Appendix A).

Core invariants under test:

* An EvidenceKind enum is a CLAIM; only provenance-backed records in the
  bound store are PROOF. The machine accepts no evidence arguments.
* Verification evidence (TESTS_PASSED / REVIEW_PASSED) cannot be forged
  by model-authored assertions.
* Evidence is task-scoped; duplicates are inert; missing provenance is
  rejected at the store boundary.
* Human approval is conditional on policy — but when policy requires it,
  no path to DONE exists without an explicit human-produced record.
"""

import pytest

from relay.core.evidence import (
    EvidenceKind,
    InMemoryEvidenceStore,
    InvalidProducerError,
    MissingProvenanceError,
)
from relay.core.permissions import CompletionPolicy
from relay.core.state_machine import (
    IllegalTransitionError,
    MissingEvidenceError,
    TaskState,
    TaskStateMachine,
)
from relay.storage.models import EvidenceRecord

TASK = "task-1"
OTHER_TASK = "task-OTHER"

#: Producer identity per kind, honoring the store's producer conventions.
PRODUCER: dict[EvidenceKind, str] = {
    EvidenceKind.CONTEXT_COLLECTED: "relay:context-engine",
    EvidenceKind.PLAN_PRODUCED: "agent:gpt",
    EvidenceKind.IMPLEMENTATION_PRODUCED: "agent:codex",
    EvidenceKind.TESTS_PASSED: "relay:test-runner",
    EvidenceKind.REVIEW_PASSED: "agent:claude",
    EvidenceKind.APPROVAL_GRANTED: "human:utku",
    EvidenceKind.NO_PENDING_APPROVALS: "relay:policy",
}

#: Linkage fields each kind needs to satisfy its provenance contract.
LINKAGE: dict[EvidenceKind, dict[str, str]] = {
    EvidenceKind.PLAN_PRODUCED: {"run_id": "run-plan"},
    EvidenceKind.IMPLEMENTATION_PRODUCED: {"run_id": "run-impl"},
    EvidenceKind.TESTS_PASSED: {"tool_run_id": "tool-pytest"},
    EvidenceKind.REVIEW_PASSED: {"run_id": "run-review"},
}


def make_record(kind: EvidenceKind, task_id: str = TASK, **overrides) -> EvidenceRecord:
    """A fully provenance-compliant record for ``kind``."""
    fields: dict = {"kind": kind, "task_id": task_id, "produced_by": PRODUCER[kind]}
    fields.update(LINKAGE.get(kind, {}))
    fields.update(overrides)
    return EvidenceRecord(**fields)


def fresh_machine(
    state: TaskState = TaskState.CREATED,
    *,
    task_id: str = TASK,
    store: InMemoryEvidenceStore | None = None,
) -> TaskStateMachine:
    return TaskStateMachine(
        task_id=task_id,
        store=store if store is not None else InMemoryEvidenceStore(),
        state=state,
    )


#: The happy-path chain from SPEC §6 with the evidence each edge requires.
HAPPY_PATH: list[tuple[TaskState, list[EvidenceKind]]] = [
    (TaskState.CONTEXT_READY, [EvidenceKind.CONTEXT_COLLECTED]),
    (TaskState.PLAN_READY, [EvidenceKind.PLAN_PRODUCED]),
    (TaskState.IMPLEMENTING, []),
    (TaskState.IMPLEMENTED, [EvidenceKind.IMPLEMENTATION_PRODUCED]),
    (TaskState.VERIFYING, []),
    (TaskState.REVIEWING, [EvidenceKind.TESTS_PASSED]),
    (TaskState.APPROVAL_REQUIRED, [EvidenceKind.REVIEW_PASSED]),
    (TaskState.DONE, [EvidenceKind.APPROVAL_GRANTED]),
]


def walk(
    machine: TaskStateMachine,
    store: InMemoryEvidenceStore,
    steps: list[tuple[TaskState, list[EvidenceKind]]],
) -> None:
    for target, kinds in steps:
        for kind in kinds:
            store.record(make_record(kind))
        machine.transition(target)


class TestHappyPath:
    def test_full_lifecycle_reaches_done(self):
        store = InMemoryEvidenceStore()
        sm = fresh_machine(store=store)
        walk(sm, store, HAPPY_PATH)
        assert sm.state is TaskState.DONE
        assert sm.is_terminal

    def test_gated_steps_reject_any_other_evidence_mix(self):
        all_kinds = set(EvidenceKind)
        for i, (target, kinds) in enumerate(HAPPY_PATH):
            if not kinds:
                continue  # unguarded edge: any evidence is irrelevant
            store = InMemoryEvidenceStore()
            sm = fresh_machine(store=store)
            walk(sm, store, HAPPY_PATH[:i])
            # Every kind EXCEPT this step's own must not unlock the edge.
            for kind in all_kinds - set(kinds):
                store.record(make_record(kind))
            with pytest.raises(MissingEvidenceError):
                sm.transition(target)


class TestReworkLoops:
    def test_failed_tests_return_task_to_implementing(self):
        sm = fresh_machine(TaskState.VERIFYING)
        sm.transition(TaskState.IMPLEMENTING)  # FAIL → rework, no gate
        assert sm.state is TaskState.IMPLEMENTING

    def test_fix_required_returns_task_to_implementing(self):
        sm = fresh_machine(TaskState.REVIEWING)
        sm.transition(TaskState.IMPLEMENTING)  # FIX_REQUIRED → rework
        assert sm.state is TaskState.IMPLEMENTING

    def test_rework_can_reach_done_again(self):
        store = InMemoryEvidenceStore()
        sm = fresh_machine(TaskState.VERIFYING, store=store)
        sm.transition(TaskState.IMPLEMENTING)
        store.record(make_record(EvidenceKind.IMPLEMENTATION_PRODUCED))
        sm.transition(TaskState.IMPLEMENTED)
        sm.transition(TaskState.VERIFYING)
        store.record(make_record(EvidenceKind.TESTS_PASSED))
        sm.transition(TaskState.REVIEWING)
        store.record(make_record(EvidenceKind.REVIEW_PASSED))
        sm.transition(TaskState.APPROVAL_REQUIRED)
        store.record(make_record(EvidenceKind.APPROVAL_GRANTED))
        sm.transition(TaskState.DONE)
        assert sm.state is TaskState.DONE


class TestClaimsAreNotProof:
    """The old API let callers pass enum values as proof. It no longer exists.

    These tests pin the replacement semantics: with nothing (or only
    irrelevant evidence) in the trusted store, a claimed pass moves nothing.
    """

    def test_raw_tests_claim_cannot_satisfy_tests_passed(self):
        sm = fresh_machine(TaskState.VERIFYING)  # empty store; model says "tests pass"
        with pytest.raises(MissingEvidenceError) as excinfo:
            sm.transition(TaskState.REVIEWING)
        assert excinfo.value.missing == {EvidenceKind.TESTS_PASSED}
        assert sm.state is TaskState.VERIFYING

    def test_raw_review_claim_cannot_satisfy_review_passed(self):
        store = InMemoryEvidenceStore()
        store.record(make_record(EvidenceKind.TESTS_PASSED))  # real, but review is not
        sm = fresh_machine(TaskState.REVIEWING, store=store)
        with pytest.raises(MissingEvidenceError) as excinfo:
            sm.transition(TaskState.APPROVAL_REQUIRED)
        assert excinfo.value.missing == {EvidenceKind.REVIEW_PASSED}

    def test_provenance_backed_evidence_does_satisfy_transitions(self):
        store = InMemoryEvidenceStore()
        sm = fresh_machine(TaskState.VERIFYING, store=store)
        store.record(make_record(EvidenceKind.TESTS_PASSED, tool_run_id="tool-ci-9"))
        sm.transition(TaskState.REVIEWING)
        store.record(make_record(EvidenceKind.REVIEW_PASSED, run_id="run-reviewer"))
        sm.transition(TaskState.APPROVAL_REQUIRED)
        assert sm.state is TaskState.APPROVAL_REQUIRED


class TestEvidenceIntegrity:
    def test_foreign_task_evidence_cannot_satisfy_a_transition(self):
        store = InMemoryEvidenceStore()
        store.record(make_record(EvidenceKind.TESTS_PASSED, task_id=OTHER_TASK))
        sm = fresh_machine(TaskState.VERIFYING, store=store)
        assert not sm.can_transition(TaskState.REVIEWING)
        # Once the SAME task holds the evidence, the gate opens.
        store.record(make_record(EvidenceKind.TESTS_PASSED))
        assert sm.can_transition(TaskState.REVIEWING)

    def test_duplicate_evidence_does_not_change_semantics(self):
        store = InMemoryEvidenceStore()
        sm = fresh_machine(TaskState.VERIFYING, store=store)
        assert sm.missing_evidence_for(TaskState.REVIEWING) == {EvidenceKind.TESTS_PASSED}
        store.record(make_record(EvidenceKind.TESTS_PASSED))
        first_missing = sm.missing_evidence_for(TaskState.REVIEWING)
        store.record(make_record(EvidenceKind.TESTS_PASSED))  # duplicate
        assert len(store.records_for_task(TASK, EvidenceKind.TESTS_PASSED)) == 2
        assert sm.missing_evidence_for(TaskState.REVIEWING) == first_missing == set()
        sm.transition(TaskState.REVIEWING)  # exactly once, unaffected by dupes

    def test_verification_kinds_demand_provenance_at_the_store(self):
        store = InMemoryEvidenceStore()
        with pytest.raises(MissingProvenanceError):
            store.record(make_record(EvidenceKind.TESTS_PASSED, tool_run_id=None))
        with pytest.raises(MissingProvenanceError):
            store.record(make_record(EvidenceKind.REVIEW_PASSED, run_id=None))

    @pytest.mark.parametrize("kind", [EvidenceKind.PLAN_PRODUCED, EvidenceKind.IMPLEMENTATION_PRODUCED])
    def test_agent_output_kinds_demand_run_linkage(self, kind):
        store = InMemoryEvidenceStore()
        with pytest.raises(MissingProvenanceError):
            store.record(make_record(kind, run_id=None))

    def test_approval_evidence_may_only_come_from_a_human(self):
        store = InMemoryEvidenceStore()
        with pytest.raises(InvalidProducerError):
            store.record(
                make_record(EvidenceKind.APPROVAL_GRANTED, produced_by="agent:claude")
            )
        # The forged record never entered the store:
        assert store.records_for_task(TASK, EvidenceKind.APPROVAL_GRANTED) == ()
        store.record(make_record(EvidenceKind.APPROVAL_GRANTED, produced_by="human:utku"))
        assert len(store.records_for_task(TASK, EvidenceKind.APPROVAL_GRANTED)) == 1

    def test_policy_evidence_may_only_come_from_relay(self):
        store = InMemoryEvidenceStore()
        with pytest.raises(InvalidProducerError):
            store.record(
                make_record(EvidenceKind.NO_PENDING_APPROVALS, produced_by="agent:gpt")
            )
        assert store.records_for_task(TASK, EvidenceKind.NO_PENDING_APPROVALS) == ()


class TestConditionalCompletion:
    """REVIEWING -> DONE is legal only under a cleared completion policy."""

    def _reviewed_machine(self, policy: CompletionPolicy) -> tuple[TaskStateMachine, InMemoryEvidenceStore]:
        store = InMemoryEvidenceStore()
        store.record(make_record(EvidenceKind.TESTS_PASSED))
        store.record(make_record(EvidenceKind.REVIEW_PASSED))
        if policy.cleared(pending_approvals=0):
            store.record(make_record(EvidenceKind.NO_PENDING_APPROVALS))
        return fresh_machine(TaskState.REVIEWING, store=store), store

    def test_default_policy_is_secure(self):
        policy = CompletionPolicy()
        assert policy.require_human_approval is True
        assert policy.cleared(pending_approvals=0) is False

    def test_safe_workflow_finishes_without_human_when_policy_allows(self):
        policy = CompletionPolicy(require_human_approval=False)
        sm, _ = self._reviewed_machine(policy)
        assert sm.can_transition(TaskState.DONE)
        sm.transition(TaskState.DONE)
        assert sm.state is TaskState.DONE

    def test_gated_workflow_cannot_bypass_approval_required(self):
        policy = CompletionPolicy()  # human approval required
        sm, _ = self._reviewed_machine(policy)  # no NO_PENDING_APPROVALS recorded
        with pytest.raises(MissingEvidenceError) as excinfo:
            sm.transition(TaskState.DONE)
        assert EvidenceKind.NO_PENDING_APPROVALS in excinfo.value.missing
        # The gated path remains open:
        assert sm.can_transition(TaskState.APPROVAL_REQUIRED)

    def test_pending_approvals_block_even_a_relaxed_policy(self):
        policy = CompletionPolicy(require_human_approval=False)
        assert policy.cleared(pending_approvals=1) is False  # queue not empty
        store = InMemoryEvidenceStore()
        store.record(make_record(EvidenceKind.TESTS_PASSED))
        store.record(make_record(EvidenceKind.REVIEW_PASSED))
        sm = fresh_machine(TaskState.REVIEWING, store=store)
        with pytest.raises(MissingEvidenceError) as excinfo:
            sm.transition(TaskState.DONE)
        assert EvidenceKind.NO_PENDING_APPROVALS in excinfo.value.missing

    def test_direct_completion_without_review_evidence_fails(self):
        store = InMemoryEvidenceStore()
        store.record(make_record(EvidenceKind.TESTS_PASSED))
        store.record(make_record(EvidenceKind.NO_PENDING_APPROVALS))
        sm = fresh_machine(TaskState.REVIEWING, store=store)
        with pytest.raises(MissingEvidenceError) as excinfo:
            sm.transition(TaskState.DONE)
        assert EvidenceKind.REVIEW_PASSED in excinfo.value.missing

    def test_approval_required_still_needs_explicit_grant(self):
        store = InMemoryEvidenceStore()
        store.record(make_record(EvidenceKind.REVIEW_PASSED))
        sm = fresh_machine(TaskState.APPROVAL_REQUIRED, store=store)
        assert not sm.can_transition(TaskState.DONE)
        store.record(make_record(EvidenceKind.APPROVAL_GRANTED))
        sm.transition(TaskState.DONE)
        assert sm.state is TaskState.DONE

    def test_model_authored_approval_text_has_no_authority(self):
        """An agent claiming 'approved' can't even write the record."""
        store = InMemoryEvidenceStore()
        with pytest.raises(InvalidProducerError):
            store.record(make_record(EvidenceKind.APPROVAL_GRANTED, produced_by="codex"))
        sm = fresh_machine(TaskState.APPROVAL_REQUIRED, store=store)
        assert not sm.can_transition(TaskState.DONE)


class TestIllegalMoves:
    @pytest.mark.parametrize(
        ("current", "target"),
        [
            (TaskState.CREATED, TaskState.PLAN_READY),  # skipping context
            (TaskState.CREATED, TaskState.DONE),  # the fantasy shortcut
            (TaskState.IMPLEMENTING, TaskState.DONE),
            (TaskState.DONE, TaskState.CREATED),  # terminal stays terminal
            (TaskState.VERIFYING, TaskState.DONE),
        ],
    )
    def test_nonexistent_edges_are_refused_even_with_full_store(self, current, target):
        store = InMemoryEvidenceStore()
        for kind in EvidenceKind:
            store.record(make_record(kind))
        sm = fresh_machine(current, store=store)
        with pytest.raises(IllegalTransitionError):
            sm.transition(target)
        assert not sm.can_transition(target)

    def test_can_transition_reports_without_raising_or_mutating(self):
        store = InMemoryEvidenceStore()
        store.record(make_record(EvidenceKind.CONTEXT_COLLECTED))
        sm = fresh_machine(store=store)
        assert sm.can_transition(TaskState.CONTEXT_READY)
        assert sm.state is TaskState.CREATED  # pure check mutated nothing


class TestIntrospection:
    def test_missing_evidence_names_what_blocks_an_edge(self):
        sm = fresh_machine(TaskState.VERIFYING)
        assert sm.missing_evidence_for(TaskState.REVIEWING) == {EvidenceKind.TESTS_PASSED}

    def test_available_transitions_reflect_current_state(self):
        sm = fresh_machine(TaskState.REVIEWING)
        targets = {t.target for t in sm.available_transitions()}
        assert targets == {
            TaskState.APPROVAL_REQUIRED,
            TaskState.DONE,
            TaskState.IMPLEMENTING,
        }

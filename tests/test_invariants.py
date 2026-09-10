"""Property/invariant tests for Relay's canonical machinery (pre-P6 hardening).

Hypothesis generates inputs around the invariants the codebase actually
enforces — state legality, evidence gating, append-only history, deterministic
identity, communication budgets, redaction — rather than duplicating the unit
tests. Every run is offline and deterministic (``derandomize=True``).
"""

from __future__ import annotations

import sqlite3

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from relay.agents.base import AgentRole
from relay.core.bus import ConversationBus, MessageRejected
from relay.core.driver import MessageKind, canonical_identity, derive_message_id
from relay.core.evidence import (
    EvidenceKind,
    InMemoryEvidenceStore,
    InvalidProducerError,
    validate_provenance,
)
from relay.core.policy import (
    BlockingBudgetExhausted,
    CommunicationBudgets,
    CommunicationPolicy,
    SqliteCommunicationPolicyGate,
    TurnBudgetExhausted,
)
from relay.core.protocol_encoding import (
    decode_definition,
    definition_bytes,
    definition_digest,
    request_id,
)
from relay.core.protocols import (
    BudgetDimension,
    BudgetExhaustionFact,
    BudgetScope,
    EvaluationStatus,
    ExpectedOutput,
    ParticipantRequirement,
    ProtocolCompletion,
    ProtocolDefinition,
    RequestState,
    StageAnswerFact,
    StageBudgets,
    StageCompletion,
    StageContext,
    StageDefinition,
    StageFacts,
    StageRequestFact,
    evaluate_stage,
)
from relay.core.state_machine import (
    TRANSITIONS,
    IllegalTransitionError,
    MissingEvidenceError,
    TaskState,
    TaskStateMachine,
)
from relay.harness.sanitization import redact
from relay.storage.db import connect, migrate
from relay.storage.events import EventLogWriter
from relay.storage.models import (
    EventLogEntry,
    EventType,
    EvidenceRecord,
    Message,
    MessageType,
    ProtocolExecution,
    Room,
    Run,
)
from relay.storage.store import ImmutableHistoryError, SqliteRelayStore

pytestmark = pytest.mark.invariants

_GIVEN = settings(derandomize=True, deadline=None, max_examples=60)
# SQLite-bound properties get a fresh in-memory database per example.
_GIVEN_DB = settings(derandomize=True, deadline=None, max_examples=30)


def _fresh_store() -> SqliteRelayStore:
    conn = connect(":memory:")
    migrate(conn)
    return SqliteRelayStore(conn)


def _counts(store: SqliteRelayStore) -> dict[str, int]:
    return store.counts()


def _ensure_room(store: SqliteRelayStore, room_id: str) -> None:
    """Create the Room row once — messages carry a real FK to ``rooms``."""
    if store.load_model(Room, room_id) is None:
        store.save_model(Room(id=room_id, name=room_id))


# ---------------------------------------------------------------------------
# State machine: legality + evidence gating
# ---------------------------------------------------------------------------

_LEGAL_EDGES = {
    (state, transition.target) for state in TaskState for transition in TRANSITIONS[state]
}


def _legal_edge(state: TaskState, target: TaskState) -> bool:
    return (state, target) in _LEGAL_EDGES


def _evidence(kind: EvidenceKind, task_id: str) -> EvidenceRecord:
    """A minimally valid record for ``kind`` honoring provenance contracts."""
    produced_by = {
        EvidenceKind.APPROVAL_GRANTED: "human:reviewer",
        EvidenceKind.NO_PENDING_APPROVALS: "relay:core",
    }.get(kind, "agent:worker")
    return EvidenceRecord(
        kind=kind,
        task_id=task_id,
        produced_by=produced_by,
        run_id=(
            "run-1"
            if kind
            in (
                EvidenceKind.PLAN_PRODUCED,
                EvidenceKind.IMPLEMENTATION_PRODUCED,
                EvidenceKind.REVIEW_PASSED,
            )
            else None
        ),
        tool_run_id="tool-1" if kind is EvidenceKind.TESTS_PASSED else None,
    )


@_GIVEN
@given(state=st.sampled_from(TaskState), target=st.sampled_from(TaskState))
def test_illegal_transition_pairs_always_rejected(state, target):
    """No edge ⇒ IllegalTransitionError; the machine state is unchanged."""
    machine = TaskStateMachine(task_id="t", store=InMemoryEvidenceStore(), state=state)
    if _legal_edge(state, target):
        return
    with pytest.raises(IllegalTransitionError):
        machine.transition(target)
    assert machine.state is state
    assert not machine.can_transition(target)


@_GIVEN
@given(target=st.sampled_from(TaskState))
def test_done_is_terminal(target):
    """A terminal state can never silently regress or re-open."""
    machine = TaskStateMachine(task_id="t", store=InMemoryEvidenceStore(), state=TaskState.DONE)
    assert machine.is_terminal
    with pytest.raises(IllegalTransitionError):
        machine.transition(target)
    assert machine.state is TaskState.DONE


_GATED_EDGES = [
    (state, transition)
    for state in TaskState
    for transition in TRANSITIONS[state]
    if transition.required_evidence
]


@_GIVEN
@given(
    edge=st.sampled_from(_GATED_EDGES),
    recorded=st.frozensets(st.sampled_from(EvidenceKind)),
    foreign_task=st.booleans(),
)
def test_evidence_gating_is_exact(edge, recorded, foreign_task):
    """A gated edge passes iff the store holds every required kind.

    Records pinned to a *different* task can never satisfy the gate.
    """
    state, transition = edge
    task_id = "task-under-test"
    evidence = InMemoryEvidenceStore()
    for kind in recorded:
        evidence.record(_evidence(kind, "other-task" if foreign_task else task_id))
    machine = TaskStateMachine(task_id=task_id, store=evidence, state=state)

    expected_gap = (
        set(transition.required_evidence)
        if foreign_task
        else set(transition.required_evidence) - set(recorded)
    )
    feasible = machine.can_transition(transition.target)
    try:
        machine.transition(transition.target)
    except MissingEvidenceError as exc:
        assert set(exc.missing) == expected_gap
        assert expected_gap  # a refusal must name a nonempty gap
        assert machine.state is state
        assert not feasible
    else:
        assert not expected_gap
        assert machine.state is transition.target
        assert feasible


@_GIVEN
@given(kind=st.sampled_from(EvidenceKind), bad_producer=st.booleans())
def test_evidence_provenance_is_enforced(kind, bad_producer):
    """validate_provenance refuses only restricted kinds with bad producers."""
    record = _evidence(kind, "t")
    if bad_producer:
        record = record.model_copy(update={"produced_by": "mallory"})
    restricted = kind in (
        EvidenceKind.APPROVAL_GRANTED,
        EvidenceKind.NO_PENDING_APPROVALS,
    )
    if bad_producer and restricted:
        with pytest.raises(InvalidProducerError):
            validate_provenance(record)
    else:
        validate_provenance(record)


# ---------------------------------------------------------------------------
# Deterministic identity / canonical encoding
# ---------------------------------------------------------------------------

_IDENT = st.fixed_dictionaries(
    {
        "conversation_key": st.text(max_size=24),
        "room_id": st.one_of(st.none(), st.text(max_size=16)),
        "task_id": st.one_of(st.none(), st.text(max_size=16)),
        "hop_index": st.integers(min_value=1, max_value=64),
        "attempt_index": st.integers(min_value=1, max_value=4),
        "kind": st.sampled_from(MessageKind),
        "prev_message_id": st.one_of(st.none(), st.text(max_size=16)),
        "recipient_role": st.one_of(st.none(), st.text(max_size=16)),
        "resolved_recipient": st.text(max_size=16),
    }
)

# Adversarial strings exercising the injective-encoding contract: separator
# characters, literal "null"/"-" payloads, and unicode escapes.
_IDENT = _IDENT | st.fixed_dictionaries(
    {
        "conversation_key": st.sampled_from(["a:b", "null", "-", 'a"b', "x/y", "❄"]),
        "room_id": st.one_of(st.none(), st.just("r:0")),
        "task_id": st.one_of(st.none(), st.just("null"), st.just("-")),
        "hop_index": st.integers(min_value=1, max_value=8),
        "attempt_index": st.just(1),
        "kind": st.sampled_from(MessageKind),
        "prev_message_id": st.one_of(st.none(), st.just("p:q")),
        "recipient_role": st.one_of(st.none(), st.just("null")),
        "resolved_recipient": st.just("a:b"),
    }
)


@_GIVEN
@given(first=_IDENT, second=_IDENT)
def test_identity_is_deterministic_and_injective(first, second):
    """Equal inputs ⇒ equal ids; distinct inputs ⇒ distinct ids."""
    id_a, id_b = derive_message_id(**first), derive_message_id(**second)
    canon_a, canon_b = canonical_identity(**first), canonical_identity(**second)
    assert id_a == derive_message_id(**first)
    assert canon_a == canonical_identity(**first)
    if first == second:
        assert id_a == id_b and canon_a == canon_b
    else:
        assert id_a != id_b and canon_a != canon_b


@_GIVEN
@given(
    execution_key=st.text(min_size=1, max_size=24),
    stage_id=st.text(min_size=1, max_size=24),
    occurrence=st.integers(min_value=0, max_value=8),
    room_id=st.one_of(st.none(), st.text(min_size=1, max_size=12)),
    task_id=st.one_of(st.none(), st.text(min_size=1, max_size=12)),
    mutate=st.sampled_from(
        ["protocol_name", "protocol_version", "execution_key", "stage_id", "occurrence_index"]
    ),
)
def test_stage_key_is_deterministic_and_input_sensitive(
    execution_key, stage_id, occurrence, room_id, task_id, mutate
):
    """Equivalent canonical inputs give equal keys; any input change shifts it."""
    if room_id is None and task_id is None:
        room_id = "room"
    base = {
        "protocol_name": "p",
        "protocol_version": "v1",
        "execution_key": execution_key,
        "stage_id": stage_id,
        "occurrence_index": occurrence,
        "room_id": room_id,
        "task_id": task_id,
    }
    context = StageContext(**base)
    assert context.stage_key == StageContext(**base).stage_key

    changed = dict(base)
    if mutate == "occurrence_index":
        changed[mutate] = occurrence + 1
    else:
        changed[mutate] = base[mutate] + "-x"
    assert StageContext(**changed).stage_key != context.stage_key


@_GIVEN
@given(
    execution_id=st.text(min_size=1, max_size=24),
    stage_key=st.text(min_size=1, max_size=24),
    role=st.sampled_from(AgentRole),
)
def test_request_id_is_deterministic(execution_id, stage_key, role):
    """Equivalent canonical inputs produce equivalent request identities."""
    first = request_id(execution_id, stage_key, role)
    assert first == request_id(execution_id, stage_key, role)
    assert request_id(execution_id + "-x", stage_key, role) != first


# ---------------------------------------------------------------------------
# Storage: append-only history + transaction atomicity
# ---------------------------------------------------------------------------


@_GIVEN_DB
@given(
    content=st.text(min_size=1, max_size=200),
    room_id=st.text(min_size=1, max_size=16),
)
def test_append_only_tables_reject_mutation(content, room_id):
    """Messages, evidence, and event log refuse update/delete; rows stay intact."""
    store = _fresh_store()
    _ensure_room(store, room_id)
    message = store.save_model(
        Message(
            sender="relay:test",
            recipient="agent-b",
            room_id=room_id,
            type=MessageType.NOTE,
            content=content,
        )
    )
    event = EventLogWriter(store.conn).record(
        EventLogEntry(type=EventType.MESSAGE_SENT, content="m", room_id=room_id)
    )
    record = store.save_model(
        EvidenceRecord(kind=EvidenceKind.CONTEXT_COLLECTED, task_id="t", produced_by="agent:a")
    )

    with pytest.raises(ImmutableHistoryError):
        store.update_model(message.model_copy(update={"content": content + "-x"}))
    with pytest.raises(ImmutableHistoryError):
        store.delete_model(message)
    with pytest.raises(ImmutableHistoryError):
        store.update_model(record.model_copy(update={"task_id": "other"}))
    with pytest.raises(ImmutableHistoryError):
        store.delete_model(record)

    reloaded = store.load_model(Message, message.id)
    assert reloaded is not None and reloaded.content == content
    reloaded_evidence = store.load_model(EvidenceRecord, record.id)
    assert reloaded_evidence is not None and reloaded_evidence.task_id == "t"
    assert event.sequence is not None


@_GIVEN_DB
@given(content=st.text(min_size=1, max_size=80))
def test_duplicate_message_id_is_rejected(content):
    """Uniqueness survives generated payloads; the original row is unchanged."""
    store = _fresh_store()
    _ensure_room(store, "r")
    first = store.save_model(
        Message(
            sender="relay:test",
            recipient="agent-b",
            room_id="r",
            type=MessageType.NOTE,
            content=content,
        )
    )
    with pytest.raises(sqlite3.IntegrityError):
        store.save_model(first.model_copy(update={"content": content + "-dup"}))
    reloaded = store.load_model(Message, first.id)
    assert reloaded is not None and reloaded.content == content


@_GIVEN_DB
@given(
    writes=st.lists(st.text(min_size=1, max_size=40), min_size=1, max_size=6),
    explode_at=st.integers(min_value=0, max_value=5),
)
def test_failed_transaction_leaves_no_partial_state(writes, explode_at):
    """A mid-transaction exception rolls back every write in the block."""
    store = _fresh_store()
    writer = EventLogWriter(store.conn)
    _ensure_room(store, "r")
    before = _counts(store)
    boom_at = explode_at % len(writes)

    with pytest.raises(RuntimeError, match="mid-transaction failure"), store.transaction():
        for index, text in enumerate(writes):
            store.save_model(
                Message(
                    sender="relay:test",
                    recipient="agent-b",
                    room_id="r",
                    type=MessageType.NOTE,
                    content=text,
                )
            )
            writer.record(EventLogEntry(type=EventType.MESSAGE_SENT, content=text, room_id="r"))
            if index == boom_at:
                raise RuntimeError("mid-transaction failure")
    assert _counts(store) == before


# ---------------------------------------------------------------------------
# Conversation bus: authorship provenance + atomic rejection
# ---------------------------------------------------------------------------

_BARE_NAME = st.from_regex(r"[a-z][a-z0-9-]{0,12}", fullmatch=True)


@_GIVEN_DB
@given(
    sender=st.one_of(
        _BARE_NAME,
        st.builds(lambda n: f"human:{n}", _BARE_NAME),
        st.builds(lambda n: f"relay:{n}", _BARE_NAME),
        st.sampled_from(["", "  ", "bad sender", "ns:op", "sys:tem"]),
    ),
    recipient=st.one_of(
        _BARE_NAME,
        st.builds(lambda n: f"human:{n}", _BARE_NAME),
        st.none(),
    ),
    run_id_flavor=st.sampled_from(["none", "matching", "other", "bogus"]),
    message_type=st.sampled_from(MessageType),
    blocking=st.booleans(),
    content=st.text(max_size=80),
)
def test_send_rejection_never_persists(
    sender, recipient, run_id_flavor, message_type, blocking, content
):
    """bus.send is atomic: a typed rejection leaves messages/markers untouched."""
    store = _fresh_store()
    writer = EventLogWriter(store.conn)
    bus = ConversationBus(store, writer)
    _ensure_room(store, "r")
    run_self = store.save_model(Run(agent=sender or "x", role="planner"))
    run_other = store.save_model(Run(agent="someone-else", role="planner"))
    run_id = {
        "none": None,
        "matching": run_self.id,
        "other": run_other.id,
        "bogus": "no-such-run",
    }[run_id_flavor]
    before = _counts(store)

    message = Message(
        sender=sender,
        recipient=recipient,
        room_id="r",
        type=message_type,
        content=content,
        blocking=blocking,
        run_id=run_id,
    )
    try:
        saved = bus.send(message)
    except MessageRejected:
        assert _counts(store) == before
        return
    # Accepted: exactly one new message and one new marker, correctly bound.
    after = _counts(store)
    assert after["messages"] == before["messages"] + 1
    assert after["event_log"] == before["event_log"] + 1
    markers = [e for e in writer.all() if e.type is EventType.MESSAGE_SENT]
    assert f"message:{saved.id}" in markers[-1].references
    assert markers[-1].sender == saved.sender


# ---------------------------------------------------------------------------
# Communication budgets: counters are ledger-derived and cannot be exceeded
# ---------------------------------------------------------------------------


@_GIVEN_DB
@given(
    limit=st.integers(min_value=0, max_value=3),
    human_extra=st.integers(min_value=0, max_value=3),
    attempts=st.integers(min_value=0, max_value=3),
)
def test_blocking_budget_counts_only_nonhuman_senders(limit, human_extra, attempts):
    """human: senders never consume the blocking budget; agents exhaust it."""
    store = _fresh_store()
    writer = EventLogWriter(store.conn)
    room_id = "r"
    _ensure_room(store, room_id)
    policy = CommunicationPolicy(
        budgets=CommunicationBudgets(max_agent_turns=8, max_blocking_messages=limit)
    )
    bus = ConversationBus(store, writer, policy=SqliteCommunicationPolicyGate(store, policy))
    for index in range(human_extra):
        bus.send(
            Message(
                sender="human:operator",
                recipient="agent-b",
                room_id=room_id,
                type=MessageType.PROPOSAL,
                content=f"human {index}",
                blocking=True,
            )
        )
    admitted = 0
    exhausted = False
    total = attempts + 2
    for index in range(total):
        try:
            bus.send(
                Message(
                    sender="relay:test",
                    recipient="agent-b",
                    room_id=room_id,
                    type=MessageType.PROPOSAL,
                    content=f"blocking {index}",
                    blocking=True,
                )
            )
            admitted += 1
        except BlockingBudgetExhausted:
            exhausted = True
            break
    assert admitted == min(total, limit)
    assert exhausted == (total > limit)
    rows = store.conn.execute(
        "SELECT COUNT(*) FROM messages WHERE blocking = 1 AND room_id IS ?",
        [room_id],
    ).fetchone()
    assert int(rows[0]) == admitted + human_extra


@_GIVEN_DB
@given(
    limit=st.integers(min_value=1, max_value=4),
    delivered=st.integers(min_value=0, max_value=6),
)
def test_turn_budget_counts_delivery_markers(limit, delivered):
    """Aggregate turn budget refuses exactly when used >= limit."""
    store = _fresh_store()
    room_id = "r"
    gate = SqliteCommunicationPolicyGate(
        store,
        CommunicationPolicy(
            budgets=CommunicationBudgets(max_agent_turns=limit, max_blocking_messages=4)
        ),
    )
    writer = EventLogWriter(store.conn)
    with store.transaction():
        for _ in range(delivered):
            writer.record(
                EventLogEntry(
                    type=EventType.MESSAGE_DELIVERED,
                    content="bound",
                    room_id=room_id,
                    sender="relay:delivery",
                    recipient="agent-b",
                    references=["message:m", "run:x"],
                )
            )
    try:
        gate.check_turn_budget(room_id, None)
    except TurnBudgetExhausted:
        assert delivered >= limit
    else:
        assert delivered < limit


# ---------------------------------------------------------------------------
# Pure stage evaluation: facts, never prose, drive completion
# ---------------------------------------------------------------------------

_FACT_ROLES = (AgentRole.PLANNER, AgentRole.REVIEWER)


def _fact_stage(
    *, require_synthesis: bool, early_stop: bool
) -> tuple[ProtocolDefinition, StageDefinition]:
    """Two-role stage; REVIEWER emits SYNTHESIS when the flag demands it."""
    reviewer_type = MessageType.SYNTHESIS if require_synthesis else MessageType.NOTE
    allowed = (
        (MessageType.NOTE, MessageType.SYNTHESIS)
        if require_synthesis
        else (MessageType.NOTE, MessageType.OPINION)
    )
    stage = StageDefinition(
        id="s1",
        participants=_FACT_ROLES,
        edges=(),
        allowed_message_types=allowed,
        budgets=StageBudgets(max_agent_turns=8, max_blocking_messages=2),
        expected_outputs=(
            ExpectedOutput(role=AgentRole.PLANNER, type=MessageType.NOTE),
            ExpectedOutput(role=AgentRole.REVIEWER, type=reviewer_type),
        ),
        completion=StageCompletion(
            require_synthesis=require_synthesis, early_stop_on_answered=early_stop
        ),
    )
    definition = ProtocolDefinition(
        name="p",
        version="v1",
        participants=tuple(ParticipantRequirement(role=r) for r in _FACT_ROLES),
        stages=(stage,),
        completion=ProtocolCompletion(require_synthesis=require_synthesis),
    )
    return definition, stage


@_GIVEN
@given(
    require_synthesis=st.booleans(),
    early_stop=st.booleans(),
    states=st.tuples(st.sampled_from(RequestState), st.sampled_from(RequestState)),
    reply_types=st.tuples(
        st.sampled_from((MessageType.NOTE, MessageType.SYNTHESIS, MessageType.OPINION)),
        st.sampled_from((MessageType.NOTE, MessageType.SYNTHESIS, MessageType.OPINION)),
    ),
    budget_hit=st.booleans(),
    answered=st.booleans(),
)
def test_stage_completion_requires_canonical_facts(
    require_synthesis, early_stop, states, reply_types, budget_hit, answered
):
    """evaluate_stage completes only when provenance-backed facts qualify."""
    definition, stage = _fact_stage(require_synthesis=require_synthesis, early_stop=early_stop)
    context = StageContext("p", "v1", "exec", "s1", 0, room_id="r")
    requests: list[StageRequestFact] = []
    for role, state, reply_type in zip(_FACT_ROLES, states, reply_types):
        if state is RequestState.SUCCEEDED:
            requests.append(
                StageRequestFact(
                    role,
                    f"req-{role.value}",
                    state,
                    f"reply-{role.value}",
                    reply_type,
                )
            )
        elif state is RequestState.MISSING:
            requests.append(StageRequestFact(role, None, state))
        else:
            requests.append(StageRequestFact(role, f"req-{role.value}", state))
    answers = (
        (
            StageAnswerFact(
                seed_id="seed-1",
                answering_reply_id="reply-planner" if answered else None,
            ),
        )
        if early_stop
        else ()
    )
    facts = StageFacts(
        context=context,
        stage_key=context.stage_key,
        requests=tuple(requests),
        answers=answers,
        budget_exhaustion=(
            BudgetExhaustionFact(BudgetScope.AGGREGATE, BudgetDimension.TURN)
            if budget_hit
            else None
        ),
    )

    evaluation = evaluate_stage(definition, facts)
    assert evaluation == evaluate_stage(definition, facts)  # pure/deterministic

    expected = {o.role: o.type for o in stage.expected_outputs}
    qualifying = {
        r.role
        for r in requests
        if r.state is RequestState.SUCCEEDED and r.reply_type is expected[r.role]
    }
    synthesis_ok = not require_synthesis or any(
        r.state is RequestState.SUCCEEDED and r.reply_type is MessageType.SYNTHESIS
        for r in requests
    )
    all_done = len(qualifying) == len(_FACT_ROLES)
    answered_ok = (
        early_stop and bool(answers) and all(a.answering_reply_id is not None for a in answers)
    )

    if evaluation.status is EvaluationStatus.COMPLETE:
        # Completion only when canonical outputs qualify or the answered path
        # closed early — reply text can never fabricate either.
        assert (all_done and synthesis_ok) or (answered_ok and synthesis_ok)
    elif evaluation.status is EvaluationStatus.BLOCKED:
        assert (
            any(r.state is RequestState.FAILED for r in requests)
            or budget_hit
            or not (all_done or answered_ok)
            or not synthesis_ok
        )


# ---------------------------------------------------------------------------
# Protocol definition pinning: round-trip + tamper refusal
# ---------------------------------------------------------------------------


def _definition(
    roles: tuple[AgentRole, AgentRole],
    name: str,
    version: str,
    turns: int,
    require_synthesis: bool,
) -> ProtocolDefinition:
    reviewer_type = MessageType.SYNTHESIS if require_synthesis else MessageType.NOTE
    allowed = (
        (MessageType.NOTE, MessageType.SYNTHESIS)
        if require_synthesis
        else (MessageType.NOTE, MessageType.OPINION)
    )
    stage = StageDefinition(
        id="s1",
        participants=roles,
        edges=(),
        allowed_message_types=allowed,
        budgets=StageBudgets(max_agent_turns=turns, max_blocking_messages=0),
        expected_outputs=(
            ExpectedOutput(role=roles[0], type=MessageType.NOTE),
            ExpectedOutput(role=roles[1], type=reviewer_type),
        ),
        completion=StageCompletion(require_synthesis=require_synthesis),
    )
    return ProtocolDefinition(
        name=name,
        version=version,
        participants=tuple(ParticipantRequirement(role=r) for r in roles),
        stages=(stage,),
        completion=ProtocolCompletion(require_synthesis=require_synthesis),
    )


_ROLE_PAIRS = st.sampled_from(
    [
        (AgentRole.PLANNER, AgentRole.REVIEWER),
        (AgentRole.IMPLEMENTER, AgentRole.REVIEWER),
        (AgentRole.ARCHITECT, AgentRole.PARTICIPANT),
    ]
)


@_GIVEN
@given(
    roles=_ROLE_PAIRS,
    name=st.text(min_size=1, max_size=24),
    version=st.text(min_size=1, max_size=8),
    turns=st.integers(min_value=1, max_value=1000),
    require_synthesis=st.booleans(),
)
def test_definition_round_trips_byte_identical(roles, name, version, turns, require_synthesis):
    """decode(encode(d)) == d and re-encodes to the same canonical bytes."""
    definition = _definition(roles, name, version, turns, require_synthesis)
    snapshot = definition_bytes(definition).decode()
    digest = definition_digest(definition)
    decoded = decode_definition(snapshot, digest)
    assert decoded == definition
    assert definition_bytes(decoded).decode() == snapshot


@_GIVEN
@given(
    roles=_ROLE_PAIRS,
    index=st.integers(min_value=0, max_value=100),
    replacement=st.characters(),
)
def test_tampered_definition_snapshot_is_refused(roles, index, replacement):
    """Any byte-level change to a pinned snapshot is refused."""
    definition = _definition(roles, "p", "v1", 8, False)
    snapshot = definition_bytes(definition).decode()
    digest = definition_digest(definition)
    position = index % len(snapshot)
    if snapshot[position] == replacement:
        return
    mutated = snapshot[:position] + replacement + snapshot[position + 1 :]
    with pytest.raises(ValueError):
        decode_definition(mutated, digest)


@_GIVEN
@given(roles=_ROLE_PAIRS, wrong_digest=st.text(min_size=1, max_size=24))
def test_wrong_digest_is_refused(roles, wrong_digest):
    """A snapshot whose digest does not match is refused."""
    definition = _definition(roles, "p", "v1", 8, False)
    snapshot = definition_bytes(definition).decode()
    digest = definition_digest(definition)
    if wrong_digest == digest:
        return
    with pytest.raises(ValueError):
        decode_definition(snapshot, wrong_digest)


# ---------------------------------------------------------------------------
# Redaction: secret-shaped material can never reach persisted history
# ---------------------------------------------------------------------------

_SECRET_LITERALS = st.sampled_from(
    [
        "sk-AbCdEfGhIjKlMnOp",
        "ghp_0123456789abcdefghij",
        "github_pat_0123456789abcdefghij_kl",
        "AKIA1234567890ABCDEF",
        "Bearer token-value-123",
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozg",
    ]
)


@_GIVEN
@given(
    secret=_SECRET_LITERALS,
    prefix=st.text(max_size=40),
    suffix=st.text(max_size=40),
)
def test_redact_masks_credential_literals(secret, prefix, suffix):
    """Credential-shaped literals never survive into persisted text."""
    out = redact(f"{prefix} {secret} {suffix}")
    assert secret not in out


@_GIVEN
@given(
    name=st.sampled_from(
        ["API_KEY", "MY_TOKEN", "DB_SECRET", "AUTH_HEADER", "SESSION_ID", "PASSWORD"]
    ),
    value=st.text(min_size=1, max_size=30, alphabet="abcdefghijklmnopqrstuvwxyz0123456789"),
    separator=st.sampled_from(["=", ": ", "="]),
)
def test_redact_masks_secret_assignments(name, value, separator):
    out = redact(f"{name}{separator}{value}")
    assert value not in out
    assert "[REDACTED]" in out


@_GIVEN
@given(secret=st.text(min_size=6, max_size=40), body=st.text(max_size=60))
def test_redact_masks_explicit_secrets(secret, body):
    """Caller-known secrets are masked wherever they appear."""
    out = redact(f"head {secret} tail {body}", secrets=(secret,))
    assert secret not in out


@_GIVEN
@given(text=st.text(max_size=120))
def test_redact_is_idempotent_and_never_raises(text):
    """redact is total and stable under re-application."""
    once = redact(text)
    assert redact(once) == once


# ---------------------------------------------------------------------------
# Append-only execution inputs
# ---------------------------------------------------------------------------


@_GIVEN_DB
@given(
    key=st.text(min_size=1, max_size=20),
    topic=st.text(min_size=1, max_size=60),
    scope_room=st.booleans(),
)
def test_execution_pinning_rejects_conflicts(key, topic, scope_room):
    """Same id AND same (execution_key, scope) both refuse; inputs stay pinned."""
    store = _fresh_store()
    room_id = "room-x" if scope_room else None
    task_id = None if scope_room else "task-x"
    first = store.save_model(
        ProtocolExecution(
            execution_key=key,
            room_id=room_id,
            task_id=task_id,
            topic=topic,
            definition_snapshot="[]",
            definition_digest="d1",
            bindings_snapshot="[]",
        )
    )
    with pytest.raises(sqlite3.IntegrityError):
        store.save_model(first.model_copy(update={"topic": topic + "-changed"}))
    with pytest.raises(sqlite3.IntegrityError):
        # Same resume key + scope under a different id must also refuse:
        # one execution_key can never mint two canonical executions.
        store.save_model(
            ProtocolExecution(
                execution_key=key,
                room_id=room_id,
                task_id=task_id,
                topic=topic,
                definition_snapshot="[]",
                definition_digest="d2",
                bindings_snapshot="[]",
            )
        )
    reloaded = store.load_model(ProtocolExecution, first.id)
    assert reloaded is not None and reloaded.topic == topic

"""Stage enforcement at real transactional seams, using offline agents."""

import asyncio
import concurrent.futures
import sqlite3
import threading
from dataclasses import replace
from pathlib import Path

import pytest

from relay.agents.base import Agent, AgentRequest, AgentResponse, AgentRole, BackendType
from relay.context.protocols import load_protocol
from relay.core.bus import ConversationBus
from relay.core.delivery import DeliveryPendingRefusal, DeliveryRefusal, MessageDelivery
from relay.core.policy import (
    BlockingBudgetExhausted,
    CommunicationBudgets,
    CommunicationPolicy,
    EdgeNotPermitted,
    PolicyEdge,
    SqliteCommunicationPolicyGate,
    TurnBudgetExhausted,
)
from relay.core.protocols import (
    EvaluationReason,
    EvaluationStatus,
    ExpectedOutput,
    ParticipantRequirement,
    ProtocolDefinition,
    ProtocolFactsError,
    RequestState,
    StageBudgets,
    StageCompletion,
    StageContext,
    StageDefinition,
    StageEdge,
    evaluate_protocol,
    evaluate_stage,
)
from relay.core.stage_facts import collect_stage_facts
from relay.core.stage_policy import StageContextRefusal, StageScheduleRefusal
from relay.storage.db import _MIGRATIONS, connect, migrate
from relay.storage.events import EventLogWriter
from relay.storage.models import (
    EventLogEntry,
    EventType,
    Message,
    MessageType,
    Room,
    Run,
    RunStatus,
)
from relay.storage.store import SqliteRelayStore


class OfflineAgent(Agent):
    backend = BackendType.API

    def __init__(self):
        self.calls = 0
        self.fail = False

    async def run(self, request: AgentRequest) -> AgentResponse:
        self.calls += 1
        if self.fail:
            raise RuntimeError("offline failure")
        return AgentResponse(agent="agent", role=request.role, output="Persisted response")


class Factory:
    def __init__(self, agent):
        self.agent = agent

    def build(self, name):
        return self.agent

    def model_of(self, name):
        return None


class Resolver:
    def resolve_role(self, role):
        return role + "_agent"

    def knows_agent(self, name):
        return name.endswith("_agent")


def protocol(*, turns=2, blocking=1, early=False):
    return ProtocolDefinition(
        "test",
        "1",
        (ParticipantRequirement(AgentRole.ARCHITECT),),
        (
            StageDefinition(
                "analysis",
                (AgentRole.ARCHITECT,),
                (),
                (
                    MessageType.NOTE,
                    MessageType.OPINION,
                    MessageType.CLARIFICATION_REQUEST,
                    MessageType.CLARIFICATION_RESPONSE,
                ),
                StageBudgets(turns, blocking),
                (ExpectedOutput(AgentRole.ARCHITECT, MessageType.OPINION),),
                StageCompletion(early_stop_on_answered=early),
            ),
        ),
    )


def ctx(definition, stage_index=0, *, execution_key="run-1"):
    return StageContext(
        definition.name,
        definition.version,
        execution_key,
        definition.stages[stage_index].id,
        room_id="room-1",
    )


def scoped(store, definition, context=None, *, gate=None, agent=None):
    context = context or ctx(definition)
    gate = gate or SqliteCommunicationPolicyGate(
        store, CommunicationPolicy(CommunicationBudgets(16, 3))
    )
    agent = agent or OfflineAgent()
    writer = EventLogWriter(store.conn)
    bus = ConversationBus(
        store, writer, Resolver(), gate, stage_context=context, protocol=definition
    )
    delivery = MessageDelivery(
        store, writer, Factory(agent), bus, gate, stage_context=context, protocol=definition
    )
    return bus, delivery, gate, agent


def request(**kwargs):
    values = {
        "sender": "relay:test",
        "recipient_role": "architect",
        "room_id": "room-1",
        "type": MessageType.NOTE,
        "content": "Discuss",
    }
    values.update(kwargs)
    return Message(**values)


def snapshot(store):
    return tuple(store.conn.iterdump())


@pytest.fixture
def store(tmp_path):
    conn = connect(tmp_path / "stage.sqlite3")
    migrate(conn)
    store = SqliteRelayStore(conn)
    store.save_model(Room(id="room-1", name="test"))
    yield store
    conn.close()


@pytest.mark.asyncio
async def test_debate_exit_gate_through_existing_bus_and_delivery(store):
    definition = load_protocol(Path(__file__).resolve().parents[1] / "protocols/debate.yaml")
    agent = OfflineAgent()
    gate = SqliteCommunicationPolicyGate(store, CommunicationPolicy(CommunicationBudgets(16, 3)))
    authority_tables = ("tasks", "decisions", "approvals", "evidence_records")
    before = {
        table: list(store.conn.execute(f"SELECT * FROM {table}")) for table in authority_tables
    }
    results = []
    for i, stage in enumerate(definition.stages):
        context = ctx(definition, i)
        bus, delivery, _, _ = scoped(store, definition, context, gate=gate, agent=agent)
        inputs = {}
        for output in stage.expected_outputs:
            parent = bus.send(request(recipient_role=output.role.value))
            inputs[output.role] = parent.id
            facts = collect_stage_facts(store, definition, context, inputs, policy=gate)
            assert evaluate_stage(definition, facts).status is not EvaluationStatus.COMPLETE
            outcome = await delivery.deliver_and_reply(parent.id, reply_type=output.type)
            assert outcome.reply.stage_key == context.stage_key
            assert delivery.deliveries_for_message(parent.id)[0].stage_key == context.stage_key
        facts = collect_stage_facts(store, definition, context, inputs, policy=gate)
        assert facts.budget_exhaustion.scope == "stage"
        results.append(evaluate_stage(definition, facts))
        assert results[-1].status is EvaluationStatus.COMPLETE
    assert agent.calls == 10
    assert evaluate_protocol(definition, tuple(results)).status is EvaluationStatus.COMPLETE
    assert {
        table: list(store.conn.execute(f"SELECT * FROM {table}")) for table in authority_tables
    } == before


@pytest.mark.asyncio
async def test_stage_type_refusal_before_provider_and_zero_delta(store):
    bus, delivery, _, agent = scoped(store, protocol())
    parent = bus.send(request())
    before = snapshot(store)
    with pytest.raises(StageScheduleRefusal):
        await delivery.deliver_and_reply(parent.id, reply_type=MessageType.REBUTTAL)
    assert snapshot(store) == before
    assert agent.calls == 0
    for sender in ("relay:test", "human:test"):
        with pytest.raises(StageScheduleRefusal):
            bus.send(request(sender=sender, type=MessageType.REBUTTAL))
        assert snapshot(store) == before


@pytest.mark.asyncio
async def test_stage_keys_and_service_context_cannot_be_bypassed(store):
    definition = protocol()
    bus, delivery, gate, agent = scoped(store, definition)
    parent = bus.send(request())
    before = snapshot(store)
    with pytest.raises(StageContextRefusal):
        bus.send(request(stage_key="wrong"))
    with pytest.raises(StageContextRefusal):
        bus.send(request(room_id=None, task_id="different"))
    unscoped = ConversationBus(store, EventLogWriter(store.conn), Resolver(), gate)
    with pytest.raises(StageContextRefusal):
        unscoped.send(request(stage_key=parent.stage_key))
    raw_delivery = MessageDelivery(
        store, EventLogWriter(store.conn), Factory(agent), unscoped, gate
    )
    for method in (raw_delivery.deliver, raw_delivery.deliver_and_reply):
        with pytest.raises(StageContextRefusal):
            await method(parent.id)
    foreign, foreign_delivery, _, _ = scoped(
        store, definition, replace(ctx(definition), execution_key="foreign"), gate=gate
    )
    with pytest.raises(StageContextRefusal):
        await foreign_delivery.deliver(parent.id)
    assert snapshot(store) == before
    outcome = await delivery.deliver_and_reply(parent.id, reply_type=MessageType.OPINION)
    with pytest.raises(StageContextRefusal):
        foreign.send(
            request(
                sender=outcome.reply.sender,
                recipient_role=None,
                recipient=parent.sender,
                run_id=outcome.reply.run_id,
                reply_to_id=parent.id,
                type=MessageType.OPINION,
            )
        )


def test_stage_services_require_compatible_gate_and_shared_context(store):
    definition = protocol()
    with pytest.raises(StageContextRefusal, match="stage-aware"):
        ConversationBus(
            store, EventLogWriter(store.conn), stage_context=ctx(definition), protocol=definition
        )
    bus, _, gate, agent = scoped(store, definition)
    with pytest.raises(StageContextRefusal, match="share stage"):
        MessageDelivery(store, EventLogWriter(store.conn), Factory(agent), bus, gate)


@pytest.mark.asyncio
async def test_stage_and_aggregate_turn_limits_retries_and_reentry(store):
    definition = protocol(turns=1)
    gate = SqliteCommunicationPolicyGate(store, CommunicationPolicy(CommunicationBudgets(2, 3)))
    agent = OfflineAgent()
    bus, delivery, _, _ = scoped(store, definition, gate=gate, agent=agent)
    parent = bus.send(request())
    first = await delivery.deliver_and_reply(parent.id, reply_type=MessageType.OPINION)
    before = snapshot(store)
    again = await delivery.deliver_and_reply(parent.id, reply_type=MessageType.OPINION)
    assert first.reply.id == again.reply.id and snapshot(store) == before
    retry = bus.send(request())
    before = snapshot(store)
    with pytest.raises(TurnBudgetExhausted) as error:
        await delivery.deliver_and_reply(retry.id, reply_type=MessageType.OPINION)
    assert error.value.scope == "stage" and snapshot(store) == before
    # Distinct execution/stage key has fresh stage capacity but shares the aggregate ceiling.
    other_bus, other_delivery, _, _ = scoped(
        store, definition, replace(ctx(definition), execution_key="second"), gate=gate, agent=agent
    )
    second = other_bus.send(request())
    await other_delivery.deliver_and_reply(second.id, reply_type=MessageType.OPINION)
    third_bus, third_delivery, _, _ = scoped(
        store, definition, replace(ctx(definition), execution_key="third"), gate=gate, agent=agent
    )
    third = third_bus.send(request())
    before = snapshot(store)
    with pytest.raises(TurnBudgetExhausted) as error:
        await third_delivery.deliver_and_reply(third.id, reply_type=MessageType.OPINION)
    assert error.value.scope == "aggregate" and snapshot(store) == before
    assert agent.calls == 2


def test_blocking_accounting_exempts_human_but_counts_relay(store):
    definition = protocol(blocking=1)
    bus, _, gate, _ = scoped(store, definition)
    bus.send(request(sender="human:test", type=MessageType.CLARIFICATION_REQUEST, blocking=True))
    bus.send(request(type=MessageType.CLARIFICATION_REQUEST, blocking=True))
    before = snapshot(store)
    with pytest.raises(BlockingBudgetExhausted) as error:
        bus.send(request(type=MessageType.CLARIFICATION_REQUEST, blocking=True))
    assert error.value.scope == "stage" and snapshot(store) == before
    # Ordinary notes remain allowed even when the blocking allowance is full.
    bus.send(request())
    facts = collect_stage_facts(
        store, definition, ctx(definition), {}, policy=gate, next_message_blocking=True
    )
    assert facts.budget_exhaustion.scope == "stage"
    assert facts.budget_exhaustion.dimension == "blocking"
    gate.check_stage_blocking_budget(
        "room-1", "foreign-task", ctx(definition).stage_key, CommunicationBudgets(2, 1)
    )


def test_stage_edges_intersect_workspace_edges(store):
    definition = protocol()
    stage = replace(
        definition.stages[0],
        participants=(AgentRole.ARCHITECT, AgentRole.CRITIC),
        expected_outputs=(
            ExpectedOutput(AgentRole.ARCHITECT, MessageType.OPINION),
            ExpectedOutput(AgentRole.CRITIC, MessageType.OPINION),
        ),
        edges=(StageEdge(AgentRole.ARCHITECT, AgentRole.CRITIC, (MessageType.OPINION,)),),
    )
    definition = replace(
        definition,
        participants=(
            ParticipantRequirement(AgentRole.ARCHITECT),
            ParticipantRequirement(AgentRole.CRITIC),
        ),
        stages=(stage,),
    )
    run = store.save_model(
        Run(agent="architect_agent", role="architect", status=RunStatus.SUCCEEDED)
    )
    message = request(
        sender="architect_agent", run_id=run.id, recipient_role="critic", type=MessageType.OPINION
    )
    denied = SqliteCommunicationPolicyGate(
        store, CommunicationPolicy(CommunicationBudgets(16, 3), frozenset())
    )
    bus, _, _, _ = scoped(store, definition, gate=denied)
    before = snapshot(store)
    with pytest.raises(EdgeNotPermitted):
        bus.send(message)
    assert snapshot(store) == before
    allowed = SqliteCommunicationPolicyGate(
        store,
        CommunicationPolicy(
            CommunicationBudgets(16, 3),
            frozenset(
                {
                    PolicyEdge(
                        AgentRole.ARCHITECT, AgentRole.CRITIC, frozenset({MessageType.OPINION})
                    )
                }
            ),
        ),
    )
    bus, _, _, _ = scoped(store, definition, gate=allowed)
    bus.send(message)
    closed = replace(definition, stages=(replace(stage, edges=()),))
    bus, _, _, _ = scoped(store, closed, gate=allowed)
    with pytest.raises(EdgeNotPermitted):
        bus.send(message.model_copy(update={"id": "new"}))


@pytest.mark.asyncio
async def test_bound_reply_recovers_after_schedule_drift_without_spending(store, monkeypatch):
    definition = protocol(turns=1)
    bus, delivery, gate, agent = scoped(store, definition)
    parent = bus.send(request())
    original_send = bus.send

    def crash(message, **kwargs):
        if message.reply_to_id:
            raise RuntimeError("crash before reply persistence")
        return original_send(message, **kwargs)

    monkeypatch.setattr(bus, "send", crash)
    with pytest.raises(RuntimeError, match="crash"):
        await delivery.deliver_and_reply(parent.id, reply_type=MessageType.OPINION)
    facts = collect_stage_facts(
        store, definition, ctx(definition), {AgentRole.ARCHITECT: parent.id}
    )
    assert facts.requests[0].state is RequestState.PENDING
    narrowed = replace(
        definition,
        stages=(
            replace(
                definition.stages[0],
                allowed_message_types=(MessageType.NOTE,),
                expected_outputs=(ExpectedOutput(AgentRole.ARCHITECT, MessageType.NOTE),),
            ),
        ),
    )
    _, recovered_delivery, _, _ = scoped(store, narrowed, gate=gate, agent=agent)
    recovered = await recovered_delivery.deliver_and_reply(
        parent.id, reply_type=MessageType.OPINION
    )
    before = snapshot(store)
    assert (
        await recovered_delivery.deliver_and_reply(parent.id, reply_type=MessageType.OPINION)
    ).reply.id == recovered.reply.id
    assert agent.calls == 1 and snapshot(store) == before
    facts = collect_stage_facts(
        store, definition, ctx(definition), {AgentRole.ARCHITECT: parent.id}, policy=gate
    )
    assert evaluate_stage(definition, facts).status is EvaluationStatus.COMPLETE


@pytest.mark.asyncio
async def test_standalone_binding_has_no_schedule_exemption(store):
    definition = protocol()
    bus, delivery, gate, agent = scoped(store, definition)
    parent = bus.send(request())
    await delivery.deliver(parent.id)
    narrowed = replace(
        definition,
        stages=(
            replace(
                definition.stages[0],
                allowed_message_types=(MessageType.NOTE,),
                expected_outputs=(ExpectedOutput(AgentRole.ARCHITECT, MessageType.NOTE),),
            ),
        ),
    )
    _, delivery, _, _ = scoped(store, narrowed, gate=gate, agent=agent)
    before = snapshot(store)
    with pytest.raises(StageScheduleRefusal):
        await delivery.deliver_and_reply(parent.id, reply_type=MessageType.OPINION)
    assert agent.calls == 1 and snapshot(store) == before


@pytest.mark.asyncio
async def test_collector_clarification_early_stop_and_failed_retry_accounting(store):
    definition = protocol(early=True)
    bus, delivery, gate, agent = scoped(store, definition)
    seed = bus.send(request(type=MessageType.CLARIFICATION_REQUEST, blocking=True))
    facts = collect_stage_facts(store, definition, ctx(definition), {}, (seed.id,))
    assert evaluate_stage(definition, facts).status is EvaluationStatus.CONTINUE
    await delivery.deliver_and_reply(seed.id)
    facts = collect_stage_facts(store, definition, ctx(definition), {}, (seed.id,))
    assert evaluate_stage(definition, facts).reason is EvaluationReason.ANSWERED
    agent.fail = True
    second = bus.send(request())
    await delivery.deliver_and_reply(second.id, reply_type=MessageType.OPINION)
    facts = collect_stage_facts(
        store, definition, ctx(definition), {AgentRole.ARCHITECT: second.id}, policy=gate
    )
    assert facts.requests[0].state is RequestState.FAILED
    assert facts.budget_exhaustion.scope == "stage"
    assert evaluate_stage(definition, facts).status is EvaluationStatus.BLOCKED
    retry = bus.send(request())
    before = snapshot(store)
    with pytest.raises(TurnBudgetExhausted):
        await delivery.deliver_and_reply(retry.id, reply_type=MessageType.OPINION)
    assert agent.calls == 2 and snapshot(store) == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "run_status, request_state, stage_status",
    [
        (RunStatus.RUNNING, RequestState.PENDING, EvaluationStatus.CONTINUE),
        (RunStatus.FAILED, RequestState.FAILED, EvaluationStatus.BLOCKED),
        (RunStatus.CANCELLED, RequestState.FAILED, EvaluationStatus.BLOCKED),
        (RunStatus.SUCCEEDED, RequestState.SUCCEEDED, EvaluationStatus.COMPLETE),
    ],
)
async def test_delivery_run_status_drives_stage_completion(
    store, run_status, request_state, stage_status
):
    definition = protocol(turns=1)
    bus, delivery, gate, agent = scoped(store, definition)
    parent = bus.send(request())
    outcome = await delivery.deliver(parent.id)
    store.update_model(outcome.ask.run.model_copy(update={"status": run_status}))
    if run_status is RunStatus.SUCCEEDED:
        await delivery.deliver_and_reply(parent.id, reply_type=MessageType.OPINION)
    before = snapshot(store)
    facts = collect_stage_facts(
        store, definition, ctx(definition), {AgentRole.ARCHITECT: parent.id}
    )
    assert facts.requests[0].state is request_state
    assert evaluate_stage(definition, facts).status is stage_status
    if run_status is RunStatus.CANCELLED:
        assert evaluate_stage(definition, facts).reason is EvaluationReason.REQUEST_FAILED
        # Cancellation remains terminal even when the delivered turn exhausted its budget.
        exhausted = collect_stage_facts(
            store, definition, ctx(definition), {AgentRole.ARCHITECT: parent.id}, policy=gate
        )
        assert exhausted.budget_exhaustion.scope == "stage"
        assert evaluate_stage(definition, exhausted).reason is EvaluationReason.REQUEST_FAILED
        with pytest.raises(DeliveryRefusal) as refusal:
            await delivery.deliver_and_reply(parent.id, reply_type=MessageType.OPINION)
        assert not isinstance(refusal.value, DeliveryPendingRefusal)
    assert snapshot(store) == before
    assert agent.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_status", [RunStatus.FAILED, RunStatus.CANCELLED])
async def test_terminal_unsuccessful_run_cannot_supply_completion_reply(store, terminal_status):
    definition = protocol()
    bus, delivery, _, _ = scoped(store, definition)
    parent = bus.send(request())
    outcome = await delivery.deliver_and_reply(parent.id, reply_type=MessageType.OPINION)
    # A terminal status change must not leave a prior reply usable as success evidence.
    store.update_model(outcome.ask.run.model_copy(update={"status": terminal_status}))
    before = snapshot(store)
    with pytest.raises(ProtocolFactsError, match="unsuccessful Run"):
        collect_stage_facts(store, definition, ctx(definition), {AgentRole.ARCHITECT: parent.id})
    assert snapshot(store) == before


def test_collector_rejects_foreign_duplicate_and_unbound_reply_facts(store):
    definition = protocol()
    bus, _, _, _ = scoped(store, definition)
    parent = bus.send(request())
    before = snapshot(store)
    for mapping, seeds in [
        ({AgentRole.ARCHITECT: "absent"}, ()),
        ({AgentRole.CRITIC: parent.id}, ()),
        ({}, (parent.id, parent.id)),
        ({}, (parent.id,)),
    ]:
        with pytest.raises(ProtocolFactsError):
            collect_stage_facts(store, definition, ctx(definition), mapping, seeds)
    with pytest.raises(ProtocolFactsError, match="foreign"):
        collect_stage_facts(
            store,
            definition,
            replace(ctx(definition), execution_key="other"),
            {AgentRole.ARCHITECT: parent.id},
        )
    assert snapshot(store) == before
    # Low-level import corruption must not become completion evidence.
    store.save_model(
        Message(
            sender=parent.recipient,
            recipient=parent.sender,
            room_id=parent.room_id,
            stage_key=parent.stage_key,
            type=MessageType.OPINION,
            reply_to_id=parent.id,
            content="Claim without binding",
        )
    )
    with pytest.raises(ProtocolFactsError, match="no delivery binding"):
        collect_stage_facts(store, definition, ctx(definition), {AgentRole.ARCHITECT: parent.id})


def test_stage_turn_limit_serializes_independent_connections(tmp_path):
    path = tmp_path / "race.sqlite3"
    conn = connect(path)
    migrate(conn)
    root = SqliteRelayStore(conn)
    root.save_model(Room(id="room-1", name="test"))
    definition = protocol(turns=1)
    barrier = threading.Barrier(2)

    def attempt():
        connection = connect(path)
        try:
            local = SqliteRelayStore(connection)
            bus, delivery, _, agent = scoped(local, definition)
            parent = bus.send(request())
            barrier.wait(timeout=10)
            try:
                asyncio.run(delivery.deliver_and_reply(parent.id, reply_type=MessageType.OPINION))
                return "success", agent.calls
            except TurnBudgetExhausted as exc:
                return exc.scope, agent.calls
        finally:
            connection.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: attempt(), range(2)))
    assert sorted(results) == [("stage", 0), ("success", 1)]
    assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
    conn.close()


def test_populated_v5_migration_preserves_history_and_stage_roundtrip(tmp_path):
    conn = connect(tmp_path / "legacy.sqlite3")
    for version in range(1, 6):
        for statement in _MIGRATIONS[version]:
            conn.execute(statement)
    conn.execute("PRAGMA user_version = 5")
    conn.execute(
        "INSERT INTO messages (id,sender,recipient,type,content,created_at) "
        "VALUES ('old','relay:test','agent','note','legacy','2026-01-01T00:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO event_log (type,content,created_at) "
        "VALUES ('message_sent','legacy','2026-01-01T00:00:00+00:00')"
    )
    old_message = dict(conn.execute("SELECT * FROM messages").fetchone())
    assert migrate(conn) == 6
    assert migrate(conn) == 6
    migrated = dict(conn.execute("SELECT * FROM messages").fetchone())
    assert migrated == {**old_message, "stage_key": None}
    store = SqliteRelayStore(conn)
    assert store.load_model(Message, "old").stage_key is None
    writer = EventLogWriter(conn)
    assert writer.all()[0].stage_key is None
    record = writer.record(
        EventLogEntry(type=EventType.MESSAGE_SENT, content="stage", stage_key="key")
    )
    assert writer.tail(1)[0] == record
    assert store.load_model(EventLogEntry, record.sequence) == record
    assert {row[1] for row in conn.execute("PRAGMA index_list(messages)")} >= {
        "idx_messages_stage_blocking"
    }
    assert {row[1] for row in conn.execute("PRAGMA index_list(event_log)")} >= {
        "idx_event_stage_type"
    }
    for table in ("messages", "event_log"):
        for operation in (f"DELETE FROM {table}", f"UPDATE {table} SET stage_key = 'changed'"):
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                conn.execute(operation)
    conn.close()


def test_collector_uses_one_snapshot_during_concurrent_delivery(store, monkeypatch):
    definition = protocol()
    bus, _, _, _ = scoped(store, definition)
    parent = bus.send(request())
    path = store.conn.execute("PRAGMA database_list").fetchone()[2]
    original_load = store.load_model
    inserted = False
    trace = []
    store.conn.set_trace_callback(trace.append)

    def load_then_deliver(model, key):
        nonlocal inserted
        result = original_load(model, key)
        if model is Message and key == parent.id and not inserted:
            inserted = True
            other_conn = connect(path)
            try:
                other = SqliteRelayStore(other_conn)
                _, delivery, _, _ = scoped(other, definition)
                asyncio.run(delivery.deliver_and_reply(parent.id, reply_type=MessageType.OPINION))
            finally:
                other_conn.close()
        return result

    monkeypatch.setattr(store, "load_model", load_then_deliver)
    facts = collect_stage_facts(
        store, definition, ctx(definition), {AgentRole.ARCHITECT: parent.id}
    )
    assert facts.requests[0].state is RequestState.PENDING
    assert not store.conn.in_transaction
    assert not any(sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for sql in trace)
    fresh = collect_stage_facts(
        store, definition, ctx(definition), {AgentRole.ARCHITECT: parent.id}
    )
    assert fresh.requests[0].state is RequestState.SUCCEEDED
    # An existing caller transaction remains open even when fact collection refuses.
    store.conn.execute("BEGIN")
    try:
        with pytest.raises(ProtocolFactsError):
            collect_stage_facts(store, definition, ctx(definition), {AgentRole.ARCHITECT: "absent"})
        assert store.conn.in_transaction
    finally:
        store.conn.execute("ROLLBACK")
        store.conn.set_trace_callback(None)


@pytest.mark.asyncio
@pytest.mark.parametrize("corruption", ["stage", "role", "references"])
async def test_foreign_or_contradictory_binding_cannot_authorize_recovery(store, corruption):
    definition = protocol()
    bus, delivery, _, agent = scoped(store, definition)
    parent = bus.send(request())
    run = store.save_model(
        Run(
            agent=parent.recipient,
            role="critic" if corruption == "role" else "architect",
            status=RunStatus.SUCCEEDED,
        )
    )
    refs = [f"message:{parent.id}", f"run:{run.id}", "reply-type:opinion"]
    if corruption == "references":
        refs.append("run:contradiction")
    EventLogWriter(store.conn).record(
        EventLogEntry(
            type=EventType.MESSAGE_DELIVERED,
            content="bad imported binding",
            room_id=parent.room_id,
            stage_key="foreign" if corruption == "stage" else parent.stage_key,
            sender="relay:delivery",
            recipient=parent.recipient,
            references=refs,
        )
    )
    before = snapshot(store)
    with pytest.raises(StageContextRefusal):
        await delivery.deliver_and_reply(parent.id, reply_type=MessageType.OPINION)
    with pytest.raises(ProtocolFactsError):
        collect_stage_facts(store, definition, ctx(definition), {AgentRole.ARCHITECT: parent.id})
    assert snapshot(store) == before and agent.calls == 0


def test_stage_blocking_limit_serializes_independent_connections(tmp_path):
    path = tmp_path / "blocking-race.sqlite3"
    conn = connect(path)
    migrate(conn)
    SqliteRelayStore(conn).save_model(Room(id="room-1", name="test"))
    definition = protocol(blocking=1)
    barrier = threading.Barrier(2)

    def attempt():
        local_conn = connect(path)
        try:
            bus, _, _, _ = scoped(SqliteRelayStore(local_conn), definition)
            barrier.wait(timeout=10)
            try:
                bus.send(request(type=MessageType.CLARIFICATION_REQUEST, blocking=True))
                return "success"
            except BlockingBudgetExhausted as exc:
                return exc.scope
        finally:
            local_conn.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: attempt(), range(2))) == ["stage", "success"]
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM event_log").fetchone()[0] == 1
    conn.close()


@pytest.mark.asyncio
async def test_legacy_traffic_counts_only_toward_aggregate_limits(store):
    definition = protocol(turns=1)
    gate = SqliteCommunicationPolicyGate(store, CommunicationPolicy(CommunicationBudgets(1, 1)))
    writer = EventLogWriter(store.conn)
    bus = ConversationBus(store, writer, Resolver(), gate)
    delivery = MessageDelivery(store, writer, Factory(OfflineAgent()), bus, gate)
    parent = bus.send(request())
    await delivery.deliver_and_reply(parent.id, reply_type=MessageType.OPINION)
    assert parent.stage_key is None
    gate.check_stage_turn_budget(
        "room-1", None, ctx(definition).stage_key, CommunicationBudgets(1, 1)
    )
    with pytest.raises(TurnBudgetExhausted) as exc:
        gate.check_turn_budget("room-1", None)
    assert exc.value.scope == "aggregate"

"""P5.3 offline execution, recovery, and persistence acceptance cases."""

import asyncio
import concurrent.futures
import json
import sqlite3
import threading
from dataclasses import replace
from pathlib import Path

import pytest

from relay.agents.base import Agent, AgentResponse, BackendType
from relay.context.protocols import load_protocol
from relay.core.delivery import MessageDelivery
from relay.core.policy import (
    CommunicationBudgets,
    CommunicationPolicy,
    SqliteCommunicationPolicyGate,
)
from relay.core.protocol_encoding import ParticipantConfig, definition_bytes, definition_digest
from relay.core.protocol_runner import ProtocolRunner, ProtocolSpec, ProtocolStopReason
from relay.core.protocols import EvaluationStatus, ParticipantRequirement
from relay.core.rooms import RoomLifecycle
from relay.core.state_machine import TaskState
from relay.harness.capabilities import HarnessCapability
from relay.storage.db import _MIGRATIONS, SCHEMA_VERSION, connect, migrate
from relay.storage.events import EventLogWriter
from relay.storage.models import (
    Message,
    MessageType,
    ProtocolExecution,
    Room,
    RoomStatus,
    Run,
    RunStatus,
    Task,
    Workspace,
)
from relay.storage.store import ImmutableHistoryError, SqliteRelayStore


def debate(repeated=False):
    definition = load_protocol(Path(__file__).resolve().parents[1] / "protocols/debate.yaml")
    return definition if repeated else replace(definition, repeat=None)


class OfflineAgent(Agent):
    def __init__(self):
        self.requests = []
        self.fail = False
        self.wait = None
        self.entered = None

    async def run(self, request):
        self.requests.append(request)
        if self.entered is not None:
            self.entered.set()
        if self.wait is not None:
            await self.wait()
        if self.fail:
            raise RuntimeError("offline failure")
        return AgentResponse(
            agent="offline", role=request.role, output=f"output-{len(self.requests)}"
        )


class Factory:
    def __init__(self, agent=None):
        self.agent = agent or OfflineAgent()
        self.model = "offline-v1"
        self.missing = False

    def build(self, name):
        return self.agent

    def model_of(self, name):
        return self.model

    def protocol_participant(self, role):
        if self.missing:
            raise ValueError("missing role")
        return ParticipantConfig(role.value + "_agent", BackendType.API, "offline", self.model)


@pytest.fixture
def store(tmp_path):
    conn = connect(tmp_path / "runner.db")
    migrate(conn)
    store = SqliteRelayStore(conn)
    store.save_model(Room(id="room", name="Test"))
    yield store
    conn.close()


def runner(store, factory=None, turns=100):
    factory = factory or Factory()
    gate = SqliteCommunicationPolicyGate(store, CommunicationPolicy(CommunicationBudgets(turns, 0)))
    return ProtocolRunner(store, EventLogWriter(store.conn), factory, factory, gate)


def spec(definition=None, **kwargs):
    return ProtocolSpec(
        definition or debate(), "key", "Design the system", room_id="room", **kwargs
    )


@pytest.mark.parametrize("repeated,turns", [(False, 10), (True, 22)])
async def test_debate_context_resume_and_authority(store, repeated, turns):
    factory = Factory()
    service = runner(store, factory, turns=turns)
    authority = ["tasks", "evidence_records", "approvals", "decisions"]
    before = {t: list(store.conn.execute(f"SELECT * FROM {t}")) for t in authority}
    result = await service.start(spec(debate(repeated)))
    assert result.stop_reason is ProtocolStopReason.COMPLETE, result.refusal
    assert result.evaluation.status is EvaluationStatus.COMPLETE
    assert len(result.output_ids) == len(factory.agent.requests) == turns
    messages = list(store.all_models(Message, "WHERE sender = 'relay:protocol'"))
    assert len(messages) == turns
    seen = []
    groups = {}
    for message in messages:
        payload = json.loads(message.content)
        group = (payload["stage"], payload["occurrence"])
        if group not in groups:
            groups[group] = list(seen)
        assert [p["message_id"] for p in payload["prior_outputs"]] == groups[group]
        assert message.references == [f"message:{mid}" for mid in groups[group]]
        for output in payload["prior_outputs"]:
            assert output["content"] == store.load_model(Message, output["message_id"]).content
        if payload["stage"] == "independent_analysis":
            assert payload["prior_outputs"] == []
        reply = next(store.all_models(Message, "WHERE reply_to_id = ?", [message.id]))
        seen.append(reply.id)
    assert len(json.loads(messages[-1].content)["prior_outputs"]) == turns - 1
    assert len({m.stage_key for m in messages}) == (8 if repeated else 4)
    assert {t: list(store.conn.execute(f"SELECT * FROM {t}")) for t in authority} == before
    counts = store.counts()
    assert await service.resume(result.execution_id) == result
    assert await service.start(spec(debate(repeated))) == result
    assert store.counts() == counts
    assert len(factory.agent.requests) == turns


async def test_conflicting_inputs_and_drift_only_record_outcome(store):
    factory = Factory()
    service = runner(store, factory)
    original = spec()
    result = await service.start(original)
    counts = store.counts()
    for changed in (
        replace(original, topic="different"),
        replace(original, definition=replace(original.definition, repeat=debate(True).repeat)),
        replace(
            original,
            definition=replace(
                original.definition,
                stages=(
                    replace(
                        original.definition.stages[0],
                        budgets=replace(original.definition.stages[0].budgets, max_agent_turns=4),
                    ),
                    *original.definition.stages[1:],
                ),
            ),
        ),
    ):
        refused = await service.start(changed)
        assert refused.stop_reason is ProtocolStopReason.INPUT_REFUSED
    assert store.counts() == counts  # pre-execution refusals do not create observations
    factory.model = "changed"
    assert (
        await service.resume(result.execution_id)
    ).stop_reason is ProtocolStopReason.INPUT_REFUSED
    assert store.counts() == {**counts, "event_log": counts["event_log"] + 1}
    assert len(factory.agent.requests) == 10


async def test_closed_room_start_refuses_with_zero_persistence(store):
    room = store.load_model(Room, "room")
    store.update_model(room.model_copy(update={"status": RoomStatus.CLOSED}))
    factory = Factory()
    service = runner(store, factory)
    before = store.counts()

    result = await service.start(spec())

    assert result.stop_reason is ProtocolStopReason.INPUT_REFUSED
    assert store.counts() == before
    assert factory.agent.requests == []


async def test_closed_room_resume_refuses_without_outcome_or_agent(store):
    factory = Factory()
    service = runner(store, factory)
    execution = service.prepare(spec())
    store.save_model(execution)
    room = store.load_model(Room, "room")
    store.update_model(room.model_copy(update={"status": RoomStatus.CLOSED}))
    before = store.counts()

    result = await service.resume(execution.id)

    assert result.stop_reason is ProtocolStopReason.INPUT_REFUSED
    assert store.counts() == before
    assert factory.agent.requests == []


async def test_room_resume_restores_protocol_traffic_without_changing_task(store):
    workspace = store.save_model(Workspace(id="workspace", name="demo"))
    room = store.load_model(Room, "room")
    room = store.update_model(room.model_copy(update={"workspace_id": workspace.id}))
    task = store.save_model(Task(title="preserved", room_id=room.id))
    lifecycle = RoomLifecycle(store, EventLogWriter(store.conn))
    closed = lifecycle.close(workspace, room)
    factory = Factory()
    service = runner(store, factory)

    refused = await service.start(spec())
    assert refused.stop_reason is ProtocolStopReason.INPUT_REFUSED
    assert factory.agent.requests == []

    lifecycle.resume(store.load_model(Workspace, workspace.id), closed)
    completed = await service.start(spec())
    assert completed.stop_reason is ProtocolStopReason.COMPLETE
    assert factory.agent.requests
    assert store.load_model(Task, task.id).state is TaskState.CREATED


async def test_failed_and_cancelled_requests_are_never_retried(store):
    factory = Factory()
    factory.agent.fail = True
    service = runner(store, factory)
    result = await service.start(spec())
    assert result.stop_reason is ProtocolStopReason.REQUEST_FAILED
    run = next(store.all_models(Run))
    for status in (RunStatus.FAILED, RunStatus.CANCELLED):
        store.update_model(run.model_copy(update={"status": status}))
        counts = store.counts()
        assert (
            await service.resume(result.execution_id)
        ).stop_reason is ProtocolStopReason.REQUEST_FAILED
        delta = 1 if status is RunStatus.CANCELLED else 0
        assert store.counts() == {**counts, "event_log": counts["event_log"] + delta}
    assert len(factory.agent.requests) == 1


async def test_pending_returns_without_polling_then_recovers(store):
    factory = Factory()
    release = asyncio.Event()
    factory.agent.entered = asyncio.Event()
    factory.agent.wait = release.wait
    service = runner(store, factory)
    active = asyncio.create_task(service.start(spec()))
    await asyncio.wait_for(factory.agent.entered.wait(), 3)
    execution = next(store.all_models(ProtocolExecution))
    pending = await service.resume(execution.id)
    assert pending.stop_reason is ProtocolStopReason.DELIVERY_PENDING
    assert len(factory.agent.requests) == 1
    factory.agent.wait = None
    release.set()
    assert (await active).stop_reason is ProtocolStopReason.COMPLETE
    assert len(factory.agent.requests) == 10


@pytest.mark.parametrize("fail_at", [1, 10])
async def test_reply_materialization_crash_recovers_without_spending(store, monkeypatch, fail_at):
    from relay.core.discussion_view import build_discussion_view
    from relay.core.protocol_outcomes import outcome_events

    factory = Factory()
    service = runner(store, factory, turns=10)
    build_reply = MessageDelivery._build_reply

    def crash(*args, **kwargs):
        if len(factory.agent.requests) == fail_at:
            raise RuntimeError("simulated crash after successful Run")
        return build_reply(*args, **kwargs)

    monkeypatch.setattr(MessageDelivery, "_build_reply", staticmethod(crash))
    with pytest.raises(RuntimeError, match="simulated crash"):
        await service.start(spec())
    assert len(factory.agent.requests) == fail_at
    execution = next(store.all_models(ProtocolExecution))
    changes = store.conn.total_changes
    view = build_discussion_view(store, execution)
    assert view["last_observation"] is None
    assert len(view["outputs"]) == fail_at - 1
    assert len(factory.agent.requests) == fail_at
    assert store.conn.total_changes == changes
    monkeypatch.setattr(MessageDelivery, "_build_reply", staticmethod(build_reply))
    result = await service.resume(execution.id)
    assert result.stop_reason is ProtocolStopReason.COMPLETE
    assert len(factory.agent.requests) == store.counts()["runs"] == 10
    assert len(outcome_events(store, execution.id)) == 1


async def test_refusals_and_budgets(store):
    factory = Factory()
    service = runner(store, factory, turns=1)
    initial = store.counts()
    definition = debate()
    required = replace(
        definition,
        participants=(
            ParticipantRequirement(
                definition.participants[0].role, (next(iter(HarnessCapability)),)
            ),
            *definition.participants[1:],
        ),
    )
    assert (await service.start(spec(required))).stop_reason is ProtocolStopReason.INPUT_REFUSED
    factory.missing = True
    assert (await service.start(spec())).stop_reason is ProtocolStopReason.INPUT_REFUSED
    factory.missing = False
    invalid = replace(
        definition,
        stages=(
            replace(
                definition.stages[0],
                allowed_message_types=(definition.stages[0].expected_outputs[0].type,),
            ),
            *definition.stages[1:],
        ),
    )
    assert (await service.start(spec(invalid))).stop_reason is ProtocolStopReason.POLICY_REFUSED
    assert store.counts() == initial
    result = await service.start(spec())
    assert result.stop_reason is ProtocolStopReason.BUDGET_EXHAUSTED
    assert result.refusal.scope == "aggregate"
    counts = store.counts()
    assert (
        await service.resume(result.execution_id)
    ).stop_reason is ProtocolStopReason.BUDGET_EXHAUSTED
    assert store.counts() == counts
    assert len(factory.agent.requests) == 1


async def test_stage_budget_blocks_without_widening(store):
    definition = debate()
    definition = replace(
        definition,
        stages=(
            replace(
                definition.stages[0],
                budgets=replace(definition.stages[0].budgets, max_agent_turns=1),
            ),
            *definition.stages[1:],
        ),
    )
    result = await runner(store).start(spec(definition))
    assert result.stop_reason is ProtocolStopReason.BUDGET_EXHAUSTED
    assert result.refusal.scope == "stage"
    assert store.counts()["runs"] == 1


def execution(**kwargs):
    definition = debate()
    return ProtocolExecution(
        execution_key="same",
        topic="topic",
        definition_snapshot=definition_bytes(definition).decode(),
        definition_digest=definition_digest(definition),
        bindings_snapshot="[]",
        **kwargs,
    )


@pytest.mark.parametrize(
    "scope", [{"room_id": "r"}, {"task_id": "t"}, {"room_id": "r", "task_id": "t"}]
)
def test_scope_indexes_and_append_only_layers(store, scope):
    record = execution(**scope)
    store.save_model(record)
    assert store.load_model(ProtocolExecution, record.id) == record
    with pytest.raises(sqlite3.IntegrityError):
        store.save_model(execution(**scope))
    for action in (store.update_model, store.delete_model):
        with pytest.raises(ImmutableHistoryError):
            action(record)
    for sql in (
        "UPDATE protocol_executions SET topic = 'changed'",
        "DELETE FROM protocol_executions",
    ):
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            store.conn.execute(sql)
    assert store.load_model(ProtocolExecution, record.id) == record


def test_null_scope_distinctions_and_check(store):
    for scope in ({"room_id": "null"}, {"task_id": "null"}, {"room_id": "null", "task_id": "null"}):
        store.save_model(execution(**scope))
    assert store.counts()["protocol_executions"] == 3
    invalid = execution(room_id="r").model_copy(update={"room_id": None})
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        store.save_model(invalid)


def test_populated_v6_migration(tmp_path):
    conn = connect(tmp_path / "legacy.db")
    for version in range(1, 7):
        for sql in _MIGRATIONS[version]:
            conn.execute(sql)
    conn.execute("PRAGMA user_version = 6")
    store = SqliteRelayStore(conn)
    conn.execute(
        "INSERT INTO rooms (id, name, members_json, created_at) VALUES (?, ?, '[]', ?)",
        ["legacy", "Legacy", "2025-01-01T00:00:00+00:00"],
    )
    legacy = Message(
        sender="relay:legacy",
        recipient="agent",
        room_id="legacy",
        content="old stage output",
        stage_key="stage:v1:legacy",
        type=MessageType.OPINION,
    )
    store.save_model(legacy)
    before = list(conn.execute("SELECT * FROM rooms"))
    assert migrate(conn) == SCHEMA_VERSION
    assert [tuple(row)[:6] for row in conn.execute("SELECT * FROM rooms")] == [tuple(before[0])]
    assert store.load_model(Message, legacy.id) == legacy
    assert migrate(conn) == SCHEMA_VERSION
    store.save_model(execution(room_id="legacy"))
    indexes = conn.execute("PRAGMA index_list(protocol_executions)").fetchall()
    assert sum(row[2] == 1 and row[4] == 1 for row in indexes) == 3
    conn.close()


async def test_corrupt_snapshot_and_request_semantics_refuse_without_spending(store):
    factory = Factory()
    service = runner(store, factory)
    result = await service.start(spec())
    execution = store.load_model(ProtocolExecution, result.execution_id)
    bad = execution.model_copy(
        update={
            "id": "corrupt",
            "execution_key": "corrupt",
            "definition_digest": "invalid",
        }
    )
    store.save_model(bad)
    counts = store.counts()
    assert (await service.resume(bad.id)).stop_reason is ProtocolStopReason.INPUT_REFUSED
    # Simulate storage corruption outside the supported append-only APIs.
    store.conn.execute("DROP TRIGGER messages_no_update")
    store.conn.execute("UPDATE messages SET content = 'corrupt' WHERE sender = 'relay:protocol'")
    refused = await service.resume(result.execution_id)
    assert refused.stop_reason is ProtocolStopReason.INPUT_REFUSED
    assert "conflicting canonical content" in str(refused.refusal)
    assert store.counts() == {**counts, "event_log": counts["event_log"] + 2}
    assert len(factory.agent.requests) == 10


async def test_resume_uses_saved_definition_after_source_is_removed(store, tmp_path):
    from relay.core.policy import BudgetExhausted

    factory = Factory()
    service = runner(store, factory)
    source = tmp_path / "protocol.yaml"
    source.write_bytes((Path(__file__).resolve().parents[1] / "protocols/debate.yaml").read_bytes())
    result = await service.start(spec(load_protocol(source)))
    source.unlink()

    class DenyFreshPolicy(SqliteCommunicationPolicyGate):
        def check_edge(self, envelope):
            raise BudgetExhausted("new admission disabled")

    service._policy = DenyFreshPolicy(store, CommunicationPolicy(CommunicationBudgets(22, 0)))
    assert (await service.resume(result.execution_id)).stop_reason is ProtocolStopReason.COMPLETE
    assert len(factory.agent.requests) == 22


@pytest.mark.parametrize(
    "scope", [{"room_id": "room"}, {"task_id": "task"}, {"room_id": "room", "task_id": "task"}]
)
def test_concurrent_duplicate_starts(tmp_path, scope):
    path = tmp_path / "concurrent.db"
    conn = connect(path)
    migrate(conn)
    # Task scope needs a real Task for the existing Run foreign key.
    from relay.storage.models import Task

    store = SqliteRelayStore(conn)
    store.save_model(Room(id="room", name="Room"))
    store.save_model(Task(id="task", title="Task"))
    conn.close()
    barrier = threading.Barrier(2)

    def work():
        conn = connect(path)
        try:
            service = runner(SqliteRelayStore(conn))
            barrier.wait(timeout=5)
            return asyncio.run(service.start(ProtocolSpec(debate(), "same", "topic", **scope)))
        finally:
            conn.close()

    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        outcomes = list(pool.map(lambda _: work(), range(2)))
    assert outcomes[0].execution_id == outcomes[1].execution_id
    assert all(
        o.stop_reason in (ProtocolStopReason.COMPLETE, ProtocolStopReason.DELIVERY_PENDING)
        for o in outcomes
    ), outcomes
    conn = connect(path)
    store = SqliteRelayStore(conn)
    assert (
        asyncio.run(runner(store).resume(outcomes[0].execution_id)).stop_reason
        is ProtocolStopReason.COMPLETE
    )
    assert store.counts()["protocol_executions"] == 1
    assert store.counts()["runs"] == 10
    conn.close()

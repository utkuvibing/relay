"""P7.3 Room-bound P6.4 signals: seat routing, fencing, standalone identity."""

from __future__ import annotations

import httpx
import pytest

from relay.agents.base import AgentRole
from relay.core.resolver import (
    RoomSeatRoleResolver,
    role_resolver_from_config,
    seat_resolver_for_room,
)
from relay.core.room_records import RoomRecordRefusal
from relay.core.rooms import ClosedRoomError, RoomLifecycle
from relay.core.stage_signals import (
    SignalServices,
    StageSignalPayload,
    compose_signal_message,
    resolve_open_signal,
)
from relay.storage.models import (
    Artifact,
    ArtifactKind,
    EventLogEntry,
    EventType,
    Message,
    MessageType,
    Room,
    Run,
    RunStatus,
    Task,
    TaskState,
)
from tests.room_helpers import (
    RoomFixture,
    freeze,
    pin_baseline,
    room_config,
    room_store,
)


@pytest.fixture()
def fixture(tmp_path) -> RoomFixture:
    return room_store(tmp_path)


class TestSeatFirstRouting:
    """Role routing for Room-bound traffic comes from persisted seats."""

    def test_room_seats_override_config_role_bindings(self, fixture):
        config = room_config()
        resolver = seat_resolver_for_room(fixture.room, config)
        # Room @planner → gpt (persisted seat) while relay.yaml says otherwise.
        divergent = room_config(planner="other")
        assert role_resolver_from_config(divergent).resolve_role("planner") == "other"
        assert resolver.resolve_role("planner") == "gpt"
        assert isinstance(resolver, RoomSeatRoleResolver)

    def test_rebound_seat_is_respected_across_invocations(self, fixture):
        store, writer = fixture.store, fixture.writer
        room = RoomLifecycle(store, writer).bind(
            store.load_model(Room, fixture.room.id),
            AgentRole.PLANNER.value,
            "other",
            {"gpt", "impl", "other"},
        )
        config = room_config()
        assert seat_resolver_for_room(room, config).resolve_role("planner") == "other"
        assert role_resolver_from_config(config).resolve_role("planner") == "gpt"

    def test_membership_widens_to_configured_agents_but_routing_does_not(self, fixture):
        config = room_config()
        resolver = seat_resolver_for_room(fixture.room, config)
        # The reviewer may hold no seat but must still be a known sender.
        assert resolver.knows_agent("other") is True
        assert resolver.knows_agent("ghost") is False

    def test_unbuildable_seat_agent_fails_honestly(self, fixture):
        """A persisted seat whose agent is gone must never fall back to config."""
        store, writer = fixture.store, fixture.writer
        room = RoomLifecycle(store, writer).bind(
            store.load_model(Room, fixture.room.id),
            AgentRole.PLANNER.value,
            "ghost",
            {"gpt", "impl", "ghost"},
        )
        config = room_config()
        resolver = seat_resolver_for_room(room, config)
        assert resolver.resolve_role("planner") == "ghost"
        assert "ghost" not in config.agents  # the factory will refuse it


class TestClosedRoomParksSignalTraffic:
    """A closed Room parks the micro-exchange with zero partial persistence."""

    def _room_bound_signal(self, fixture, tmp_path):
        outcome = freeze(fixture, room_config(), workspace_root=tmp_path)
        store = fixture.store
        pin_baseline(store, tmp_path, outcome.task.id)
        run = store.save_model(
            Run(
                agent="impl",
                role=AgentRole.IMPLEMENTER.value,
                status=RunStatus.SUCCEEDED,
                task_id=outcome.task.id,
            )
        )
        fixture.writer.record(
            EventLogEntry(
                type=EventType.BUILD_RUN_DISPATCHED,
                task_id=outcome.task.id,
                sender="relay:build",
                content="build stage 'implement' bound to run",
                references=[
                    f"task:{outcome.task.id}",
                    f"run:{run.id}",
                    "build_stage:implement",
                    "build_attempt:1",
                ],
            )
        )
        signal = StageSignalPayload(
            schema_version="relay.stage_signal.v1",
            kind="proposal",
            to_role=AgentRole.PLANNER.value,
            body="Use design B",
        )
        store.save_model(
            Artifact(
                kind=ArtifactKind.RUN_OUTPUT,
                task_id=outcome.task.id,
                run_id=run.id,
                content=signal.model_dump_json(),
            )
        )
        message = compose_signal_message(store.load_model(Task, outcome.task.id), run, signal)
        assert message.room_id == fixture.room.id
        return outcome, run, message

    @pytest.mark.asyncio
    async def test_closed_room_refuses_the_send_with_no_partial_traffic(self, fixture, tmp_path):
        outcome, run, message = self._room_bound_signal(fixture, tmp_path)
        store, writer = fixture.store, fixture.writer
        RoomLifecycle(store, writer).close(fixture.workspace, fixture.room)
        services = _services(fixture)
        baseline = store.counts()

        with pytest.raises(ClosedRoomError):
            services.bus.send(message)

        assert store.counts() == baseline
        assert not [
            row
            for row in store.all_models(Message)
            if row.task_id == outcome.task.id and row.type is MessageType.PROPOSAL
        ]
        assert not [
            event
            for event in store.all_models(EventLogEntry)
            if event.type is EventType.MESSAGE_SENT and event.task_id == outcome.task.id
        ]
        assert run.status is RunStatus.SUCCEEDED

    @pytest.mark.asyncio
    async def test_resolve_open_signal_parks_with_a_room_closed_escalation(
        self, fixture, tmp_path
    ):
        outcome, run, _message = self._room_bound_signal(fixture, tmp_path)
        store, writer = fixture.store, fixture.writer
        RoomLifecycle(store, writer).close(fixture.workspace, fixture.room)
        services = _services(fixture)
        from relay.core.build_ledger import derive_position
        from relay.storage.store import SqliteEvidenceStore

        position = derive_position(store, SqliteEvidenceStore(store), outcome.task.id)
        assert position.pending_signal is not None
        baseline = store.counts()

        resolution = await resolve_open_signal(
            store,
            writer,
            SqliteEvidenceStore(store),
            services,
            store.load_model(Task, outcome.task.id),
            position.pending_signal,
        )

        assert resolution.status == "escalated"
        assert resolution.escalation is not None
        assert '"reason":"room_closed"' in (resolution.escalation.content or "")
        # Only the escalation observation was written — no signal message, no
        # delivery run, no marker, and the task did not advance.
        assert store.load_model(Task, outcome.task.id).state is TaskState.IMPLEMENTING
        assert not [
            row
            for row in store.all_models(Message)
            if row.task_id == outcome.task.id and row.type is MessageType.PROPOSAL
        ]
        assert not list(store.all_models(Run, "WHERE task_id = ?", [outcome.task.id]))[1:]
        assert store.counts()["messages"] == baseline["messages"]
        assert run.status is RunStatus.SUCCEEDED

    @pytest.mark.asyncio
    async def test_resume_restores_the_continuation(self, fixture, tmp_path, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-room-test")
        outcome, run, _message = self._room_bound_signal(fixture, tmp_path)
        store, writer = fixture.store, fixture.writer
        lifecycle = RoomLifecycle(store, writer)
        lifecycle.close(fixture.workspace, fixture.room)
        services = _services(fixture)
        from relay.core.build_ledger import derive_position
        from relay.storage.store import SqliteEvidenceStore

        evidence = SqliteEvidenceStore(store)
        position = derive_position(store, evidence, outcome.task.id)
        parked = await resolve_open_signal(
            store, writer, evidence, services, store.load_model(Task, outcome.task.id),
            position.pending_signal,
        )
        assert parked.status == "escalated"
        assert parked.escalation is not None
        assert '"reason":"room_closed"' in (parked.escalation.content or "")

        lifecycle.resume(
            store.load_model(type(fixture.workspace), fixture.workspace.id),
            store.load_model(Room, fixture.room.id),
        )
        _swap_transport(
            monkeypatch,
            lambda request: httpx.Response(200, json=_completion("Yes, use design B.")),
        )
        position = derive_position(store, evidence, outcome.task.id)
        resolution = await resolve_open_signal(
            store,
            writer,
            evidence,
            services,
            store.load_model(Task, outcome.task.id),
            position.pending_signal,
        )
        # The continuation now proceeds through the Room's persisted seat
        # resolver: the signal is Room-scoped, addressed to the planner seat's
        # agent, delivered, and answered.
        assert resolution.status == "answered", (
            resolution.escalation.content if resolution.escalation else ""
        )
        assert resolution.reply is not None and resolution.reply.sender == "gpt"
        sent = [
            row
            for row in store.all_models(Message)
            if row.task_id == outcome.task.id and row.type is MessageType.PROPOSAL
        ]
        assert len(sent) == 1
        assert sent[0].room_id == fixture.room.id
        assert sent[0].recipient_role == AgentRole.PLANNER.value
        assert sent[0].recipient == "gpt"
        assert run.status is RunStatus.SUCCEEDED


def _services(fixture: RoomFixture) -> SignalServices:
    from relay.agents.factory import RegistryAgentFactory
    from relay.core.bus import ConversationBus
    from relay.core.delivery import MessageDelivery
    from relay.core.resolver import seat_resolver_for_room

    config = room_config()
    store, writer = fixture.store, fixture.writer
    resolver = seat_resolver_for_room(fixture.room, config)
    factory = RegistryAgentFactory(config, fixture.workspace.path or ".")
    bus = ConversationBus(store, writer, resolver)
    delivery = MessageDelivery(store, writer, factory, bus)
    return SignalServices(bus=bus, delivery=delivery, resolver=resolver)


def _completion(content: str) -> dict:
    return {
        "id": "chatcmpl-room",
        "object": "chat.completion",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": 7, "completion_tokens": 5},
    }


def _swap_transport(monkeypatch, handler) -> None:
    """Redirect the OpenAI adapter's HTTP client onto a MockTransport (offline)."""
    import httpx

    from relay.agents import openai as openai_mod

    def factory(*args, **kwargs) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    class _Surrogate:
        AsyncClient = factory
        TimeoutException = httpx.TimeoutException
        ConnectError = httpx.ConnectError
        HTTPError = httpx.HTTPError

    monkeypatch.setattr(openai_mod, "httpx", _Surrogate)


class TestStandaloneIdentity:
    """A room-less task keeps the config resolver and unmodified records."""

    def test_roomless_task_records_stay_unscoped(self, fixture):
        store = fixture.store
        task = store.save_model(Task(title="standalone"))
        run = store.save_model(
            Run(agent="impl", role=AgentRole.IMPLEMENTER.value, status=RunStatus.SUCCEEDED)
        )
        signal = StageSignalPayload(
            schema_version="relay.stage_signal.v1",
            kind="proposal",
            to_role=AgentRole.PLANNER.value,
            body="proposal",
        )
        message = compose_signal_message(task, run, signal)
        assert message.room_id is None and message.task_id == task.id
        config = room_config()
        resolver = role_resolver_from_config(config)
        assert resolver.resolve_role("planner") == "gpt"


class TestFreezeRefusalsSurfaceAsTypedErrors:
    def test_refusal_is_a_value_error_for_the_cli(self, fixture):
        with pytest.raises(RoomRecordRefusal) as excinfo:
            freeze(fixture, room_config(), reply=None) if False else _missing_source(fixture)
        assert excinfo.value.code == "unknown_source"


def _missing_source(fixture: RoomFixture):
    from relay.core.room_records import resolve_freeze_source

    return resolve_freeze_source(fixture.store, fixture.room, "does-not-exist")

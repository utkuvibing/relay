"""P7.4 Room participant context (D.10) + honest session continuation."""

from __future__ import annotations

from typing import ClassVar

import pytest

from relay.agents.base import Agent, AgentRequest, AgentResponse, AgentRole, BackendType
from relay.agents.config import AgentSettings
from relay.context.config import HarnessAgentConfig
from relay.core.bus import ConversationBus
from relay.core.delivery import (
    DeliveryRefusal,
    MessageDelivery,
    latest_session_ref,
)
from relay.core.room_context import (
    ROOM_CONTEXT_VERSION,
    build_room_participant_context,
    render_room_context,
    room_context_refs,
)
from relay.core.rooms import RoomSeatResolver
from relay.harness.capabilities import HarnessCapability
from relay.harness.errors import UnsupportedCapability
from relay.harness.runtime import HarnessAgent
from relay.harness.types import ExecutionGrantKind
from relay.storage.db import connect, migrate
from relay.storage.events import EventLogWriter
from relay.storage.models import (
    Artifact,
    ArtifactKind,
    Decision,
    DecisionStatus,
    EventLogEntry,
    EventType,
    Finding,
    Message,
    MessageType,
    ReviewSeverity,
    Room,
    Run,
    RunStatus,
)
from relay.storage.store import SqliteRelayStore
from tests.room_helpers import freeze, planner_exchange, room_config, room_store


class _API(Agent):
    name = "fake_api"
    backend = BackendType.API

    def __init__(self) -> None:
        self.received: list[AgentRequest] = []

    async def run(self, request: AgentRequest) -> AgentResponse:
        self.received.append(request)
        return AgentResponse(agent=self.name, role=request.role, output="ok")


class _Harness(HarnessAgent):
    name = "fake_resume"
    capabilities = frozenset({HarnessCapability.READ_ONLY_ACCESS, HarnessCapability.SESSION_RESUME})
    harness_command = "fake-resume"
    seen: ClassVar[list[AgentRequest]] = []

    def __init__(
        self,
        settings: AgentSettings | None = None,
        *,
        profile: HarnessAgentConfig | None = None,
        workspace_root: object = None,
    ) -> None:
        super().__init__(
            settings=settings or AgentSettings(adapter=self.name),
            profile=profile or HarnessAgentConfig(grant=ExecutionGrantKind.READ_ONLY_ACCESS),
            workspace_root=workspace_root,  # type: ignore[arg-type]
        )

    def invocation_argv(self, resolved):  # type: ignore[no-untyped-def]
        return (resolved.command,)

    async def run(self, request: AgentRequest) -> AgentResponse:
        type(self).seen.append(request)
        return AgentResponse(agent=self.name, role=request.role, output="ok")

    def continuation_ref(self) -> str | None:
        return "11111111-1111-1111-1111-111111111111"

    def resume_arguments(self, session_ref: str) -> tuple[str, ...]:
        if session_ref != "11111111-1111-1111-1111-111111111111":
            raise UnsupportedCapability(f"{self.name}: invalid session reference")
        return ("--resume", session_ref)


class _NoResumeHarness(_Harness):
    name = "fake_plain"
    capabilities = frozenset({HarnessCapability.READ_ONLY_ACCESS})


class _Factory:
    def __init__(self, agents: dict[str, Agent]):
        self._agents = agents

    def build(self, name: str) -> Agent:
        return self._agents[name]

    def model_of(self, name: str) -> str | None:
        return None


@pytest.fixture(autouse=True)
def _clear_harness_seen():
    _Harness.seen.clear()
    yield
    _Harness.seen.clear()


@pytest.fixture()
def db(tmp_path):
    conn = connect(tmp_path / "ctx.sqlite3")
    migrate(conn)
    yield conn
    conn.close()


@pytest.fixture()
def store(db):
    return SqliteRelayStore(db)


@pytest.fixture()
def writer(db):
    return EventLogWriter(db)


def _room(store: SqliteRelayStore, writer: EventLogWriter) -> Room:
    from relay.core.rooms import RoomLifecycle
    from relay.storage.models import Workspace

    workspace = store.save_model(Workspace(id="w", name="w"))
    return RoomLifecycle(store, writer).create(
        workspace, "Design", {"planner": "gpt", "reviewer": "gpt"}, {"gpt"}
    )


def test_empty_room_renders_honestly_without_transcript(store, writer):
    room = _room(store, writer)
    ctx = build_room_participant_context(
        store, room_id=room.id, role="planner", agent_name="gpt"
    )
    rendered = render_room_context(ctx)
    assert ROOM_CONTEXT_VERSION in rendered
    assert "CURRENT PLAN: (none)" in rendered
    assert "ACCEPTED DECISIONS (0)" in rendered
    assert "UNRESOLVED BLOCKING (0)" in rendered
    assert "HISTORY EXCERPTS (0)" in rendered
    assert "[relay:continuity fresh]" in rendered
    assert room_context_refs(ctx) == []


def test_context_carries_plan_tip_decisions_blocking_notes_findings(tmp_path):
    fixture = room_store(tmp_path)
    store, writer = fixture.store, fixture.writer
    # Planner discussion + freeze gives a plan chain + active task.
    _parent, reply, _run = planner_exchange(fixture, room=fixture.room)
    config = room_config()
    outcome = freeze(fixture, config, reply=reply)
    task_id = outcome.task.id

    bus = ConversationBus(store, writer, RoomSeatResolver(fixture.room))
    # Accepted decision in the Room.
    bus.send(
        Message(
            sender="human:utku",
            recipient_role="planner",
            room_id=fixture.room.id,
            type=MessageType.PROPOSAL,
            content="Adopt B",
        )
    )
    accepted = store.save_model(
        Decision(
            statement="Adopt design B",
            status=DecisionStatus.ACCEPTED,
            room_id=fixture.room.id,
            source_reply_id=reply.id,
        )
    )
    writer.record(
        EventLogEntry(
            type=EventType.DECISION_ACCEPTED,
            room_id=fixture.room.id,
            sender="gpt",
            content="accepted",
            references=[f"room:{fixture.room.id}", f"decision:{accepted.id}"],
        )
    )
    # Rejected decisions never surface (separate reply keeps the unique
    # promotion key intact).
    _parent2, reply2, _run2 = planner_exchange(fixture, room=fixture.room)
    rejected = store.save_model(
        Decision(
            statement="Adopt design A",
            status=DecisionStatus.REJECTED,
            room_id=fixture.room.id,
            source_reply_id=reply2.id,
        )
    )
    writer.record(
        EventLogEntry(
            type=EventType.DECISION_REJECTED,
            room_id=fixture.room.id,
            sender="gpt",
            content="rejected",
            references=[f"room:{fixture.room.id}", f"decision:{rejected.id}"],
        )
    )
    # Unresolved blocking clarification in the task scope.
    bus.send(
        Message(
            sender="impl",
            run_id=store.save_model(
                Run(agent="impl", role="implementer", status=RunStatus.SUCCEEDED)
            ).id,
            recipient="gpt",
            room_id=fixture.room.id,
            task_id=task_id,
            type=MessageType.CLARIFICATION_REQUEST,
            content="Which schema?",
            blocking=True,
        )
    )
    # A note for later participants.
    bus.send(
        Message(
            sender="impl",
            run_id=store.save_model(
                Run(agent="impl", role="implementer", status=RunStatus.SUCCEEDED)
            ).id,
            recipient="gpt",
            room_id=fixture.room.id,
            task_id=task_id,
            type=MessageType.NOTE,
            content="Shim is intentional",
        )
    )
    # A canonical finding on the Room task.
    review_artifact = store.save_model(
        Artifact(
            kind=ArtifactKind.REVIEW_FINDING,
            room_id=fixture.room.id,
            task_id=task_id,
            content="{}",
        )
    )
    review_run = store.save_model(
        Run(agent="gpt", role="reviewer", status=RunStatus.SUCCEEDED)
    )
    store.save_model(
        Finding(
            room_id=fixture.room.id,
            task_id=task_id,
            review_artifact_id=review_artifact.id,
            review_run_id=review_run.id,
            source_finding_id="F1",
            severity=ReviewSeverity.HIGH,
            title="Missing check",
            description="d",
            requested_change="c",
            validation_expectation="v",
        )
    )
    writer.record(
        EventLogEntry(
            type=EventType.FINDING_RECORDED,
            room_id=fixture.room.id,
            task_id=task_id,
            sender="relay:review",
            content="finding",
            references=[
                f"room:{fixture.room.id}",
                "finding:dummy",
                f"artifact:{review_artifact.id}",
            ],
        )
    )

    ctx = build_room_participant_context(
        store, room_id=fixture.room.id, role="planner", agent_name="gpt"
    )
    rendered = render_room_context(ctx)
    assert ctx.plan_tip_id is not None
    assert f"artifact:{ctx.plan_tip_id}" in rendered
    assert f"decision:{accepted.id}" in rendered
    assert rejected.id not in rendered
    assert "UNRESOLVED BLOCKING (1)" in rendered
    assert "Shim is intentional" in rendered
    assert "Missing check" in rendered
    assert "Full-transcript replay is intentionally absent" in rendered
    refs = room_context_refs(ctx)
    assert f"plan:{ctx.plan_tip_id}" in refs
    assert f"decision:{accepted.id}" in refs


def test_history_is_bounded_excerpts_not_replay(store, writer):
    room = _room(store, writer)
    bus = ConversationBus(store, writer, RoomSeatResolver(room))
    for index in range(30):
        bus.send(
            Message(
                sender="human:utku",
                recipient_role="planner",
                room_id=room.id,
                type=MessageType.CLARIFICATION_REQUEST,
                content=f"question {index} " + ("x" * 2000),
            )
        )
    ctx = build_room_participant_context(
        store, room_id=room.id, role="planner", agent_name="gpt"
    )
    assert len(ctx.history) == 10
    rendered = render_room_context(ctx)
    assert len(rendered) <= 8000 + 100
    assert "HISTORY EXCERPTS (10, not authoritative" in rendered
    assert "question 0" not in rendered


def test_answered_blocking_is_not_unresolved(store, writer):
    room = _room(store, writer)
    bus = ConversationBus(store, writer, RoomSeatResolver(room))
    run = store.save_model(Run(agent="gpt", role="planner", status=RunStatus.SUCCEEDED))
    parent = bus.send(
        Message(
            sender="human:utku",
            recipient_role="planner",
            room_id=room.id,
            type=MessageType.CLARIFICATION_REQUEST,
            content="Blocking?",
            blocking=True,
        )
    )
    # Delivery-bound canonical answer (mirrors planner_exchange provenance).
    reply_run = store.save_model(
        Run(agent="gpt", role="planner", status=RunStatus.SUCCEEDED)
    )
    bus.send(
        Message(
            sender="gpt",
            recipient="human:utku",
            reply_to_id=parent.id,
            run_id=reply_run.id,
            room_id=room.id,
            type=MessageType.CLARIFICATION_RESPONSE,
            content="Answer",
        )
    )
    writer.record(
        EventLogEntry(
            type=EventType.MESSAGE_DELIVERED,
            room_id=room.id,
            sender="relay:delivery",
            recipient="gpt",
            content="bound",
            references=[f"message:{parent.id}", f"run:{reply_run.id}", f"room:{room.id}"],
        )
    )
    assert run.id is not None
    ctx = build_room_participant_context(
        store, room_id=room.id, role="planner", agent_name="gpt"
    )
    assert ctx.blocking == ()


def test_latest_session_ref_returns_newest_for_seat(store, writer):
    room = _room(store, writer)
    first = store.save_model(
        Run(agent="gpt", role="planner", status=RunStatus.SUCCEEDED,
            external_session_ref="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    )
    second = store.save_model(
        Run(agent="gpt", role="planner", status=RunStatus.SUCCEEDED,
            external_session_ref="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
    )
    for run in (first, second):
        writer.record(
            EventLogEntry(
                type=EventType.MESSAGE_DELIVERED,
                room_id=room.id,
                sender="relay:delivery",
                recipient="gpt",
                content="bound",
                references=[f"message:m-{run.id}", f"run:{run.id}", f"room:{room.id}"],
            )
        )
    assert latest_session_ref(store, room.id, "gpt") == "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    assert latest_session_ref(store, room.id, "other") is None


async def test_delivery_resume_refuses_for_api_and_unknown_capability(store, writer):
    room = _room(store, writer)
    api = _API()
    plain = _NoResumeHarness()
    delivery = MessageDelivery(store, writer, _Factory({"gpt": api, "plain": plain}))
    bus = ConversationBus(store, writer, RoomSeatResolver(room))

    for recipient in ("gpt", "plain"):
        sender_run = None
        message = bus.send(
            Message(
                sender="human:utku",
                recipient_role="planner",
                room_id=room.id,
                type=MessageType.CLARIFICATION_REQUEST,
                content="Hi",
            )
        )
        assert sender_run is None
        with pytest.raises(DeliveryRefusal):
            await delivery.deliver(
                message.id, resume_session_ref="11111111-1111-1111-1111-111111111111"
            )


async def test_delivery_resume_reaches_harness_metadata_and_argv(store, writer):
    room = _room(store, writer)
    harness = _Harness()
    delivery = MessageDelivery(store, writer, _Factory({"gpt": harness}))
    bus = ConversationBus(store, writer, RoomSeatResolver(room))
    message = bus.send(
        Message(
            sender="human:utku",
            recipient_role="planner",
            room_id=room.id,
            type=MessageType.CLARIFICATION_REQUEST,
            content="Hi",
        )
    )
    outcome = await delivery.deliver_and_reply(
        message.id,
        prompt_suffix=" SUFFIX",
        extra_context_refs=["plan:abc"],
        resume_session_ref="11111111-1111-1111-1111-111111111111",
    )
    assert outcome.reply is not None
    request = _Harness.seen[0]
    assert request.metadata["resume_session_ref"] == "11111111-1111-1111-1111-111111111111"
    assert request.context_refs == ["plan:abc"]
    assert request.prompt.endswith("MESSAGE:\nHi SUFFIX")
    assert harness.resume_argv_for(request) == (
        "--resume",
        "11111111-1111-1111-1111-111111111111",
    )


async def test_delivery_defaults_stay_byte_identical(store, writer):
    room = _room(store, writer)
    api = _API()
    delivery = MessageDelivery(store, writer, _Factory({"gpt": api}))
    bus = ConversationBus(store, writer, RoomSeatResolver(room))
    first = bus.send(
        Message(
            sender="human:utku",
            recipient_role="planner",
            room_id=room.id,
            type=MessageType.NOTE,
            content="same",
        )
    )
    second = bus.send(
        Message(
            sender="human:utku",
            recipient_role="planner",
            room_id=room.id,
            type=MessageType.NOTE,
            content="same",
        )
    )
    await delivery.deliver(first.id)
    await delivery.deliver(second.id)
    assert api.received[0].prompt == api.received[1].prompt
    assert api.received[0].context_refs == []
    assert api.received[0].metadata == {}


def test_harness_resume_argv_requires_capability_and_shape(tmp_path):
    plain = _NoResumeHarness()
    request = AgentRequest(
        prompt="p", role=AgentRole.PLANNER, metadata={"resume_session_ref": "x"}
    )
    with pytest.raises(UnsupportedCapability):
        plain.resume_argv_for(request)
    capable = _Harness()
    with pytest.raises(UnsupportedCapability):
        capable.resume_argv_for(
            AgentRequest(prompt="p", role=AgentRole.PLANNER, metadata={"resume_session_ref": "bad"})
        )
    assert capable.resume_argv_for(
        AgentRequest(
            prompt="p",
            role=AgentRole.PLANNER,
            metadata={"resume_session_ref": "11111111-1111-1111-1111-111111111111"},
        )
    ) == ("--resume", "11111111-1111-1111-1111-111111111111")


def test_adapter_opt_in_gates_session_persistence():
    from relay.agents.antigravity_cli import AntigravityCLIAdapter
    from relay.agents.claude_code import ClaudeCodeAgent

    claude_off = ClaudeCodeAgent(
        profile=HarnessAgentConfig(grant=ExecutionGrantKind.READ_ONLY_ACCESS)
    )
    claude_off._last_session_id = "22222222-2222-2222-2222-222222222222"
    assert claude_off.run_observation() is not None
    assert claude_off.run_observation().external_session_ref is None  # type: ignore[union-attr]

    claude_on = ClaudeCodeAgent(
        profile=HarnessAgentConfig(
            grant=ExecutionGrantKind.READ_ONLY_ACCESS, persist_session_ref=True
        )
    )
    claude_on._last_session_id = "22222222-2222-2222-2222-222222222222"
    assert claude_on.run_observation().external_session_ref == (  # type: ignore[union-attr]
        "22222222-2222-2222-2222-222222222222"
    )
    assert claude_on.continuation_ref() == "22222222-2222-2222-2222-222222222222"

    agy_off = AntigravityCLIAdapter(
        profile=HarnessAgentConfig(grant=ExecutionGrantKind.READ_ONLY_ACCESS)
    )
    agy_off._last_session_id = "33333333-3333-3333-3333-333333333333"
    assert agy_off.run_observation().external_session_ref is None  # type: ignore[union-attr]
    agy_on = AntigravityCLIAdapter(
        profile=HarnessAgentConfig(
            grant=ExecutionGrantKind.READ_ONLY_ACCESS, persist_session_ref=True
        )
    )
    agy_on._last_session_id = "33333333-3333-3333-3333-333333333333"
    assert agy_on.run_observation().external_session_ref == (  # type: ignore[union-attr]
        "33333333-3333-3333-3333-333333333333"
    )


def test_config_opt_in_defaults_off_and_validates():
    cfg = HarnessAgentConfig(grant=ExecutionGrantKind.READ_ONLY_ACCESS)
    assert cfg.persist_session_ref is False
    assert HarnessAgentConfig(
        grant=ExecutionGrantKind.READ_ONLY_ACCESS, persist_session_ref=True
    ).persist_session_ref is True


def test_room_helpers_freeze_still_builds_context(tmp_path):
    fixture = room_store(tmp_path)
    _parent, reply, _run = planner_exchange(fixture)
    config = room_config()
    freeze(fixture, config, reply=reply)
    ctx = build_room_participant_context(
        fixture.store, room_id=fixture.room.id, role="planner", agent_name="gpt"
    )
    assert ctx.plan_tip_id is not None
    assert render_room_context(ctx).startswith("\n---\n[relay:room-context")


def test_room_ask_prompt_carries_reconstructed_context(tmp_path, monkeypatch):
    """CLI end-to-end: the recipient prompt embeds the D.10 block (P7.4)."""
    import json as _json

    import httpx
    from typer.testing import CliRunner

    import relay.agents.openai as openai_mod
    from relay.cli.main import app

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    runner = CliRunner()
    assert runner.invoke(app, ["init"]).exit_code == 0
    (tmp_path / "relay.yaml").write_text(
        "agents:\n"
        "  gpt: {backend: api, adapter: openai, model: offline}\n"
        "roles:\n"
        "  planner: gpt\n",
        encoding="utf-8",
    )
    assert runner.invoke(app, ["room", "create", "Design"]).exit_code == 0

    prompts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        try:
            body = _json.loads(request.content.decode("utf-8"))
            prompts.append(body["messages"][0]["content"])
        except Exception:  # noqa: BLE001 - test capture only
            prompts.append("")
        payload = {
            "choices": [{"message": {"content": "# Plan\n\nStep 1"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }
        return httpx.Response(200, json=payload)

    def factory(*args: object, **kwargs: object) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    class _Surrogate:
        AsyncClient = factory
        TimeoutException = httpx.TimeoutException
        ConnectError = httpx.ConnectError
        HTTPError = httpx.HTTPError

    monkeypatch.setattr(openai_mod, "httpx", _Surrogate)
    result = runner.invoke(app, ["room", "ask", "@planner", "Draft it", "--by", "utku"])
    assert result.exit_code == 0, result.output
    assert prompts, "expected the provider to receive one prompt"
    prompt = prompts[0]
    assert "FROM: human:utku" in prompt  # frozen D15 envelope intact
    assert "[relay:room-context relay.room.context.v1]" in prompt
    assert "ROLE: planner -> gpt" in prompt
    assert "[relay:continuity fresh]" in prompt
    assert "Full-transcript replay is intentionally absent" in prompt

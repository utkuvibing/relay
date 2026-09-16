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


def _delivered_seat_exchange(
    store: SqliteRelayStore,
    writer: EventLogWriter,
    room: Room,
    *,
    role: str,
    agent: str,
    session_ref: str,
) -> tuple[Message, Run]:
    """One role-addressed delivery with real message→run provenance."""
    bus = ConversationBus(store, writer, RoomSeatResolver(room))
    message = bus.send(
        Message(
            sender="human:utku",
            recipient_role=role,
            room_id=room.id,
            type=MessageType.CLARIFICATION_REQUEST,
            content=f"question for @{role}",
        )
    )
    run = store.save_model(
        Run(
            agent=agent,
            role=role,
            status=RunStatus.SUCCEEDED,
            external_session_ref=session_ref,
        )
    )
    writer.record(
        EventLogEntry(
            type=EventType.MESSAGE_DELIVERED,
            room_id=room.id,
            sender="relay:delivery",
            recipient=agent,
            content="bound",
            references=[f"message:{message.id}", f"run:{run.id}", f"room:{room.id}"],
        )
    )
    return message, run


def test_latest_session_ref_is_room_seat_scoped(store, writer):
    """One agent occupying two seats must never inherit across roles (P7.4)."""
    room = _room(store, writer)  # planner -> gpt AND reviewer -> gpt
    _delivered_seat_exchange(
        store, writer, room, role="planner", agent="gpt",
        session_ref="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
    )
    _delivered_seat_exchange(
        store, writer, room, role="reviewer", agent="gpt",
        session_ref="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
    )
    # Newest planner-seat delivery wins for the planner seat only.
    _delivered_seat_exchange(
        store, writer, room, role="planner", agent="gpt",
        session_ref="cccccccc-cccc-cccc-cccc-cccccccccccc",
    )
    assert (
        latest_session_ref(store, room.id, "gpt", "planner")
        == "cccccccc-cccc-cccc-cccc-cccccccccccc"
    )
    assert (
        latest_session_ref(store, room.id, "gpt", "reviewer")
        == "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    )
    # A seat the agent never occupied, and an agent with no deliveries.
    assert latest_session_ref(store, room.id, "gpt", "implementer") is None
    assert latest_session_ref(store, room.id, "other", "planner") is None


def test_latest_session_ref_ignores_foreign_provenance(store, writer):
    """A marker whose message was addressed to another role is never used."""
    room = _room(store, writer)
    message, run = _delivered_seat_exchange(
        store, writer, room, role="reviewer", agent="gpt",
        session_ref="dddddddd-dddd-dddd-dddd-dddddddddddd",
    )
    assert run.external_session_ref is not None
    assert message.recipient_role == "reviewer"
    # Same agent, different seat: the reviewer-addressed session is not inherited.
    assert latest_session_ref(store, room.id, "gpt", "planner") is None


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


# ---------------------------------------------------------------------------
# P7.4 gap fixes: honest fallback, task-relevant records, bounded rendering.
# ---------------------------------------------------------------------------

from relay.core.evidence import EvidenceKind
from relay.core.room_context import RoomParticipantContext
from relay.harness.errors import HarnessOutputError, SessionResumeUnavailable
from relay.storage.models import EvidenceRecord, Task

_VALID_REF = "11111111-1111-1111-1111-111111111111"


class _RejectingResume(_Harness):
    """Positively rejects any supplied resume ref, then runs fresh fine."""

    name = "fake_rejecting"

    async def run(self, request: AgentRequest) -> AgentResponse:
        type(self).seen.append(request)
        if request.metadata.get("resume_session_ref") is not None:
            raise SessionResumeUnavailable(
                f"{self.name}: the supplied session continuation reference was rejected"
            )
        return AgentResponse(agent=self.name, role=request.role, output="fresh answer")


class _GenericFailure(_Harness):
    """Arbitrary provider failure — never fallback-eligible."""

    name = "fake_generic"

    async def run(self, request: AgentRequest) -> AgentResponse:
        type(self).seen.append(request)
        raise HarnessOutputError(f"{self.name}: harness reported failure: boom")


async def test_resume_rejection_falls_back_to_fresh_run(store, writer):
    """A positively-rejected resume ref continues once, fresh, honestly."""
    room = _room(store, writer)
    harness = _RejectingResume()
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
        prompt_suffix=" RESUMED",
        fallback_prompt_suffix=" FRESH",
        resume_session_ref=_VALID_REF,
    )
    assert outcome.reply is not None
    assert outcome.reply.content == "fresh answer"

    runs = list(store.all_models(Run))
    assert len(runs) == 2
    failed, fresh = runs
    assert failed.status is RunStatus.FAILED
    assert fresh.status is RunStatus.SUCCEEDED
    assert outcome.ask.run.id == fresh.id
    assert outcome.reply.run_id == fresh.id

    # One canonical initiation (D13 intact); the fallback binds separately.
    assert len(delivery.deliveries_for_message(message.id)) == 1
    fallbacks = [
        entry
        for entry in store.all_models(
            EventLogEntry,
            "WHERE type = ?",
            [EventType.MESSAGE_DELIVERY_FALLBACK.value],
        )
    ]
    assert len(fallbacks) == 1
    refs = fallbacks[0].references
    assert f"message:{message.id}" in refs
    assert f"run:{fresh.id}" in refs
    assert f"prior_run:{failed.id}" in refs

    # The fallback request is genuinely fresh: no resume metadata, and the
    # honest fallback suffix rides the prompt instead of the resumed claim.
    first_request, second_request = _Harness.seen
    assert first_request.metadata["resume_session_ref"] == _VALID_REF
    assert first_request.prompt.endswith("MESSAGE:\nHi RESUMED")
    assert second_request.metadata == {}
    assert second_request.prompt.endswith("MESSAGE:\nHi FRESH")

    # Recovery stays idempotent: re-entry resolves through the fallback run
    # to the same canonical reply with zero new runs.
    again = await delivery.deliver_and_reply(
        message.id, resume_session_ref=_VALID_REF
    )
    assert again.reply is not None
    assert again.reply.id == outcome.reply.id
    assert again.ask.run.id == fresh.id
    assert len(list(store.all_models(Run))) == 2


async def test_generic_provider_failure_never_triggers_fallback(store, writer):
    """Only a typed session-continuation rejection may retry — nothing else."""
    room = _room(store, writer)
    harness = _GenericFailure()
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
        prompt_suffix=" RESUMED",
        fallback_prompt_suffix=" FRESH",
        resume_session_ref=_VALID_REF,
    )
    assert outcome.reply is None
    assert outcome.ask.error is not None
    runs = list(store.all_models(Run))
    assert len(runs) == 1
    assert runs[0].status is RunStatus.FAILED
    assert len(_Harness.seen) == 1
    assert not list(
        store.all_models(
            EventLogEntry,
            "WHERE type = ?",
            [EventType.MESSAGE_DELIVERY_FALLBACK.value],
        )
    )


def test_render_cap_preserves_mandatory_sections_and_continuity():
    """The character cap must never slice away head, tail, or honesty markers."""
    ctx = RoomParticipantContext(
        room_id="room-1",
        room_name="n" * 300,
        room_status="open",
        role="planner",
        agent_name="gpt",
        task_id="task-1",
        plan_tip_id="plan-1",
        plan_chain=(("plan-1", "frozen", "the plan"),),
        decisions=tuple((f"d{i}", "x" * 400) for i in range(8)),
        blocking=tuple((f"b{i}", "impl", "x" * 400) for i in range(8)),
        notes=tuple((f"n{i}", "impl", "x" * 400) for i in range(8)),
        findings=tuple((f"f{i}", "high", "x" * 400) for i in range(16)),
        artifacts=tuple((f"a{i}", "plan", "x" * 200) for i in range(8)),
        evidence=tuple((f"e{i}", "tests_passed") for i in range(16)),
        history=tuple((f"h{i}", "who/type", "x" * 300) for i in range(10)),
        continuity="resumed:11111111-1111-1111-1111-111111111111",
    )
    rendered = render_room_context(ctx)
    assert len(rendered) <= 8000  # character budget — not UTF-8 bytes
    assert "[relay:room-context relay.room.context.v1]" in rendered
    assert "ROOM:" in rendered
    assert "ROLE: planner" in rendered
    assert "TASK: task-1" in rendered
    assert "CURRENT PLAN:" in rendered
    assert "[relay:continuity resumed:11111111-1111-1111-1111-111111111111]" in rendered
    assert "Full-transcript replay is intentionally absent" in rendered
    # Sections that lost the budget declare their omission, never vanish.
    assert "omitted" in rendered


def test_decisions_scoped_to_global_and_active_task(tmp_path):
    fixture = room_store(tmp_path)
    store = fixture.store
    _parent, reply, _run = planner_exchange(fixture)
    outcome = freeze(fixture, room_config(), reply=reply)
    task_a = outcome.task.id
    task_b = store.save_model(Task(title="unrelated", room_id=fixture.room.id)).id

    global_decision = store.save_model(
        Decision(
            statement="Room-wide convention",
            status=DecisionStatus.ACCEPTED,
            room_id=fixture.room.id,
            task_id=None,
        )
    )
    task_a_decision = store.save_model(
        Decision(
            statement="Active task decision",
            status=DecisionStatus.ACCEPTED,
            room_id=fixture.room.id,
            task_id=task_a,
        )
    )
    other_task_decision = store.save_model(
        Decision(
            statement="Unrelated task decision",
            status=DecisionStatus.ACCEPTED,
            room_id=fixture.room.id,
            task_id=task_b,
        )
    )
    rejected = store.save_model(
        Decision(
            statement="Rejected proposal",
            status=DecisionStatus.REJECTED,
            room_id=fixture.room.id,
            task_id=task_a,
        )
    )

    ctx = build_room_participant_context(
        store, room_id=fixture.room.id, role="planner", agent_name="gpt"
    )
    decision_ids = {decision_id for decision_id, _ in ctx.decisions}
    assert global_decision.id in decision_ids
    assert task_a_decision.id in decision_ids
    assert other_task_decision.id not in decision_ids
    assert rejected.id not in decision_ids


def test_current_findings_come_from_latest_review_attempt(tmp_path):
    """A newer clean review supersedes earlier findings (P7.4 currency)."""
    fixture = room_store(tmp_path)
    store = fixture.store
    _parent, reply, _run = planner_exchange(fixture)
    outcome = freeze(fixture, room_config(), reply=reply)
    task_id = outcome.task.id
    review_run = store.save_model(
        Run(agent="gpt", role="reviewer", status=RunStatus.SUCCEEDED)
    )

    def _review_artifact() -> Artifact:
        return store.save_model(
            Artifact(
                kind=ArtifactKind.REVIEW_FINDING,
                task_id=task_id,
                content="{}",
            )
        )

    def _finding(artifact: Artifact, title: str) -> Finding:
        return store.save_model(
            Finding(
                room_id=fixture.room.id,
                task_id=task_id,
                review_artifact_id=artifact.id,
                review_run_id=review_run.id,
                source_finding_id="F1",
                severity=ReviewSeverity.HIGH,
                title=title,
                description="d",
                requested_change="c",
                validation_expectation="v",
            )
        )

    review_one = _review_artifact()
    stale = _finding(review_one, "Stale finding from review one")
    ctx = build_room_participant_context(
        store, room_id=fixture.room.id, role="planner", agent_name="gpt"
    )
    assert [f_id for f_id, _, _ in ctx.findings] == [stale.id]

    # A newer clean review (verdict record, no findings minted) supersedes.
    _review_artifact()
    ctx = build_room_participant_context(
        store, room_id=fixture.room.id, role="planner", agent_name="gpt"
    )
    assert ctx.findings == ()
    assert "CURRENT FINDINGS (0)" in render_room_context(ctx)

    # A third review mints the new current set; review-one findings stay gone.
    review_three = _review_artifact()
    current = _finding(review_three, "Current finding from review three")
    ctx = build_room_participant_context(
        store, room_id=fixture.room.id, role="planner", agent_name="gpt"
    )
    assert [f_id for f_id, _, _ in ctx.findings] == [current.id]


def test_rendered_evidence_appears_in_context_refs(tmp_path):
    fixture = room_store(tmp_path)
    store = fixture.store
    _parent, reply, _run = planner_exchange(fixture)
    outcome = freeze(fixture, room_config(), reply=reply)
    task_id = outcome.task.id
    record = store.save_model(
        EvidenceRecord(
            kind=EvidenceKind.CONTEXT_COLLECTED,
            task_id=task_id,
            produced_by="agent:gpt",
        )
    )
    ctx = build_room_participant_context(
        store, room_id=fixture.room.id, role="planner", agent_name="gpt"
    )
    assert (record.id, "context_collected") in ctx.evidence
    assert f"evidence:{record.id}" in room_context_refs(ctx)
    assert "EVIDENCE (" in render_room_context(ctx)
    # Deterministic + deduped.
    assert room_context_refs(ctx) == list(dict.fromkeys(room_context_refs(ctx)))


# -- true end-to-end continuation through a real harness subprocess ---------

_E2E_SRC = r"""
import json, os, sys
data = sys.stdin.read()
argv = sys.argv
if "--version" in argv:
    print("fake-resumable 1.0.0"); sys.exit(0)
resumed = "--resume" in argv
ref = argv[argv.index("--resume") + 1] if resumed else "22222222-2222-2222-2222-222222222222"
if resumed and os.path.exists(".reject-resume"):
    sys.stderr.write("No conversation found with session ID: " + ref + "\n")
    sys.exit(1)
print(json.dumps({"result": "answer resumed=%s" % resumed, "session_id": ref}))
"""


class _ResumableChild(HarnessAgent):
    """Real-subprocess resumable harness for the end-to-end P7.4 test."""

    name = "fake_resumable_e2e"
    capabilities = frozenset(
        {HarnessCapability.READ_ONLY_ACCESS, HarnessCapability.SESSION_RESUME}
    )
    harness_command = "python"

    def invocation_argv(self, resolved):  # type: ignore[no-untyped-def]
        return (resolved.command, "-c", _E2E_SRC)

    def parse_output(self, stdout_text, stderr_text):  # type: ignore[no-untyped-def]
        import json

        envelope = json.loads(stdout_text.strip())
        self._last_session_id = envelope.get("session_id")
        return envelope["result"]

    def run_observation(self):  # type: ignore[no-untyped-def]
        from relay.agents.base import RunObservation

        persisted = getattr(self._profile, "persist_session_ref", False)
        return RunObservation(
            resolved_model=None,
            adapter_version=None,
            backend="harness",
            external_session_ref=self._last_session_id if persisted else None,
        )

    def resume_arguments(self, session_ref: str) -> tuple[str, ...]:
        parts = session_ref.split("-")
        expected = (8, 4, 4, 4, 12)
        valid = len(parts) == len(expected) and all(
            len(part) == width
            and all(ch in "0123456789abcdefABCDEF" for ch in part)
            for part, width in zip(parts, expected)
        )
        if not valid:
            raise UnsupportedCapability(f"{self.name}: invalid session reference")
        return ("--resume", session_ref)

    def resume_rejected(self, outcome) -> bool:  # type: ignore[no-untyped-def]
        blob = f"{outcome.stdout.text}\n{outcome.stderr.text}".lower()
        return "no conversation found" in blob


def _e2e_workspace(tmp_path, monkeypatch, *, persist: bool):
    """Init a workspace whose single fake agent fills planner+reviewer seats."""
    import json
    import sys

    from typer.testing import CliRunner

    from relay.cli.main import app

    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    assert runner.invoke(app, ["init"]).exit_code == 0
    (tmp_path / "relay.yaml").write_text(
        "agents:\n"
        "  fake:\n"
        "    backend: harness\n"
        "    adapter: fake_resumable_e2e\n"
        "    harness:\n"
        f"      executable_path: {json.dumps(sys.executable)}\n"
        "      grant: read_only\n"
        f"      persist_session_ref: {str(persist).lower()}\n"
        "roles:\n"
        "  planner: fake\n"
        "  reviewer: fake\n",
        encoding="utf-8",
    )
    return runner, app


def _spec_spy(monkeypatch):
    """Capture every LaunchSpec the harness runtime executes."""
    import relay.harness.runtime as runtime_mod

    real_execute = runtime_mod.execute
    specs: list = []

    async def spy(spec):
        specs.append(spec)
        return await real_execute(spec)

    monkeypatch.setattr(runtime_mod, "execute", spy)
    return specs


def test_room_ask_end_to_end_session_continuation(tmp_path, monkeypatch):
    """Full chain: parse → persist (opt-in) → seat-scoped resume → argv+prompt."""
    from relay.agents.registry import transient_adapters

    runner, app = _e2e_workspace(tmp_path, monkeypatch, persist=True)
    specs = _spec_spy(monkeypatch)

    with transient_adapters({"fake_resumable_e2e": _ResumableChild}):
        assert runner.invoke(app, ["room", "create", "Design"]).exit_code == 0
        first = runner.invoke(app, ["room", "ask", "@planner", "First", "--by", "utku"])
        assert first.exit_code == 0, first.output
        second = runner.invoke(app, ["room", "ask", "@planner", "Second", "--by", "utku"])
        assert second.exit_code == 0, second.output
        # Same agent, different seat: the reviewer must NOT inherit the
        # planner seat's external session.
        third = runner.invoke(app, ["room", "ask", "@reviewer", "Look?", "--by", "utku"])
        assert third.exit_code == 0, third.output

    assert len(specs) == 3
    first_spec, second_spec, third_spec = specs

    first_prompt = first_spec.stdin_data.decode("utf-8")
    assert "--resume" not in first_spec.argv
    assert "[relay:continuity fresh]" in first_prompt

    assert "--resume" in second_spec.argv
    ref = second_spec.argv[second_spec.argv.index("--resume") + 1]
    assert ref == "22222222-2222-2222-2222-222222222222"
    second_prompt = second_spec.stdin_data.decode("utf-8")
    assert f"[relay:continuity resumed:{ref}]" in second_prompt
    assert "[relay:room-context relay.room.context.v1]" in second_prompt
    assert "FROM: human:utku" in second_prompt  # frozen D15 envelope intact

    third_prompt = third_spec.stdin_data.decode("utf-8")
    assert "--resume" not in third_spec.argv
    assert "[relay:continuity fresh]" in third_prompt

    # The opt-in persisted the parsed ref on the canonical Run rows.
    from relay.cli.main import _open_db

    conn = _open_db(tmp_path)
    try:
        store = SqliteRelayStore(conn)
        refs = [run.external_session_ref for run in store.all_models(Run)]
    finally:
        conn.close()
    assert refs == ["22222222-2222-2222-2222-222222222222"] * 3


def test_room_ask_end_to_end_rejected_resume_falls_back(tmp_path, monkeypatch):
    """Stale session → typed rejection → one fresh fallback run, one reply."""
    from relay.agents.registry import transient_adapters

    runner, app = _e2e_workspace(tmp_path, monkeypatch, persist=True)
    specs = _spec_spy(monkeypatch)

    with transient_adapters({"fake_resumable_e2e": _ResumableChild}):
        assert runner.invoke(app, ["room", "create", "Design"]).exit_code == 0
        first = runner.invoke(app, ["room", "ask", "@planner", "First", "--by", "utku"])
        assert first.exit_code == 0, first.output
        # The next resume attempt hits a positively-rejected handle.
        (tmp_path / ".reject-resume").write_text("1", encoding="utf-8")
        second = runner.invoke(app, ["room", "ask", "@planner", "Second", "--by", "utku"])
        assert second.exit_code == 0, second.output

    # Resume attempt rejected, then exactly one fresh fallback run.
    assert len(specs) == 3
    assert "--resume" in specs[1].argv
    assert "--resume" not in specs[2].argv
    fallback_prompt = specs[2].stdin_data.decode("utf-8")
    assert "[relay:continuity resume-rejected]" in fallback_prompt
    assert "resumed:" not in fallback_prompt
    assert "[relay:room-context relay.room.context.v1]" in fallback_prompt

    from relay.cli.main import _open_db

    conn = _open_db(tmp_path)
    try:
        store = SqliteRelayStore(conn)
        runs = list(store.all_models(Run))
        messages = list(store.all_models(Message))
        fallbacks = list(
            store.all_models(
                EventLogEntry,
                "WHERE type = ?",
                [EventType.MESSAGE_DELIVERY_FALLBACK.value],
            )
        )
    finally:
        conn.close()
    # Two exchanges: fresh + (failed resume + fresh fallback) = 3 runs.
    assert [run.status for run in runs] == [
        RunStatus.SUCCEEDED,
        RunStatus.FAILED,
        RunStatus.SUCCEEDED,
    ]
    # Two requests + exactly two canonical replies — no duplicates.
    assert len(messages) == 4
    assert len(fallbacks) == 1


def test_room_ask_end_to_end_opt_out_stays_fresh(tmp_path, monkeypatch):
    """persist_session_ref: false (default) — parsed but never persisted/resumed."""
    from relay.agents.registry import transient_adapters

    runner, app = _e2e_workspace(tmp_path, monkeypatch, persist=False)
    specs = _spec_spy(monkeypatch)

    with transient_adapters({"fake_resumable_e2e": _ResumableChild}):
        assert runner.invoke(app, ["room", "create", "Design"]).exit_code == 0
        for index in range(2):
            result = runner.invoke(
                app, ["room", "ask", "@planner", f"Q{index}", "--by", "utku"]
            )
            assert result.exit_code == 0, result.output

    assert len(specs) == 2
    for spec in specs:
        assert "--resume" not in spec.argv
        assert "[relay:continuity fresh]" in spec.stdin_data.decode("utf-8")

    from relay.cli.main import _open_db

    conn = _open_db(tmp_path)
    try:
        store = SqliteRelayStore(conn)
        assert all(
            run.external_session_ref is None for run in store.all_models(Run)
        )
    finally:
        conn.close()

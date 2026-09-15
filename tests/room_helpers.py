"""P7.3 test helpers: Room-bound canonical graph scenarios.

Not a test module (no ``test_`` prefix) — shared construction for the P7.3
suites: a workspace with a persisted Room, a planner discussion exchange with
honest delivery provenance, and a Room-aware Relay config.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from relay.agents.base import AgentRole
from relay.context.config import (
    AgentConfig,
    BackendType,
    HarnessAgentConfig,
    RelayConfig,
)
from relay.core.bus import ConversationBus
from relay.core.rooms import RoomLifecycle, RoomSeatResolver
from relay.harness.types import ExecutionGrantKind
from relay.storage.db import connect, migrate
from relay.storage.events import EventLogWriter
from relay.storage.models import (
    EventLogEntry,
    EventType,
    Message,
    MessageType,
    Room,
    Run,
    RunStatus,
    Workspace,
)
from relay.storage.store import SqliteRelayStore


@dataclass
class RoomFixture:
    """One initialized store plus its Room and delivery provenance."""

    conn: object
    store: SqliteRelayStore
    writer: EventLogWriter
    workspace: Workspace
    room: Room


def room_store(tmp_path: Path, *, seats: dict[str, str] | None = None) -> RoomFixture:
    conn = connect(tmp_path / "rooms.db")
    migrate(conn)
    store = SqliteRelayStore(conn)
    writer = EventLogWriter(conn)
    workspace = store.save_model(Workspace(id="workspace", name="demo", path=str(tmp_path)))
    lifecycle = RoomLifecycle(store, writer)
    bindings = seats if seats is not None else {"planner": "gpt", "implementer": "impl"}
    room = lifecycle.create(
        workspace,
        "Design",
        bindings,
        {*bindings.values(), "gpt", "impl", "other"},
    )
    workspace = store.load_model(Workspace, workspace.id)
    return RoomFixture(
        conn=conn, store=store, writer=writer, workspace=workspace, room=room
    )


def room_config(
    *,
    planner: str = "gpt",
    implementer: str = "impl",
    implementer_grant: ExecutionGrantKind | None = ExecutionGrantKind.WORKSPACE_WRITE,
    extra_agents: dict[str, AgentConfig] | None = None,
) -> RelayConfig:
    agents = {
        "gpt": AgentConfig(backend=BackendType.API, adapter="openai", model="offline"),
        "impl": AgentConfig(
            backend=BackendType.HARNESS,
            adapter="claude_code",
            model="offline",
            harness=HarnessAgentConfig(
                grant=implementer_grant,
                executable_path="python",
                timeout_seconds=30,
            )
            if implementer_grant is not None
            else None,
        ),
        "other": AgentConfig(backend=BackendType.API, adapter="openai", model="offline"),
    }
    if extra_agents:
        agents.update(extra_agents)
    return RelayConfig(
        agents=agents,
        roles={"planner": planner, "implementer": implementer},
        reviewer="gpt",
    )


def planner_exchange(
    fixture: RoomFixture,
    *,
    content: str = "# Plan\n\nStep 1: do the thing",
    request: str = "Draft the implementation plan",
    agent: str = "gpt",
    sender: str = "human:utku",
    room: Room | None = None,
) -> tuple[Message, Message, Run]:
    """A canonical planner discussion reply with delivery provenance.

    Mirrors the persisted shape of ``relay room ask``: a role-addressed human
    request, the planner's ``clarification_response`` reply authored by a
    successful planner run, and the ``MESSAGE_DELIVERED`` marker binding the
    parent request to that run.
    """
    store, writer = fixture.store, fixture.writer
    target = room if room is not None else fixture.room
    target_room = target.id
    bus = ConversationBus(store, writer, RoomSeatResolver(target))
    parent = bus.send(
        Message(
            sender=sender,
            recipient_role=AgentRole.PLANNER.value,
            room_id=target_room,
            task_id=None,
            type=MessageType.CLARIFICATION_REQUEST,
            content=request,
            blocking=False,
        )
    )
    run = store.save_model(
        Run(
            agent=agent,
            role=AgentRole.PLANNER.value,
            status=RunStatus.SUCCEEDED,
            ended_at=None,
        )
    )
    reply = bus.send(
        Message(
            sender=agent,
            recipient=parent.sender,
            reply_to_id=parent.id,
            run_id=run.id,
            room_id=target_room,
            task_id=None,
            type=MessageType.CLARIFICATION_RESPONSE,
            content=content,
            blocking=False,
        )
    )
    writer.record(
        EventLogEntry(
            type=EventType.MESSAGE_DELIVERED,
            room_id=target_room,
            sender="relay:delivery",
            recipient=agent,
            content=f"clarification_request from {sender} to {agent} bound to run {run.id}",
            references=[
                f"message:{parent.id}",
                f"run:{run.id}",
                f"room:{target_room}",
            ],
        )
    )
    return parent, reply, run


def pin_baseline(store: SqliteRelayStore, root: Path, task_id: str) -> None:
    """The durable baseline pin a real first dispatch would have written."""
    from relay.core.baseline import capture_baseline, persist_baseline
    from relay.core.reviews import canonical_json
    from relay.storage.models import Artifact, ArtifactKind

    pin = persist_baseline(root, task_id, capture_baseline(root))
    store.save_model(
        Artifact(kind=ArtifactKind.REPORT, task_id=task_id, content=canonical_json(pin))
    )


def freeze(
    fixture: RoomFixture,
    config: RelayConfig,
    *,
    reply: Message | None = None,
    workspace_root: Path | None = None,
    supersedes: str | None = None,
    frozen_by: str = "human:utku",
    title: str | None = None,
):
    """Freeze a (fresh) planner reply into canonical Room state."""
    from relay.core.room_freeze import freeze_room_plan
    from relay.storage.store import SqliteEvidenceStore

    if reply is None:
        _parent, reply, _run = planner_exchange(fixture)
    return freeze_room_plan(
        fixture.store,
        fixture.writer,
        SqliteEvidenceStore(fixture.store),
        config,
        fixture.room,
        source_message_id=reply.id,
        frozen_by=frozen_by,
        workspace_root=workspace_root or Path("."),
        title=title,
        supersedes_plan_artifact_id=supersedes,
    )

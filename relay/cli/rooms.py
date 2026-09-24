"""CLI surface for persistent Room lifecycle and targeted exchange (P7.1-P7.4)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from relay.agents.base import AgentRole
from relay.agents.config import CliOverrides, resolve_settings
from relay.agents.factory import RegistryAgentFactory
from relay.context import ConfigError, identity_key, load_config
from relay.core.bus import ConversationBus, MessageRejected
from relay.core.delivery import DeliveryRefusal, MessageDelivery
from relay.core.room_feed import RoomFeedIntegrityError, build_room_feed
from relay.core.rooms import (
    ClosedRoomError,
    RoomError,
    RoomLifecycle,
    RoomLookupError,
    RoomSeatResolver,
    require_open_room,
)
from relay.storage.events import EventLogWriter
from relay.storage.models import Message, MessageType, Room, Workspace
from relay.storage.store import SqliteRelayStore

room_app = typer.Typer(help="Create and manage persistent Relay Rooms.", no_args_is_help=True)


def _workspace(store: SqliteRelayStore, root: Path) -> Workspace:
    workspace = store.workspace_for_identity(identity_key(root))
    if workspace is None:
        raise ConfigError("workspace not initialized - run 'relay init' first")
    return workspace


def _render_room(room: Room, *, current: bool = False) -> None:
    marker = " current" if current else ""
    typer.echo(f"Room {room.id} - {room.name} [{room.status.value}{marker}]")
    for member in sorted(room.members, key=lambda item: item.role):
        typer.echo(f"  {member.role} -> {member.agent}")


def _human_sender(human_id: str) -> str:
    if not human_id or ":" in human_id or any(character.isspace() for character in human_id):
        raise RoomError("--by must be a non-empty, whitespace-free, colon-free human identity")
    return f"human:{human_id}"


def _target_role(target: str) -> str:
    if not target.startswith("@") or len(target) == 1 or "@" in target[1:]:
        raise RoomError("target must be one Room role in @role form")
    return target[1:]


@room_app.command("create")
def create_room(name: str = typer.Argument(..., help="Workspace-local Room name.")) -> None:
    """Create an open Room from configured role bindings."""
    from relay.cli.main import _open_db

    root = Path.cwd()
    conn = None
    try:
        config = load_config(root)
        conn = _open_db(root)
        store = SqliteRelayStore(conn)
        workspace = _workspace(store, root)
        room = RoomLifecycle(store, EventLogWriter(conn)).create(
            workspace, name, config.roles, config.agents.keys()
        )
    except (ConfigError, RoomError) as exc:
        typer.echo(f"ERROR {exc}")
        raise typer.Exit(1) from exc
    finally:
        if conn is not None:
            conn.close()
    _render_room(room, current=True)


@room_app.command("list")
def list_rooms() -> None:
    """List every Room belonging to the current workspace."""
    from relay.cli.main import _open_db

    root = Path.cwd()
    conn = None
    try:
        conn = _open_db(root)
        store = SqliteRelayStore(conn)
        workspace = _workspace(store, root)
        rooms = RoomLifecycle(store, EventLogWriter(conn)).list_for_workspace(workspace.id)
    except ConfigError as exc:
        typer.echo(f"ERROR {exc}")
        raise typer.Exit(1) from exc
    finally:
        if conn is not None:
            conn.close()

    table = Table(title="Relay Rooms")
    table.add_column("Current")
    table.add_column("Room")
    table.add_column("Name")
    table.add_column("Status")
    table.add_column("Seats")
    for room in rooms:
        table.add_row(
            "*" if workspace.active_room_id == room.id else "",
            room.id,
            room.name,
            room.status.value,
            str(len(room.members)),
        )
    Console().print(table)


@room_app.command("resume")
def resume_room(selector: str = typer.Argument(..., help="Room name, ID, or unique ID prefix.")) -> None:
    """Reopen/select a Room and render its canonical persisted feed."""
    from relay.cli.main import _open_db

    root = Path.cwd()
    conn = None
    try:
        conn = _open_db(root)
        store = SqliteRelayStore(conn)
        workspace = _workspace(store, root)
        lifecycle = RoomLifecycle(store, EventLogWriter(conn))
        room = lifecycle.resume(workspace, lifecycle.resolve(workspace.id, selector))
        feed = build_room_feed(store, room.id)
    except (ConfigError, RoomError, RoomFeedIntegrityError) as exc:
        typer.echo(f"ERROR {exc}")
        raise typer.Exit(1) from exc
    finally:
        if conn is not None:
            conn.close()

    _render_room(room, current=True)
    typer.echo("Feed:")
    for entry in feed:
        sender = f" {entry.sender}" if entry.sender else ""
        typer.echo(f"  #{entry.sequence} {entry.kind}{sender}: {entry.text}")


@room_app.command("close")
def close_room(selector: str = typer.Argument(..., help="Room name, ID, or unique ID prefix.")) -> None:
    """Close a Room without deleting its history or changing its tasks."""
    from relay.cli.main import _open_db

    root = Path.cwd()
    conn = None
    try:
        conn = _open_db(root)
        store = SqliteRelayStore(conn)
        workspace = _workspace(store, root)
        lifecycle = RoomLifecycle(store, EventLogWriter(conn))
        room = lifecycle.close(workspace, lifecycle.resolve(workspace.id, selector))
    except (ConfigError, RoomError) as exc:
        typer.echo(f"ERROR {exc}")
        raise typer.Exit(1) from exc
    finally:
        if conn is not None:
            conn.close()
    _render_room(room)


@room_app.command("bind")
def bind_room(
    selector: str = typer.Argument(..., help="Room name, ID, or unique ID prefix."),
    role: str = typer.Argument(..., help="Existing AgentRole value."),
    agent: str = typer.Argument(..., help="Configured logical-agent name."),
) -> None:
    """Create or rebind one stable role seat."""
    from relay.cli.main import _open_db

    root = Path.cwd()
    conn = None
    try:
        config = load_config(root)
        conn = _open_db(root)
        store = SqliteRelayStore(conn)
        workspace = _workspace(store, root)
        lifecycle = RoomLifecycle(store, EventLogWriter(conn))
        room = lifecycle.bind(
            lifecycle.resolve(workspace.id, selector), role, agent, config.agents.keys()
        )
    except (ConfigError, RoomError) as exc:
        typer.echo(f"ERROR {exc}")
        raise typer.Exit(1) from exc
    finally:
        if conn is not None:
            conn.close()
    _render_room(room, current=workspace.active_room_id == room.id)


@room_app.command("ask")
def ask_room(
    target: str = typer.Argument(..., help="Persisted Room role in @role form."),
    prompt: str = typer.Argument(..., help="Question for the addressed Room seat."),
    by: str = typer.Option(
        ...,
        "--by",
        help="Bare human identity recorded as human:<id> provenance.",
    ),
    room_selector: str | None = typer.Option(
        None,
        "--room",
        help="Room name, ID, or unique ID prefix; defaults to the active Room.",
    ),
) -> None:
    """Ask one persisted Room role and save the canonical request/reply pair."""
    from relay.cli.main import _open_db

    root = Path.cwd()
    conn = None
    delivery_started = False
    try:
        sender = _human_sender(by)
        role = _target_role(target)
        config = load_config(root)
        conn = _open_db(root)
        store = SqliteRelayStore(conn)
        writer = EventLogWriter(conn)
        workspace = _workspace(store, root)
        lifecycle = RoomLifecycle(store, writer)
        if room_selector is None:
            if workspace.active_room_id is None:
                raise RoomLookupError("no active Room - pass --room <selector>")
            room = store.load_model(Room, workspace.active_room_id)
            if room is None or room.workspace_id != workspace.id:
                raise RoomLookupError("the workspace's active Room does not exist")
        else:
            room = lifecycle.resolve(workspace.id, room_selector)
        room = require_open_room(store, room.id)

        resolver = RoomSeatResolver(room)
        agent_name = resolver.resolve_role(role)
        if agent_name is None:
            raise RoomError(f"Room '{room.name}' has no @{role} seat")
        if agent_name not in config.agents:
            raise RoomError(
                f"Room seat @{role} references unknown agent '{agent_name}' - "
                "add it under agents: in relay.yaml or rebind the seat"
            )

        factory = RegistryAgentFactory(config, root)
        bus = ConversationBus(store, writer, resolver)
        delivery = MessageDelivery(store, writer, factory, bus)
        delivery.prepare_recipient(agent_name)
        request = bus.send(
            Message(
                sender=sender,
                recipient_role=role,
                room_id=room.id,
                task_id=None,
                type=MessageType.CLARIFICATION_REQUEST,
                content=prompt,
                blocking=False,
            )
        )
        context_suffix, fallback_suffix, extra_refs, resume_ref = (
            _room_participant_delivery(store, room, role, agent_name, delivery)
        )
        try:
            outcome = asyncio.run(
                delivery.deliver_and_reply(
                    request.id,
                    prompt_suffix=context_suffix,
                    fallback_prompt_suffix=fallback_suffix,
                    extra_context_refs=extra_refs,
                    resume_session_ref=resume_ref,
                )
            )
        except ClosedRoomError:
            delivery_started = bool(delivery.deliveries_for_message(request.id))
            raise
        if outcome.ask.error is not None:
            typer.echo(f"ERROR incomplete Room exchange: {outcome.ask.error}")
            raise typer.Exit(1)
        if outcome.reply is None:
            typer.echo("ERROR incomplete Room exchange: no canonical reply was materialized")
            raise typer.Exit(1)
    except ClosedRoomError as exc:
        label = "incomplete Room exchange" if delivery_started else "Room exchange refused"
        typer.echo(f"ERROR {label}: {exc}")
        raise typer.Exit(1) from exc
    except (ConfigError, RoomError, MessageRejected, DeliveryRefusal) as exc:
        typer.echo(f"ERROR {exc}")
        raise typer.Exit(1) from exc
    finally:
        if conn is not None:
            conn.close()

    typer.echo(f"Room {room.id} - {room.name}")
    typer.echo(f"Seat @{role} -> {agent_name}")
    typer.echo(f"{request.sender}: {request.content}")
    typer.echo(f"{outcome.reply.sender}: {outcome.reply.content}")


@room_app.command("decide")
def decide_room(
    target: str = typer.Argument(..., help="Persisted Room role in @role form (must be @planner)."),
    question: str = typer.Argument(..., help="Consequential question for the decision-maker seat."),
    by: str = typer.Option(
        ...,
        "--by",
        help="Bare human identity recorded as human:<id> provenance.",
    ),
    room_selector: str | None = typer.Option(
        None,
        "--room",
        help="Room name, ID, or unique ID prefix; defaults to the active Room.",
    ),
) -> None:
    """Ask the planner seat for a canonical Room decision (P7.3).

    Sends a PROPOSAL and asks for the strict ``relay.room_decision.v1`` reply
    contract; a valid answer is promoted into canonical Room state by Relay.
    Ordinary ``relay room ask`` discussion is untouched — this is the explicit
    consequential surface, and its output can never be frozen as a plan.
    """
    from relay.cli.main import _open_db
    from relay.core.room_records import (
        ROOM_DECISION_CONTRACT,
        RoomRecordRefusal,
        promote_room_decision,
    )

    root = Path.cwd()
    conn = None
    decision = None
    try:
        sender = _human_sender(by)
        role = _target_role(target)
        if role != AgentRole.PLANNER.value:
            raise RoomError("room decide addresses the planner seat only (@planner)")
        config = load_config(root)
        conn = _open_db(root)
        store = SqliteRelayStore(conn)
        writer = EventLogWriter(conn)
        workspace = _workspace(store, root)
        room = _select_room(store, workspace, room_selector)
        room = require_open_room(store, room.id)
        resolver = RoomSeatResolver(room)
        agent_name = resolver.resolve_role(role)
        if agent_name is None:
            raise RoomError(f"Room '{room.name}' has no @{role} seat")
        if agent_name not in config.agents:
            raise RoomError(
                f"Room seat @{role} references unknown agent '{agent_name}' - "
                "add it under agents: in relay.yaml or rebind the seat"
            )

        factory = RegistryAgentFactory(config, root)
        bus = ConversationBus(store, writer, resolver)
        delivery = MessageDelivery(store, writer, factory, bus)
        delivery.prepare_recipient(agent_name)
        request = bus.send(
            Message(
                sender=sender,
                recipient_role=role,
                room_id=room.id,
                task_id=None,
                type=MessageType.PROPOSAL,
                content=question,
                blocking=False,
            )
        )
        context_suffix, fallback_suffix, extra_refs, resume_ref = (
            _room_participant_delivery(store, room, role, agent_name, delivery)
        )
        outcome = asyncio.run(
            delivery.deliver_and_reply(
                request.id,
                reply_type=MessageType.FINAL_POSITION,
                prompt_suffix=context_suffix + ROOM_DECISION_CONTRACT,
                fallback_prompt_suffix=fallback_suffix + ROOM_DECISION_CONTRACT,
                extra_context_refs=extra_refs,
                resume_session_ref=resume_ref,
            )
        )
        if outcome.ask.error is not None:
            typer.echo(f"ERROR incomplete Room exchange: {outcome.ask.error}")
            raise typer.Exit(1)
        if outcome.reply is None:
            typer.echo("ERROR incomplete Room exchange: no canonical reply was materialized")
            raise typer.Exit(1)
        decision = promote_room_decision(store, writer, room, request, outcome.reply)
    except ClosedRoomError as exc:
        typer.echo(f"ERROR Room exchange refused: {exc}")
        raise typer.Exit(1) from exc
    except (ConfigError, RoomError, RoomRecordRefusal, MessageRejected, DeliveryRefusal) as exc:
        typer.echo(f"ERROR {exc}")
        raise typer.Exit(1) from exc
    finally:
        if conn is not None:
            conn.close()

    typer.echo(f"Room {room.id} - {room.name}")
    typer.echo(f"Seat @{role} -> {agent_name}")
    typer.echo(f"{request.sender}: {request.content}")
    typer.echo(f"{outcome.reply.sender}: {outcome.reply.content}")
    if decision is None:
        typer.echo(
            "ERROR no canonical decision promoted: the reply did not carry a valid "
            "relay.room_decision.v1 object"
        )
        raise typer.Exit(1)
    typer.echo(f"Decision {decision.id} [{decision.status.value}]: {decision.statement}")


@room_app.command("freeze")
def freeze_room(
    selector: str = typer.Argument(..., help="Room name, ID, or unique ID prefix."),
    by: str = typer.Option(
        ...,
        "--by",
        help="Bare human identity recorded as human:<id> — the freeze decision.",
    ),
    from_message: str = typer.Option(
        ...,
        "--from-message",
        help="Canonical planner clarification-response reply to freeze.",
    ),
    supersedes: str | None = typer.Option(
        None,
        "--supersedes",
        help="Plan artifact id this freeze supersedes (must be the canonical tip).",
    ),
    title: str | None = typer.Option(
        None,
        "--title",
        help="Task title for the new Room task (defaults to the plan's first line).",
    ),
) -> None:
    """Freeze a planner-authored plan and bind execution (P7.3).

    The human freeze mints the canonical Room plan, the durable build request,
    and a Room-scoped task at IMPLEMENTING — then ``relay continue <task>``
    implements the frozen plan with no plan-stage run.
    """
    from relay.cli.main import _open_db
    from relay.core.room_freeze import freeze_room_plan, require_implementer_write_grant
    from relay.core.room_records import RoomRecordRefusal
    from relay.storage.store import SqliteEvidenceStore

    root = Path.cwd()
    conn = None
    try:
        sender = _human_sender(by)
        config = load_config(root)
        conn = _open_db(root)
        store = SqliteRelayStore(conn)
        writer = EventLogWriter(conn)
        workspace = _workspace(store, root)
        lifecycle = RoomLifecycle(store, writer)
        room = lifecycle.resolve(workspace.id, selector)
        # The RESOLVED implementer model is what `relay continue` re-derives
        # from CURRENT config, so the durable build request must pin it.
        seat_agent = RoomSeatResolver(room).resolve_role(AgentRole.IMPLEMENTER.value)
        implementer_model: str | None = None
        if seat_agent is not None and seat_agent in config.agents:
            implementer_model = resolve_settings(
                cli=CliOverrides(), yaml_agent=config.agents[seat_agent]
            ).model
            # P7.3: freeze binds execution — the EFFECTIVE grant is validated
            # through the real adapter before any canonical write, so an unset
            # grant deferring to a read-only adapter default refuses here, and
            # a write grant the adapter cannot honor fails pre-spawn semantics.
            require_implementer_write_grant(
                RegistryAgentFactory(config, root).build(seat_agent), seat_agent
            )
        outcome = freeze_room_plan(
            store,
            writer,
            SqliteEvidenceStore(store),
            config,
            room,
            source_message_id=from_message,
            frozen_by=sender,
            workspace_root=root,
            title=title,
            supersedes_plan_artifact_id=supersedes,
            implementer_model=implementer_model,
        )
    except ClosedRoomError as exc:
        typer.echo(f"ERROR Room freeze refused: {exc}")
        raise typer.Exit(1) from exc
    except (ConfigError, RoomError, RoomRecordRefusal) as exc:
        typer.echo(f"ERROR {exc}")
        raise typer.Exit(1) from exc
    finally:
        if conn is not None:
            conn.close()

    typer.echo(f"Room {outcome.room.id} - {outcome.room.name}")
    typer.echo(f"Frozen plan {outcome.plan_artifact.id}")
    typer.echo(f"Freeze record {outcome.freeze_record.id} by {sender}")
    if outcome.superseded_plan_artifact_id is not None:
        typer.echo(f"Supersedes {outcome.superseded_plan_artifact_id}")
    typer.echo(f"Task {outcome.task.id} [{outcome.task.state.value}] - {outcome.task.title}")
    typer.echo(f"Implementer {outcome.implementer}")
    typer.echo(f"Next: relay continue {outcome.task.id}")


@room_app.command("graph")
def graph_room(
    selector: str = typer.Argument(..., help="Room name, ID, or unique ID prefix."),
    json_output: bool = typer.Option(False, "--json", help="Machine-readable graph."),
) -> None:
    """Render the canonical Room plan/decision/finding graph (P7.3)."""
    from relay.cli.server_client import request, server_url

    if server_url():
        from urllib.parse import quote

        view = request("GET", "/rooms/" + quote(selector, safe="") + "/graph")
        typer.echo(json.dumps(view, ensure_ascii=True))
        return

    from relay.cli.main import _open_db
    from relay.core.room_graph import RoomGraphIntegrityError, build_room_graph

    root = Path.cwd()
    conn = None
    try:
        conn = _open_db(root)
        store = SqliteRelayStore(conn)
        workspace = _workspace(store, root)
        room = RoomLifecycle(store, EventLogWriter(conn)).resolve(workspace.id, selector)
        graph = build_room_graph(store, room.id)
    except (ConfigError, RoomError, RoomGraphIntegrityError) as exc:
        typer.echo(f"ERROR {exc}")
        raise typer.Exit(1) from exc
    finally:
        if conn is not None:
            conn.close()

    if json_output:
        typer.echo(json.dumps(_graph_view(graph), ensure_ascii=True))
        return
    typer.echo(f"Room {graph.room.id} - {graph.room.name} [{graph.room.status.value}]")
    for chain in graph.plans:
        typer.echo(f"Plan chain (task {chain.task_id}):")
        for node in chain.nodes:
            marker = " " if node.plan.id == chain.tip.id else ""
            if node.edge == "frozen":
                edge = f"frozen by {node.frozen_by}"
            else:
                edge = f"revised by decision {node.decision_id}"
            typer.echo(f"  {node.plan.id}{marker} [{edge}] {node.first_line}")
    for node in graph.decisions:
        superseded = f" superseded_by={node.superseded_by}" if node.superseded_by else ""
        typer.echo(
            f"Decision {node.decision.id} [{node.decision.status.value}]{superseded}: "
            f"{node.decision.statement}"
        )
        if node.decision.references:
            typer.echo(f"  references: {', '.join(node.decision.references)}")
    for node in graph.findings:
        typer.echo(
            f"Finding {node.finding.id} [{node.finding.severity.value}] "
            f"{node.finding.title} (review {node.review_artifact.id})"
        )


def _graph_view(graph: object) -> dict:
    """The versioned ``relay.room.graph.v1`` JSON envelope."""
    room = graph.room  # type: ignore[attr-defined]
    return {
        "version": "relay.room.graph.v1",
        "room": {"id": room.id, "name": room.name, "status": room.status.value},
        "plans": [
            {
                "task_id": chain.task_id,
                "tip": chain.tip.id,
                "nodes": [
                    {
                        "plan_artifact_id": node.plan.id,
                        "edge": node.edge,
                        "supersedes_plan_artifact_id": node.supersedes_plan_artifact_id,
                        "frozen_by": node.frozen_by,
                        "freeze_record_id": node.freeze_record_id,
                        "source_message_id": node.source_message_id,
                        "source_run_id": node.source_run_id,
                        "decision_id": node.decision_id,
                        "signal_message_id": node.signal_message_id,
                        "reply_message_id": node.reply_message_id,
                        "first_line": node.first_line,
                    }
                    for node in chain.nodes
                ],
            }
            for chain in graph.plans  # type: ignore[attr-defined]
        ],
        "decisions": [
            {
                "id": node.decision.id,
                "status": node.decision.status.value,
                "statement": node.decision.statement,
                "rationale": node.decision.rationale,
                "proposed_by": node.decision.proposed_by,
                "accepted_by": node.decision.accepted_by,
                "references": list(node.decision.references),
                "source_reply_id": node.decision.source_reply_id,
                "supersedes_decision_id": node.decision.supersedes_decision_id,
                "superseded_by": node.superseded_by,
                "task_id": node.decision.task_id,
            }
            for node in graph.decisions  # type: ignore[attr-defined]
        ],
        "findings": [
            {
                "id": node.finding.id,
                "severity": node.finding.severity.value,
                "title": node.finding.title,
                "description": node.finding.description,
                "requested_change": node.finding.requested_change,
                "validation_expectation": node.finding.validation_expectation,
                "task_id": node.finding.task_id,
                "review_artifact_id": node.finding.review_artifact_id,
                "review_run_id": node.finding.review_run_id,
                "source_finding_id": node.finding.source_finding_id,
            }
            for node in graph.findings  # type: ignore[attr-defined]
        ],
    }


def _room_participant_delivery(
    store: SqliteRelayStore,
    room: Room,
    role: str,
    agent_name: str,
    delivery: MessageDelivery,
) -> tuple[str, str, list[str], str | None]:
    """P7.4 (App. D.10): reconstructed Room context + honest continuation.

    Builds the deterministic participant-context block from canonical
    records, resolves a resumable prior session handle only when the seat's
    agent declares SESSION_RESUME *and* opts into persistence *and* a prior
    handle for THIS SEAT validates — otherwise an honest fresh run. The
    seat's agent instance comes from ``delivery.recipient_for_inspection``:
    the SAME prepared delivery recipient, never a second independently
    constructed probe.

    Returns ``(prompt_suffix, fallback_prompt_suffix, extra_context_refs,
    resume_session_ref)`` for :meth:`MessageDelivery.deliver_and_reply`:
    the primary suffix claims ``resumed:<ref>`` only when a resume will
    actually be attempted, and the fallback suffix — used only when the
    harness positively rejects the handle — declares ``resume-rejected``
    instead, so a fallback prompt never claims a resume that did not
    happen. The block rides OUTSIDE the frozen D15 envelope so non-Room
    deliveries stay byte-identical.
    """
    from dataclasses import replace

    from relay.core.delivery import latest_session_ref
    from relay.core.room_context import (
        build_room_participant_context,
        render_room_context,
        room_context_refs,
    )
    from relay.harness.capabilities import HarnessCapability
    from relay.harness.errors import UnsupportedCapability
    from relay.harness.runtime import HarnessAgent

    resume_ref: str | None = None
    agent = delivery.recipient_for_inspection(agent_name)
    if isinstance(agent, HarnessAgent) and (
        HarnessCapability.SESSION_RESUME in agent.capabilities_set()
    ):
        profile = agent.profile
        if profile is not None and bool(getattr(profile, "persist_session_ref", False)):
            prior = latest_session_ref(store, room.id, agent_name, role)
            if prior is not None:
                try:
                    agent.resume_arguments(prior)
                except UnsupportedCapability:
                    # Invalid persisted handle: honest fresh run, not fatal.
                    prior = None
                else:
                    resume_ref = prior
    ctx = build_room_participant_context(
        store,
        room_id=room.id,
        role=role,
        agent_name=agent_name,
        continuity="fresh",
    )
    refs = room_context_refs(ctx)
    fresh_suffix = render_room_context(ctx)
    if resume_ref is None:
        return fresh_suffix, fresh_suffix, refs, None
    resumed_suffix = render_room_context(replace(ctx, continuity=f"resumed:{resume_ref}"))
    rejected_suffix = render_room_context(replace(ctx, continuity="resume-rejected"))
    return resumed_suffix, rejected_suffix, refs, resume_ref


def _select_room(store: SqliteRelayStore, workspace: Workspace, selector: str | None) -> Room:
    """The selected Room: ``--room`` wins, else the workspace's active Room."""
    if selector is None:
        if workspace.active_room_id is None:
            raise RoomLookupError("no active Room - pass --room <selector>")
        room = store.load_model(Room, workspace.active_room_id)
        if room is None or room.workspace_id != workspace.id:
            raise RoomLookupError("the workspace's active Room does not exist")
        return room
    return RoomLifecycle(store, EventLogWriter(store.conn)).resolve(workspace.id, selector)


def register(app: typer.Typer) -> None:
    app.add_typer(room_app, name="room")

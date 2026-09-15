"""CLI surface for persistent Room lifecycle and targeted exchange (P7.1-P7.2)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

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
        try:
            outcome = asyncio.run(delivery.deliver_and_reply(request.id))
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


def register(app: typer.Typer) -> None:
    app.add_typer(room_app, name="room")

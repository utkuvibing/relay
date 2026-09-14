"""CLI surface for persistent Room lifecycle (P7.1)."""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from relay.context import ConfigError, identity_key, load_config
from relay.core.room_feed import RoomFeedIntegrityError, build_room_feed
from relay.core.rooms import RoomError, RoomLifecycle
from relay.storage.events import EventLogWriter
from relay.storage.models import Room, Workspace
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


def register(app: typer.Typer) -> None:
    app.add_typer(room_app, name="room")

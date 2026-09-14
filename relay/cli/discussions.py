"""CLI composition and literal rendering for bounded discussions."""

import asyncio
import json
from importlib.resources import as_file, files
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from typer.core import TyperCommand

from relay.agents.factory import RegistryAgentFactory
from relay.context import ConfigError, identity_key, load_config
from relay.context.protocols import load_protocol
from relay.core.discussion_view import (
    DiscussionLookupError,
    build_discussion_view,
    discussion_envelope,
    resolve_execution,
)
from relay.core.policy import (
    CommunicationPolicyRefusal,
    SqliteCommunicationPolicyGate,
    policy_from_config,
)
from relay.core.protocol_runner import ProtocolRunner, ProtocolSpec
from relay.core.rooms import RoomLifecycle
from relay.storage.events import EventLogWriter
from relay.storage.models import new_id
from relay.storage.store import SqliteRelayStore

try:  # Newer Typer vendors Click; older supported releases depend on it.
    from typer._click.exceptions import UsageError
except ImportError:
    from click import UsageError


def bundled_debate():
    with as_file(files("relay.context").joinpath("debate.yaml")) as path:
        return load_protocol(path)


def render_discussion(view: dict, *, json_output: bool = False, summary: bool = False) -> None:
    if json_output:
        typer.echo(json.dumps(view, ensure_ascii=True))
        return
    console = Console()
    if view["error"]:
        console.print(f"ERROR: {view['error']['message']}", markup=False)
    if view["execution_id"] is None:
        return
    console.print(f"Discussion {view['execution_id']} — {view['topic']}", markup=False)
    if not summary:
        console.print(f"Room: {view['room_id']}", markup=False)
        if view["protocol"]:
            console.print(
                f"Protocol: {view['protocol']['name']} v{view['protocol']['version']}",
                markup=False,
            )
    if view["progress"]:
        p = view["progress"]
        console.print(
            f"Ledger progress: {p['status']} ({p['completed_stages']}/{p['total_stages']} stages)",
            markup=False,
        )
    observation = view["last_observation"]
    if observation:
        prefix = "Human action needed" if observation["needs_human"] else "Last outcome"
        stale = " (stale observation)" if observation["stale"] else ""
        console.print(f"{prefix}: {observation['stop_reason']}{stale}", markup=False)
    if not summary:
        for output in view["outputs"]:
            console.print(
                f"{output['stage']} [{output['occurrence']}] / {output['sender']} / {output['type']}",
                markup=False,
            )
            console.print(output["content"], markup=False, highlight=False)
    console.print(view["next_action"] or "Inspect the discussion ledger.", markup=False)
    console.print(f"relay inspect-discussion {view['execution_id']}", markup=False)


def _error(message: str, json_output: bool, *, code: str = "invalid_input", exit_code: int = 1):
    view = discussion_envelope()
    view["error"] = {"code": code, "message": message}
    render_discussion(view, json_output=json_output)
    raise typer.Exit(exit_code)


class DiscussionCommand(TyperCommand):
    """Keep parser failures inside the discussion JSON contract too."""

    def parse_args(self, ctx, args):
        options = args[:args.index("--")] if "--" in args else list(args)
        try:
            return super().parse_args(ctx, args)
        except UsageError:
            if "--json" in options:
                _error("Invalid command usage; consult --help.", True, code="usage", exit_code=2)
            raise


def discuss(
    topic: str | None = typer.Argument(None, help="Discussion topic, in quotes."),
    protocol: Annotated[
        Path | None, typer.Option("--protocol", help="Custom protocol YAML path.")
    ] = None,
    resume: str | None = typer.Option(None, "--resume", help="Execution ID or unique prefix."),
    json_output: bool = typer.Option(False, "--json", help="Machine-readable discussion."),
) -> None:
    """Run a bounded discussion, or explicitly resume its saved execution."""
    from relay.cli.main import _open_db

    if (resume is not None and (topic is not None or protocol is not None)) or (
        resume is None and (topic is None or not topic.strip())
    ):
        _error(
            "Supply a topic, or --resume without a topic or --protocol.",
            json_output,
            code="usage",
            exit_code=2,
        )
    conn = None
    try:
        root = Path.cwd()
        config = load_config(root)
        conn = _open_db(root)
        store = SqliteRelayStore(conn)
        factory = RegistryAgentFactory(config, root)
        service = ProtocolRunner(
            store,
            EventLogWriter(conn),
            factory,
            factory,
            SqliteCommunicationPolicyGate(store, policy_from_config(config)),
        )
        if resume is not None:
            execution = resolve_execution(store, resume)
        else:
            definition = load_protocol(protocol) if protocol is not None else bundled_debate()
            missing = [
                p.role.value for p in definition.participants if p.role.value not in config.roles
            ]
            if missing:
                _error("Add roles: bindings in relay.yaml for: " + ", ".join(missing), json_output)
            workspace = store.workspace_for_identity(identity_key(root))
            if workspace is None:
                _error("Workspace not initialized; run relay init first.", json_output)
            participant_bindings = {
                p.role.value: config.roles[p.role.value] for p in definition.participants
            }
            room_id = new_id()
            execution = service.prepare(
                ProtocolSpec(
                    definition,
                    new_id(),
                    topic,
                    room_id=room_id,
                )
            )
            RoomLifecycle(store, EventLogWriter(conn)).create(
                workspace,
                topic[:200],
                participant_bindings,
                config.agents.keys(),
                disambiguate_name=True,
                room_id=room_id,
                attached_execution=execution,
            )
        result = asyncio.run(service.resume(execution.id))
        view = build_discussion_view(store, execution)
    except DiscussionLookupError as exc:
        _error(str(exc), json_output)
    except CommunicationPolicyRefusal:
        _error(
            "Protocol request/reply types are not permitted by communication policy. "
            "Review the protocol and communication settings before starting.",
            json_output,
            code="policy_refused",
        )
    except (ConfigError, ValueError, TypeError, OSError):
        _error(
            "Cannot open discussion: check workspace initialization, execution ID, protocol YAML, "
            "and compatible roles: bindings in relay.yaml.",
            json_output,
        )
    finally:
        if conn is not None:
            conn.close()
    render_discussion(view, json_output=json_output)
    raise typer.Exit({"complete": 0, "delivery_pending": 3}.get(result.stop_reason.value, 1))


def inspect_discussion(
    execution_id: str = typer.Argument(..., help="Execution ID or unique prefix."),
    json_output: bool = typer.Option(False, "--json", help="Machine-readable discussion."),
) -> None:
    """Inspect persisted discussion records without invoking or recovering an agent."""
    from relay.cli.main import _open_db

    conn = None
    try:
        conn = _open_db(Path.cwd())
        store = SqliteRelayStore(conn)
        execution = resolve_execution(store, execution_id)
        view = build_discussion_view(store, execution)
    except DiscussionLookupError as exc:
        _error(str(exc), json_output)
    except (ConfigError, ValueError):
        _error(
            "Cannot inspect discussion: initialize the workspace and use an exact ID or unique prefix.",
            json_output,
        )
    finally:
        if conn is not None:
            conn.close()
    render_discussion(view, json_output=json_output)
    if view["error"]:
        raise typer.Exit(1)


def register(app: typer.Typer) -> None:
    app.command(cls=DiscussionCommand)(discuss)
    app.command("inspect-discussion", cls=DiscussionCommand)(inspect_discussion)

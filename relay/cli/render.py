"""Rich rendering for the Relay CLI - no secrets ever pass through here.

App. B.3: credentials exist only in process memory inside adapters. These
renderers print configuration state ("configured" / "not configured"), never
keys, tokens, or session material.

Each call builds its own ``Console`` so output follows the *current*
``sys.stdout`` (test runners swap it per invocation).
"""

from __future__ import annotations

import json
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from relay.agents.base import BackendType
from relay.context.config import AgentConfig, RelayConfig
from relay.storage.models import Run, Workspace


def _out() -> Console:
    return Console()


def _auth_state(agent: AgentConfig, key_present: bool) -> str:
    if agent.backend is BackendType.HARNESS:
        return "harness (Phase 2)"
    return "configured" if key_present else "not configured"


def _agent_table(config: RelayConfig, key_states: dict[str, bool]) -> Table:
    table = Table(title="Relay agents")
    table.add_column("Agent")
    table.add_column("Backend")
    table.add_column("Adapter")
    table.add_column("Model")
    table.add_column("Auth")
    for name, agent in sorted(config.agents.items()):
        table.add_row(
            name,
            agent.backend.value,
            agent.adapter,
            agent.model or "-",
            _auth_state(agent, key_states.get(name, False)),
        )
    return table


def init_summary(workspace: Workspace, config: RelayConfig, key_states: dict[str, bool]) -> None:
    _out().print(_agent_table(config, key_states))
    _out().print(f"Workspace: [bold]{workspace.name}[/bold] ({workspace.id[:8]}...)")


def ask_result(
    *,
    provider: str,
    model: str | None,
    run: Run,
    output: str | None,
    error: str | None,
) -> None:
    if error is not None:
        _out().print(f"[red]ERROR {provider} failed:[/red] {error}")
        return
    _out().print(
        Panel(output or "(empty response)", title=f"{provider} | {model or 'default'}")
    )
    usage = (
        f"in {run.input_size} | out {run.output_size}"
        if run.input_size is not None or run.output_size is not None
        else "no usage data"
    )
    _out().print(f"[dim]run {run.id[:8]}... | {run.status.value} | {usage}[/dim]")


def status(workspace: Workspace | None, config: RelayConfig, key_states: dict[str, bool]) -> None:
    if workspace is None:
        _out().print("[yellow]not initialized - run 'relay init' first[/yellow]")
        return
    _out().print(f"Relay status - [bold]{workspace.name}[/bold]")
    _out().print(_agent_table(config, key_states))


def history_table(runs: list[Run]) -> None:
    table = Table(title="Run history")
    table.add_column("Run")
    table.add_column("Agent")
    table.add_column("Role")
    table.add_column("Status")
    table.add_column("Model")
    table.add_column("Started")
    for run in runs:
        table.add_row(
            run.id[:8],
            run.agent,
            run.role,
            run.status.value,
            run.model or "-",
            run.started_at.isoformat(timespec="seconds"),
        )
    _out().print(table)


def history_json(runs: list[Run]) -> None:
    payload: list[dict[str, Any]] = [run.model_dump(mode="json") for run in runs]
    _out().print(json.dumps(payload, indent=2))


def run_detail(run: Run, artifacts: list[Any], events: list[Any]) -> None:
    _out().print(Panel(f"[bold]Run {run.id}[/bold]", title=run.status.value))
    _out().print(f"agent: {run.agent} | role: {run.role} | model: {run.model or '-'}")
    _out().print(f"started: {run.started_at.isoformat()} | ended: {run.ended_at or '-'}")
    if run.input_size is not None or run.output_size is not None:
        _out().print(f"usage: in {run.input_size} | out {run.output_size}")
    for artifact in artifacts:
        _out().print(Panel(artifact.content or "", title=f"{artifact.kind.value} artifact"))
    for event in events:
        _out().print(f"[dim]event #{event.sequence}: {event.type.value} - {event.content}[/dim]")

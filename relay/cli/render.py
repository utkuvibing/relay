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
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from relay.agents.base import BackendType
from relay.context.config import AgentConfig, RelayConfig
from relay.storage.models import Run, Workspace


def _out() -> Console:
    return Console()


def _auth_state(agent: AgentConfig, key_present: bool) -> str:
    if agent.backend is BackendType.HARNESS:
        # Harness adapters own their authentication (App. B.3): Relay can
        # only report whether the adapter is registered, never login state.
        return "harness-owned auth"
    return "configured" if key_present else "not configured"


def _agent_table(config: RelayConfig, key_states: dict[str, bool]) -> Table:
    table = Table(title="Relay agents")
    table.add_column("Agent")
    table.add_column("Backend")
    table.add_column("Adapter")
    table.add_column("Model")
    table.add_column("Auth")
    for name, agent in sorted(config.agents.items()):
        model_display = agent.model or "-"
        if agent.backend is BackendType.HARNESS and agent.harness is not None:
            grant = agent.harness.grant.value if agent.harness.grant else "default"
            model_display = f"{model_display} · grant:{grant}"
        table.add_row(
            name,
            agent.backend.value,
            agent.adapter,
            model_display,
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
    _out().print(Panel(output or "(empty response)", title=f"{provider} | {model or 'default'}"))
    usage = (
        f"in {run.input_size} | out {run.output_size}"
        if run.input_size is not None or run.output_size is not None
        else "no usage data"
    )
    _out().print(f"[dim]run {run.id[:8]}... | {run.status.value} | {usage}[/dim]")


def status(
    workspace: Workspace | None,
    config: RelayConfig,
    key_states: dict[str, bool],
    tasks: list[Any] | None = None,
    active_view: Any | None = None,
) -> None:
    """Workspace summary; gains the P3.4 task table + active-task panel."""
    if workspace is None:
        _out().print("[yellow]not initialized - run 'relay init' first[/yellow]")
        return
    _out().print(f"Relay status - [bold]{workspace.name}[/bold]")
    _out().print(_agent_table(config, key_states))
    if tasks:
        _out().print(task_table(tasks))
    if active_view is not None:
        active_task_panel(active_view)


def _one_line(text: str, width: int) -> str:
    """Collapse whitespace and clamp — prompts may be multi-line."""
    collapsed = " ".join(text.split())
    return collapsed[:width] + ("..." if len(collapsed) > width else "")


def task_table(tasks: list[Any]) -> Table:
    """Recent tasks with their machine positions (P3.4).

    The column is labelled ``Created`` deliberately: ``Task.updated_at`` is
    not bumped on state transitions, and an honest timestamp beats a
    misleading one.
    """
    table = Table(title="Relay tasks (recent)")
    table.add_column("Task")
    table.add_column("Title")
    table.add_column("State")
    table.add_column("Created")
    for task in tasks:
        table.add_row(
            task.id[:8],
            _one_line(task.title, 48),
            task.state.value,
            task.created_at.isoformat(timespec="seconds"),
        )
    return table


def active_task_panel(view: Any) -> None:
    """The active task's machine position, per-edge gaps, next action (P3.4)."""
    task = view.task
    lines = [f"[bold]{task.id[:8]}[/bold] · {_one_line(escape(task.title), 60)}"]
    state_note = f"state: {task.state.value}"
    lines.append(state_note + (" (terminal)" if view.is_terminal else ""))
    if view.last_transition is not None:
        transition = view.last_transition
        sequence = f" (event #{transition.sequence})" if transition.sequence is not None else ""
        lines.append(f"last transition: {transition.from_state} -> {transition.to_state}{sequence}")
    for edge in view.edges:
        if edge.missing:
            names = ", ".join(kind.value for kind in edge.missing)
            lines.append(f"  -> {edge.to_state.value}: missing {names}")
        else:
            lines.append(f"  -> {edge.to_state.value}: ready")
    if view.pending_approval is not None:
        lines.append(f"next: relay approve {task.id} --by <name>")
    _out().print(Panel("\n".join(lines), title="Active task", expand=False))


def inspect_task(ledger: Any, json_output: bool = False) -> None:
    """One task's full ledger: transitions, evidence, approvals, artifacts (P3.4)."""
    if json_output:
        from relay.cli.taskview import ledger_payload

        # soft_wrap: JSON strings must survive byte-exact (a wrapped line
        # would inject a real newline inside a string value); markup/highlight
        # off for the same reason — content is data, never rich markup.
        _out().print(
            json.dumps(ledger_payload(ledger), indent=2),
            markup=False,
            highlight=False,
            soft_wrap=True,
        )
        return

    view = ledger.view
    task = view.task
    _out().print(
        Panel(
            Text(_one_line(escape(task.title), 200)),
            title=f"Task {task.id} — {task.state.value}",
        )
    )
    _out().print(f"created: {task.created_at.isoformat()}")

    _out().print("[bold]Transitions[/bold]")
    if ledger.transitions:
        for transition in ledger.transitions:
            sequence = f"#{transition.sequence} " if transition.sequence is not None else ""
            _out().print(
                f"[dim]  {sequence}{transition.from_state} -> {transition.to_state} "
                f"({transition.at.isoformat(timespec='seconds')})[/dim]"
            )
    else:
        _out().print("[dim]  none recorded[/dim]")

    _out().print("[bold]Evidence[/bold]")
    if ledger.evidence:
        for record in ledger.evidence:
            links = [
                ref[:8] for ref in (record.run_id, record.tool_run_id, record.artifact_id) if ref
            ]
            suffix = f" ({', '.join(links)})" if links else ""
            _out().print(f"  {record.kind.value} by {record.produced_by}{suffix}")
    else:
        _out().print("[dim]  none recorded[/dim]")

    _out().print("[bold]Approvals[/bold]")
    if ledger.approvals:
        for approval in ledger.approvals:
            decided = f" by {approval.decided_by}" if approval.decided_by else ""
            reason = _one_line(approval.reason or "", 80)
            _out().print(f"  {approval.status.value} {approval.action.value}{decided} — {reason}")
    else:
        _out().print("[dim]  none recorded[/dim]")

    _out().print("[bold]Artifacts[/bold]")
    if ledger.artifacts:
        for artifact in ledger.artifacts:
            created = artifact.created_at.isoformat(timespec="seconds")
            _out().print(f"  {artifact.kind.value} {artifact.id[:8]} ({created})")
        for artifact in ledger.artifacts:
            # Text(): agent-produced content is data — never markup.
            _out().print(
                Panel(
                    Text(artifact.content or ""),
                    title=f"{artifact.kind.value} {artifact.id[:8]}",
                )
            )
    else:
        _out().print("[dim]  none recorded[/dim]")


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


def build_result(*, task, outcome, view=None) -> None:
    """Render one `relay build` outcome (P2.2b; P3.4 final-state line).

    D6: the ending is always explicit — the task's machine state plus the
    unblocking action when one exists. A gated build ending at
    APPROVAL_REQUIRED names the ``relay approve`` command; a blocked ending
    names the missing evidence; a terminal ending says so. No invented
    actions: states with no driver (rework parking) render the bare state.
    """
    ask = outcome.ask
    if ask.error is not None:
        _out().print(f"[red]ERROR {ask.run.agent} failed:[/red] {ask.error}")
        _final_state_line(task, view)
        return
    _out().print(
        Panel(
            (ask.response.output if ask.response else "") or "(empty response)",
            title=f"{task.title[:60]}",
        )
    )
    observations = len(outcome.tool_run_ids)
    diff_note = (
        f"diff artifact {outcome.diff_artifact_id[:8]}..."
        if outcome.diff_artifact_id
        else "no workspace changes"
    )
    _out().print(
        f"[dim]task {task.id[:8]}... | run {ask.run.id[:8]}... | {ask.run.status.value} | "
        f"{observations} observed harness events | {diff_note} | evidence recorded[/dim]"
    )
    _final_state_line(task, view)


def _final_state_line(task, view) -> None:
    """One dim line: machine state + next action (pending hint beats gap)."""
    note = f"[dim]state: {task.state.value}"
    if view is None:
        _out().print(f"{note}[/dim]")
        return
    if view.pending_approval is not None:
        _out().print(f"{note} | next: relay approve {task.id} --by <name>[/dim]")
        return
    gap = next((edge for edge in view.edges if edge.missing), None)
    if gap is not None:
        missing = ", ".join(kind.value for kind in gap.missing)
        _out().print(f"{note} | blocked: missing {missing}[/dim]")
        return
    _out().print(f"{note}[/dim]")

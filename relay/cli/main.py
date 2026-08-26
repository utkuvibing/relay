"""Relay CLI - Phase 1 exit gate: init | ask | status | history.

SPEC reference: §27 Phase 1; §13 (init), §25 (run logging), App. B.

Secret hygiene (App. B.3): keys exist only in process memory inside the
adapter; ``status`` reports "configured / not configured" and nothing else.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import typer
from rich.console import Console

from relay.agents import (
    AgentRequest,
    AgentRole,
    CliOverrides,
    UnknownAgentError,
    build_agent,
    resolve_settings,
)
from relay.agents.errors import AgentError, AgentNotConfigured
from relay.context import (
    ConfigError,
    agent_config,
    identity_key,
    initialize_workspace,
    load_config,
    workspace_layout,
)
from relay.core.orchestrator import run_ask
from relay.storage import connect, migrate
from relay.storage.events import EventLogWriter
from relay.storage.models import Run
from relay.storage.store import SqliteRelayStore

app = typer.Typer(
    name="relay",
    help="Local-first AI orchestration runtime.",
    no_args_is_help=True,
)


def _out() -> Console:
    """A Console bound to the *current* stdout (test runners swap it)."""
    return Console()


def _harden_streams() -> None:
    """Never crash on characters outside the console encoding.

    Legacy Windows consoles (cp1252) cannot encode e.g. a 'Ş' in a filesystem
    path or emoji in a model answer; Python then raises UnicodeEncodeError
    mid-print. Replacing unencodable characters keeps the CLI alive on any
    terminal; UTF-8 consoles are unaffected (nothing gets replaced there).
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(errors="replace")
            except (ValueError, OSError):
                pass


_harden_streams()


def _open_db(root: Path):
    """Open (and migrate) the workspace database; raises ConfigError if uninitialized."""
    layout = workspace_layout(root)
    if not layout.db_path.exists():
        raise ConfigError(
            f"workspace not initialized - run 'relay init' in {root} first"
        )
    conn = connect(layout.db_path)
    migrate(conn)
    return conn


def _key_states(config) -> dict[str, bool]:
    """Agent name → whether its API key env var is present (presence only)."""
    states: dict[str, bool] = {}
    for name, agent in config.agents.items():
        if agent.backend.value == "api":
            env_name = os.environ.get("RELAY_API_KEY_ENV", "OPENAI_API_KEY")
            states[name] = bool(os.environ.get(env_name))
    return states


def _default_relay_yaml() -> str:
    return (
        "# Relay configuration - non-secret provider facts only (SPEC App. B.3).\n"
        "# API keys come from the environment; never put them in this file.\n"
        "agents:\n"
        "  gpt:\n"
        "    backend: api\n"
        "    adapter: openai\n"
        "    model: gpt-4o-mini\n"
        "# Phase 2: harness-backed adapters own their own authentication.\n"
        "# codex:  {backend: harness, adapter: codex_cli}\n"
        "# claude: {backend: harness, adapter: claude_code}\n"
    )


@app.command()
def init() -> None:
    """Discover this project, write .relay/profile.yaml + relay.yaml, open the DB."""
    root = Path.cwd()
    layout = workspace_layout(root)
    layout.data_dir.mkdir(parents=True, exist_ok=True)
    conn = connect(layout.db_path)
    migrate(conn)
    try:
        workspace = initialize_workspace(root, conn)
        if not layout.config_path.exists():
            layout.config_path.write_text(_default_relay_yaml(), encoding="utf-8")
    finally:
        conn.close()
    config = load_config(root)
    _out().print(f"[green]OK[/green] initialized {root}")
    from relay.cli.render import init_summary

    init_summary(workspace, config, _key_states(config))


@app.command()
def ask(
    provider: str = typer.Argument(..., help="Agent name from relay.yaml, e.g. 'gpt'."),
    prompt: str = typer.Argument(..., help="What to ask, in quotes."),
    role: AgentRole = typer.Option(
        AgentRole.RESEARCHER, "--role", help="Agent role for this run."
    ),
    model: str | None = typer.Option(None, "--model", help="Model override (CLI wins)."),
) -> None:
    """Run one agent on one prompt; persist the run crash-safely."""
    from relay.cli.render import ask_result

    root = Path.cwd()
    try:
        config = load_config(root)
        agent_cfg = agent_config(config, provider)
        settings = resolve_settings(cli=CliOverrides(model=model), yaml_agent=agent_cfg)

        conn = _open_db(root)
        try:
            # G0/R1: registry presence + backend-family validation happen
            # here; harness adapters additionally resolve their ExecutionGrant
            # inside run() before any process spawns.
            agent = build_agent(provider, settings, agent_cfg, workspace_root=root)
            store = SqliteRelayStore(conn)
            writer = EventLogWriter(conn)
            request = AgentRequest(prompt=prompt, role=role)
            outcome = asyncio.run(run_ask(store, writer, agent, request, model=settings.model))
        finally:
            conn.close()
    except (ConfigError, AgentError, AgentNotConfigured, UnknownAgentError) as exc:
        _out().print(f"[red]ERROR[/red] {exc}")
        raise typer.Exit(code=1) from exc

    ask_result(
        provider=provider,
        model=settings.model,
        run=outcome.run,
        output=outcome.response.output if outcome.response else None,
        error=outcome.error,
    )
    if outcome.error is not None:
        raise typer.Exit(code=1)


@app.command()
def build(
    prompt: str = typer.Argument(..., help="What to implement, in quotes."),
    agent: str | None = typer.Option(
        None, "--agent", help="Configured harness agent to implement with (default: auto-select)."
    ),
    model: str | None = typer.Option(None, "--model", help="Model override (CLI wins)."),
) -> None:
    """Implement a change via a configured harness agent; persist diff + evidence."""
    from relay.cli.render import build_result
    from relay.core.orchestrator import BuildRefusal, run_build
    from relay.storage.models import EventLogEntry, EventType, Task
    from relay.storage.store import SqliteEvidenceStore

    root = Path.cwd()
    try:
        config = load_config(root)
        conn = _open_db(root)
        try:
            store = SqliteRelayStore(conn)
            writer = EventLogWriter(conn)

            if not _worktree_is_clean(root):
                _out().print(
                    "[red]ERROR[/red] working tree has uncommitted changes — "
                    "commit or stash them first (diff integrity)"
                )
                raise typer.Exit(code=1)

            candidates = _harness_implementer_candidates(config, store, conn, root)
            chosen_name = agent or _select_implementer(candidates)
            agent_cfg = agent_config(config, chosen_name)
            settings = resolve_settings(cli=CliOverrides(model=model), yaml_agent=agent_cfg)
            implementer = build_agent(chosen_name, settings, agent_cfg, workspace_root=root)

            task = Task(title=prompt[:200])
            store.save_model(task)
            writer.record(
                EventLogEntry(
                    type=EventType.TASK_CREATED,
                    content=f"task created for build: {task.title}",
                    references=[f"task:{task.id}"],
                )
            )

            request = AgentRequest(prompt=prompt, role=AgentRole.IMPLEMENTER, task_id=task.id)
            outcome = asyncio.run(
                run_build(
                    store,
                    writer,
                    SqliteEvidenceStore(store),
                    implementer,
                    request,
                    workspace_root=root,
                    model=settings.model,
                    agent_name=chosen_name,
                )
            )
        finally:
            conn.close()
    except (
        ConfigError,
        AgentError,
        AgentNotConfigured,
        UnknownAgentError,
        BuildRefusal,
    ) as exc:
        _out().print(f"[red]ERROR[/red] {exc}")
        raise typer.Exit(code=1) from exc

    build_result(task=outcome.task, outcome=outcome)


def _worktree_is_clean(root: Path) -> bool:
    """Refuse builds whose *tracked* content diverges from HEAD."""
    import subprocess

    result = subprocess.run(
        ["git", "-C", str(root), "status", "--porcelain", "--untracked-files=no"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0 and not result.stdout.strip()


def _harness_implementer_candidates(config, store, conn, root) -> list[str]:
    """Configured agents whose adapter executes as HARNESS (family-blind)."""
    names = []
    for name, agent_cfg in config.agents.items():
        if agent_cfg.backend.value != "harness":
            continue
        names.append(name)
    return sorted(names)


def _select_implementer(candidates: list[str]) -> str:
    if not candidates:
        raise ConfigError(
            "no harness-backed agent is configured — add one to relay.yaml, e.g.\n"
            "agents:\n"
            "  codex: {backend: harness, adapter: codex_cli}"
        )
    if len(candidates) > 1:
        listed = ", ".join(candidates)
        raise ConfigError(f"multiple harness agents configured — pick one with --agent: {listed}")
    return candidates[0]


@app.command()
def status() -> None:
    """Show workspace state and whether each agent is configured."""
    from relay.cli.render import status as render_status

    root = Path.cwd()
    config = load_config(root)
    workspace = None
    try:
        conn = _open_db(root)
        try:
            store = SqliteRelayStore(conn)
            workspace = store.workspace_for_identity(identity_key(root))
        finally:
            conn.close()
    except ConfigError:
        pass  # renderer prints the not-initialized hint
    render_status(workspace, config, _key_states(config))


@app.command()
def history(
    limit: int = typer.Option(10, "--limit", min=1, help="How many runs to list."),
    json_output: bool = typer.Option(False, "--json", help="Machine-readable output."),
    full: str | None = typer.Option(None, "--full", help="Run id to inspect in detail."),
) -> None:
    """List persisted runs, or inspect one run in full detail."""
    from relay.cli.render import history_json, history_table, run_detail

    root = Path.cwd()
    try:
        conn = _open_db(root)
        try:
            store = SqliteRelayStore(conn)
            if full is not None:
                run = store.load_model(Run, full)
                if run is None:
                    _out().print(f"[red]ERROR[/red] no run with id '{full}'")
                    raise typer.Exit(code=1)
                artifacts = store.artifacts_for_run(run.id)
                writer = EventLogWriter(conn)
                events = [
                    entry
                    for entry in writer.all()
                    if f"run:{run.id}" in entry.references
                ]
                run_detail(run, artifacts, events)
                return
            runs = list(store.all_models(Run, order_by="started_at DESC, rowid DESC", limit=limit))
            if json_output:
                history_json(runs)
            else:
                history_table(runs)
        finally:
            conn.close()
    except ConfigError as exc:
        _out().print(f"[red]ERROR[/red] {exc}")
        raise typer.Exit(code=1) from exc


if __name__ == "__main__":
    app()


"""Crash-safe single-agent run orchestration (SPEC §5, §25, App. B.1).

Two-phase persistence, in this exact order:

1. **Tx 1 — commit before provider I/O.** The ``Run(RUNNING)`` row, its
   ``run_input`` artifact (the prompt), and the ``AGENT_RUN_STARTED`` event
   commit atomically *before* the adapter is invoked. The prompt survives
   crashes, timeouts, and failures by construction.
2. **Provider call** — strictly after Tx 1. Nothing here is persisted.
3. **Final Tx — success or failure.** Success records the ``run_output``
   artifact plus ``SUCCEEDED``/``AGENT_RUN_FINISHED``; failure records
   ``FAILED``/``AGENT_RUN_FINISHED`` with a sanitized error and no output artifact.

Family-blind by App. B.2: API- and harness-backed adapters flow through this
exact code. No ``Message`` rows and no ``Task`` — a one-shot ask is not
conversation-bus traffic (App. A.2/B.1).

``run_build`` (P2.2b) reuses this crash-safe spine for a task-scoped
implementation run, then closes gate G2: observed harness tool events land
as ToolRun rows; Relay extracts the diff itself as a DIFF artifact;
IMPLEMENTATION_PRODUCED evidence is recorded with run provenance.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass

from relay.agents.base import Agent, AgentRequest, AgentResponse, BackendType, RunObservation
from relay.agents.errors import AgentError
from relay.core.evidence import EvidenceKind, EvidenceStore
from relay.core.permissions import Action, PermissionGate, ToolRequest
from relay.harness.sanitization import redact
from relay.storage.events import EventLogWriter
from relay.storage.models import (
    Artifact,
    ArtifactKind,
    EventLogEntry,
    EventType,
    EvidenceRecord,
    Run,
    RunStatus,
    Task,
    ToolRun,
    utcnow,
)
from relay.storage.store import SqliteRelayStore


@dataclass(frozen=True)
class AskOutcome:
    run: Run
    response: AgentResponse | None = None
    error: str | None = None


def _persistable_error(exc: Exception) -> str:
    """Return only error text safe to persist and render.

    Adapter-authored ``AgentError`` messages are part of the sanitized public
    contract. Arbitrary implementation exceptions may contain request bodies,
    credentials, paths, or other sensitive runtime details, so only their type
    crosses the persistence boundary.
    """
    if isinstance(exc, AgentError):
        return str(exc)
    return f"unexpected agent failure ({type(exc).__name__})"


def _observation_updates(observation: RunObservation | None) -> dict[str, str | None]:
    """Map an optional C.6 observation onto nullable Run columns."""
    if observation is None:
        return {}
    backend = observation.backend
    return {
        "resolved_model": observation.resolved_model,
        "adapter_version": observation.adapter_version,
        "backend": backend.value if isinstance(backend, BackendType) else backend,
        "external_session_ref": observation.external_session_ref,
    }


async def run_ask(
    store: SqliteRelayStore,
    writer: EventLogWriter,
    agent: Agent,
    request: AgentRequest,
    *,
    model: str | None = None,
    agent_name: str | None = None,
) -> AskOutcome:
    """Execute one agent run with crash-safe persistence; never raises."""
    run = Run(
        agent=agent_name or agent.name,
        role=request.role,
        model=model,
        status=RunStatus.RUNNING,
    )

    with store.transaction():
        store.save_model(run)
        input_artifact = store.save_model(
            Artifact(kind=ArtifactKind.RUN_INPUT, run_id=run.id, content=request.prompt)
        )
        writer.record(
            EventLogEntry(
                type=EventType.AGENT_RUN_STARTED,
                content=f"agent '{run.agent}' started",
                references=[f"run:{run.id}", f"artifact:{input_artifact.id}"],
            )
        )

    try:
        response = await agent.run(request)
    except Exception as exc:  # noqa: BLE001 - all failures must become durable run history.
        safe_error = _persistable_error(exc)
        failed = run.model_copy(update={"status": RunStatus.FAILED, "ended_at": utcnow()})
        with store.transaction():
            store.update_model(failed)
            writer.record(
                EventLogEntry(
                    type=EventType.AGENT_RUN_FINISHED,
                    content=f"agent '{run.agent}' failed: {safe_error}",
                    references=[f"run:{run.id}"],
                )
            )
        return AskOutcome(run=failed, error=safe_error)

    usage = response.usage
    succeeded = run.model_copy(
        update={
            "status": RunStatus.SUCCEEDED,
            "input_size": usage.input_tokens if usage else None,
            "output_size": usage.output_tokens if usage else None,
            "cost_usd": usage.cost_usd if usage else None,
            # App. C.6 seam — harness observations land as nullable facts;
            # absent observation keeps historical rows byte-identical.
            **_observation_updates(response.observation),
            "ended_at": utcnow(),
        }
    )
    with store.transaction():
        store.update_model(succeeded)
        output_artifact = store.save_model(
            Artifact(kind=ArtifactKind.RUN_OUTPUT, run_id=run.id, content=response.output)
        )
        writer.record(
            EventLogEntry(
                type=EventType.AGENT_RUN_FINISHED,
                content=f"agent '{run.agent}' succeeded",
                references=[f"run:{run.id}", f"artifact:{output_artifact.id}"],
            )
        )

    return AskOutcome(run=succeeded, response=response)


# ---------------------------------------------------------------------------
# relay build (P2.2b) — task-scoped implementation run + G2 evidence flow
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BuildOutcome:
    task: Task
    ask: AskOutcome
    diff_artifact_id: str | None = None
    tool_run_ids: tuple[str, ...] = ()


class BuildRefusal(Exception):
    """Typed refusal: the requested implementer cannot do a build safely."""


def _observed_tool_events(response_output: str) -> list[dict[str, object]]:
    """Extract sanitized observations from a harness transcript.

    Claim-bearing only (App. C.5/C.7): these become ToolRun *records* —
    observability, never enforcement claims or state-transition authority.
    """
    events: list[dict[str, object]] = []
    for line in response_output.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        item = payload.get("item")
        if isinstance(item, dict) and item.get("id") and item.get("type"):
            command = item.get("command")
            summary = {"id": item["id"], "type": item["type"]}
            # Keep a bounded, already-typed field only; free-form child text
            # is redacted before persisting anything.
            if isinstance(command, str) and command:
                summary["command"] = redact(command[:200])
            events.append(summary)
    return events


def _record_observed_events(
    store: SqliteRelayStore,
    writer: EventLogWriter,
    response: AgentResponse,
    parent_run_id: str,
) -> tuple[str, ...]:
    ids: list[str] = []
    for event in _observed_tool_events(response.output):
        tool_run = ToolRun(
            parent_run_id=parent_run_id,
            tool=f"harness.{event['type']}",
            arguments={k: v for k, v in event.items() if k != "type"},
            status=RunStatus.SUCCEEDED,
        )
        saved = store.save_model(tool_run)
        ids.append(saved.id)
        writer.record(
            EventLogEntry(
                type=EventType.TOOL_COMPLETED,
                content=f"harness reported {event['type']} ({event['id']})",
                references=[f"run:{parent_run_id}", f"tool_run:{saved.id}"],
            )
        )
    return tuple(ids)


def _worktree_is_clean(root) -> bool:
    """Refuse builds whose *tracked* content diverges from HEAD.

    Untracked files are deliberately ignored: they can't corrupt a
    ``HEAD``-relative diff baseline (``relay init`` artifacts live there),
    while modified/staged tracked files absolutely do.
    """
    result = subprocess.run(
        ["git", "-C", str(root), "status", "--porcelain", "--untracked-files=no"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise BuildRefusal(f"cannot verify git status in {root}: {result.stderr.strip()}")
    return not result.stdout.strip()


def _extract_diff_via_gate(gate: PermissionGate, root, agent_name: str, task_id: str) -> str:
    """Relay-owned post-run diff through the single permission path (A.4).

    New files the harness created are untracked and invisible to
    ``git diff HEAD`` until recorded; ``git add -N`` (intent-to-add) lists
    them without staging content. Relay-owned artifacts (.relay/, relay.yaml)
    are excluded from that recording so store bytes never enter the diff.
    """
    decision = gate.check(
        ToolRequest(
            action=Action.READ_FILES,
            agent="relay",
            task_id=task_id,
            reason="post-run repository diff extraction (compensating control)",
        )
    )
    if decision.outcome != "allow":
        raise BuildRefusal(
            f"diff extraction refused by policy: {decision.action.value} -> {decision.outcome}"
        )

    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "add",
            "--intent-to-add",
            "--all",
            "--",
            ".",
            ":(exclude).relay",
            ":(exclude)relay.yaml",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    result = subprocess.run(
        ["git", "-C", str(root), "diff", "HEAD", "--binary"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise BuildRefusal(f"git diff failed: {result.stderr.strip()}")
    return result.stdout


async def run_build(
    store: SqliteRelayStore,
    writer: EventLogWriter,
    evidence: EvidenceStore,
    agent: Agent,
    request: AgentRequest,
    *,
    workspace_root,
    gate: PermissionGate | None = None,
    model: str | None = None,
    agent_name: str | None = None,
) -> BuildOutcome:
    """One implementation run under a task; closes acceptance gate G2."""
    gate = gate or PermissionGate()
    if request.task_id is None:
        raise BuildRefusal("build runs are task-scoped: provide request.task_id")

    task = store.load_model(Task, request.task_id)
    if task is None:
        raise BuildRefusal(f"task '{request.task_id}' does not exist")

    outcome = await run_ask(store, writer, agent, request, model=model, agent_name=agent_name)
    if outcome.response is None:
        return BuildOutcome(task=task, ask=outcome)

    response = outcome.response

    # Observed harness events → ToolRun rows (observability tier only).
    tool_run_ids = _record_observed_events(store, writer, response, outcome.run.id)

    # Relay-owned diff extraction as DIFF artifact (compensating control).
    diff_text = _extract_diff_via_gate(gate, workspace_root, agent.name, task.id)
    diff_artifact_id: str | None = None
    if diff_text.strip():
        with store.transaction():
            artifact = store.save_model(
                Artifact(kind=ArtifactKind.DIFF, run_id=outcome.run.id, task_id=task.id, content=diff_text)
            )
            writer.record(
                EventLogEntry(
                    type=EventType.ARTIFACT_CREATED,
                    content="diff extracted from workspace after build",
                    references=[f"run:{outcome.run.id}", f"artifact:{artifact.id}", f"task:{task.id}"],
                )
            )
        diff_artifact_id = artifact.id

    # IMPLEMENTATION_PRODUCED with run provenance (A.1: claims ≠ proof).
    evidence.record(
        EvidenceRecord(
            kind=EvidenceKind.IMPLEMENTATION_PRODUCED,
            task_id=task.id,
            run_id=outcome.run.id,
            produced_by=f"agent:{agent.name}",
        )
    )
    writer.record(
        EventLogEntry(
            type=EventType.EVIDENCE_RECORDED,
            content=f"{EvidenceKind.IMPLEMENTATION_PRODUCED.value} recorded for task",
            references=[f"task:{task.id}", f"run:{outcome.run.id}"],
        )
    )

    return BuildOutcome(task=task, ask=outcome, diff_artifact_id=diff_artifact_id, tool_run_ids=tool_run_ids)

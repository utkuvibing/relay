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
implementation run, then closes gate G2: adapter-normalized tool
observations land as ToolRun rows; Relay extracts the diff itself from a
pre-run baseline (non-mutating) as a DIFF artifact;
IMPLEMENTATION_PRODUCED evidence is recorded with run provenance only when
a workspace change was actually produced.

Core never sees provider event vocabulary: adapters expose normalized
:class:`~relay.agents.base.ToolObservation` values on ``AgentResponse``
(App. C.1 — vendor JSONL parsing lives only inside each adapter).
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass

from relay.agents.base import (
    Agent,
    AgentRequest,
    AgentResponse,
    BackendType,
    RunObservation,
)
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
        # Task linkage is first-class whenever the request carries one
        # (P2.2 builds are task-scoped; P1 asks have none).
        task_id=request.task_id,
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


def _record_observed_events(
    store: SqliteRelayStore,
    writer: EventLogWriter,
    response: AgentResponse,
    parent_run_id: str,
) -> tuple[str, ...]:
    """Persist adapter-normalized tool observations as ToolRun rows.

    Observability tier only (App. C.5/C.7): these are claim-bearing records —
    never enforcement claims or state-transition authority. Core understands
    only the neutral ToolObservation shape, no provider vocabulary.
    """
    ids: list[str] = []
    for obs in response.tool_observations:
        arguments: dict[str, object] = {"summary": redact(obs.summary[:200])}
        if obs.command:
            arguments["command"] = redact(obs.command[:200])
        tool_run = ToolRun(
            parent_run_id=parent_run_id,
            tool=obs.kind,
            arguments=arguments,
            status=RunStatus.SUCCEEDED,
        )
        saved = store.save_model(tool_run)
        ids.append(saved.id)
        writer.record(
            EventLogEntry(
                type=EventType.TOOL_COMPLETED,
                content=f"harness reported {obs.kind}: {redact(obs.summary[:120])}",
                references=[f"run:{parent_run_id}", f"tool_run:{saved.id}"],
            )
        )
    return tuple(ids)


def _worktree_is_clean(root) -> bool:
    """Refuse builds whose *tracked* content diverges from HEAD.

    Untracked files are deliberately ignored here (they cannot corrupt a
    HEAD-relative baseline check; ``relay init`` artifacts live untracked).
    Provenance for them is handled by the baseline snapshot instead.
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


def _capture_baseline(root) -> dict[str, bytes]:
    """Snapshot every working-tree file Relay could later attribute to a run.

    Bounded: skips the Relay store dir and .git. Used as the provenance
    baseline so pre-existing files are never attributed to the harness.
    """
    baseline: dict[str, bytes] = {}
    git_dir = root / ".git"
    relay_dir = root / ".relay"
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if any(part in (".git", ".relay") for part in rel.parts):
            continue
        try:
            if path.stat().st_size > _BASELINE_FILE_CAP_BYTES:
                continue
            baseline[str(rel).replace("\\", "/")] = path.read_bytes()
        except OSError:
            continue
    del git_dir, relay_dir  # documentation-only locals
    return baseline


def _diff_against_baseline(gate: PermissionGate, root, task_id: str) -> str:
    """Relay-owned diff vs pre-run baseline through the single gate path (A.4).

    Non-mutating: no git index/HEAD changes at all. Only files that differ
    from the captured baseline are attributed to this run. Binary-safe via
    literal ``diff --git``-style textual patch construction over UTF-8 text
    with lossy fallback markers for binary content.
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

    baseline = _workdir_state.get("baseline") or {}
    current_files = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        parts = rel.parts
        if any(part in (".git", ".relay", "node_modules", "__pycache__") for part in parts):
            continue
        try:
            if path.stat().st_size > _BASELINE_FILE_CAP_BYTES:
                continue
            current_files[str(rel).replace("\\", "/")] = path.read_bytes()
        except OSError:
            continue

    changed_paths: set[str] = set()
    for name, before in baseline.items():
        after = current_files.get(name)
        if after != before:
            changed_paths.add(name)
    for name in set(current_files) - set(baseline):
        changed_paths.add(name)

    lines: list[str] = []
    for name in sorted(changed_paths):
        before = baseline.get(name)
        after = current_files.get(name)

        def _text(blob: bytes | None) -> str:
            return blob.decode("utf-8", errors="replace") if blob is not None else ""

        is_binary_before = (
            before is not None and b"\x00" in before[: _BINARY_SNIFF_BYTES]
        )
        is_binary_after = after is not None and b"\x00" in after[: _BINARY_SNIFF_BYTES]
        if is_binary_before or is_binary_after:
            lines.append(f"Binary files {name} differ")
            continue

        before_text = _text(before).splitlines(keepends=True)
        after_text = _text(after).splitlines(keepends=True)
        if before is None:
            lines.append(f"new file: {name}")
        elif after is None:
            lines.append(f"deleted file: {name}")
        else:
            lines.append(f"modified: {name}")

        import difflib

        for diff_line in difflib.unified_diff(
            before_text, after_text, fromfile=f"a/{name}", tofile=f"b/{name}", lineterm=""
        ):
            lines.append(diff_line.rstrip("\n"))
    return "\n".join(lines)


# Baseline handoff between the pre-run capture and post-run comparison.
_workdir_state: dict[str, object] = {}

_BASELINE_FILE_CAP_BYTES = 4 * 1024 * 1024
_BINARY_SNIFF_BYTES = 8000


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

    # Blocker 4: a build must carry an implementation-capable grant BEFORE
    # any process spawns. READ_ONLY_ACCESS can never implement anything.
    from relay.harness.runtime import HarnessAgent
    from relay.harness.types import ExecutionGrantKind

    if not isinstance(agent, HarnessAgent):
        raise BuildRefusal("build requires a harness-backed implementer")
    profile_grant = agent._profile.grant if agent._profile is not None else None
    effective = profile_grant or agent.default_grant
    if effective is None:
        raise BuildRefusal("build requires an ExecutionGrant; none resolvable")
    if effective is ExecutionGrantKind.READ_ONLY_ACCESS:
        raise BuildRefusal(
            "build requires at least 'workspace_write' — "
            f"configured grant '{effective.value}' cannot implement changes"
        )

    # Blocker 2 provenance baseline: snapshot BEFORE any harness I/O so
    # pre-existing files are never attributed to the run.
    _workdir_state["baseline"] = _capture_baseline(workspace_root)

    outcome = await run_ask(store, writer, agent, request, model=model, agent_name=agent_name)
    if outcome.response is None:
        return BuildOutcome(task=task, ask=outcome)

    response = outcome.response

    # Adapter-normalized tool observations → ToolRun rows (observability only).
    tool_run_ids = _record_observed_events(store, writer, response, outcome.run.id)

    # Relay-owned non-mutating diff extraction as DIFF artifact.
    diff_text = _diff_against_baseline(gate, workspace_root, task.id)
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

    # IMPLEMENTATION_PRODUCED only when the run actually produced changes
    # (Blocker 4): a no-op build mints nothing.
    if diff_text.strip():
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

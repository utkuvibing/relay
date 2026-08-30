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

``run_build`` (P2.2b, lifecycle-wired in P3.1) reuses this crash-safe spine
for a task-scoped flow and closes gate G2: the deterministic task state
machine (``relay.core.state_machine``) owns the task from ``CREATED`` —
Relay collects context (``relay:core``), a READ_ONLY planning run mints the
canonical ``ArtifactKind.PLAN`` (``PLAN_PRODUCED`` with that run's
``run_id``), the standalone build's explicit human initiation is the D.3
implicit freeze onto ``IMPLEMENTING``, and the implementation run's Relay-
extracted diff backs ``IMPLEMENTATION_PRODUCED``. Every edge is validated
against the ``EvidenceStore`` — a model claiming "done" moves nothing
(SPEC §27 Phase 3 exit gate; App. A.1). Adapter-normalized tool
observations land as ToolRun rows; Relay extracts the diff itself from a
pre-run baseline (non-mutating) as a DIFF artifact.

Core never sees provider event vocabulary: adapters expose normalized
:class:`~relay.agents.base.ToolObservation` values on ``AgentResponse``
(App. C.1 — vendor JSONL parsing lives only inside each adapter).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from relay.agents.base import (
    Agent,
    AgentRequest,
    AgentResponse,
    AgentRole,
    BackendType,
    RunObservation,
)
from relay.agents.errors import AgentError
from relay.context.config import ApprovalPolicyConfig, VerificationConfig
from relay.core.evidence import EvidenceKind, EvidenceStore
from relay.core.permissions import Action, PermissionGate, ToolRequest
from relay.core.state_machine import TaskState, TaskStateMachine
from relay.harness.sanitization import redact
from relay.storage.events import EventLogWriter
from relay.storage.models import (
    Approval,
    ApprovalStatus,
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
    pre_provider: Callable[[Run, Artifact], Iterable[EventLogEntry]] | None = None,
) -> AskOutcome:
    """Execute one agent run with crash-safe persistence.

    Provider-I/O failures never raise: they become durable run history
    (``AskOutcome`` carrying the FAILED run and a sanitized error) — the
    spine owns them.

    P4.2 pre-provider seam (frozen plan D14): when ``pre_provider`` is
    supplied, it is invoked INSIDE Tx1 after the run row and the
    ``run_input`` artifact are staged; its returned entries commit in the
    same ``BEGIN IMMEDIATE`` transaction (the delivery binding marker). The
    hook may raise to VETO: the exception propagates through the transaction
    context — atomic rollback, nothing persisted, typed refusal at the
    caller. Hook exceptions are CALLER-authored refusals and intentionally
    may propagate; they never become run history. Default ``None`` keeps
    this spine byte-identical for every existing caller.
    """
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
        if pre_provider is not None:
            for entry in pre_provider(run, input_artifact):
                writer.record(entry)

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

        is_binary_before = before is not None and b"\x00" in before[:_BINARY_SNIFF_BYTES]
        is_binary_after = after is not None and b"\x00" in after[:_BINARY_SNIFF_BYTES]
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


# ---------------------------------------------------------------------------
# P3.1 — deterministic task lifecycle (SPEC §6, §27 Phase 3, App. A.1, D.3)
# ---------------------------------------------------------------------------

_PLAN_DIRECTIVE = (
    "You are the planner. Produce a concise, executable implementation plan "
    "for the task below. Output only the plan as markdown with the sections: "
    "Goal, Steps, Files, Verification.\n\nTASK:\n{prompt}"
)

_IMPLEMENT_DIRECTIVE = (
    "Implement the following accepted plan exactly.\n\n"
    "ACCEPTED PLAN:\n{plan}\n\nORIGINAL REQUEST:\n{prompt}"
)


def advance_task(
    machine: TaskStateMachine,
    store: SqliteRelayStore,
    writer: EventLogWriter,
    task: Task,
    target: TaskState,
    *,
    evidence_store: EvidenceStore | None = None,
    created_approval: Approval | None = None,
    updated_approval: Approval | None = None,
    evidence_records: tuple[EvidenceRecord, ...] = (),
    events: tuple[EventLogEntry, ...] = (),
) -> Task:
    """One persisted lifecycle transition — validate, then persist ATOMICALLY.

    The machine validates the edge against the ``EvidenceStore``; only a
    granted transition writes. Companion records a completion boundary
    demands (a created/updated approval row, boundary evidence, their
    events) commit in the SAME ``BEGIN IMMEDIATE`` transaction as the
    ``Task.state`` update and the ``STATE_TRANSITIONED`` event: a crash or
    exception can never leave committed approval/evidence on the old side
    of a transition (P3 hardening). Companion evidence is written BEFORE
    the machine validates, inside the transaction, so gates that demand
    the very evidence the boundary is minting (e.g. ``APPROVAL_GRANTED``
    on the ``APPROVAL_REQUIRED -> DONE`` edge) validate against it.
    No caller-supplied enum ever carries transition authority (App. A.1).
    """
    if evidence_records and evidence_store is None:
        raise ValueError("evidence_records require evidence_store")
    with store.transaction():
        if created_approval is not None:
            store.save_model(created_approval)
        if updated_approval is not None:
            store.update_model(updated_approval)
        if evidence_store is not None and evidence_records:
            for record in evidence_records:
                evidence_store.record(record)
        previous = machine.state
        machine.transition(target)
        updated = task.model_copy(update={"state": target})
        store.update_model(updated)
        for entry in events:
            writer.record(entry)
        writer.record(
            EventLogEntry(
                type=EventType.STATE_TRANSITIONED,
                content=f"task state: {previous.value} -> {target.value}",
                references=[f"task:{task.id}"],
            )
        )
    return updated


def _collect_context(
    store: SqliteRelayStore,
    writer: EventLogWriter,
    evidence: EvidenceStore,
    task: Task,
    workspace_root,
) -> Task:
    """Relay-collected workspace context backing ``CREATED→CONTEXT_READY``.

    The brief is real discovered repository fact (SPEC §13 profile: languages,
    frameworks, instruction files, test suites) persisted as a RESEARCH
    artifact the evidence record points at — never a caller-supplied claim.
    """
    from relay.context.workspace import discover_profile

    profile = discover_profile(workspace_root)
    brief = (
        "# Workspace context (relay:core)\n\n"
        f"- languages: {', '.join(profile.languages) or '(none detected)'}\n"
        f"- frameworks: {', '.join(profile.frameworks) or '(none detected)'}\n"
        f"- package managers: {', '.join(profile.package_managers) or '(none detected)'}\n"
        f"- instruction files: {', '.join(profile.instructions) or '(none)'}\n"
        f"- test suites: {json.dumps(profile.tests, sort_keys=True) if profile.tests else '(none detected)'}\n"
        f"- default branch: {profile.default_branch}\n"
    )
    with store.transaction():
        artifact = store.save_model(
            Artifact(kind=ArtifactKind.REPORT, task_id=task.id, content=brief)
        )
        evidence.record(
            EvidenceRecord(
                kind=EvidenceKind.CONTEXT_COLLECTED,
                task_id=task.id,
                produced_by="relay:core",
                artifact_id=artifact.id,
            )
        )
        writer.record(
            EventLogEntry(
                type=EventType.EVIDENCE_RECORDED,
                content=f"{EvidenceKind.CONTEXT_COLLECTED.value} recorded for task",
                references=[f"task:{task.id}", f"artifact:{artifact.id}"],
            )
        )
    return task


def _freeze_plan(
    store: SqliteRelayStore,
    writer: EventLogWriter,
    evidence: EvidenceStore,
    task: Task,
    plan_outcome: AskOutcome,
) -> Task:
    """Mint the canonical ``ArtifactKind.PLAN`` + ``PLAN_PRODUCED`` evidence.

    Provenance is honest by construction: the plan artifact and the evidence
    record both point at the planning run that produced them (App. A.1).
    """
    plan_text = plan_outcome.response.output if plan_outcome.response else ""
    with store.transaction():
        artifact = store.save_model(
            Artifact(
                kind=ArtifactKind.PLAN,
                run_id=plan_outcome.run.id,
                task_id=task.id,
                content=plan_text,
            )
        )
        evidence.record(
            EvidenceRecord(
                kind=EvidenceKind.PLAN_PRODUCED,
                task_id=task.id,
                run_id=plan_outcome.run.id,
                artifact_id=artifact.id,
                produced_by=f"agent:{plan_outcome.run.agent}",
            )
        )
        writer.record(
            EventLogEntry(
                type=EventType.ARTIFACT_CREATED,
                content="canonical plan artifact minted",
                references=[
                    f"run:{plan_outcome.run.id}",
                    f"artifact:{artifact.id}",
                    f"task:{task.id}",
                ],
            )
        )
        writer.record(
            EventLogEntry(
                type=EventType.EVIDENCE_RECORDED,
                content=f"{EvidenceKind.PLAN_PRODUCED.value} recorded for task",
                references=[f"task:{task.id}", f"run:{plan_outcome.run.id}"],
            )
        )
    return task


def _planner_for(agent: Agent) -> Agent:
    """Same adapter bound to a READ_ONLY grant for the planning run (D3).

    Mirrors the existing ``agent._profile`` access precedent: the planner is
    the configured implementer's own adapter downgraded to READ_ONLY — plans
    are authored without any write capability. API-backed agents (which
    cannot reach this code path — build refuses them earlier) fall through
    unchanged.
    """
    from relay.harness.runtime import HarnessAgent
    from relay.harness.types import ExecutionGrantKind

    if not isinstance(agent, HarnessAgent):
        return agent
    profile = None
    if agent._profile is not None:
        profile = agent._profile.model_copy(update={"grant": ExecutionGrantKind.READ_ONLY_ACCESS})
    return type(agent)(
        settings=agent._settings,
        profile=profile,
        workspace_root=agent._workspace_root,
    )


# ---------------------------------------------------------------------------
# P3.2 — Relay-scoped verification (SPEC §27 Phase 3 exit gate, App. A.1)
# ---------------------------------------------------------------------------

_VERIFICATION_OUTPUT_CAP_CHARS = 20_000


async def _run_verification(
    store: SqliteRelayStore,
    writer: EventLogWriter,
    evidence: EvidenceStore,
    gate: PermissionGate,
    machine: TaskStateMachine,
    task: Task,
    verification: VerificationConfig | None,
    workspace_root,
) -> Task:
    """Relay grades the exam — the implementer never does (frozen plan Q-c).

    The configured command executes as a Relay-owned ToolRun (no agent
    parent) through the permission gate with a stripped, baseline child
    environment and a bounded timeout. Dispositions (frozen-plan D3):

    * absent config → stay blocked in ``VERIFYING`` (the gap is queryable);
    * exit 0 → ``TESTS_PASSED`` (``tool_run_id`` + artifact provenance)
      → ``REVIEWING``;
    * non-zero exit → ``TEST_RESULT`` artifact → rework to ``IMPLEMENTING``;
    * could-not-execute / timeout / gate refusal → blocked in ``VERIFYING``
      with the reason on the ToolRun row — "Relay could not run the exam"
      is never recorded as "the tests failed".

    Exit code is the only verdict; output is persisted (capped, redacted)
    for humans, never parsed.
    """
    if verification is None:
        return task

    from relay.harness.env_policy import DEFAULT_CONFLICT_VARIABLES, build_child_env
    from relay.harness.process import LaunchSpec
    from relay.harness.process import execute as execute_process

    # Policy first (D4): the same single gate path as every Relay execution.
    decision = gate.check(
        ToolRequest(
            action=Action.RUN_TESTS,
            agent="relay",
            task_id=task.id,
            reason="relay-scoped verification of the implemented plan (P3.2)",
        )
    )
    resolved = shutil.which(verification.program)
    blocked_reason: str | None = None
    if decision.outcome != "allow":
        blocked_reason = (
            f"verification refused by policy: {decision.action.value} -> {decision.outcome}"
        )
    elif resolved is None:
        blocked_reason = f"verification program {verification.program!r} was not found on PATH"

    tool_run = ToolRun(
        parent_run_id=None,  # Relay-owned: no agent run triggered this (D5)
        tool="verification",
        arguments={"program": verification.program, "args": list(verification.args)},
        status=RunStatus.RUNNING,
    )
    with store.transaction():
        store.save_model(tool_run)
        writer.record(
            EventLogEntry(
                type=EventType.TOOL_REQUESTED,
                content=f"verification requested: {verification.program}",
                references=[f"task:{task.id}", f"tool_run:{tool_run.id}"],
            )
        )

    def _finalize(row_status: RunStatus, error: str | None, result_ref: str | None) -> None:
        finished = tool_run.model_copy(
            update={
                "status": row_status,
                "ended_at": utcnow(),
                "error": error,
                "result_ref": result_ref,
            }
        )
        store.update_model(finished)
        writer.record(
            EventLogEntry(
                type=EventType.TOOL_COMPLETED,
                content=(
                    f"verification {row_status.value}"
                    + (f": {redact(error[:160])}" if error else "")
                ),
                references=[f"task:{task.id}", f"tool_run:{tool_run.id}"],
            )
        )

    if blocked_reason is not None:
        with store.transaction():
            _finalize(RunStatus.FAILED, blocked_reason, None)
        return task  # blocked in VERIFYING — never a minted verdict

    try:
        outcome = await execute_process(
            LaunchSpec(
                argv=(resolved, *verification.args),
                cwd=workspace_root,
                env=build_child_env(
                    dict(os.environ),
                    conflict_variables=DEFAULT_CONFLICT_VARIABLES,
                ),
                timeout_s=verification.timeout_seconds,
            )
        )
    except OSError as exc:
        with store.transaction():
            _finalize(
                RunStatus.FAILED,
                f"verification could not execute: {type(exc).__name__}",
                None,
            )
        return task

    output = redact(
        f"exit={outcome.exit_code} duration={outcome.duration_s:.2f}s\n"
        f"--- stdout ---\n{outcome.stdout.text}\n--- stderr ---\n{outcome.stderr.text}"
    )[:_VERIFICATION_OUTPUT_CAP_CHARS]

    if outcome.timed_out or outcome.exit_code is None:
        with store.transaction():
            _finalize(
                RunStatus.FAILED,
                "verification exceeded its bounded deadline — tree terminated",
                None,
            )
        return task

    with store.transaction():
        artifact = store.save_model(
            Artifact(kind=ArtifactKind.TEST_RESULT, task_id=task.id, content=output)
        )
        writer.record(
            EventLogEntry(
                type=EventType.ARTIFACT_CREATED,
                content="verification output persisted as TEST_RESULT",
                references=[f"task:{task.id}", f"artifact:{artifact.id}"],
            )
        )

    if outcome.exit_code == 0:
        with store.transaction():
            _finalize(RunStatus.SUCCEEDED, None, artifact.id)
            evidence.record(
                EvidenceRecord(
                    kind=EvidenceKind.TESTS_PASSED,
                    task_id=task.id,
                    tool_run_id=tool_run.id,
                    artifact_id=artifact.id,
                    produced_by="relay:verification",
                )
            )
            writer.record(
                EventLogEntry(
                    type=EventType.EVIDENCE_RECORDED,
                    content=f"{EvidenceKind.TESTS_PASSED.value} recorded for task",
                    references=[f"task:{task.id}", f"tool_run:{tool_run.id}"],
                )
            )
        return advance_task(machine, store, writer, task, TaskState.REVIEWING)

    with store.transaction():
        _finalize(RunStatus.FAILED, None, artifact.id)
    return advance_task(machine, store, writer, task, TaskState.IMPLEMENTING)


# ---------------------------------------------------------------------------
# P3.3 — review + approval closure (SPEC §27 Phase 3, App. A.1/A.3, Q-d/Q-e)
# ---------------------------------------------------------------------------

_REVIEW_DIRECTIVE = (
    "You are the reviewer. Review the implemented changes against the "
    "accepted plan below. Assess correctness, completeness, and risks.\n"
    "End your output with EXACTLY one final line:\n"
    "VERDICT: PASS\n"
    "or\n"
    "VERDICT: FINDINGS\n"
    "(a garbled or missing verdict is treated as FINDINGS).\n\n"
    "ACCEPTED PLAN:\n{plan}\n\nIMPLEMENTED DIFF:\n{diff}\n\n"
    "ORIGINAL REQUEST:\n{prompt}"
)

_VERDICT_PATTERN = re.compile(r"verdict:\s*(pass|findings)", re.IGNORECASE)


def _parse_verdict(text: str) -> str:
    """Fail-closed verdict extraction (frozen-plan D2): the last verdict
    marker wins; missing or garbled markers are FINDINGS — the machine never
    guesses a pass from an agent's prose."""
    matches = _VERDICT_PATTERN.findall(text)
    return matches[-1].lower() if matches else "findings"


async def _run_review(
    store: SqliteRelayStore,
    writer: EventLogWriter,
    evidence: EvidenceStore,
    machine: TaskStateMachine,
    task: Task,
    reviewer: Agent,
    request: AgentRequest,
    plan_outcome: AskOutcome,
    diff_text: str,
    *,
    model: str | None = None,
    agent_name: str | None = None,
) -> Task:
    """One review run against the frozen plan + the Relay-extracted diff.

    Returns the task at ``REVIEWING`` with ``REVIEW_PASSED`` recorded (the
    caller decides gated vs direct closure), or advanced to ``IMPLEMENTING``
    on findings. A failed reviewer run leaves the task honestly blocked at
    ``REVIEWING`` (D8) — no verdict is ever invented.
    """
    review_request = request.model_copy(
        update={
            "role": AgentRole.REVIEWER,
            "prompt": _REVIEW_DIRECTIVE.format(
                plan=plan_outcome.response.output,
                diff=diff_text,
                prompt=request.prompt,
            ),
        }
    )
    review_outcome = await run_ask(
        store, writer, reviewer, review_request, model=model, agent_name=agent_name
    )
    if review_outcome.response is None:
        return task

    review_text = review_outcome.response.output
    verdict = _parse_verdict(review_text)
    with store.transaction():
        artifact = store.save_model(
            Artifact(
                kind=ArtifactKind.REVIEW_FINDING,
                run_id=review_outcome.run.id,
                task_id=task.id,
                content=review_text,
            )
        )
        writer.record(
            EventLogEntry(
                type=EventType.ARTIFACT_CREATED,
                content="review output persisted as REVIEW_FINDING",
                references=[
                    f"run:{review_outcome.run.id}",
                    f"artifact:{artifact.id}",
                    f"task:{task.id}",
                ],
            )
        )

    if verdict != "pass":
        return advance_task(machine, store, writer, task, TaskState.IMPLEMENTING)

    with store.transaction():
        evidence.record(
            EvidenceRecord(
                kind=EvidenceKind.REVIEW_PASSED,
                task_id=task.id,
                run_id=review_outcome.run.id,
                artifact_id=artifact.id,
                produced_by=f"agent:{review_outcome.run.agent}",
            )
        )
        writer.record(
            EventLogEntry(
                type=EventType.EVIDENCE_RECORDED,
                content=f"{EvidenceKind.REVIEW_PASSED.value} recorded for task",
                references=[f"task:{task.id}", f"run:{review_outcome.run.id}"],
            )
        )
    return task


def _open_approval_gate(task: Task) -> tuple[Approval, EventLogEntry]:
    """Build the human gate records: one PENDING approval + its request event.

    ``action=EDIT_FILES`` carries the vocabulary wrinkle surfaced at freeze:
    the Action enum has no task-completion member, so the approval targets
    the changes with the reason carrying the specifics (frozen-plan veto
    item 1). The records are PERSISTED BY :func:`advance_task` — created in
    the same transaction as the ``REVIEWING -> APPROVAL_REQUIRED``
    transition (P3 hardening: a committed approval can never outlive a
    failed transition).
    """
    approval = Approval(
        action=Action.EDIT_FILES,
        task_id=task.id,
        requested_by="relay:review",
        reason=f"task completion approval — {task.title[:120]}",
        status=ApprovalStatus.PENDING,
    )
    event = EventLogEntry(
        type=EventType.APPROVAL_REQUESTED,
        content="task completion approval requested",
        references=[f"task:{task.id}", f"approval:{approval.id}"],
    )
    return approval, event


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
    verification: VerificationConfig | None = None,
    reviewer: Agent | None = None,
    reviewer_name: str | None = None,
    approval: ApprovalPolicyConfig | None = None,
) -> BuildOutcome:
    """Drive one task through the deterministic lifecycle to closure.

    P3.1 wiring (SPEC §27 Phase 3): the task state machine owns the task
    from ``CREATED`` — context collection, a READ_ONLY planning run minting
    the canonical plan artifact, the D.3 implicit freeze (standalone build =
    explicit human initiation), then the implementation run. Every edge is
    validated against the ``EvidenceStore``; a run that produces no workspace
    change leaves the task honestly blocked at ``IMPLEMENTING`` (claims never
    close tasks). Closes acceptance gate G2 along the way.

    P3.2 chains Relay-scoped verification after ``IMPLEMENTED``; P3.3 chains
    the review run and lands the task at ``APPROVAL_REQUIRED`` (gated
    default) or ``DONE`` (``approval: {mode: direct}`` policy opt-out).
    """
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

    # P3.1: the deterministic machine owns the task from here on. A task
    # whose lifecycle already started is refused (resumability is a later
    # seam; guessing at mid-flight state would transfer authority back to
    # callers).
    if task.state is not TaskState.CREATED:
        raise BuildRefusal(
            f"task '{task.id}' is at state '{task.state.value}' — "
            "lifecycle already started; a fresh build creates a fresh task"
        )
    machine = TaskStateMachine(task_id=task.id, store=evidence)

    # 1. Context collection (relay:core) → CONTEXT_READY.
    task = _collect_context(store, writer, evidence, task, workspace_root)
    task = advance_task(machine, store, writer, task, TaskState.CONTEXT_READY)

    # 2. Planning run (same adapter, READ_ONLY) → canonical plan → PLAN_READY.
    plan_request = request.model_copy(
        update={
            "role": AgentRole.PLANNER,
            "prompt": _PLAN_DIRECTIVE.format(prompt=request.prompt),
        }
    )
    plan_outcome = await run_ask(
        store, writer, _planner_for(agent), plan_request, model=model, agent_name=agent_name
    )
    if plan_outcome.response is None or not plan_outcome.response.output.strip():
        # Planning failed or produced nothing usable: the task stays honestly
        # blocked at CONTEXT_READY (no plan evidence, no implementation).
        return BuildOutcome(task=task, ask=plan_outcome)
    task = _freeze_plan(store, writer, evidence, task, plan_outcome)
    task = advance_task(machine, store, writer, task, TaskState.PLAN_READY)

    # 3. Implicit freeze (App. D.3): a standalone `relay build` IS the
    # human's explicit initiation — the PLAN_READY→IMPLEMENTING edge.
    task = advance_task(machine, store, writer, task, TaskState.IMPLEMENTING)

    # 4. Implementation run — the prompt is the frozen plan artifact content,
    # not the raw prompt (App. D.3: downstream agents operate against the
    # canonical plan).
    implement_request = request.model_copy(
        update={
            "prompt": _IMPLEMENT_DIRECTIVE.format(
                plan=plan_outcome.response.output, prompt=request.prompt
            )
        }
    )

    # Blocker 2 provenance baseline: snapshot BEFORE implement-run I/O so
    # pre-existing files are never attributed to the run. (The plan run is
    # READ_ONLY; anything it left behind is pre-existing by definition.)
    _workdir_state["baseline"] = _capture_baseline(workspace_root)

    outcome = await run_ask(
        store, writer, agent, implement_request, model=model, agent_name=agent_name
    )
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
                Artifact(
                    kind=ArtifactKind.DIFF,
                    run_id=outcome.run.id,
                    task_id=task.id,
                    content=diff_text,
                )
            )
            writer.record(
                EventLogEntry(
                    type=EventType.ARTIFACT_CREATED,
                    content="diff extracted from workspace after build",
                    references=[
                        f"run:{outcome.run.id}",
                        f"artifact:{artifact.id}",
                        f"task:{task.id}",
                    ],
                )
            )
        diff_artifact_id = artifact.id

    # IMPLEMENTATION_PRODUCED only when the run actually produced changes
    # (Blocker 4): a no-op build mints nothing — and the machine therefore
    # keeps the task at IMPLEMENTING (§27 Phase 3 exit gate).
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
        task = advance_task(machine, store, writer, task, TaskState.IMPLEMENTED)

        # P3.2: auto-chained Relay-scoped verification (frozen plan Q-f) —
        # endings: REVIEWING (pass), IMPLEMENTING (test-failure rework), or
        # blocked in VERIFYING (no config / could-not-execute / timeout).
        task = advance_task(machine, store, writer, task, TaskState.VERIFYING)
        task = await _run_verification(
            store, writer, evidence, gate, machine, task, verification, workspace_root
        )

        # P3.3: review + closure (frozen plan Q-d/Q-e) — only reachable when
        # verification passed (task at REVIEWING with TESTS_PASSED on record).
        if task.state is TaskState.REVIEWING:
            task = await _run_review(
                store,
                writer,
                evidence,
                machine,
                task,
                reviewer or _planner_for(agent),
                request,
                plan_outcome,
                diff_text,
                model=model,
                agent_name=reviewer_name or agent_name,
            )
            # D8: a failed reviewer run leaves the task at REVIEWING with no
            # verdict evidence — store truth decides, never the state alone.
            review_passed = any(
                record.kind is EvidenceKind.REVIEW_PASSED
                for record in evidence.records_for_task(task.id, kind=EvidenceKind.REVIEW_PASSED)
            )
            if review_passed:
                if approval is not None and approval.mode == "direct":
                    # A.3 opt-out: Relay attests the empty-by-construction
                    # queue (no approval row is ever created in direct mode).
                    # Hardening: evidence + event + the DONE transition commit
                    # atomically — no stranded attestation on a REVIEWING task.
                    task = advance_task(
                        machine,
                        store,
                        writer,
                        task,
                        TaskState.DONE,
                        evidence_store=evidence,
                        evidence_records=(
                            EvidenceRecord(
                                kind=EvidenceKind.NO_PENDING_APPROVALS,
                                task_id=task.id,
                                produced_by="relay:review",
                            ),
                        ),
                        events=(
                            EventLogEntry(
                                type=EventType.EVIDENCE_RECORDED,
                                content=(
                                    f"{EvidenceKind.NO_PENDING_APPROVALS.value} recorded for task"
                                ),
                                references=[f"task:{task.id}"],
                            ),
                        ),
                    )
                else:
                    # Hardening: the PENDING approval row and its request event
                    # commit in the SAME transaction as the gate transition —
                    # a crash cannot open a gate the task never reached.
                    approval_row, approval_event = _open_approval_gate(task)
                    task = advance_task(
                        machine,
                        store,
                        writer,
                        task,
                        TaskState.APPROVAL_REQUIRED,
                        created_approval=approval_row,
                        events=(approval_event,),
                    )

    return BuildOutcome(
        task=task, ask=outcome, diff_artifact_id=diff_artifact_id, tool_run_ids=tool_run_ids
    )

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

import enum
import json
import os
import shutil
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

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
from relay.core.reviews import (
    ReviewContractError,
    ReviewInputs,
    build_fix_packet,
    build_review_record,
    build_review_sources,
    build_review_subject,
    encode_fix_packet,
    encode_invalid_review_diagnostic,
    encode_review_record,
    parse_review,
)
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
    InvalidReviewDiagnosticPayload,
    ReviewVerdict,
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


class ReviewDisposition(str, enum.Enum):
    """Observable outcome of one structured review stage."""

    PASSED = "passed"
    FINDINGS = "findings"
    INVALID_OUTPUT = "invalid_output"
    INVALID_CONTEXT = "invalid_context"
    RUN_FAILED = "run_failed"


@dataclass(frozen=True)
class ReviewResult:
    """Committed review-stage facts for CLI and downstream orchestration."""

    disposition: ReviewDisposition
    run_id: str | None = None
    review_artifact_id: str | None = None
    fix_packet_artifact_id: str | None = None
    diagnostic_artifact_id: str | None = None
    reason_code: str | None = None
    finding_count: int = 0


@dataclass(frozen=True)
class ReviewStageOutcome:
    task: Task
    result: ReviewResult


@dataclass(frozen=True)
class BuildOutcome:
    task: Task
    ask: AskOutcome
    diff_artifact_id: str | None = None
    tool_run_ids: tuple[str, ...] = ()
    review: ReviewResult | None = None


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


def _capture_baseline(root: Path) -> dict[str, bytes]:
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


def _diff_against_baseline(
    gate: PermissionGate, root: Path, task_id: str, baseline: dict[str, bytes]
) -> str:
    """Relay-owned diff vs pre-run baseline through the single gate path (A.4).

    Non-mutating: no git index/HEAD changes at all. Only files that differ
    from the captured baseline are attributed to this run. Binary-safe via
    literal ``diff --git``-style textual patch construction over UTF-8 text
    with lossy fallback markers for binary content.

    The baseline is execution-local — passed in by the caller, never shared
    module state — so concurrent builds cannot contaminate each other's
    provenance.
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
    current_files: dict[str, bytes] = {}
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
    artifacts: tuple[Artifact, ...] = (),
    events: tuple[EventLogEntry, ...] = (),
) -> Task:
    """One persisted lifecycle transition — validate, then persist ATOMICALLY.

    The machine validates the edge against the ``EvidenceStore``; only a
    granted transition writes. Companion records a completion boundary
    demands (a created/updated approval row, boundary artifacts/evidence,
    their events) commit in the SAME ``BEGIN IMMEDIATE`` transaction as the
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
        for artifact in artifacts:
            store.save_model(artifact)
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
    workspace_root: Path,
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
) -> Artifact:
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
    return artifact


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
    if agent.profile is not None:
        profile = agent.profile.model_copy(update={"grant": ExecutionGrantKind.READ_ONLY_ACCESS})
    return type(agent)(
        settings=agent.settings,
        profile=profile,
        workspace_root=agent.workspace_root,
    )


def _reviewer_for(agent: Agent) -> Agent:
    """Bind any harness reviewer to an explicit READ_ONLY profile.

    Unlike the frozen planner fallback, review must never inherit an
    adapter-default write grant: a missing profile becomes an explicit
    READ_ONLY profile, and an unsupported grant fails before spawn.
    """

    from relay.context.config import HarnessAgentConfig
    from relay.harness.runtime import HarnessAgent
    from relay.harness.types import ExecutionGrantKind

    if not isinstance(agent, HarnessAgent):
        return agent
    if agent.profile is not None:
        profile = agent.profile.model_copy(
            update={"grant": ExecutionGrantKind.READ_ONLY_ACCESS}
        )
    else:
        profile = HarnessAgentConfig(grant=ExecutionGrantKind.READ_ONLY_ACCESS)
    return type(agent)(
        settings=agent.settings,
        profile=profile,
        workspace_root=agent.workspace_root,
    )


# ---------------------------------------------------------------------------
# P3.2 — Relay-scoped verification (SPEC §27 Phase 3 exit gate, App. A.1)
# ---------------------------------------------------------------------------

_VERIFICATION_OUTPUT_CAP_CHARS = 20_000


@dataclass(frozen=True)
class _VerificationResult:
    """Records produced by this verification invocation, when it ran."""

    task: Task
    tool_run: ToolRun | None = None
    test_result_artifact: Artifact | None = None
    evidence_record: EvidenceRecord | None = None


async def _run_verification(
    store: SqliteRelayStore,
    writer: EventLogWriter,
    evidence: EvidenceStore,
    gate: PermissionGate,
    machine: TaskStateMachine,
    task: Task,
    verification: VerificationConfig | None,
    workspace_root: Path,
) -> _VerificationResult:
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
        return _VerificationResult(task=task)

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

    def _finalize(
        row_status: RunStatus, error: str | None, result_ref: str | None
    ) -> ToolRun:
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
        return finished

    if blocked_reason is not None:
        with store.transaction():
            finished = _finalize(RunStatus.FAILED, blocked_reason, None)
        # blocked in VERIFYING — never a minted verdict
        return _VerificationResult(task=task, tool_run=finished)

    try:
        assert resolved is not None  # blocked above when None
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
            finished = _finalize(
                RunStatus.FAILED,
                f"verification could not execute: {type(exc).__name__}",
                None,
            )
        return _VerificationResult(task=task, tool_run=finished)

    output = redact(
        f"exit={outcome.exit_code} duration={outcome.duration_s:.2f}s\n"
        f"--- stdout ---\n{outcome.stdout.text}\n--- stderr ---\n{outcome.stderr.text}"
    )[:_VERIFICATION_OUTPUT_CAP_CHARS]

    if outcome.timed_out or outcome.exit_code is None:
        with store.transaction():
            finished = _finalize(
                RunStatus.FAILED,
                "verification exceeded its bounded deadline — tree terminated",
                None,
            )
        return _VerificationResult(task=task, tool_run=finished)

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
            finished = _finalize(RunStatus.SUCCEEDED, None, artifact.id)
            evidence_record = evidence.record(
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
        updated = advance_task(machine, store, writer, task, TaskState.REVIEWING)
        return _VerificationResult(
            task=updated,
            tool_run=finished,
            test_result_artifact=artifact,
            evidence_record=evidence_record,
        )

    with store.transaction():
        finished = _finalize(RunStatus.FAILED, None, artifact.id)
    updated = advance_task(machine, store, writer, task, TaskState.IMPLEMENTING)
    return _VerificationResult(
        task=updated,
        tool_run=finished,
        test_result_artifact=artifact,
    )


# ---------------------------------------------------------------------------
# P3.3 — review + approval closure (SPEC §27 Phase 3, App. A.1/A.3, Q-d/Q-e)
# ---------------------------------------------------------------------------

_REVIEW_DIRECTIVE = (
    "You are the reviewer. Review the implemented changes against the pinned "
    "canonical inputs below. Assess correctness, completeness, and risks.\n"
    "Return exactly one JSON object and no markdown, prose, or code fences.\n"
    "Schema:\n"
    "{{\"schema_version\":\"relay.review.v1\",\"verdict\":\"pass\",\"summary\":\"...\","
    "\"findings\":[]}}\n"
    "or\n"
    "{{\"schema_version\":\"relay.review.v1\",\"verdict\":\"findings\",\"summary\":\"...\","
    "\"findings\":[{{\"id\":\"F1\",\"severity\":\"high\",\"title\":\"...\","
    "\"description\":\"...\",\"requested_change\":\"...\","
    "\"validation_expectation\":\"...\",\"location\":{{\"path\":\"relative/path.py\","
    "\"start_line\":1,\"end_line\":3}}}}]}}\n"
    "Rules: pass requires zero findings; findings requires at least one; severity is "
    "metadata only and never changes blocking; finding ids are unique; locations are "
    "optional workspace-relative '/' paths; do not supply task, run, or artifact IDs.\n\n"
    "PINNED REVIEW INPUTS:\n"
    "task: {task_id}\n"
    "plan artifact: {plan_artifact_id}\n"
    "diff artifact: {diff_artifact_id}\n"
    "verification evidence: {evidence_id}\n"
    "verification tool run: {tool_run_id}\n"
    "test result artifact: {test_result_artifact_id}\n\n"
    "ACCEPTED PLAN:\n{plan}\n\n"
    "IMPLEMENTED DIFF:\n{diff}\n\n"
    "RELAY VERIFICATION RESULT:\n{verification}\n\n"
    "ORIGINAL REQUEST:\n{prompt}"
)


def _review_artifact_event(task: Task, run_id: str, artifact: Artifact) -> EventLogEntry:
    return EventLogEntry(
        type=EventType.ARTIFACT_CREATED,
        content="structured review persisted as REVIEW_FINDING",
        references=[f"task:{task.id}", f"run:{run_id}", f"artifact:{artifact.id}"],
    )


def _review_evidence_event(task: Task, run_id: str, artifact_id: str) -> EventLogEntry:
    return EventLogEntry(
        type=EventType.EVIDENCE_RECORDED,
        content=f"{EvidenceKind.REVIEW_PASSED.value} recorded for task",
        references=[f"task:{task.id}", f"run:{run_id}", f"artifact:{artifact_id}"],
    )


def _persist_review_diagnostic(
    store: SqliteRelayStore,
    writer: EventLogWriter,
    task: Task,
    code: str,
    *,
    run_id: str | None = None,
    output_artifact_id: str | None = None,
) -> Artifact:
    """Persist a safe diagnostic; never a review, packet, or PASS evidence."""

    payload = InvalidReviewDiagnosticPayload(
        schema_version="relay.review.invalid.v1",
        task_id=task.id,
        code=code,
        review_run_id=run_id,
        review_output_artifact_id=output_artifact_id,
    )
    artifact = Artifact(
        kind=ArtifactKind.REPORT,
        task_id=task.id,
        run_id=run_id,
        content=encode_invalid_review_diagnostic(payload),
    )
    with store.transaction():
        store.save_model(artifact)
        writer.record(
            EventLogEntry(
                type=EventType.ARTIFACT_CREATED,
                content=f"structured review rejected: {code}",
                references=[
                    ref
                    for ref in (
                        f"task:{task.id}",
                        f"artifact:{artifact.id}",
                        f"run:{run_id}" if run_id else None,
                        f"artifact:{output_artifact_id}" if output_artifact_id else None,
                    )
                    if ref is not None
                ],
            )
        )
    return artifact


async def _run_review(
    store: SqliteRelayStore,
    writer: EventLogWriter,
    evidence: EvidenceStore,
    machine: TaskStateMachine,
    task: Task,
    reviewer: Agent,
    request: AgentRequest,
    inputs: ReviewInputs,
    *,
    approval: ApprovalPolicyConfig | None,
    model: str | None = None,
    agent_name: str | None = None,
) -> ReviewStageOutcome:
    """Run and promote one strict structured review.

    Every PASS promotion is atomic with its resulting state transition. A
    findings report atomically writes its canonical review, fix packet, and
    REVIEWING -> IMPLEMENTING rework edge. Invalid output stays REVIEWING.
    """

    subject = build_review_subject(inputs)
    review_request = request.model_copy(
        update={
            "role": AgentRole.REVIEWER,
            "prompt": _REVIEW_DIRECTIVE.format(
                task_id=task.id,
                plan_artifact_id=inputs.plan_artifact.id,
                diff_artifact_id=inputs.diff_artifact.id,
                evidence_id=inputs.verification_evidence.id,
                tool_run_id=inputs.verification_tool_run.id,
                test_result_artifact_id=inputs.test_result_artifact.id,
                plan=inputs.plan_artifact.content,
                diff=inputs.diff_artifact.content,
                verification=inputs.test_result_artifact.content,
                prompt=request.prompt,
            ),
            "context_refs": [
                f"task:{task.id}",
                f"run:{inputs.plan_run.id}",
                f"artifact:{inputs.plan_artifact.id}",
                f"run:{inputs.implementation_run.id}",
                f"artifact:{inputs.diff_artifact.id}",
                f"evidence:{inputs.verification_evidence.id}",
                f"tool_run:{inputs.verification_tool_run.id}",
                f"artifact:{inputs.test_result_artifact.id}",
            ],
        }
    )
    review_run_outcome = await run_ask(
        store,
        writer,
        reviewer,
        review_request,
        model=model,
        agent_name=agent_name,
    )
    if review_run_outcome.response is None or review_run_outcome.response.status != "ok":
        return ReviewStageOutcome(
            task=task,
            result=ReviewResult(
                disposition=ReviewDisposition.RUN_FAILED,
                run_id=review_run_outcome.run.id,
            ),
        )

    outputs = store.artifacts_for_run(review_run_outcome.run.id, kind=ArtifactKind.RUN_OUTPUT)
    output_artifact = outputs[0] if len(outputs) == 1 else None
    try:
        if output_artifact is None:
            raise ReviewContractError("invalid_context")
        report = parse_review(review_run_outcome.response.output)
        sources = build_review_sources(
            subject,
            inputs,
            review_run_outcome.run,
            output_artifact,
        )
        record = build_review_record(task, report, sources)
    except ReviewContractError as exc:
        diagnostic = _persist_review_diagnostic(
            store,
            writer,
            task,
            exc.code,
            run_id=review_run_outcome.run.id,
            output_artifact_id=None if output_artifact is None else output_artifact.id,
        )
        context_codes = {
            "foreign_task",
            "invalid_context",
            "missing_content",
            "source_mismatch",
            "digest_mismatch",
        }
        return ReviewStageOutcome(
            task=task,
            result=ReviewResult(
                disposition=(
                    ReviewDisposition.INVALID_CONTEXT
                    if exc.code in context_codes
                    else ReviewDisposition.INVALID_OUTPUT
                ),
                run_id=review_run_outcome.run.id,
                diagnostic_artifact_id=diagnostic.id,
                reason_code=exc.code,
            ),
        )

    review_artifact = Artifact(
        kind=ArtifactKind.REVIEW_FINDING,
        run_id=review_run_outcome.run.id,
        task_id=task.id,
        content=encode_review_record(record),
    )
    review_event = _review_artifact_event(task, review_run_outcome.run.id, review_artifact)

    if report.verdict is ReviewVerdict.FINDINGS:
        packet = build_fix_packet(
            review_artifact,
            inputs,
            review_run_outcome.run,
            output_artifact,
        )
        packet_artifact = Artifact(
            kind=ArtifactKind.FIX_PACKET,
            task_id=task.id,
            content=encode_fix_packet(packet),
        )
        packet_event = EventLogEntry(
            type=EventType.ARTIFACT_CREATED,
            content="fix packet generated from structured review",
            references=[
                f"task:{task.id}",
                f"run:{review_run_outcome.run.id}",
                f"artifact:{review_artifact.id}",
                f"artifact:{packet_artifact.id}",
            ],
        )
        updated = advance_task(
            machine,
            store,
            writer,
            task,
            TaskState.IMPLEMENTING,
            artifacts=(review_artifact, packet_artifact),
            events=(review_event, packet_event),
        )
        return ReviewStageOutcome(
            task=updated,
            result=ReviewResult(
                disposition=ReviewDisposition.FINDINGS,
                run_id=review_run_outcome.run.id,
                review_artifact_id=review_artifact.id,
                fix_packet_artifact_id=packet_artifact.id,
                finding_count=len(report.findings),
            ),
        )

    review_passed = EvidenceRecord(
        kind=EvidenceKind.REVIEW_PASSED,
        task_id=task.id,
        run_id=review_run_outcome.run.id,
        artifact_id=review_artifact.id,
        produced_by=f"agent:{review_run_outcome.run.agent}",
    )
    review_passed_event = _review_evidence_event(
        task, review_run_outcome.run.id, review_artifact.id
    )
    if approval is not None and approval.mode == "direct":
        no_pending = EvidenceRecord(
            kind=EvidenceKind.NO_PENDING_APPROVALS,
            task_id=task.id,
            produced_by="relay:review",
        )
        no_pending_event = EventLogEntry(
            type=EventType.EVIDENCE_RECORDED,
            content=f"{EvidenceKind.NO_PENDING_APPROVALS.value} recorded for task",
            references=[f"task:{task.id}"],
        )
        updated = advance_task(
            machine,
            store,
            writer,
            task,
            TaskState.DONE,
            evidence_store=evidence,
            evidence_records=(review_passed, no_pending),
            artifacts=(review_artifact,),
            events=(review_event, review_passed_event, no_pending_event),
        )
    else:
        approval_row, approval_event = _open_approval_gate(task)
        updated = advance_task(
            machine,
            store,
            writer,
            task,
            TaskState.APPROVAL_REQUIRED,
            created_approval=approval_row,
            evidence_store=evidence,
            evidence_records=(review_passed,),
            artifacts=(review_artifact,),
            events=(review_event, review_passed_event, approval_event),
        )
    return ReviewStageOutcome(
        task=updated,
        result=ReviewResult(
            disposition=ReviewDisposition.PASSED,
            run_id=review_run_outcome.run.id,
            review_artifact_id=review_artifact.id,
        ),
    )


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
    workspace_root: Path,
    gate: PermissionGate | None = None,
    model: str | None = None,
    agent_name: str | None = None,
    verification: VerificationConfig | None = None,
    reviewer: Agent | None = None,
    reviewer_name: str | None = None,
    reviewer_model: str | None = None,
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
    profile_grant = agent.profile.grant if agent.profile is not None else None
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
    plan_artifact = _freeze_plan(store, writer, evidence, task, plan_outcome)
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
                plan=plan_artifact.content, prompt=request.prompt
            )
        }
    )

    # Blocker 2 provenance baseline: snapshot BEFORE implement-run I/O so
    # pre-existing files are never attributed to the run. (The plan run is
    # READ_ONLY; anything it left behind is pre-existing by definition.)
    # Execution-local: concurrent builds can never share this map.
    baseline = _capture_baseline(workspace_root)

    outcome = await run_ask(
        store, writer, agent, implement_request, model=model, agent_name=agent_name
    )
    if outcome.response is None:
        return BuildOutcome(task=task, ask=outcome)

    response = outcome.response

    # Adapter-normalized tool observations → ToolRun rows (observability only).
    tool_run_ids = _record_observed_events(store, writer, response, outcome.run.id)

    # Relay-owned non-mutating diff extraction as DIFF artifact.
    diff_text = _diff_against_baseline(gate, workspace_root, task.id, baseline)
    diff_artifact_id: str | None = None
    diff_artifact: Artifact | None = None
    if diff_text.strip():
        with store.transaction():
            diff_artifact = store.save_model(
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
                        f"artifact:{diff_artifact.id}",
                        f"task:{task.id}",
                    ],
                )
            )
        assert diff_artifact is not None
        diff_artifact_id = diff_artifact.id

    review_result: ReviewResult | None = None

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
        verification_result = await _run_verification(
            store, writer, evidence, gate, machine, task, verification, workspace_root
        )
        task = verification_result.task

        # P6.1: review uses pinned persisted inputs and promotes PASS atomically
        # with the resulting closure edge. Invalid output never becomes evidence.
        if task.state is TaskState.REVIEWING:
            assert diff_artifact is not None
            assert verification_result.tool_run is not None
            assert verification_result.test_result_artifact is not None
            assert verification_result.evidence_record is not None
            review_inputs = ReviewInputs(
                task=task,
                plan_run=plan_outcome.run,
                plan_artifact=plan_artifact,
                implementation_run=outcome.run,
                diff_artifact=diff_artifact,
                verification_evidence=verification_result.evidence_record,
                verification_tool_run=verification_result.tool_run,
                test_result_artifact=verification_result.test_result_artifact,
            )
            try:
                build_review_subject(review_inputs)
            except ReviewContractError as exc:
                diagnostic = _persist_review_diagnostic(store, writer, task, exc.code)
                review_result = ReviewResult(
                    disposition=ReviewDisposition.INVALID_CONTEXT,
                    diagnostic_artifact_id=diagnostic.id,
                    reason_code=exc.code,
                )
            else:
                selected_reviewer = _reviewer_for(reviewer or agent)
                review_stage = await _run_review(
                    store,
                    writer,
                    evidence,
                    machine,
                    task,
                    selected_reviewer,
                    request,
                    review_inputs,
                    approval=approval,
                    model=reviewer_model if reviewer is not None else model,
                    agent_name=reviewer_name if reviewer is not None else agent_name,
                )
                task = review_stage.task
                review_result = review_stage.result

    return BuildOutcome(
        task=task,
        ask=outcome,
        diff_artifact_id=diff_artifact_id,
        tool_run_ids=tool_run_ids,
        review=review_result,
    )

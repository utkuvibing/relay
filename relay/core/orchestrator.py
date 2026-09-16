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

import asyncio
import enum
import json
import os
import shutil
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import pydantic

from relay.agents.base import (
    Agent,
    AgentRequest,
    AgentResponse,
    AgentRole,
    BackendType,
    RunObservation,
)
from relay.agents.errors import AgentError
from relay.context.config import ApprovalPolicyConfig, BudgetConfig, VerificationConfig
from relay.core import baseline as _baseline_mod
from relay.core.baseline import (
    BaselineIntegrityError,
    load_baseline,
    persist_baseline,
)
from relay.core.build_ledger import (
    BuildRefusal,
    ContinueRefusal,
    build_dispatch_hook,
    derive_position,
    settle_interrupted_runs,
)
from relay.core.evidence import EvidenceKind, EvidenceStore
from relay.core.permissions import Action, PermissionGate, ToolRequest
from relay.core.reviews import (
    ReviewContractError,
    ReviewInputs,
    build_fix_packet,
    build_review_record,
    build_review_sources,
    build_review_subject,
    canonical_json,
    decode_fix_packet,
    encode_fix_packet,
    encode_invalid_review_diagnostic,
    encode_review_record,
    parse_review,
)
from relay.core.room_records import build_review_findings
from relay.core.stage_signals import (
    SignalContractError,
    SignalDeliveryContext,
    SignalServices,
    check_signal_legal,
    exchange_appendix,
    notes_appendix,
    parse_stage_signal,
    persist_signal_diagnostic,
    resolve_open_signal,
    send_note_signal,
    signal_appendix,
    signal_is_blocking,
)
from relay.core.state_machine import TaskState, TaskStateMachine
from relay.harness.sanitization import redact
from relay.storage.events import EventLogWriter
from relay.storage.models import (
    Approval,
    ApprovalStatus,
    Artifact,
    ArtifactKind,
    BuildBaselineRecordPayload,
    BuildLoopRecordPayload,
    BuildRequestRecordPayload,
    EventLogEntry,
    EventType,
    EvidenceRecord,
    InvalidReviewDiagnosticPayload,
    ReviewVerdict,
    Run,
    RunStatus,
    StageSignalPayload,
    Task,
    ToolRun,
    utcnow,
)
from relay.storage.store import SqliteRelayStore

# P6.3: the tracked-workspace scan primitives live in relay.core.baseline;
# these aliases keep the established ``orchestrator._name`` surface stable
# for existing callers and tests.
_TrackedFileState = _baseline_mod.TrackedFileState
_WorkspaceBaseline = _baseline_mod.WorkspaceBaseline
_workspace_path_is_excluded = _baseline_mod.workspace_path_is_excluded
_capture_baseline = _baseline_mod.capture_baseline
_sha256_file = _baseline_mod.sha256_file
_tracked_workspace_files = _baseline_mod.tracked_workspace_files
_workspace_state_digest = _baseline_mod.workspace_state_digest
_render_workspace_diff = _baseline_mod.render_workspace_diff


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
    except asyncio.CancelledError:
        # Cancellation settles the run row too — a RUNNING row must never
        # outlive the invocation that owned it (P6.3). The exception still
        # propagates so asyncio cancellation semantics are preserved.
        cancelled = run.model_copy(
            update={"status": RunStatus.CANCELLED, "ended_at": utcnow()}
        )
        with store.transaction():
            store.update_model(cancelled)
            writer.record(
                EventLogEntry(
                    type=EventType.AGENT_RUN_FINISHED,
                    content=f"agent '{run.agent}' cancelled",
                    references=[f"run:{run.id}"],
                )
            )
        raise
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
    #: The run emitted a valid blocking stage signal — no verdict minted
    #: (P6.4); the driver resolves the exchange, then re-dispatches.
    SIGNAL = "signal"
    #: The run's output claimed the signal contract but failed validation —
    #: a ``relay.build.signal.invalid.v1`` diagnostic was persisted.
    SIGNAL_INVALID = "signal_invalid"


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
    #: The emitted blocking signal when disposition is SIGNAL (P6.4).
    signal: StageSignalPayload | None = None


@dataclass(frozen=True)
class ReviewStageOutcome:
    task: Task
    result: ReviewResult


class LoopStopReason(str, enum.Enum):
    """Why a bounded fix loop ended (P6.2).

    Outcome vocabulary only — persisted records carry the string value
    (``relay.build.loop.v1`` ``reason`` field), never the enum.
    """

    PASS_PROMOTED = "pass_promoted"
    BUDGET_EXHAUSTED = "budget_exhausted"
    NO_WORKSPACE_CHANGE = "no_workspace_change"
    RUN_FAILED = "run_failed"
    VERIFICATION_BLOCKED = "verification_blocked"
    REVIEW_BLOCKED = "review_blocked"
    LOOP_DISABLED = "loop_disabled"
    NO_BLOCKING_INPUT = "no_blocking_input"
    #: P6.4 — a blocking micro-exchange stalled (policy/budget/delivery)
    #: or a run emitted an invalid signal; a durable escalation record
    #: explains the park and ``relay continue`` resumes it.
    COMMUNICATION_BLOCKED = "communication_blocked"


@dataclass(frozen=True)
class BuildOutcome:
    """Latest-attempt facts for one ``relay build`` (P6.2 frozen semantics).

    ``ask``, ``diff_artifact_id``, ``tool_run_ids``, ``verification`` and
    ``review`` describe the LAST implementation/fix run actually dispatched
    — never attempt 1 after later fixes ran. ``ask`` is ``None`` when no
    impl/fix run was ever dispatched (planning failure, early refusal);
    ``planner`` then carries the planning run's outcome. ``attempts``
    counts dispatched implementation/fix runs only — the planner never
    counts, so ``fix_runs_used == max(0, attempts - 1)`` and the default
    bound (``max_fix_loops=3``) allows at most 4.

    P6.3: ``attempts`` is CUMULATIVE across invocations (identical to P6.2
    for ``relay build``); ``prior_attempts`` counts the build-owned runs
    that already existed when this invocation started, and ``resumed_from``
    records the parked state ``relay continue`` entered at (``None`` for
    ``relay build``).
    """

    task: Task
    ask: AskOutcome | None = None
    planner: AskOutcome | None = None
    diff_artifact_id: str | None = None
    tool_run_ids: tuple[str, ...] = ()
    verification: _VerificationResult | None = None
    review: ReviewResult | None = None
    attempts: int = 0
    prior_attempts: int = 0
    resumed_from: TaskState | None = None
    stop: LoopStopReason | None = None
    #: P6.4 — the ``relay.build.escalation.v1`` artifact explaining a
    #: COMMUNICATION_BLOCKED park, when one was recorded.
    signal_escalation_id: str | None = None


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


def _diff_and_state_against_baseline(
    gate: PermissionGate, root: Path, task_id: str, baseline: _WorkspaceBaseline
) -> tuple[str, str]:
    """One gate-checked scan → (rendered cumulative diff, raw state digest).

    The digest is the no-progress identity; the text is the reviewer/human
    DIFF artifact. Both derive from the SAME scan so a workspace changing
    mid-extraction can never produce a diff and a verdict that disagree.
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
    current_files = _tracked_workspace_files(root, baseline)
    return (
        _render_workspace_diff(baseline.files, current_files),
        _workspace_state_digest(current_files),
    )




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

_FIX_DIRECTIVE = (
    "You are the implementer executing fix attempt {attempt} of a bounded loop.\n"
    "Work only within the accepted plan; address the blocking input below "
    "exactly.\n\n"
    "ACCEPTED PLAN:\n{plan}\n\n"
    "{blocking_label}:\n{blocking}\n\n"
    "ORIGINAL REQUEST:\n{prompt}"
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
    models: tuple[pydantic.BaseModel, ...] = (),
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
    ``models`` carries extra canonical rows a boundary mints (P7.3 Room
    findings) into the same transaction. No caller-supplied enum ever
    carries transition authority (App. A.1).
    """
    if evidence_records and evidence_store is None:
        raise ValueError("evidence_records require evidence_store")
    with store.transaction():
        return advance_locked(
            machine,
            store,
            writer,
            task,
            target,
            evidence_store=evidence_store,
            created_approval=created_approval,
            updated_approval=updated_approval,
            evidence_records=evidence_records,
            artifacts=artifacts,
            events=events,
            models=models,
        )


def advance_locked(
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
    models: tuple[pydantic.BaseModel, ...] = (),
) -> Task:
    """``advance_task`` body — the CALLER owns an open transaction (P7.3)."""
    if created_approval is not None:
        store.save_model(created_approval)
    if updated_approval is not None:
        store.update_model(updated_approval)
    for artifact in artifacts:
        store.save_model(artifact)
    for model in models:
        store.save_model(model)
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


def _context_brief(workspace_root: Path) -> str:
    """Relay-discovered workspace context as a bounded brief (SPEC §13 profile)."""
    from relay.context.workspace import discover_profile

    profile = discover_profile(workspace_root)
    return (
        "# Workspace context (relay:core)\n\n"
        f"- languages: {', '.join(profile.languages) or '(none detected)'}\n"
        f"- frameworks: {', '.join(profile.frameworks) or '(none detected)'}\n"
        f"- package managers: {', '.join(profile.package_managers) or '(none detected)'}\n"
        f"- instruction files: {', '.join(profile.instructions) or '(none)'}\n"
        f"- test suites: {json.dumps(profile.tests, sort_keys=True) if profile.tests else '(none detected)'}\n"
        f"- default branch: {profile.default_branch}\n"
    )


def mint_context_locked(
    store: SqliteRelayStore,
    writer: EventLogWriter,
    evidence: EvidenceStore,
    task: Task,
    workspace_root: Path,
) -> Task:
    """Persist the workspace brief + ``CONTEXT_COLLECTED`` evidence (P7.3).

    The CALLER owns an open transaction — the P7.3 Room freeze composes this
    inside its single all-or-nothing ``BEGIN IMMEDIATE``.
    """
    brief = _context_brief(workspace_root)
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


def _collect_context(
    store: SqliteRelayStore,
    writer: EventLogWriter,
    evidence: EvidenceStore,
    task: Task,
    workspace_root: Path,
) -> Task:
    """Relay-collected workspace context backing ``CREATED→CONTEXT_READY``.

    The brief is real discovered repository fact (SPEC §13 profile: languages,
    frameworks, instruction files, test suites) persisted as a REPORT
    artifact the evidence record points at — never a caller-supplied claim.
    """
    with store.transaction():
        return mint_context_locked(store, writer, evidence, task, workspace_root)


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
    pre_provider: Callable[[Run, Artifact], Iterable[EventLogEntry]] | None = None,
    signals: SignalServices | None = None,
    prompt_suffix: str = "",
) -> ReviewStageOutcome:
    """Run and promote one strict structured review.

    Every PASS promotion is atomic with its resulting state transition. A
    findings report atomically writes its canonical review, fix packet, and
    REVIEWING -> IMPLEMENTING rework edge. Invalid output stays REVIEWING.

    P6.4: before the review contract runs, the output is checked for a
    stage signal — a valid blocking signal mints nothing (disposition
    SIGNAL) and an intended-but-invalid one persists a signal diagnostic
    (SIGNAL_INVALID), never a review verdict.
    """

    subject = build_review_subject(inputs, store=store)
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
            )
            + prompt_suffix,
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
        pre_provider=pre_provider,
    )
    if review_run_outcome.response is None or review_run_outcome.response.status != "ok":
        return ReviewStageOutcome(
            task=task,
            result=ReviewResult(
                disposition=ReviewDisposition.RUN_FAILED,
                run_id=review_run_outcome.run.id,
            ),
        )

    # P6.4: a review run may answer with a stage signal instead of a
    # verdict. The signal branch NEVER mints review artifacts — an intended
    # but invalid signal is a signal-contract failure (parked), not an
    # invalid review.
    if signals is not None:
        try:
            signal = parse_stage_signal(review_run_outcome.response.output)
        except SignalContractError as exc:
            diagnostic = persist_signal_diagnostic(
                store,
                writer,
                task,
                review_run_outcome.run,
                stage="review",
                code=exc.code,
            )
            return ReviewStageOutcome(
                task=task,
                result=ReviewResult(
                    disposition=ReviewDisposition.SIGNAL_INVALID,
                    run_id=review_run_outcome.run.id,
                    diagnostic_artifact_id=diagnostic.id,
                    reason_code=exc.code,
                ),
            )
        if signal is not None:
            try:
                check_signal_legal(signal, review_run_outcome.run.role)
            except SignalContractError as exc:
                diagnostic = persist_signal_diagnostic(
                    store,
                    writer,
                    task,
                    review_run_outcome.run,
                    stage="review",
                    code=exc.code,
                )
                return ReviewStageOutcome(
                    task=task,
                    result=ReviewResult(
                        disposition=ReviewDisposition.SIGNAL_INVALID,
                        run_id=review_run_outcome.run.id,
                        diagnostic_artifact_id=diagnostic.id,
                        reason_code=exc.code,
                    ),
                )
            if signal_is_blocking(signal):
                return ReviewStageOutcome(
                    task=task,
                    result=ReviewResult(
                        disposition=ReviewDisposition.SIGNAL,
                        run_id=review_run_outcome.run.id,
                        signal=signal,
                    ),
                )
            # A non-blocking kind from a reviewer (``note``) is already
            # refused by check_signal_legal above — unreachable.
            raise AssertionError("unreachable")  # pragma: no cover

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
            store=store,
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
        # P7.3 (App. D.3): a Room-bound task's findings become canonical,
        # individually addressable Room records in the SAME transaction as the
        # review artifacts; a standalone build mints nothing.
        room_findings, finding_events = build_review_findings(
            store, task, review_artifact, review_run_outcome.run, report
        )
        updated = advance_task(
            machine,
            store,
            writer,
            task,
            TaskState.IMPLEMENTING,
            artifacts=(review_artifact, packet_artifact),
            events=(review_event, packet_event, *finding_events),
            models=room_findings,
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


# ---------------------------------------------------------------------------
# P6.2 — bounded fix loop (SPEC §27 Phase 6; §23 budgets)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _AttemptOutcome:
    """Records produced by one dispatched implementation/fix run."""

    task: Task
    ask: AskOutcome | None = None
    tool_run_ids: tuple[str, ...] = ()
    diff_artifact: Artifact | None = None
    state_digest: str = ""
    #: A valid, legal, BLOCKING stage signal emitted as the run's whole
    #: output (P6.4) — the run mints nothing; the driver resolves it.
    signal: StageSignalPayload | None = None
    #: ``relay.build.signal.invalid.v1`` diagnostic for an intended-but-
    #: invalid signal — the stage parks COMMUNICATION_BLOCKED.
    signal_invalid: Artifact | None = None


async def _run_implementation_attempt(
    store: SqliteRelayStore,
    writer: EventLogWriter,
    evidence: EvidenceStore,
    gate: PermissionGate,
    machine: TaskStateMachine,
    task: Task,
    agent: Agent,
    request: AgentRequest,
    *,
    model: str | None,
    agent_name: str | None,
    workspace_root: Path,
    baseline: _WorkspaceBaseline,
    previous_state_digest: str | None,
    pre_provider: Callable[[Run, Artifact], Iterable[EventLogEntry]] | None = None,
    signals: SignalServices | None = None,
    stage: str = "implement",
    attempt_number: int | None = None,
) -> _AttemptOutcome:
    """Dispatch one implementation/fix run through the crash-safe spine.

    Records observed tool events, then extracts the CUMULATIVE workspace
    diff against the frozen pre-implementation baseline (one baseline for
    the whole build — a later PASS certifies the whole change, never just
    the last delta). Only a fresh, non-empty diff mints a DIFF artifact +
    ``IMPLEMENTATION_PRODUCED`` and advances IMPLEMENTING → IMPLEMENTED →
    VERIFYING; a run that produces no net change mints nothing and leaves
    the task honestly at IMPLEMENTING. No-progress compares RAW workspace
    state digests — never rendered diff text, which collapses binary and
    lossy-decode differences (the §24 "no new evidence" condition in
    deterministic, collision-safe form). P6.3: the DIFF artifact and its
    ``IMPLEMENTATION_PRODUCED`` evidence commit in ONE transaction — a
    crash between them could never leave a DIFF without its gate evidence.
    """
    outcome = await run_ask(
        store,
        writer,
        agent,
        request,
        model=model,
        agent_name=agent_name,
        pre_provider=pre_provider,
    )
    if outcome.response is None:
        return _AttemptOutcome(task=task, ask=outcome)

    # Adapter-normalized tool observations → ToolRun rows (observability only).
    tool_run_ids = _record_observed_events(
        store, writer, outcome.response, outcome.run.id
    )

    # P6.4: a run may end its whole output with a stage signal instead of
    # implementation work. Detection runs BEFORE diff extraction — a signal
    # run must never mint a DIFF (fail closed).
    if signals is not None:
        try:
            signal = parse_stage_signal(outcome.response.output)
        except SignalContractError as exc:
            diagnostic = persist_signal_diagnostic(
                store, writer, task, outcome.run, stage=stage, code=exc.code
            )
            return _AttemptOutcome(
                task=task,
                ask=outcome,
                tool_run_ids=tool_run_ids,
                signal_invalid=diagnostic,
            )
        if signal is not None:
            try:
                check_signal_legal(signal, outcome.run.role)
            except SignalContractError as exc:
                diagnostic = persist_signal_diagnostic(
                    store, writer, task, outcome.run, stage=stage, code=exc.code
                )
                return _AttemptOutcome(
                    task=task,
                    ask=outcome,
                    tool_run_ids=tool_run_ids,
                    signal_invalid=diagnostic,
                )
            if signal_is_blocking(signal):
                return _AttemptOutcome(
                    task=task,
                    ask=outcome,
                    tool_run_ids=tool_run_ids,
                    signal=signal,
                )
            # ``note``: non-blocking — send best-effort, then fall through
            # to normal diff extraction with nothing minted for the note.
            send_note_signal(
                store,
                writer,
                signals,
                task,
                outcome.run,
                signal,
                stage=stage,
                attempt=attempt_number,
            )

    # Relay-owned non-mutating extraction: rendered DIFF artifact + the raw
    # state digest share ONE scan so they can never disagree.
    diff_text, state_digest = _diff_and_state_against_baseline(
        gate, workspace_root, task.id, baseline
    )
    if not diff_text.strip() or state_digest == previous_state_digest:
        return _AttemptOutcome(
            task=task,
            ask=outcome,
            tool_run_ids=tool_run_ids,
            state_digest=state_digest,
        )

    # IMPLEMENTATION_PRODUCED only when the run actually produced changes
    # (Blocker 4): a no-op mints nothing — and the machine therefore keeps
    # the task at IMPLEMENTING (§27 Phase 3 exit gate). P6.3: artifact +
    # evidence mint ATOMICALLY — a DIFF can never exist without its gate
    # evidence, which is what makes the boundary crash-safe to resume.
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
    task = advance_task(machine, store, writer, task, TaskState.VERIFYING)
    return _AttemptOutcome(
        task=task,
        ask=outcome,
        tool_run_ids=tool_run_ids,
        diff_artifact=diff_artifact,
        state_digest=state_digest,
    )


def _fix_context_refs(
    request: AgentRequest,
    task: Task,
    plan_artifact: Artifact,
    blocking: Artifact,
) -> list[str]:
    """Ordered, de-duplicated context refs for one fix attempt's request.

    A fix run keeps the caller's original refs and additionally names the
    canonical inputs it must work from: the task, the frozen plan artifact,
    and the blocking artifact itself. When the blocker is a FIX_PACKET the
    packet's own pinned source references are surfaced too — decoded
    read-only; the persisted packet bytes remain the blocking authority and
    are never regenerated or reinterpreted.
    """
    refs: list[str] = []

    def _add(ref: str) -> None:
        if ref not in refs:
            refs.append(ref)

    for ref in request.context_refs:
        _add(ref)
    _add(f"task:{task.id}")
    _add(f"artifact:{plan_artifact.id}")
    _add(f"artifact:{blocking.id}")
    if blocking.kind is ArtifactKind.FIX_PACKET and blocking.content:
        packet = decode_fix_packet(blocking.content)
        subject = packet.sources.subject
        _add(f"artifact:{packet.review_artifact_id}")
        _add(f"run:{subject.plan_run_id}")
        _add(f"artifact:{subject.plan_artifact_id}")
        _add(f"run:{subject.implementation_run_id}")
        _add(f"artifact:{subject.diff_artifact_id}")
        _add(f"evidence:{subject.verification_evidence_id}")
        _add(f"tool_run:{subject.verification_tool_run_id}")
        _add(f"artifact:{subject.test_result_artifact_id}")
        _add(f"run:{packet.sources.review_run_id}")
        _add(f"artifact:{packet.sources.review_output_artifact_id}")
    return refs


# The only loop stops that ever become a stored observation — the map keeps
# the storage-layer Literal vocabulary honest without an import cycle.
_LOOP_RECORD_REASONS: dict[
    LoopStopReason, Literal["budget_exhausted", "no_workspace_change"]
] = {
    LoopStopReason.BUDGET_EXHAUSTED: "budget_exhausted",
    LoopStopReason.NO_WORKSPACE_CHANGE: "no_workspace_change",
}


def _persist_loop_record(
    store: SqliteRelayStore,
    writer: EventLogWriter,
    task: Task,
    *,
    reason: LoopStopReason,
    fix_runs_used: int,
    last_review_artifact_id: str | None,
    last_fix_packet_artifact_id: str | None,
    last_diff_artifact_id: str | None,
) -> Artifact:
    """Persist the ``relay.build.loop.v1`` stop observation (P6.2 D6).

    A stored observation only — no evidence, no transition. Pinned ids keep
    the parked state's ledger reachable for inspection (and a future
    resume slice): the most recent DIFF still describes the workspace even
    when the last dispatched run minted none.
    """
    payload = BuildLoopRecordPayload(
        schema_version="relay.build.loop.v1",
        task_id=task.id,
        reason=_LOOP_RECORD_REASONS[reason],
        fix_runs_used=fix_runs_used,
        last_review_artifact_id=last_review_artifact_id,
        last_fix_packet_artifact_id=last_fix_packet_artifact_id,
        last_diff_artifact_id=last_diff_artifact_id,
    )
    artifact = Artifact(
        kind=ArtifactKind.REPORT,
        task_id=task.id,
        content=canonical_json(payload),
    )
    with store.transaction():
        store.save_model(artifact)
        writer.record(
            EventLogEntry(
                type=EventType.ARTIFACT_CREATED,
                content=f"fix loop stopped: {reason.value}",
                references=[f"task:{task.id}", f"artifact:{artifact.id}"],
            )
        )
    return artifact


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
    budget: BudgetConfig | None = None,
    signals: SignalServices | None = None,
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

    P6.2 closes the Phase-6 loop inside this one invocation: review
    findings or a failed verification re-enter ``IMPLEMENTING`` and dispatch
    a bounded number of fix runs (``budget.max_fix_loops``, default 3; 0 =
    the P6.1 one-pass behavior) — each consuming the pending blocking input
    (the deterministic FIX_PACKET or the failed TEST_RESULT), re-diffed
    against the frozen baseline, re-verified, and re-reviewed. Interruption
    semantics are boundary-only: stop conditions are evaluated at stage
    boundaries, never mid-run.
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
    # whose lifecycle already started is refused — `relay continue` (P6.3)
    # is the resume path for a parked build.
    if task.state is not TaskState.CREATED:
        raise BuildRefusal(
            f"task '{task.id}' is at state '{task.state.value}' — "
            "lifecycle already started; 'relay continue' resumes a parked build"
        )
    machine = TaskStateMachine(task_id=task.id, store=evidence)

    # Durable build request (P6.3 D5) — the FIRST write of the build:
    # Task.title only keeps prompt[:200], and resume must rebuild the
    # pinned implementer identity/model, so the full request is persisted
    # before any stage runs.
    _persist_build_request(
        store,
        writer,
        task,
        request,
        implementer=agent_name or agent.name,
        model=model,
    )

    return await _drive_build(
        store,
        writer,
        evidence,
        gate,
        machine,
        task,
        agent,
        request,
        workspace_root=workspace_root,
        model=model,
        agent_name=agent_name,
        verification=verification,
        reviewer=_reviewer_for(reviewer or agent),
        review_name=reviewer_name if reviewer is not None else agent_name,
        review_model=reviewer_model if reviewer is not None else model,
        approval=approval,
        budget=budget,
        baseline=None,
        prior_attempts=0,
        resumed_from=None,
        signals=signals,
    )


def persist_build_request_locked(
    store: SqliteRelayStore,
    writer: EventLogWriter,
    task: Task,
    request: AgentRequest,
    *,
    implementer: str,
    model: str | None,
) -> Artifact:
    """``relay.build.request.v1`` write — the CALLER owns a transaction (P7.3)."""
    payload = BuildRequestRecordPayload(
        schema_version="relay.build.request.v1",
        task_id=task.id,
        prompt=request.prompt,
        implementer=implementer,
        model=model,
    )
    artifact = Artifact(
        kind=ArtifactKind.REPORT, task_id=task.id, content=canonical_json(payload)
    )
    store.save_model(artifact)
    writer.record(
        EventLogEntry(
            type=EventType.ARTIFACT_CREATED,
            content="build request persisted for resume",
            references=[f"task:{task.id}", f"artifact:{artifact.id}"],
        )
    )
    return artifact


def _persist_build_request(
    store: SqliteRelayStore,
    writer: EventLogWriter,
    task: Task,
    request: AgentRequest,
    *,
    implementer: str,
    model: str | None,
) -> Artifact:
    """Persist the ``relay.build.request.v1`` record — resume's entry point."""
    with store.transaction():
        return persist_build_request_locked(
            store, writer, task, request, implementer=implementer, model=model
        )


def _persist_baseline_pin(
    store: SqliteRelayStore,
    writer: EventLogWriter,
    task: Task,
    pin: BuildBaselineRecordPayload,
) -> Artifact:
    """Persist the ``relay.build.baseline.v1`` pin — LAST, after the
    snapshot's publication is fully durable (fsynced blobs + manifest,
    atomic rename, parent-dir fsync)."""
    artifact = Artifact(
        kind=ArtifactKind.REPORT, task_id=task.id, content=canonical_json(pin)
    )
    with store.transaction():
        store.save_model(artifact)
        writer.record(
            EventLogEntry(
                type=EventType.ARTIFACT_CREATED,
                content="durable build baseline pinned",
                references=[f"task:{task.id}", f"artifact:{artifact.id}"],
            )
        )
    return artifact


async def _drive_build(
    store: SqliteRelayStore,
    writer: EventLogWriter,
    evidence: EvidenceStore,
    gate: PermissionGate,
    machine: TaskStateMachine,
    task: Task,
    agent: Agent,
    request: AgentRequest,
    *,
    workspace_root: Path,
    model: str | None,
    agent_name: str | None,
    verification: VerificationConfig | None,
    reviewer: Agent,
    review_name: str | None,
    review_model: str | None,
    approval: ApprovalPolicyConfig | None,
    budget: BudgetConfig | None,
    baseline: _WorkspaceBaseline | None,
    prior_attempts: int,
    resumed_from: TaskState | None,
    signals: SignalServices | None = None,
) -> BuildOutcome:
    """The shared ledger-driven stage driver (P6.3 D1/D9).

    Every iteration re-derives :class:`BuildPosition` from durable records —
    the ledger is the cursor, so the same loop serves ``relay build`` and
    ``relay continue`` identically. A stage whose output already exists is
    a pure ``advance``: no re-dispatch, no budget consult, no duplicate
    artifacts. ``budget.max_fix_loops`` is consulted at exactly one point —
    immediately before dispatching a NEW implementer/fix run (a P6.4
    same-attempt continuation never consults it). Stop
    conditions are evaluated only at stage boundaries (pre-P6 hardening:
    interruption is never mid-run).
    """
    max_fix_loops = (budget or BudgetConfig()).max_fix_loops
    stop: LoopStopReason | None = None
    outcome: AskOutcome | None = None
    plan_outcome: AskOutcome | None = None
    tool_run_ids: tuple[str, ...] = ()
    diff_artifact_id: str | None = None
    verification_result: _VerificationResult | None = None
    review_result: ReviewResult | None = None
    signal_escalation_id: str | None = None

    while True:
        position = derive_position(store, evidence, task.id)
        task = position.task
        action = position.next_action

        if action == "collect_context":
            # Relay-collected workspace context → CONTEXT_READY.
            task = _collect_context(store, writer, evidence, task, workspace_root)
            task = advance_task(machine, store, writer, task, TaskState.CONTEXT_READY)
            continue

        if action == "plan":
            # Planning run (same adapter, READ_ONLY) → canonical plan.
            plan_request = request.model_copy(
                update={
                    "role": AgentRole.PLANNER,
                    "prompt": _PLAN_DIRECTIVE.format(prompt=request.prompt),
                }
            )
            plan_outcome = await run_ask(
                store,
                writer,
                _planner_for(agent),
                plan_request,
                model=model,
                agent_name=agent_name,
                pre_provider=build_dispatch_hook(task.id, "plan"),
            )
            if plan_outcome.response is None or not plan_outcome.response.output.strip():
                # Planning failed or produced nothing usable: honestly
                # blocked at CONTEXT_READY — `relay continue` re-plans.
                break
            _freeze_plan(store, writer, evidence, task, plan_outcome)
            task = advance_task(machine, store, writer, task, TaskState.PLAN_READY)
            continue

        if action == "advance":
            # A persisted boundary whose outputs already committed — the
            # crash-safe recovery path: transition only, no stage re-run.
            assert position.advance_target is not None
            task = advance_task(machine, store, writer, task, position.advance_target)
            continue

        if action == "verify":
            # Relay grades the exam — the implementer never does (Q-c).
            verification_result = await _run_verification(
                store, writer, evidence, gate, machine, task, verification, workspace_root
            )
            task = verification_result.task
            if task.state is TaskState.VERIFYING:
                stop = LoopStopReason.VERIFICATION_BLOCKED
                break
            continue

        if action == "review":
            # P6.1: review binds THIS attempt's persisted run, diff, and
            # verification records exactly — reconstructed from the ledger,
            # so a parked REVIEWING task resumes with identical inputs.
            records = position.latest_records
            assert records is not None
            assert records.diff_artifact is not None
            assert records.implementation_produced is not None
            assert records.tests_passed is not None
            assert records.verification_tool_run is not None
            assert records.test_result_artifact is not None
            assert position.plan_artifact is not None and position.plan_run is not None
            review_inputs = ReviewInputs(
                task=task,
                plan_run=position.plan_run,
                plan_artifact=position.plan_artifact,
                implementation_run=records.run,
                diff_artifact=records.diff_artifact,
                verification_evidence=records.tests_passed,
                verification_tool_run=records.verification_tool_run,
                test_result_artifact=records.test_result_artifact,
            )
            try:
                build_review_subject(review_inputs, store=store)
            except ReviewContractError as exc:
                diagnostic = _persist_review_diagnostic(store, writer, task, exc.code)
                review_result = ReviewResult(
                    disposition=ReviewDisposition.INVALID_CONTEXT,
                    diagnostic_artifact_id=diagnostic.id,
                    reason_code=exc.code,
                )
                stop = LoopStopReason.REVIEW_BLOCKED
                break
            # P6.4: an answered review-stage signal resumes the review as a
            # continuation run carrying the exchange's provenance.
            review_continuation: tuple[str, str] | None = None
            if (
                position.pending_signal is not None
                and position.pending_signal.reply is not None
                and position.pending_signal.message is not None
            ):
                review_continuation = (
                    position.pending_signal.message.id,
                    position.pending_signal.reply.id,
                )
            review_prompt_suffix = ""
            if signals is not None:
                review_prompt_suffix = signal_appendix(
                    signals,
                    task,
                    AgentRole.REVIEWER,
                    review_name or reviewer.name,
                )
            # Exchange history and notes are durable records — they ride
            # the continuation prompt even if services were dropped since
            # the exchange happened. Both are empty-string no-ops when the
            # task has none, keeping pre-P6.4 prompts byte-compatible.
            review_prompt_suffix += exchange_appendix(position.exchanges)
            review_prompt_suffix += notes_appendix(store, task.id, AgentRole.REVIEWER)
            review_stage = await _run_review(
                store,
                writer,
                evidence,
                machine,
                task,
                reviewer,
                request,
                review_inputs,
                approval=approval,
                model=review_model,
                agent_name=review_name,
                pre_provider=build_dispatch_hook(
                    task.id, "review", continuation=review_continuation
                ),
                signals=signals,
                prompt_suffix=review_prompt_suffix,
            )
            task = review_stage.task
            review_result = review_stage.result
            if review_result.disposition is ReviewDisposition.SIGNAL_INVALID:
                signal_escalation_id = review_result.diagnostic_artifact_id
                stop = LoopStopReason.COMMUNICATION_BLOCKED
                break
            if review_result.disposition is ReviewDisposition.SIGNAL:
                continue  # open blocking signal — resolve on next derive
            if task.state is TaskState.IMPLEMENTING:
                continue  # findings promotion — packet is the next blocker
            if task.state is TaskState.REVIEWING:
                # Reviewer run failure or invalid output parks here (P6.1).
                stop = LoopStopReason.REVIEW_BLOCKED
                break
            stop = LoopStopReason.PASS_PROMOTED
            break

        if action == "recover_signal":
            # Crash-gap recovery: the last bound run's RUN_OUTPUT is an
            # intended-invalid signal whose diagnostic was never persisted.
            # Persist it (idempotently) and park — never a fresh attempt,
            # never a diff/verdict from that run.
            invalid = position.invalid_signal
            if invalid is None:
                stop = LoopStopReason.COMMUNICATION_BLOCKED
                break
            diagnostic = persist_signal_diagnostic(
                store,
                writer,
                task,
                invalid.run,
                stage=invalid.stage,
                code=invalid.code,
            )
            signal_escalation_id = diagnostic.id
            stop = LoopStopReason.COMMUNICATION_BLOCKED
            break

        if action == "resolve_signal":
            # P6.4: the latest stage run emitted a blocking signal whose
            # exchange is unresolved — send/deliver/promote from durable
            # state. An answered signal is a "dispatch" (continuation), so
            # this action always carries an unanswered one.
            open_signal = position.pending_signal
            if signals is None or open_signal is None:
                stop = LoopStopReason.COMMUNICATION_BLOCKED
                break
            resolution = await resolve_open_signal(
                store,
                writer,
                evidence,
                signals,
                task,
                open_signal,
                signal_context=SignalDeliveryContext(
                    plan_artifact_id=(
                        position.plan_artifact.id
                        if position.plan_artifact is not None
                        else None
                    ),
                    plan_content=(
                        position.plan_artifact.content
                        if position.plan_artifact is not None
                        else None
                    ),
                    request_prompt=position.request.prompt,
                    blocker_artifact_id=(
                        position.pending_input.id
                        if position.pending_input is not None
                        else None
                    ),
                ),
            )
            if resolution.status == "escalated":
                signal_escalation_id = (
                    resolution.escalation.id if resolution.escalation is not None else None
                )
                stop = LoopStopReason.COMMUNICATION_BLOCKED
                break
            continue

        if action == "dispatch":
            if baseline is None:
                if position.baseline_pin is not None:
                    # Verify the pinned snapshot before trusting it — a
                    # corrupt/missing baseline fails closed, never rebuilds.
                    baseline = load_baseline(
                        workspace_root, task.id, position.baseline_pin
                    )
                else:
                    # First-ever capture, at the exact P6.2 boundary (before
                    # the first implementation run): snapshot, publish
                    # crash-safe, pin LAST — the ledger never references a
                    # snapshot whose publication isn't durable.
                    snapshot = _capture_baseline(workspace_root)
                    pin = persist_baseline(workspace_root, task.id, snapshot)
                    _persist_baseline_pin(store, writer, task, pin)
                    baseline = snapshot

            # P6.4: an ANSWERED pending signal re-dispatches the SAME
            # attempt as a continuation — the marker carries the exchange's
            # causal refs and the fix budget is never consulted.
            signal_state = position.pending_signal
            continuation: tuple[str, str] | None = None
            if signal_state is not None and signal_state.reply is not None:
                assert signal_state.message is not None
                assert signal_state.attempt is not None
                continuation = (
                    signal_state.message.id,
                    signal_state.reply.id,
                )
                attempt_number = signal_state.attempt
            else:
                # The ONLY budget consult: a genuinely new implementer/fix
                # dispatch. Recovery, verification, review, continuations
                # and PASS promotion never see this check.
                if position.attempts >= 1 and position.fix_runs_used >= max_fix_loops:
                    stop = (
                        LoopStopReason.BUDGET_EXHAUSTED
                        if max_fix_loops
                        else LoopStopReason.LOOP_DISABLED
                    )
                    break
                attempt_number = position.attempts + 1

            assert position.plan_artifact is not None
            blocking = position.pending_input
            if blocking is None:
                # The prompt is the frozen plan artifact content (App. D.3).
                prompt = _IMPLEMENT_DIRECTIVE.format(
                    plan=position.plan_artifact.content, prompt=request.prompt
                )
                context_refs = request.context_refs
                stage = "implement"
            else:
                label = (
                    "FIX PACKET (relay.fix_packet.v1)"
                    if blocking.kind is ArtifactKind.FIX_PACKET
                    else "FAILED VERIFICATION OUTPUT"
                )
                prompt = _FIX_DIRECTIVE.format(
                    attempt=attempt_number,
                    plan=position.plan_artifact.content,
                    blocking_label=label,
                    blocking=blocking.content or "",
                    prompt=request.prompt,
                )
                context_refs = _fix_context_refs(
                    request, task, position.plan_artifact, blocking
                )
                stage = "fix"

            # P6.4: signal instructions are advertised only where currently
            # deliverable; the exchange history (durable record) rides the
            # continuation regardless of live services.
            if signals is not None:
                prompt += signal_appendix(
                    signals,
                    task,
                    AgentRole.IMPLEMENTER,
                    agent_name or agent.name,
                )
            prompt += exchange_appendix(position.exchanges)
            prompt += notes_appendix(store, task.id, AgentRole.IMPLEMENTER)

            # No-progress is RAW workspace identity (D10): the pre-run
            # digest of the CURRENT tree — in-process this equals the prior
            # post-run digest; cross-process it is re-derived fresh.
            previous_state_digest = _workspace_state_digest(
                _tracked_workspace_files(workspace_root, baseline)
            )
            attempt_request = request.model_copy(
                update={"prompt": prompt, "context_refs": context_refs}
            )
            attempt = await _run_implementation_attempt(
                store,
                writer,
                evidence,
                gate,
                machine,
                task,
                agent,
                attempt_request,
                model=model,
                agent_name=agent_name,
                workspace_root=workspace_root,
                baseline=baseline,
                previous_state_digest=previous_state_digest,
                pre_provider=build_dispatch_hook(
                    task.id, stage, attempt_number, continuation=continuation
                ),
                signals=signals,
                stage=stage,
                attempt_number=attempt_number,
            )
            task = attempt.task
            outcome = attempt.ask
            tool_run_ids = attempt.tool_run_ids
            diff_artifact_id = (
                attempt.diff_artifact.id if attempt.diff_artifact is not None else None
            )
            verification_result = None
            review_result = None
            if outcome is None or outcome.response is None:
                stop = LoopStopReason.RUN_FAILED
                break
            if attempt.signal_invalid is not None:
                signal_escalation_id = attempt.signal_invalid.id
                stop = LoopStopReason.COMMUNICATION_BLOCKED
                break
            if attempt.signal is not None:
                continue  # blocking signal — resolve on next derive
            if attempt.diff_artifact is None:
                # No net workspace change: on the first-ever dispatch the
                # honest no-op park; on a fix run the identical raw state
                # means the same inputs would only burn budget repeating.
                stop = (
                    LoopStopReason.NO_BLOCKING_INPUT
                    if position.attempts == 0
                    else LoopStopReason.NO_WORKSPACE_CHANGE
                )
                break
            continue

        # action == "stop": APPROVAL_REQUIRED or DONE — nothing to drive.
        stop = LoopStopReason.PASS_PROMOTED
        break

    final = derive_position(store, evidence, task.id)
    if stop is not None and stop in _LOOP_RECORD_REASONS:
        _persist_loop_record(
            store,
            writer,
            final.task,
            reason=stop,
            fix_runs_used=final.fix_runs_used,
            last_review_artifact_id=final.last_review_artifact_id,
            last_fix_packet_artifact_id=final.last_fix_packet_artifact_id,
            last_diff_artifact_id=final.last_diff_artifact_id,
        )

    return BuildOutcome(
        task=final.task,
        ask=outcome,
        planner=plan_outcome,
        diff_artifact_id=diff_artifact_id,
        tool_run_ids=tool_run_ids,
        verification=verification_result,
        review=review_result,
        attempts=final.attempts,
        prior_attempts=prior_attempts,
        resumed_from=resumed_from,
        stop=stop,
        signal_escalation_id=signal_escalation_id,
    )


async def continue_build(
    store: SqliteRelayStore,
    writer: EventLogWriter,
    evidence: EvidenceStore,
    agent: Agent,
    task_id: str,
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
    budget: BudgetConfig | None = None,
    settle_interrupted: bool = False,
    signals: SignalServices | None = None,
) -> BuildOutcome:
    """Resume a parked build in a new process (P6.3).

    Everything resume needs comes from the ledger: the full prompt and the
    pinned implementer identity/model from ``relay.build.request.v1``, the
    verified baseline snapshot, and the derived position. Verification,
    reviewer, approval and budget come from CURRENT Relay config — no
    caller-supplied overrides exist. Every pre-execution refusal raises
    :class:`ContinueRefusal` with a stable ``code`` and writes NOTHING.
    """
    gate = gate or PermissionGate()
    task = store.load_model(Task, task_id)
    if task is None:
        raise ContinueRefusal("no_task", f"task '{task_id}' does not exist")
    if task.state is TaskState.DONE:
        raise ContinueRefusal(
            "terminal", f"task '{task.id}' is already DONE — nothing to resume"
        )
    if task.state is TaskState.APPROVAL_REQUIRED:
        raise ContinueRefusal(
            "awaiting_approval",
            f"task '{task.id}' awaits human approval — "
            f"'relay approve {task.id} --by <name>' closes it",
        )

    # Same capability contract as build: harness-backed implementer with a
    # resolvable write grant BEFORE anything resumes.
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

    position = derive_position(store, evidence, task.id)

    # Pinned implementer identity — `continue` takes no agent/model
    # overrides; drift refuses rather than silently re-binding (D5).
    if (agent_name or agent.name) != position.request.implementer:
        raise ContinueRefusal(
            "implementer_mismatch",
            f"task '{task.id}' was built by '{position.request.implementer}' — "
            f"this invocation resolves '{agent_name or agent.name}'",
        )
    if model != position.request.model:
        raise ContinueRefusal(
            "implementer_mismatch",
            f"task '{task.id}' pinned model '{position.request.model}' — "
            f"this invocation resolves '{model}'",
        )

    # Verify the pinned baseline up front — missing or corrupt snapshots
    # fail closed with zero persistence; a baseline is never rebuilt.
    baseline: _WorkspaceBaseline | None = None
    if position.baseline_pin is not None:
        try:
            baseline = load_baseline(workspace_root, task.id, position.baseline_pin)
        except BaselineIntegrityError as exc:
            raise ContinueRefusal(
                "baseline_corrupt",
                f"baseline verification failed for task '{task.id}': {exc}",
            ) from exc

    # Interrupted build-owned runs block resume until explicitly settled —
    # including P6.4 delivery runs parked mid-signal. Unbound rows are
    # never touched either way.
    if (
        position.in_flight_runs
        or position.in_flight_delivery_runs
        or position.in_flight_tool_runs
    ):
        if not settle_interrupted:
            ids = (
                [r.id for r in position.in_flight_runs]
                + [r.id for r in position.in_flight_delivery_runs]
                + [tr.id for tr in position.in_flight_tool_runs]
            )
            raise ContinueRefusal(
                "run_in_flight",
                f"task '{task.id}' has interrupted build-owned/delivery runs "
                f"({', '.join(ids)}) — pass --settle-interrupted to mark "
                "them cancelled and resume",
            )
        settle_interrupted_runs(store, writer, position)
        position = derive_position(store, evidence, task.id)

    # Dispatch-only budget (D4): refuse ONLY when the derived next step is
    # a genuinely new implementer/fix run. Persisted boundaries, pending
    # verification, review, P6.4 signal resolution/continuations, and PASS
    # promotion resume regardless.
    max_fix_loops = (budget or BudgetConfig()).max_fix_loops
    if (
        position.next_action == "dispatch"
        and position.pending_signal is None
        and position.attempts >= 1
        and position.fix_runs_used >= max_fix_loops
    ):
        raise ContinueRefusal(
            "budget_exhausted" if max_fix_loops else "loop_disabled",
            f"fix budget exhausted: used {position.fix_runs_used} of "
            f"{max_fix_loops} fix runs — raise 'budget.max_fix_loops' in "
            "relay.yaml to dispatch another fix",
        )

    machine = TaskStateMachine(task_id=task.id, store=evidence, state=task.state)
    resumed_request = AgentRequest(
        prompt=position.request.prompt,
        role=AgentRole.IMPLEMENTER,
        task_id=task.id,
    )
    return await _drive_build(
        store,
        writer,
        evidence,
        gate,
        machine,
        task,
        agent,
        resumed_request,
        workspace_root=workspace_root,
        model=model,
        agent_name=agent_name,
        verification=verification,
        reviewer=_reviewer_for(reviewer or agent),
        review_name=reviewer_name if reviewer is not None else agent_name,
        review_model=reviewer_model if reviewer is not None else model,
        approval=approval,
        budget=budget,
        baseline=baseline,
        prior_attempts=position.attempts,
        resumed_from=task.state,
        signals=signals,
    )

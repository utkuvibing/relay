"""Domain records for Relay's canonical store.

SPEC reference: §5 (Fundamental Domain Objects), §14 (Persistent Memory),
§15 (Event Log).

These models are the single source of truth for what Relay remembers.
Agent memory is never canonical; these records are. SQLite tables
(Phase 1) will be generated from this vocabulary — field names here are
the schema contract.
"""

from __future__ import annotations

import enum
import re
import uuid
from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

from relay.core.evidence import EvidenceKind
from relay.core.permissions import Action
from relay.core.state_machine import TaskState


def new_id() -> str:
    return uuid.uuid4().hex


def utcnow() -> datetime:
    return datetime.now(UTC)


class WorkspaceKind(str, enum.Enum):
    GIT_REPO = "git_repo"
    FOLDER = "folder"
    RESEARCH = "research"
    CONVERSATION = "conversation"  # no-repo mode (SPEC §3.5)


class Workspace(BaseModel):
    id: str = Field(default_factory=new_id)
    name: str
    path: str | None = None
    kind: WorkspaceKind = WorkspaceKind.CONVERSATION
    created_at: datetime = Field(default_factory=utcnow)


class RoomMember(BaseModel):
    """One seat in a room: an agent bound to a role (SPEC §8)."""

    agent: str
    role: str


class Room(BaseModel):
    """Long-lived shared AI work area; survives days of inactivity (SPEC §5)."""

    id: str = Field(default_factory=new_id)
    name: str
    workspace_id: str | None = None
    members: list[RoomMember] = Field(default_factory=lambda: list[RoomMember]())
    active_task_id: str | None = None
    created_at: datetime = Field(default_factory=utcnow)


class Task(BaseModel):
    """Bounded unit of work whose lifecycle Relay owns (SPEC §5/§6)."""

    id: str = Field(default_factory=new_id)
    title: str
    state: TaskState = TaskState.CREATED
    room_id: str | None = None
    workspace_id: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class RunStatus(str, enum.Enum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ProtocolExecution(BaseModel):
    """Append-only execution inputs; progress belongs to the conversation ledger."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    id: str = Field(default_factory=new_id)
    execution_key: str = Field(min_length=1)
    room_id: str | None = Field(default=None, min_length=1)
    task_id: str | None = Field(default=None, min_length=1)
    topic: str = Field(min_length=1)
    definition_snapshot: str
    definition_digest: str
    bindings_snapshot: str
    runner_version: str = "relay.protocol.runner.v1"
    created_at: datetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def _scope(self) -> ProtocolExecution:
        if self.room_id is None and self.task_id is None:
            raise ValueError("execution requires room or task scope")
        return self


class Run(BaseModel):
    """A single execution of one agent for one task (SPEC §5, §25)."""

    id: str = Field(default_factory=new_id)
    task_id: str | None = None
    agent: str
    role: str
    model: str | None = None
    status: RunStatus = RunStatus.RUNNING
    input_size: int | None = Field(default=None, description="Prompt size in tokens.")
    output_size: int | None = Field(default=None, description="Completion size in tokens.")
    cost_usd: float | None = None
    #: App. C.6 seam — additive, nullable, provider-neutral harness facts.
    #: ``model`` stays the REQUESTED model; these report what actually ran.
    resolved_model: str | None = Field(
        default=None, description="Model reported by the backend when known."
    )
    adapter_version: str | None = Field(
        default=None, description="Harness binary/version when discovered."
    )
    backend: str | None = Field(
        default=None, description="Execution-family snapshot (api|harness) at run time."
    )
    external_session_ref: str | None = Field(
        default=None,
        description="NON-SECRET provider continuation handle; C.4 allowlist only.",
    )
    started_at: datetime = Field(default_factory=utcnow)
    ended_at: datetime | None = None


class MessageType(str, enum.Enum):
    """Vocabulary of the conversation bus (SPEC §9, §15; App. D.5).

    The first six members are the frozen Phase-0 conversational set.
    The D.5 extensions (P4.1) are additive, lowercase concept-form values;
    blocking-ness is per-message ``Message.blocking`` metadata, never
    implied by type alone (App. D.6).
    """

    OPINION = "opinion"
    CHALLENGE = "challenge"
    REBUTTAL = "rebuttal"
    FINAL_POSITION = "final_position"
    SYNTHESIS = "synthesis"
    REVIEW_FINDING = "review_finding"
    #: App. D.5 additive extensions (P4.1).
    CLARIFICATION_REQUEST = "clarification_request"
    CLARIFICATION_RESPONSE = "clarification_response"
    PROPOSAL = "proposal"
    NOTE = "note"
    SYSTEM = "system"


class Message(BaseModel):
    """First-class inter-agent communication record (SPEC §5/§9; App. D.5).

    Append-only at persistence (P4.1): ``messages`` joins the
    ``_APPEND_ONLY_TABLES`` family, so every field is final at insert.
    ``recipient`` always stores the RESOLVED logical-agent identity that
    received the message; ``recipient_role`` preserves the original role
    address when the sender addressed a role (App. D.11-P4 provenance).
    ``references`` are generic semantic references (plans/decisions/
    findings/artifacts/evidence); no reply-linkage representation is
    frozen here — that is a P4.3 decision.
    """

    id: str = Field(default_factory=new_id)
    stage_key: str | None = None
    sender: str
    recipient: str | None = Field(
        default=None,
        description="Resolved logical-agent identity; None means broadcast to the room.",
    )
    recipient_role: str | None = Field(
        default=None,
        description="Original role address when the sender addressed a role; else None.",
    )
    run_id: str | None = Field(
        default=None,
        description=(
            "Authorship provenance (P4.2): the Run that authored this message. "
            "Required for bare logical-agent senders, validated against "
            "run.agent == sender at the bus boundary; forbidden for "
            "human:/relay: senders."
        ),
    )
    reply_to_id: str | None = Field(
        default=None,
        description="Parent message id when this message is a reply; else None (P4.3).",
    )
    room_id: str | None = None
    task_id: str | None = None
    type: MessageType
    content: str
    blocking: bool = Field(
        default=False,
        description="Per-message metadata (App. D.6); never implied by type alone.",
    )
    references: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)


class ArtifactKind(str, enum.Enum):
    PLAN = "plan"
    DIFF = "diff"
    REPORT = "report"
    TEST_RESULT = "test_result"
    PROPOSAL = "proposal"
    REVIEW_FINDING = "review_finding"
    FIX_PACKET = "fix_packet"
    #: Canonical record of what entered / came out of one agent run
    #: (SPEC Appendix B.1). Lifecycle events reference these instead of
    #: carrying prompt/response payloads, and remain pure lifecycle markers.
    RUN_INPUT = "run_input"
    RUN_OUTPUT = "run_output"
    OTHER = "other"


class Artifact(BaseModel):
    """Durable output produced by an agent run (SPEC §5)."""

    id: str = Field(default_factory=new_id)
    task_id: str | None = None
    run_id: str | None = None
    kind: ArtifactKind
    content_ref: str | None = Field(
        default=None,
        description="Pointer to stored content (path or blob key) for large artifacts.",
    )
    content: str | None = Field(default=None, description="Inline content for small artifacts.")
    created_at: datetime = Field(default_factory=utcnow)


# ---------------------------------------------------------------------------
# P6.1 structured review contracts — artifact payloads, not database rows.
# ---------------------------------------------------------------------------

_Digest = Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
_RequiredId = Annotated[StrictStr, Field(min_length=1)]
_BoundedText = Annotated[StrictStr, Field(min_length=1, max_length=4_000)]


def _nonblank(value: str) -> str:
    if not value.strip():
        raise ValueError("value must be nonblank")
    return value


class ReviewSeverity(str, enum.Enum):
    """Reviewer-provided ordering metadata; never a blocking threshold."""

    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class ReviewVerdict(str, enum.Enum):
    PASS = "pass"
    FINDINGS = "findings"


class ReviewLocationPayload(BaseModel):
    """Optional workspace-relative source hint authored by a reviewer."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: Annotated[StrictStr, Field(min_length=1, max_length=1_024)]
    start_line: StrictInt | None = Field(default=None, ge=1, le=1_000_000)
    end_line: StrictInt | None = Field(default=None, ge=1, le=1_000_000)

    @field_validator("path")
    @classmethod
    def _workspace_relative_path(cls, value: str) -> str:
        _nonblank(value)
        if (
            value != value.strip()
            or "\\" in value
            or value.startswith("/")
            or re.match(r"^[A-Za-z]:", value)
            or any(part in ("", ".", "..") for part in value.split("/"))
            or any(ord(char) < 32 or ord(char) == 127 for char in value)
        ):
            raise ValueError("location.path must be a normalized workspace-relative '/' path")
        return value

    @model_validator(mode="after")
    def _line_range(self) -> ReviewLocationPayload:
        if self.end_line is not None and self.start_line is None:
            raise ValueError("location.end_line requires location.start_line")
        if (
            self.start_line is not None
            and self.end_line is not None
            and self.end_line < self.start_line
        ):
            raise ValueError("location.end_line must not precede location.start_line")
        return self


class ReviewFindingPayload(BaseModel):
    """One actionable finding in a ``relay.review.v1`` report."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: Annotated[StrictStr, Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")]
    severity: ReviewSeverity
    title: Annotated[StrictStr, Field(min_length=1, max_length=200)]
    description: _BoundedText
    requested_change: _BoundedText
    validation_expectation: _BoundedText
    location: ReviewLocationPayload | None = None

    @field_validator("title", "description", "requested_change", "validation_expectation")
    @classmethod
    def _nonblank_text(cls, value: str) -> str:
        return _nonblank(value)


class ReviewReportPayload(BaseModel):
    """The strict object a reviewer emits for ``relay.review.v1``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["relay.review.v1"]
    verdict: ReviewVerdict
    summary: _BoundedText
    findings: tuple[ReviewFindingPayload, ...] = Field(max_length=100)

    @field_validator("summary")
    @classmethod
    def _nonblank_summary(cls, value: str) -> str:
        return _nonblank(value)

    @model_validator(mode="after")
    def _verdict_matches_findings(self) -> ReviewReportPayload:
        if self.verdict is ReviewVerdict.PASS and self.findings:
            raise ValueError("verdict 'pass' requires an empty findings list")
        if self.verdict is ReviewVerdict.FINDINGS and not self.findings:
            raise ValueError("verdict 'findings' requires at least one finding")
        ids = [finding.id for finding in self.findings]
        if len(ids) != len(set(ids)):
            raise ValueError("finding ids must be unique")
        return self


class ReviewSubjectPayload(BaseModel):
    """Pinned build inputs a review assessed; assigned only by Relay."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: _RequiredId
    plan_run_id: _RequiredId
    plan_artifact_id: _RequiredId
    plan_digest: _Digest
    implementation_run_id: _RequiredId
    diff_artifact_id: _RequiredId
    diff_digest: _Digest
    verification_evidence_id: _RequiredId
    verification_tool_run_id: _RequiredId
    test_result_artifact_id: _RequiredId
    test_result_digest: _Digest


class ReviewSourcesPayload(BaseModel):
    """Subject references plus the reviewer output that authored the review."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    subject: ReviewSubjectPayload
    review_run_id: _RequiredId
    review_output_artifact_id: _RequiredId
    review_output_digest: _Digest


class ReviewRecordPayload(BaseModel):
    """Canonical ``REVIEW_FINDING`` content (``relay.review.record.v1``)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["relay.review.record.v1"]
    task_id: _RequiredId
    report: ReviewReportPayload
    sources: ReviewSourcesPayload

    @model_validator(mode="after")
    def _task_matches_sources(self) -> ReviewRecordPayload:
        if self.task_id != self.sources.subject.task_id:
            raise ValueError("review record task_id must match its pinned subject")
        return self


class FixPacketPayload(BaseModel):
    """Canonical ``FIX_PACKET`` content (``relay.fix_packet.v1``)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["relay.fix_packet.v1"]
    task_id: _RequiredId
    review_artifact_id: _RequiredId
    review_digest: _Digest
    summary: _BoundedText
    sources: ReviewSourcesPayload
    instructions: tuple[StrictStr, ...] = Field(min_length=1)
    findings: tuple[ReviewFindingPayload, ...] = Field(min_length=1, max_length=100)

    @field_validator("summary")
    @classmethod
    def _nonblank_packet_summary(cls, value: str) -> str:
        return _nonblank(value)

    @model_validator(mode="after")
    def _task_matches_sources(self) -> FixPacketPayload:
        if self.task_id != self.sources.subject.task_id:
            raise ValueError("fix packet task_id must match its pinned subject")
        return self


class InvalidReviewDiagnosticPayload(BaseModel):
    """Safe diagnostic report for a review run whose output failed validation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["relay.review.invalid.v1"]
    task_id: _RequiredId
    code: _RequiredId
    review_run_id: _RequiredId | None = None
    review_output_artifact_id: _RequiredId | None = None


class DecisionStatus(str, enum.Enum):
    PROPOSED = "proposed"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


class Decision(BaseModel):
    """A decision emerging from discussion, with full provenance (SPEC §5/§16).

    Shaped so `relay why <id>` can reconstruct: who proposed it, who
    challenged it, what evidence verified it, and which alternative lost.
    """

    id: str = Field(default_factory=new_id)
    statement: str
    rationale: str | None = None
    proposed_by: str | None = None
    supported_by: list[str] = Field(default_factory=list)
    challenged_by: list[str] = Field(default_factory=list)
    verified_by: str | None = None
    accepted_by: str | None = None
    alternatives_considered: list[str] = Field(default_factory=list)
    primary_objection: str | None = None
    status: DecisionStatus = DecisionStatus.PROPOSED
    room_id: str | None = None
    task_id: str | None = None
    created_at: datetime = Field(default_factory=utcnow)


class ApprovalStatus(str, enum.Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class Approval(BaseModel):
    """Human verdict on a gated action (SPEC §19)."""

    id: str = Field(default_factory=new_id)
    action: Action
    requested_by: str | None = Field(default=None, description="Requesting agent.")
    reason: str | None = None
    status: ApprovalStatus = ApprovalStatus.PENDING
    decided_by: str | None = None
    decided_at: datetime | None = None
    task_id: str | None = None
    created_at: datetime = Field(default_factory=utcnow)


class ToolRun(BaseModel):
    """Audit record of one tool execution through the permission gate (SPEC §17)."""

    id: str = Field(default_factory=new_id)
    parent_run_id: str | None = Field(default=None, description="Agent Run that triggered it.")
    tool: str = Field(examples=["git.diff", "filesystem.read", "shell.run"])
    arguments: dict[str, object] = Field(default_factory=dict)
    status: RunStatus = RunStatus.RUNNING
    result_ref: str | None = None
    error: str | None = None
    started_at: datetime = Field(default_factory=utcnow)
    ended_at: datetime | None = None


class EventType(str, enum.Enum):
    """System-history events for the append-only log (SPEC §15, App. A.2).

    Strictly distinct from :class:`MessageType`: this vocabulary describes
    what Relay's machinery did (state changes, runs, tool calls, approvals,
    decisions). Conversation content between agents travels on ``Message``
    with a ``MessageType`` and appears in the log only as a MESSAGE_SENT
    marker.
    """

    TASK_CREATED = "task_created"
    STATE_TRANSITIONED = "state_transitioned"
    AGENT_RUN_STARTED = "agent_run_started"
    AGENT_RUN_FINISHED = "agent_run_finished"
    MESSAGE_SENT = "message_sent"
    ARTIFACT_CREATED = "artifact_created"
    EVIDENCE_RECORDED = "evidence_recorded"
    TOOL_REQUESTED = "tool_requested"
    TOOL_COMPLETED = "tool_completed"
    APPROVAL_REQUESTED = "approval_requested"
    APPROVAL_GRANTED = "approval_granted"
    APPROVAL_REJECTED = "approval_rejected"
    DECISION_PROPOSED = "decision_proposed"
    DECISION_ACCEPTED = "decision_accepted"
    DECISION_REJECTED = "decision_rejected"
    #: P4.2 (frozen plan D10): delivery BINDING marker — Relay bound a
    #: persisted Message to a concrete recipient Run. Committed atomically in
    #: the delivery run's pre-provider Tx1; retained for failed runs (the
    #: outcome lives on the Run row + AGENT_RUN_FINISHED).
    MESSAGE_DELIVERED = "message_delivered"
    PROTOCOL_OUTCOME_RECORDED = "protocol_outcome_recorded"


class EvidenceRecord(BaseModel):
    """Immutable proof that verification happened (SPEC §6, App. A.1).

    A ``kind`` value in a caller's hand is a claim; a provenance-backed
    record inside an ``EvidenceStore`` is proof. Stores refuse records
    whose kind demands linkage fields (run, tool run) or a producer
    prefix they may not attest — see ``relay.core.evidence``.

    Frozen: evidence, once recorded, never mutates.
    """

    model_config = ConfigDict(frozen=True)

    id: str = Field(default_factory=new_id)
    kind: EvidenceKind
    task_id: str
    run_id: str | None = Field(default=None, description="Agent Run that produced it.")
    tool_run_id: str | None = Field(default=None, description="ToolRun that produced it.")
    artifact_id: str | None = Field(default=None, description="Artifact backing it.")
    produced_by: str = Field(
        description="Producer identity: 'agent:<name>', 'human:<name>' or 'relay:<component>'.",
    )
    created_at: datetime = Field(default_factory=utcnow)


class EventLogEntry(BaseModel):
    """Append-only system event; history is rebuildable from these (SPEC §15).

    ``type`` uses the system-level :class:`EventType` vocabulary — never
    conversation semantics. Agent messages appear here only as
    MESSAGE_SENT markers pointing at the Message record.

    ``sequence`` is assigned by the store on insert, never by callers.
    """

    sequence: int | None = None
    stage_key: str | None = None
    room_id: str | None = None
    task_id: str | None = None
    sender: str | None = None
    recipient: str | None = None
    type: EventType
    content: str
    references: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)

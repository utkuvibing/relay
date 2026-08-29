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
import uuid
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field

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
    members: list[RoomMember] = Field(default_factory=list)
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
    sender: str
    recipient: str | None = Field(
        default=None,
        description="Resolved logical-agent identity; None means broadcast to the room.",
    )
    recipient_role: str | None = Field(
        default=None,
        description="Original role address when the sender addressed a role; else None.",
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
    room_id: str | None = None
    task_id: str | None = None
    sender: str | None = None
    recipient: str | None = None
    type: EventType
    content: str
    references: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)

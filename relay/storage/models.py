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

from pydantic import BaseModel, Field

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
    started_at: datetime = Field(default_factory=utcnow)
    ended_at: datetime | None = None


class MessageType(str, enum.Enum):
    """Vocabulary of the conversation bus (SPEC §9, §15)."""

    OPINION = "opinion"
    CHALLENGE = "challenge"
    REBUTTAL = "rebuttal"
    FINAL_POSITION = "final_position"
    SYNTHESIS = "synthesis"
    REVIEW_FINDING = "review_finding"
    SYSTEM = "system"


class Message(BaseModel):
    """First-class inter-agent communication record (SPEC §5/§9)."""

    id: str = Field(default_factory=new_id)
    sender: str
    recipient: str | None = Field(
        default=None,
        description="None means broadcast to the room.",
    )
    room_id: str | None = None
    task_id: str | None = None
    type: MessageType
    content: str
    references: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)


class ArtifactKind(str, enum.Enum):
    PLAN = "plan"
    DIFF = "diff"
    REPORT = "report"
    TEST_RESULT = "test_result"
    PROPOSAL = "proposal"
    REVIEW_FINDING = "review_finding"
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


class EventLogEntry(BaseModel):
    """Append-only event; history is rebuildable from these (SPEC §15).

    ``sequence`` is assigned by the store on insert, never by callers.
    """

    sequence: int | None = None
    room_id: str | None = None
    task_id: str | None = None
    sender: str | None = None
    recipient: str | None = None
    type: MessageType
    content: str
    references: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)

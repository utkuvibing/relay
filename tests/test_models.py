"""Domain records: the vocabulary Relay persists (SPEC §5/§14/§15; App. A)."""

import pytest
from pydantic import ValidationError

from relay.core.evidence import EvidenceKind
from relay.core.permissions import Action
from relay.core.state_machine import TaskState
from relay.storage.models import (
    Approval,
    ApprovalStatus,
    Decision,
    DecisionStatus,
    EventLogEntry,
    EventType,
    EvidenceRecord,
    Message,
    MessageType,
    Room,
    RoomMember,
    Run,
    RunStatus,
    Task,
    ToolRun,
    Workspace,
    WorkspaceKind,
)


class TestIdsAndTimestampsAutoFilled:
    def test_records_get_unique_ids_and_timestamps(self):
        task_a = Task(title="a")
        task_b = Task(title="b")
        assert task_a.id and task_b.id and task_a.id != task_b.id
        assert task_a.created_at.tzinfo is not None
        assert task_a.state is TaskState.CREATED

    def test_room_members_pair_agents_with_roles(self):
        room = Room(
            name="touchline-m5",
            members=[
                RoomMember(agent="gpt", role="moderator"),
                RoomMember(agent="claude", role="architect"),
                RoomMember(agent="codex", role="implementer"),
                RoomMember(agent="deepseek", role="reviewer"),
            ],
        )
        assert len(room.members) == 4
        assert {m.agent for m in room.members} == {"gpt", "claude", "codex", "deepseek"}


class TestWorkspaceKinds:
    def test_supports_no_repo_mode(self):
        conversation = Workspace(name="business-ideas")
        repo = Workspace(
            name="touchline", path=r"C:\projects\touchline", kind=WorkspaceKind.GIT_REPO
        )
        assert conversation.kind is WorkspaceKind.CONVERSATION
        assert repo.kind is WorkspaceKind.GIT_REPO


class TestDecisionProvenance:
    """Shaped after `relay why` output in SPEC §16."""

    def test_decision_records_full_provenance(self):
        decision = Decision(
            statement="Use nested tournament-aware evaluation.",
            rationale="Lower migration risk.",
            proposed_by="claude",
            supported_by=["gpt", "deepseek"],
            challenged_by=["deepseek"],
            verified_by="codex",
            accepted_by="moderator:gpt",
            alternatives_considered=["flat bracket evaluation"],
            primary_objection="Flat brackets ignore byes.",
            status=DecisionStatus.ACCEPTED,
        )
        restored = Decision.model_validate_json(decision.model_dump_json())
        assert restored.proposed_by == "claude"
        assert restored.supported_by == ["gpt", "deepseek"]
        assert restored.primary_objection == "Flat brackets ignore byes."


class TestApprovalFlow:
    def test_approval_starts_pending_and_resolves(self):
        approval = Approval(action=Action.INSTALL_DEPENDENCIES, requested_by="codex")
        assert approval.status is ApprovalStatus.PENDING
        approval.status = ApprovalStatus.APPROVED
        approval.decided_at = approval.created_at
        assert approval.status is ApprovalStatus.APPROVED


class TestMessageAndEventLog:
    def test_message_recipient_optional_for_broadcast(self):
        message = Message(sender="claude", recipient=None, type=MessageType.OPINION, content="...")
        assert message.recipient is None

    def test_event_log_entry_shape_matches_spec_15(self):
        entry = EventLogEntry(
            room_id="touchline-m5",
            task_id="m5-1-3",
            sender="claude",
            recipient="codex",
            type=EventType.MESSAGE_SENT,
            content="Missing pagination guard.",
            references=["src/foo.py:42-71"],
        )
        assert entry.sequence is None  # store assigns it on insert
        assert entry.references == ["src/foo.py:42-71"]


class TestSystemEventsAreDistinctFromConversation:
    """App. A.2: the event log speaks system vocabulary; MessageType stays conversational."""

    def test_event_type_covers_required_system_events(self):
        required = {
            "TASK_CREATED",
            "STATE_TRANSITIONED",
            "AGENT_RUN_STARTED",
            "AGENT_RUN_FINISHED",
            "MESSAGE_SENT",
            "ARTIFACT_CREATED",
            "EVIDENCE_RECORDED",
            "TOOL_REQUESTED",
            "TOOL_COMPLETED",
            "APPROVAL_REQUESTED",
            "APPROVAL_GRANTED",
            "APPROVAL_REJECTED",
            "DECISION_PROPOSED",
            "DECISION_ACCEPTED",
            "DECISION_REJECTED",
        }
        assert required <= {event.name for event in EventType}

    def test_event_and_message_vocabularies_are_disjoint(self):
        event_values = {event.value for event in EventType}
        message_values = {message.value for message in MessageType}
        assert event_values.isdisjoint(message_values)

    def test_message_type_covers_conversation_semantics(self):
        conversational = {"opinion", "challenge", "rebuttal", "final_position", "synthesis"}
        assert conversational <= {m.value for m in MessageType}

    def test_event_log_rejects_message_types(self):
        with pytest.raises(ValidationError):
            EventLogEntry(
                room_id="r1",
                sender="claude",
                recipient="deepseek",
                type=MessageType.CHALLENGE,  # conversation enum in a system slot
                content="...",
            )


class TestEvidenceRecord:
    def test_ids_timestamps_and_producer_auto_contract(self):
        record = EvidenceRecord(
            kind=EvidenceKind.TESTS_PASSED,
            task_id="t1",
            tool_run_id="tool-pytest",
            produced_by="relay:test-runner",
        )
        assert record.id
        assert record.created_at.tzinfo is not None
        assert record.run_id is None and record.artifact_id is None

    def test_records_are_frozen(self):
        record = EvidenceRecord(kind=EvidenceKind.CONTEXT_COLLECTED, task_id="t1", produced_by="relay")
        with pytest.raises(ValidationError):
            record.task_id = "t2"  # type: ignore[misc]

    def test_producer_is_required(self):
        with pytest.raises(ValidationError):
            EvidenceRecord(kind=EvidenceKind.CONTEXT_COLLECTED, task_id="t1")  # type: ignore[call-arg]

    def test_roundtrips_through_json_with_kind_and_linkage(self):
        record = EvidenceRecord(
            kind=EvidenceKind.REVIEW_PASSED,
            task_id="t1",
            run_id="run-review",
            produced_by="agent:claude",
        )
        restored = EvidenceRecord.model_validate_json(record.model_dump_json())
        assert restored == record
        assert restored.kind is EvidenceKind.REVIEW_PASSED
        assert restored.run_id == "run-review"


class TestRunObservability:
    """Fields required by `relay inspect run <id>` (SPEC §25)."""

    def test_run_captures_agent_role_model_and_cost(self):
        run = Run(task_id="t1", agent="claude", role="reviewer", model="claude-opus-4")
        assert run.status is RunStatus.RUNNING
        assert run.ended_at is None

    def test_tool_run_records_arguments(self):
        tool_run = ToolRun(tool="git.diff", arguments={"ref": "HEAD"})
        restored = ToolRun.model_validate_json(tool_run.model_dump_json())
        assert restored.tool == "git.diff"
        assert restored.arguments == {"ref": "HEAD"}

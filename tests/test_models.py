"""Domain records: the vocabulary Relay persists (SPEC §5/§14/§15; App. A)."""

import pytest
from pydantic import ValidationError

from relay.agents.base import AgentRequest, AgentResponse, AgentRole, TokenUsage
from relay.core.evidence import EvidenceKind
from relay.core.permissions import Action
from relay.core.state_machine import TaskState
from relay.storage.models import (
    Approval,
    ApprovalStatus,
    Artifact,
    ArtifactKind,
    BuildBaselineManifestPayload,
    BuildBaselineRecordPayload,
    BuildLoopRecordPayload,
    BuildRequestRecordPayload,
    Decision,
    DecisionStatus,
    EventLogEntry,
    EventType,
    EvidenceRecord,
    Message,
    MessageType,
    ReviewFindingPayload,
    ReviewLocationPayload,
    ReviewReportPayload,
    ReviewSeverity,
    ReviewVerdict,
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

    def test_d5_vocabulary_extension_is_additive(self):
        """P4.1 (App. D.5): lowercase concept-form additions beside the frozen set."""
        frozen = {
            "opinion",
            "challenge",
            "rebuttal",
            "final_position",
            "synthesis",
            "review_finding",
            "system",
        }
        values = {m.value for m in MessageType}
        assert frozen <= values
        assert {
            "clarification_request",
            "clarification_response",
            "proposal",
            "note",
        } <= values
        event_values = {event.value for event in EventType}
        assert event_values.isdisjoint(values)

    def test_message_blocking_and_role_addressing_defaults(self):
        """P4.1 additive fields: blocking metadata off; direct addressing."""
        message = Message(sender="claude", recipient="codex", type=MessageType.OPINION, content="x")
        assert message.blocking is False
        assert message.recipient_role is None

    def test_message_role_addressing_shape(self):
        """P4.1 (plan D3): role addressing resolves recipient, keeps role provenance."""
        message = Message(
            sender="gpt",
            recipient="claude",
            recipient_role="reviewer",
            type=MessageType.CHALLENGE,
            content="justified?",
            blocking=True,
            references=["plan:abc"],
        )
        restored = Message.model_validate_json(message.model_dump_json())
        assert restored == message
        assert restored.recipient == "claude" and restored.recipient_role == "reviewer"
        assert restored.blocking is True

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


class TestStructuredReviewPayloads:
    """P6.1 domain payload vocabulary is strict and provider-neutral."""

    def test_report_payload_is_strict_and_frozen(self):
        finding = ReviewFindingPayload(
            id="F1",
            severity=ReviewSeverity.LOW,
            title="Issue",
            description="What is wrong.",
            requested_change="What to change.",
            validation_expectation="How it is checked.",
            location=ReviewLocationPayload(path="src/app.py", start_line=1),
        )
        report = ReviewReportPayload(
            schema_version="relay.review.v1",
            verdict=ReviewVerdict.FINDINGS,
            summary="Needs work.",
            findings=(finding,),
        )
        assert report.findings[0].severity is ReviewSeverity.LOW
        with pytest.raises(ValidationError):
            report.summary = "mutate"  # type: ignore[misc]

    def test_report_rejects_extra_fields_and_nonstrict_types(self):
        with pytest.raises(ValidationError):
            ReviewReportPayload.model_validate(
                {
                    "schema_version": "relay.review.v1",
                    "verdict": "pass",
                    "summary": "ok",
                    "findings": [],
                    "provider_note": "vendor-specific",
                }
            )
        with pytest.raises(ValidationError):
            ReviewLocationPayload.model_validate(
                {"path": "src/app.py", "start_line": "3"}
            )
        with pytest.raises(ValidationError):
            ReviewLocationPayload.model_validate(
                {"path": "src/app.py", "start_line": True}
            )

    def test_fix_packet_kind_is_additive(self):
        assert ArtifactKind.FIX_PACKET.value == "fix_packet"


class TestBuildLoopRecordPayload:
    """P6.2: the loop-stop observation is strict, frozen, and canonical."""

    def test_payload_is_strict_and_frozen(self):
        payload = BuildLoopRecordPayload(
            schema_version="relay.build.loop.v1",
            task_id="task-1",
            reason="budget_exhausted",
            fix_runs_used=3,
        )
        assert payload.last_diff_artifact_id is None
        with pytest.raises(ValidationError):
            payload.reason = "mutate"  # type: ignore[misc]

    def test_rejects_extra_fields_negative_counts_and_wrong_version(self):
        with pytest.raises(ValidationError):
            BuildLoopRecordPayload.model_validate(
                {
                    "schema_version": "relay.build.loop.v1",
                    "task_id": "task-1",
                    "reason": "budget_exhausted",
                    "fix_runs_used": 1,
                    "provider_note": "vendor-specific",
                }
            )
        with pytest.raises(ValidationError):
            BuildLoopRecordPayload.model_validate(
                {
                    "schema_version": "relay.build.loop.v2",
                    "task_id": "task-1",
                    "reason": "budget_exhausted",
                    "fix_runs_used": 1,
                }
            )
        with pytest.raises(ValidationError):
            BuildLoopRecordPayload.model_validate(
                {
                    "schema_version": "relay.build.loop.v1",
                    "task_id": "task-1",
                    "reason": "budget_exhausted",
                    "fix_runs_used": -1,
                }
            )
        with pytest.raises(ValidationError):
            BuildLoopRecordPayload.model_validate(
                {
                    "schema_version": "relay.build.loop.v1",
                    "task_id": "task-1",
                    "reason": "budget_exhausted",
                    "fix_runs_used": "1",
                }
            )

    def test_reason_vocabulary_is_strict(self):
        """Only the two persisted loop stops validate — nothing else."""
        for reason in ("budget_exhausted", "no_workspace_change"):
            payload = BuildLoopRecordPayload.model_validate(
                {
                    "schema_version": "relay.build.loop.v1",
                    "task_id": "task-1",
                    "reason": reason,
                    "fix_runs_used": 0,
                }
            )
            assert payload.reason == reason
        for rejected in ("banana", "pass_promoted", "review_blocked", ""):
            with pytest.raises(ValidationError):
                BuildLoopRecordPayload.model_validate(
                    {
                        "schema_version": "relay.build.loop.v1",
                        "task_id": "task-1",
                        "reason": rejected,
                        "fix_runs_used": 0,
                    }
                )


class TestBuildResumePayloads:
    """P6.3 resume contracts are strict, frozen, and canonical."""

    def test_request_record_is_strict_and_frozen(self):
        payload = BuildRequestRecordPayload(
            schema_version="relay.build.request.v1",
            task_id="task-1",
            prompt="write implemented.txt",
            implementer="impl",
            model="fake-1",
        )
        assert payload.model == "fake-1"
        with pytest.raises(ValidationError):
            payload.prompt = "mutate"  # type: ignore[misc]

    def test_request_record_rejects_extra_blank_and_wrong_version(self):
        with pytest.raises(ValidationError):
            BuildRequestRecordPayload.model_validate(
                {
                    "schema_version": "relay.build.request.v1",
                    "task_id": "task-1",
                    "prompt": "x",
                    "implementer": "impl",
                    "provider_note": "vendor-specific",
                }
            )
        with pytest.raises(ValidationError):
            BuildRequestRecordPayload.model_validate(
                {
                    "schema_version": "relay.build.request.v2",
                    "task_id": "task-1",
                    "prompt": "x",
                    "implementer": "impl",
                }
            )
        with pytest.raises(ValidationError):
            BuildRequestRecordPayload.model_validate(
                {
                    "schema_version": "relay.build.request.v1",
                    "task_id": "task-1",
                    "prompt": "   ",
                    "implementer": "impl",
                }
            )

    def test_baseline_manifest_validates_paths_and_digests(self):
        digest = "a" * 64
        manifest = BuildBaselineManifestPayload(
            schema_version="relay.build.baseline.manifest.v1",
            task_id="task-1",
            files={"src/app.py": digest},
            oversized_paths=("big.bin",),
        )
        assert manifest.files["src/app.py"] == digest
        for bad_path in ("../up.py", "C:\\abs.py", "/abs.py", "a//b.py", " a.py"):
            with pytest.raises(ValidationError):
                BuildBaselineManifestPayload.model_validate(
                    {
                        "schema_version": "relay.build.baseline.manifest.v1",
                        "task_id": "task-1",
                        "files": {bad_path: digest},
                    }
                )
        with pytest.raises(ValidationError):
            BuildBaselineManifestPayload.model_validate(
                {
                    "schema_version": "relay.build.baseline.manifest.v1",
                    "task_id": "task-1",
                    "files": {"src/app.py": "not-a-digest"},
                }
            )

    def test_baseline_pin_is_strict(self):
        payload = BuildBaselineRecordPayload(
            schema_version="relay.build.baseline.v1",
            task_id="task-1",
            manifest_digest="b" * 64,
            file_count=2,
            oversized_count=1,
        )
        assert payload.file_count == 2
        with pytest.raises(ValidationError):
            BuildBaselineRecordPayload.model_validate(
                {
                    "schema_version": "relay.build.baseline.v1",
                    "task_id": "task-1",
                    "manifest_digest": "b" * 64,
                    "file_count": -1,
                }
            )
        with pytest.raises(ValidationError):
            BuildBaselineRecordPayload.model_validate(
                {
                    "schema_version": "relay.build.baseline.v1",
                    "task_id": "task-1",
                    "manifest_digest": "b" * 64,
                    "file_count": 0,
                    "provider_note": "x",
                }
            )


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
        record = EvidenceRecord(
            kind=EvidenceKind.CONTEXT_COLLECTED, task_id="t1", produced_by="relay"
        )
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


class TestRunIOArtifacts:
    """Phase 1 amendment: run I/O persists as first-class artifacts (App. B.1)."""

    def test_run_input_and_output_kinds_exist(self):
        assert ArtifactKind.RUN_INPUT.value == "run_input"
        assert ArtifactKind.RUN_OUTPUT.value == "run_output"

    def test_run_io_artifacts_are_tied_to_a_run(self):
        run = Run(agent="gpt", role=AgentRole.RESEARCHER)
        prompt_artifact = Artifact(kind=ArtifactKind.RUN_INPUT, run_id=run.id, content="prompt")
        output_artifact = Artifact(kind=ArtifactKind.RUN_OUTPUT, run_id=run.id, content="answer")
        assert prompt_artifact.run_id == output_artifact.run_id == run.id
        assert prompt_artifact.content == "prompt"

    def test_run_output_without_usage_is_valid(self):
        # App. B.2: usage/cost are optional; harness-backed runs may carry none.
        artifact = Artifact(kind=ArtifactKind.RUN_OUTPUT, run_id="r1", content="answer")
        assert artifact.model_dump_json()


class TestTokenUsageIsOptional:
    """Transport-neutral Agent layer (SPEC App. B.2)."""

    def test_token_usage_all_fields_optional(self):
        usage = TokenUsage()
        assert usage.input_tokens is None
        assert usage.output_tokens is None
        assert usage.cost_usd is None

    def test_agent_request_response_have_no_transport_fields(self):
        request = AgentRequest(prompt="p", role=AgentRole.RESEARCHER)
        dump = request.model_dump()
        forbidden = {"api_key", "url", "headers", "auth", "model", "cost"}
        assert not (forbidden & set(dump))
        response = AgentResponse(agent="gpt", role=AgentRole.RESEARCHER, output="o", usage=None)
        assert not (forbidden & set(response.model_dump()))

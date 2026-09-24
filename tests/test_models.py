"""Domain records: the vocabulary Relay persists (SPEC §5/§14/§15; App. A)."""

import pytest
from pydantic import ValidationError

from relay.core.evidence import EvidenceKind
from relay.storage.models import (
    BuildBaselineManifestPayload,
    BuildBaselineRecordPayload,
    BuildLoopRecordPayload,
    BuildRequestRecordPayload,
    EventLogEntry,
    EvidenceRecord,
    MessageType,
    ReviewFindingPayload,
    ReviewLocationPayload,
    ReviewReportPayload,
    ReviewSeverity,
    ReviewVerdict,
    RoomDecisionPayload,
    RoomPlanFreezePayload,
)


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
            ReviewLocationPayload.model_validate({"path": "src/app.py", "start_line": "3"})
        with pytest.raises(ValidationError):
            ReviewLocationPayload.model_validate({"path": "src/app.py", "start_line": True})


class TestBuildLoopRecordPayload:
    """P6.2: the loop-stop observation is strict, frozen, and canonical."""

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


class TestRoomCanonicalRecordPayloads:
    """P7.3 (App. D.3): strict Room payload contracts for freeze and decisions."""

    def test_decision_payload_accepts_and_rejects_supersession(self):
        accepted = RoomDecisionPayload(
            schema_version="relay.room_decision.v1",
            outcome="accept",
            statement="use bundle registries",
            supersedes_decision_id="d1",
            references=("finding:f1",),
        )
        assert accepted.supersedes_decision_id == "d1"
        rejected = RoomDecisionPayload(
            schema_version="relay.room_decision.v1",
            outcome="reject",
            statement="keep the current design",
        )
        assert rejected.supersedes_decision_id is None
        with pytest.raises(ValidationError, match="rejected decision cannot supersede"):
            RoomDecisionPayload(
                schema_version="relay.room_decision.v1",
                outcome="reject",
                statement="no",
                supersedes_decision_id="d1",
            )
        with pytest.raises(ValidationError):
            RoomDecisionPayload(
                schema_version="relay.room_decision.v1",
                outcome="accept",
                statement="   ",
            )


class TestPersistedPayloadValidation:
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

    def test_plan_freeze_payload_roundtrips_and_forbids_extras(self):
        payload = RoomPlanFreezePayload(
            schema_version="relay.room.plan_freeze.v1",
            room_id="r1",
            task_id="t1",
            plan_artifact_id="a1",
            source_message_id="m2",
            source_run_id="run1",
            frozen_by="human:utku",
        )
        assert payload.supersedes_plan_artifact_id is None
        decoded = RoomPlanFreezePayload.model_validate_json(payload.model_dump_json())
        assert decoded == payload
        with pytest.raises(ValidationError):
            RoomPlanFreezePayload.model_validate(
                {
                    "schema_version": "relay.room.plan_freeze.v1",
                    "room_id": "r1",
                    "task_id": "t1",
                    "plan_artifact_id": "a1",
                    "source_message_id": "m2",
                    "source_run_id": "run1",
                    "frozen_by": "human:utku",
                    "frozen_at": "2026-01-01T00:00:00+00:00",
                }
            )

    def test_event_log_rejects_message_types(self):
        with pytest.raises(ValidationError):
            EventLogEntry(
                room_id="r1",
                sender="claude",
                recipient="deepseek",
                type=MessageType.CHALLENGE,  # conversation enum in a system slot
                content="...",
            )

    def test_producer_is_required(self):
        with pytest.raises(ValidationError):
            EvidenceRecord(kind=EvidenceKind.CONTEXT_COLLECTED, task_id="t1")  # type: ignore[call-arg]

"""P6.1 structured reviews and deterministic fix packets.

These tests pin the contract boundary: reviewer output is an untrusted JSON
object, Relay owns provenance and canonical encoding, and fix packets are
pure deterministic derivatives of the exact persisted source bytes.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace

import pytest

from relay.core.evidence import EvidenceKind
from relay.core.reviews import (
    FIX_PACKET_INSTRUCTIONS,
    ReviewContractError,
    ReviewInputs,
    artifact_digest,
    build_fix_packet,
    build_review_record,
    build_review_sources,
    build_review_subject,
    decode_fix_packet,
    decode_review_record,
    encode_fix_packet,
    encode_review_record,
    parse_review,
    verify_review_sources,
    verify_review_subject,
)
from relay.storage.models import (
    Artifact,
    ArtifactKind,
    EvidenceRecord,
    ReviewRecordPayload,
    ReviewReportPayload,
    ReviewVerdict,
    Run,
    RunStatus,
    Task,
    ToolRun,
)


def _report(*, verdict: str = "findings") -> dict[str, object]:
    findings: list[dict[str, object]] = []
    if verdict == "findings":
        findings = [
            {
                "id": "F1",
                "severity": "low",
                "title": "Missing focused check",
                "description": "The change needs a focused assertion.",
                "requested_change": "Add the assertion described by the plan.",
                "validation_expectation": "The configured verification command passes.",
                "location": {"path": "src/example.py", "start_line": 3, "end_line": 4},
            }
        ]
    return {
        "schema_version": "relay.review.v1",
        "verdict": verdict,
        "summary": "Review summary.",
        "findings": findings,
    }


def _inputs(task_id: str = "task-1") -> ReviewInputs:
    task = Task(id=task_id, title="Implement it")
    plan_run = Run(
        id="plan-run",
        task_id=task_id,
        agent="planner",
        role="planner",
        status=RunStatus.SUCCEEDED,
    )
    implementation_run = Run(
        id="impl-run",
        task_id=task_id,
        agent="impl",
        role="implementer",
        status=RunStatus.SUCCEEDED,
    )
    plan = Artifact(
        id="plan-artifact",
        task_id=task_id,
        run_id=plan_run.id,
        kind=ArtifactKind.PLAN,
        content="plan — ünicode\n",
    )
    diff = Artifact(
        id="diff-artifact",
        task_id=task_id,
        run_id=implementation_run.id,
        kind=ArtifactKind.DIFF,
        content="diff -- one\n",
    )
    test_result = Artifact(
        id="test-artifact",
        task_id=task_id,
        kind=ArtifactKind.TEST_RESULT,
        content="exit=0\n",
    )
    tool_run = ToolRun(
        id="tool-run",
        parent_run_id=None,
        tool="verification",
        status=RunStatus.SUCCEEDED,
        result_ref=test_result.id,
    )
    evidence = EvidenceRecord(
        id="evidence-1",
        kind=EvidenceKind.TESTS_PASSED,
        task_id=task_id,
        tool_run_id=tool_run.id,
        artifact_id=test_result.id,
        produced_by="relay:verification",
    )
    return ReviewInputs(
        task=task,
        plan_run=plan_run,
        plan_artifact=plan,
        implementation_run=implementation_run,
        diff_artifact=diff,
        verification_evidence=evidence,
        verification_tool_run=tool_run,
        test_result_artifact=test_result,
    )


def _review_run(task_id: str = "task-1") -> Run:
    return Run(
        id="review-run",
        task_id=task_id,
        agent="reviewer",
        role="reviewer",
        status=RunStatus.SUCCEEDED,
    )


def _review_output(run: Run, text: str) -> Artifact:
    return Artifact(
        id="review-output",
        run_id=run.id,
        kind=ArtifactKind.RUN_OUTPUT,
        content=text,
    )


def _findings_record(inputs: ReviewInputs | None = None) -> tuple[
    ReviewInputs,
    Run,
    Artifact,
    ReviewRecordPayload,
]:
    actual = inputs or _inputs()
    raw = json.dumps(_report(), ensure_ascii=False)
    report = parse_review(raw)
    subject = build_review_subject(actual)
    run = _review_run(actual.task.id)
    output = _review_output(run, raw)
    sources = build_review_sources(subject, actual, run, output)
    record = build_review_record(actual.task, report, sources)
    return actual, run, output, record


class TestStrictReviewerOutput:
    def test_pass_and_findings_reports_parse(self):
        passed = parse_review(json.dumps(_report(verdict="pass")))
        assert passed.verdict is ReviewVerdict.PASS
        assert passed.findings == ()

        findings = parse_review(json.dumps(_report()))
        assert findings.verdict is ReviewVerdict.FINDINGS
        assert findings.findings[0].severity.value == "low"

    @pytest.mark.parametrize(
        ("text", "code"),
        [
            ("## Review\nVERDICT: PASS", "invalid_json"),
            ("{", "invalid_json"),
            ("null", "invalid_review"),
            ('{"schema_version":"relay.review.v1","schema_version":"relay.review.v1","verdict":"pass","summary":"x","findings":[]}', "duplicate_key"),
            ('{"schema_version":"relay.review.v2","verdict":"pass","summary":"x","findings":[]}', "invalid_review"),
            ('{"schema_version":"relay.review.v1","verdict":"pass","findings":[]}', "invalid_review"),
            ('{"schema_version":"relay.review.v1","verdict":"findings","summary":"x","findings":[]}', "invalid_review"),
            ('{"schema_version":"relay.review.v1","verdict":"pass","summary":"x","findings":[],"extra":true}', "invalid_review"),
            ('{"schema_version":"relay.review.v1","verdict":"pass","summary":"x","findings":[],"score":NaN}', "nonfinite_json"),
        ],
    )
    def test_untrusted_output_fails_closed(self, text: str, code: str):
        with pytest.raises(ReviewContractError) as raised:
            parse_review(text)
        assert raised.value.code == code

    def test_contradictory_verdict_and_duplicate_finding_ids_reject(self):
        contradictory = _report()
        contradictory["verdict"] = "pass"
        with pytest.raises(ReviewContractError) as bad_verdict:
            parse_review(json.dumps(contradictory))
        assert bad_verdict.value.code == "invalid_review"

        duplicate = _report()
        assert isinstance(duplicate["findings"], list)
        duplicate["findings"].append(dict(duplicate["findings"][0]))
        with pytest.raises(ReviewContractError) as duplicate_id:
            parse_review(json.dumps(duplicate))
        assert duplicate_id.value.code == "invalid_review"

    def test_size_depth_and_location_bounds_reject(self):
        with pytest.raises(ReviewContractError) as too_large:
            parse_review(" " * 100_001)
        assert too_large.value.code == "review_too_large"

        deep: object = {}
        current = deep
        for _ in range(20):
            assert isinstance(current, dict)
            current["x"] = {}
            current = current["x"]
        report = _report(verdict="pass")
        report["extra"] = deep
        with pytest.raises(ReviewContractError) as too_deep:
            parse_review(json.dumps(report))
        assert too_deep.value.code == "json_too_deep"

        for path in ("/abs/file.py", "C:\\repo\\file.py", "../x.py", "a//b.py", "a\\b.py"):
            finding = _report()["findings"]
            assert isinstance(finding, list)
            first = dict(finding[0])
            first["location"] = {"path": path}
            bad = _report()
            bad["findings"] = [first]
            with pytest.raises(ReviewContractError):
                parse_review(json.dumps(bad))

    def test_deeply_nested_json_fails_closed_before_depth_check(self):
        # json.loads hits the interpreter recursion ceiling before the
        # explicit depth walk can run; the parser must still fail closed
        # with the safe json_too_deep code.
        with pytest.raises(ReviewContractError) as raised:
            parse_review("[" * 5000 + "]" * 5000)
        assert raised.value.code == "json_too_deep"

    def test_severity_is_metadata_only_and_never_changes_blocking(self):
        for severity in ("critical", "high", "medium", "low"):
            report = _report()
            findings = report["findings"]
            assert isinstance(findings, list)
            first = dict(findings[0])
            first["severity"] = severity
            report["findings"] = [first]
            parsed = parse_review(json.dumps(report))
            assert parsed.verdict is ReviewVerdict.FINDINGS
            assert parsed.findings[0].severity.value == severity


class TestCanonicalReviewAndPacket:
    def test_exact_utf8_digest_and_canonical_encoding(self):
        content = "résumé\r\n spaced "
        expected = hashlib.sha256(content.encode("utf-8")).hexdigest()
        assert artifact_digest(content) == expected

        inputs, run, output, record = _findings_record()
        encoded = encode_review_record(record)
        assert encoded == json.dumps(
            record.model_dump(mode="json"),
            sort_keys=True,
            ensure_ascii=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        assert "\r" not in encoded
        assert encoded.startswith('{"report":')
        assert decode_review_record(encoded) == record

        review_artifact = Artifact(
            kind=ArtifactKind.REVIEW_FINDING,
            task_id=inputs.task.id,
            run_id=run.id,
            content=encoded,
        )
        packet = build_fix_packet(review_artifact, inputs, run, output)
        assert packet.review_artifact_id == review_artifact.id
        assert packet.review_digest == artifact_digest(encoded)
        assert packet.instructions == FIX_PACKET_INSTRUCTIONS
        assert packet.findings == record.report.findings
        assert decode_fix_packet(encode_fix_packet(packet)) == packet
        assert encode_fix_packet(build_fix_packet(review_artifact, inputs, run, output)) == encode_fix_packet(packet)

    def test_persisted_record_may_exceed_the_untrusted_input_cap(self):
        # A reviewer report just under the 100k untrusted-input cap must stay
        # decodable after Relay wraps it with provenance metadata (and
        # ensure_ascii escaping expands non-ASCII content).
        inputs, _run, _output, record = _findings_record()
        emoji = "\U0001f600" * 4_000
        big_report = record.report.model_copy(
            update={
                "findings": (
                    record.report.findings[0].model_copy(
                        update={
                            "description": emoji,
                            "requested_change": emoji,
                            "validation_expectation": emoji,
                        }
                    ),
                )
            }
        )
        raw = json.dumps(
            ReviewReportPayload.model_validate(
                {
                    "schema_version": "relay.review.v1",
                    "verdict": "findings",
                    "summary": "Large but legal report.",
                    "findings": [big_report.findings[0].model_dump(mode="json")],
                }
            ).model_dump(mode="json"),
            ensure_ascii=False,
        )
        assert len(raw) <= 100_000  # legal untrusted reviewer input
        parsed = parse_review(raw)
        big_record = build_review_record(inputs.task, parsed, record.sources)
        encoded = encode_review_record(big_record)
        assert len(encoded) > 100_000  # provenance + escaping push it past the input cap
        assert decode_review_record(encoded) == big_record

    def test_persisted_canonical_content_still_has_its_own_bound(self):
        inputs, _run, _output, record = _findings_record()
        emoji = "\U0001f600" * 4_000
        findings = tuple(
            record.report.findings[0].model_copy(
                update={"id": f"F{index}", "description": emoji}
            )
            for index in range(1, 101)
        )
        report = ReviewReportPayload.model_validate(
            {
                "schema_version": "relay.review.v1",
                "verdict": "findings",
                "summary": "Maximal report.",
                "findings": [finding.model_dump(mode="json") for finding in findings],
            }
        )
        record = build_review_record(inputs.task, report, record.sources)
        encoded = encode_review_record(record)
        assert len(encoded) > 1_500_000
        with pytest.raises(ReviewContractError) as raised:
            decode_review_record(encoded)
        assert raised.value.code == "canonical_too_large"

    def test_sources_pin_ids_runs_and_exact_persisted_bytes(self):
        inputs, run, output, record = _findings_record()
        subject = record.sources.subject
        verify_review_subject(subject, inputs)
        verify_review_sources(record.sources, inputs, run, output)

        assert subject.plan_artifact_id == inputs.plan_artifact.id
        assert subject.plan_digest == artifact_digest(inputs.plan_artifact.content or "")
        assert subject.diff_digest == artifact_digest(inputs.diff_artifact.content or "")
        assert subject.test_result_digest == artifact_digest(
            inputs.test_result_artifact.content or ""
        )
        assert record.sources.review_run_id == run.id
        assert record.sources.review_output_artifact_id == output.id

    @pytest.mark.parametrize(
        "field",
        [
            "plan_artifact",
            "diff_artifact",
            "test_result_artifact",
        ],
    )
    def test_rejects_foreign_task_inputs(self, field: str):
        inputs = _inputs()
        foreign = getattr(inputs, field).model_copy(update={"task_id": "other-task"})
        bad = replace(inputs, **{field: foreign})
        with pytest.raises(ReviewContractError) as raised:
            build_review_subject(bad)
        assert raised.value.code in {"foreign_task", "invalid_context"}

    def test_rejects_wrong_kind_wrong_run_failed_tool_and_missing_content(self):
        inputs = _inputs()
        wrong_kind = replace(
            inputs,
            diff_artifact=inputs.diff_artifact.model_copy(
                update={"kind": ArtifactKind.REPORT}
            ),
        )
        with pytest.raises(ReviewContractError):
            build_review_subject(wrong_kind)

        wrong_run = replace(
            inputs,
            diff_artifact=inputs.diff_artifact.model_copy(update={"run_id": "other"}),
        )
        with pytest.raises(ReviewContractError):
            build_review_subject(wrong_run)

        failed_tool = replace(
            inputs,
            verification_tool_run=inputs.verification_tool_run.model_copy(
                update={"status": RunStatus.FAILED}
            ),
        )
        with pytest.raises(ReviewContractError):
            build_review_subject(failed_tool)

        missing = replace(
            inputs,
            test_result_artifact=inputs.test_result_artifact.model_copy(
                update={"content": None}
            ),
        )
        with pytest.raises(ReviewContractError) as raised:
            build_review_subject(missing)
        assert raised.value.code == "missing_content"

    def test_packet_rejects_digest_drift_and_pass_reviews(self):
        inputs, run, output, record = _findings_record()
        review_artifact = Artifact(
            kind=ArtifactKind.REVIEW_FINDING,
            task_id=inputs.task.id,
            run_id=run.id,
            content=encode_review_record(record),
        )
        drifted = replace(
            inputs,
            diff_artifact=inputs.diff_artifact.model_copy(
                update={"content": "diff -- changed\n"}
            ),
        )
        with pytest.raises(ReviewContractError) as raised:
            build_fix_packet(review_artifact, drifted, run, output)
        assert raised.value.code == "digest_mismatch"

        pass_report = parse_review(json.dumps(_report(verdict="pass")))
        pass_record = build_review_record(inputs.task, pass_report, record.sources)
        pass_artifact = Artifact(
            kind=ArtifactKind.REVIEW_FINDING,
            task_id=inputs.task.id,
            run_id=run.id,
            content=encode_review_record(pass_record),
        )
        with pytest.raises(ReviewContractError):
            build_fix_packet(pass_artifact, inputs, run, output)

    def test_packet_rejects_noncanonical_or_wrong_review_artifact(self):
        inputs, run, output, record = _findings_record()
        pretty = json.dumps(record.model_dump(mode="json"), indent=2)
        noncanonical = Artifact(
            kind=ArtifactKind.REVIEW_FINDING,
            task_id=inputs.task.id,
            run_id=run.id,
            content=pretty,
        )
        with pytest.raises(ReviewContractError) as raised:
            build_fix_packet(noncanonical, inputs, run, output)
        assert raised.value.code == "noncanonical_encoding"

        wrong = Artifact(
            kind=ArtifactKind.REPORT,
            task_id=inputs.task.id,
            content=encode_review_record(record),
        )
        with pytest.raises(ReviewContractError):
            build_fix_packet(wrong, inputs, run, output)

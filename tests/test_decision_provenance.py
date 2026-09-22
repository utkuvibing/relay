"""P8 decision citations and read-only provenance projection."""

from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from relay.cli.main import _open_db_readonly, app
from relay.core.bus import ConversationBus
from relay.core.decision_provenance import DecisionProvenanceError, build_decision_provenance
from relay.core.decision_references import DecisionReferenceError, validate_decision_reference
from relay.core.evidence import EvidenceKind
from relay.core.room_graph import build_room_graph
from relay.core.room_records import promote_room_decision
from relay.core.rooms import RoomSeatResolver
from relay.storage.db import connect
from relay.storage.models import (
    Artifact,
    ArtifactKind,
    Decision,
    EventLogEntry,
    EventType,
    EvidenceRecord,
    Finding,
    Message,
    MessageType,
    RoomDecisionPayload,
    Run,
    RunStatus,
    Task,
)
from tests.room_helpers import freeze, room_config, room_store


def _fallback_room_reply(fixture, *, request_type, reply_type, content):
    bus = ConversationBus(fixture.store, fixture.writer, RoomSeatResolver(fixture.room))
    parent = bus.send(Message(sender="human:utku", recipient_role="planner", room_id=fixture.room.id, type=request_type, content="Decide or plan"))
    failed = fixture.store.save_model(Run(agent="gpt", role="planner", status=RunStatus.FAILED))
    fixture.writer.record(EventLogEntry(type=EventType.MESSAGE_DELIVERED, room_id=fixture.room.id, sender="relay:delivery", content="resume attempt", references=[f"message:{parent.id}", f"run:{failed.id}", f"room:{fixture.room.id}"]))
    fresh = fixture.store.save_model(Run(agent="gpt", role="planner", status=RunStatus.SUCCEEDED))
    fixture.writer.record(EventLogEntry(type=EventType.MESSAGE_DELIVERY_FALLBACK, room_id=fixture.room.id, sender="relay:delivery", content="fresh fallback", references=[f"message:{parent.id}", f"run:{fresh.id}", f"prior_run:{failed.id}", f"room:{fixture.room.id}"]))
    reply = bus.send(Message(sender="gpt", recipient=parent.sender, reply_to_id=parent.id, run_id=fresh.id, room_id=fixture.room.id, type=reply_type, content=content))
    return parent, reply, fresh


def test_six_reference_types_and_room_task_scope(tmp_path):
    fixture = room_store(tmp_path)
    store = fixture.store
    task = store.save_model(Task(title="inside", room_id=fixture.room.id))
    other = store.save_model(Task(title="outside"))
    message = store.save_model(Message(sender="human:utku", room_id=fixture.room.id, task_id=task.id, type=MessageType.NOTE, content="context"))
    plan = store.save_model(Artifact(kind=ArtifactKind.PLAN, room_id=fixture.room.id, task_id=task.id, content="plan"))
    artifact = store.save_model(Artifact(kind=ArtifactKind.REPORT, room_id=fixture.room.id, task_id=task.id, content="review"))
    review_run = store.save_model(Run(agent="gpt", role="reviewer", task_id=task.id, status=RunStatus.SUCCEEDED))
    review_artifact = store.save_model(Artifact(kind=ArtifactKind.REVIEW_FINDING, room_id=fixture.room.id, task_id=task.id, run_id=review_run.id, content="review findings"))
    finding = store.save_model(Finding(room_id=fixture.room.id, task_id=task.id, review_artifact_id=review_artifact.id, review_run_id=review_run.id, source_finding_id="F1", severity="low", title="issue", description="detail", requested_change="fix", validation_expectation="test"))
    fixture.writer.record(EventLogEntry(type=EventType.FINDING_RECORDED, room_id=fixture.room.id, task_id=task.id, sender="relay:rooms", content="finding", references=[f"finding:{finding.id}", f"artifact:{review_artifact.id}"]))
    evidence = store.save_model(EvidenceRecord(kind=EvidenceKind.CONTEXT_COLLECTED, task_id=task.id, produced_by="relay:context"))
    previous = store.save_model(Decision(statement="earlier", room_id=fixture.room.id))
    decision = Decision(statement="current", room_id=fixture.room.id)
    for reference in (f"message:{message.id}", f"finding:{finding.id}", f"plan:{plan.id}", f"artifact:{artifact.id}", f"evidence:{evidence.id}", f"decision:{previous.id}"):
        validate_decision_reference(store, decision, reference)
    foreign = store.save_model(EvidenceRecord(kind=EvidenceKind.CONTEXT_COLLECTED, task_id=other.id, produced_by="relay:context"))
    with pytest.raises(DecisionReferenceError):
        validate_decision_reference(store, decision, f"evidence:{foreign.id}")
    scoped = Decision(statement="task", room_id=fixture.room.id, task_id=task.id)
    with pytest.raises(DecisionReferenceError):
        validate_decision_reference(store, scoped, f"decision:{previous.id}")
    standalone = Decision(statement="standalone", task_id=other.id)
    with pytest.raises(DecisionReferenceError):
        validate_decision_reference(store, standalone, f"message:{message.id}")


@pytest.mark.parametrize("corruption", ["report", "mismatched_run", "missing_run"])
def test_finding_source_corruption_refused_by_decision_and_room_graph(tmp_path, corruption):
    fixture = room_store(tmp_path)
    store = fixture.store
    task = store.save_model(Task(title="reviewed", room_id=fixture.room.id))
    author = store.save_model(Run(agent="gpt", role="reviewer", task_id=task.id, status=RunStatus.SUCCEEDED))
    artifact = store.save_model(Artifact(kind=ArtifactKind.REPORT if corruption == "report" else ArtifactKind.REVIEW_FINDING, room_id=fixture.room.id, task_id=task.id, run_id=author.id, content="review"))
    if corruption == "mismatched_run":
        cited_run_id = store.save_model(Run(agent="other", role="reviewer", task_id=task.id, status=RunStatus.SUCCEEDED)).id
    elif corruption == "missing_run":
        cited_run_id = "missing-review-run"
        fixture.conn.execute("PRAGMA foreign_keys=OFF")
    else:
        cited_run_id = author.id
    try:
        finding = store.save_model(Finding(room_id=fixture.room.id, task_id=task.id, review_artifact_id=artifact.id, review_run_id=cited_run_id, source_finding_id="F1", severity="low", title="issue", description="detail", requested_change="fix", validation_expectation="test"))
    finally:
        if corruption == "missing_run":
            fixture.conn.execute("PRAGMA foreign_keys=ON")
    decision = store.save_model(Decision(statement="cite finding", room_id=fixture.room.id, references=[f"finding:{finding.id}"]))
    with pytest.raises(DecisionProvenanceError):
        build_decision_provenance(store, decision)
    from relay.core.room_graph import RoomGraphIntegrityError

    with pytest.raises(RoomGraphIntegrityError):
        build_room_graph(store, fixture.room.id)


def test_room_rejected_citations_and_why_json(tmp_path, monkeypatch):
    fixture = room_store(tmp_path)
    task = fixture.store.save_model(Task(title="related", room_id=fixture.room.id))
    artifact = fixture.store.save_model(Artifact(kind=ArtifactKind.REPORT, task_id=task.id, room_id=fixture.room.id, content="context"))
    evidence = fixture.store.save_model(EvidenceRecord(kind=EvidenceKind.CONTEXT_COLLECTED, task_id=task.id, produced_by="relay:context"))
    parent = fixture.store.save_model(Message(sender="human:utku", room_id=fixture.room.id, type=MessageType.PROPOSAL, content="decide"))
    payload = RoomDecisionPayload(schema_version="relay.room_decision.v1", outcome="reject", statement="reject A", references=(f"message:{parent.id}", f"artifact:{artifact.id}", f"evidence:{evidence.id}"))
    reply = fixture.store.save_model(Message(sender="gpt", recipient=parent.sender, room_id=fixture.room.id, reply_to_id=parent.id, type=MessageType.FINAL_POSITION, content=payload.model_dump_json()))
    decision = promote_room_decision(fixture.store, fixture.writer, fixture.room, parent, reply)
    assert decision is not None and decision.references == list(payload.references)
    assert build_room_graph(fixture.store, fixture.room.id).decisions[0].decision.id == decision.id
    counts = fixture.store.counts()
    view = build_decision_provenance(fixture.store, decision)
    assert view["exchange"]["reply_message_id"] == reply.id
    assert view["decision"]["status"] == "rejected"
    assert fixture.store.counts() == counts
    monkeypatch.setattr("relay.cli.main._open_db_readonly", lambda _root: connect(fixture.db_path))
    result = CliRunner().invoke(app, ["why", decision.id[:12], "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["decision"]["id"] == decision.id


def test_legacy_row_fields_and_json_human_parity(tmp_path, monkeypatch):
    fixture = room_store(tmp_path)
    legacy = fixture.store.save_model(
        Decision(
            statement="Architecture B",
            rationale="Lower migration risk",
            room_id=fixture.room.id,
            status="accepted",
            supported_by=["GPT", "DeepSeek"],
            challenged_by=["Claude"],
            verified_by="Codex",
            alternatives_considered=["Architecture A"],
            primary_objection="Migration cost",
        )
    )
    view = build_decision_provenance(fixture.store, legacy)
    assert view["canonical_fields"]["source"] == "decision_row"
    assert view["canonical_fields"]["supported_by"] == ["GPT", "DeepSeek"]
    assert [node["provenance_ref"] for node in view["graph"]["nodes"]["decision"]] == [f"decision:{legacy.id}"]
    assert view["graph"]["nodes"]["proposal"] == []
    assert "source_reply" in view["graph"]["gaps"]
    assert "unrecorded_rebuttal" in view["graph"]["gaps"]

    monkeypatch.setattr("relay.cli.main._open_db_readonly", lambda _root: connect(fixture.db_path))
    runner = CliRunner()
    json_result = runner.invoke(app, ["why", legacy.id, "--json"])
    human_result = runner.invoke(app, ["why", legacy.id])
    assert json_result.exit_code == human_result.exit_code == 0
    document = json.loads(json_result.output)
    assert document["canonical_fields"] == view["canonical_fields"]
    for label, value in (
        ("Supported by", "GPT, DeepSeek"),
        ("Challenged by", "Claude"),
        ("Repository verification", "Codex"),
        ("Rejected alternative", "Architecture A"),
        ("Primary objection", "Migration cost"),
    ):
        assert f"{label}: {value}" in human_result.output
    for kind in ("proposal", "objection", "evidence", "rebuttal", "decision"):
        assert kind in document["graph"]["nodes"]
        assert f"{kind}:" in human_result.output


@pytest.mark.parametrize(
    ("request_type", "expected_class"),
    [(MessageType.PROPOSAL, "proposal"), (MessageType.CHALLENGE, "objection")],
)
def test_typed_exchange_evidence_and_rebuttal_graph(tmp_path, monkeypatch, request_type, expected_class):
    fixture = room_store(tmp_path)
    store = fixture.store
    task = store.save_model(Task(title="supporting work", room_id=fixture.room.id))
    evidence = store.save_model(EvidenceRecord(kind=EvidenceKind.CONTEXT_COLLECTED, task_id=task.id, produced_by="relay:context"))
    rebuttal = store.save_model(Message(sender="gpt", room_id=fixture.room.id, type=MessageType.REBUTTAL, content="Counterargument"))
    parent = store.save_model(Message(sender="human:utku", recipient="gpt", room_id=fixture.room.id, type=request_type, content="Consider the design"))
    payload = RoomDecisionPayload(
        schema_version="relay.room_decision.v1",
        outcome="accept",
        statement="Choose B",
        references=(f"evidence:{evidence.id}", f"message:{rebuttal.id}"),
    )
    reply = store.save_model(Message(sender="gpt", recipient=parent.sender, room_id=fixture.room.id, reply_to_id=parent.id, type=MessageType.FINAL_POSITION, content=payload.model_dump_json()))
    decision = promote_room_decision(store, fixture.writer, fixture.room, parent, reply)
    assert decision is not None
    graph = build_decision_provenance(store, decision)["graph"]
    assert [node["provenance_ref"] for node in graph["nodes"][expected_class]] == [f"message:{parent.id}"]
    assert [node["provenance_ref"] for node in graph["nodes"]["evidence"]] == [f"evidence:{evidence.id}"]
    assert [node["provenance_ref"] for node in graph["nodes"]["rebuttal"]] == [f"message:{rebuttal.id}"]
    assert [node["provenance_ref"] for node in graph["nodes"]["decision"]] == [f"decision:{decision.id}"]
    assert {edge["to"] for edge in graph["edges"]} == {f"decision:{decision.id}"}
    absent_class = "proposal" if expected_class == "objection" else "objection"
    assert f"unrecorded_{absent_class}" in graph["gaps"]
    monkeypatch.setattr("relay.cli.main._open_db_readonly", lambda _root: connect(fixture.db_path))
    runner = CliRunner()
    json_result = runner.invoke(app, ["why", decision.id, "--json"])
    human_result = runner.invoke(app, ["why", decision.id])
    assert json_result.exit_code == human_result.exit_code == 0
    assert json.loads(json_result.output)["graph"] == graph
    for reference in (f"message:{parent.id}", f"evidence:{evidence.id}", f"message:{rebuttal.id}", f"decision:{decision.id}"):
        assert reference in human_result.output


def test_fallback_authored_room_replies_freeze_and_explain(tmp_path):
    fixture = room_store(tmp_path)
    _request, plan_reply, plan_run = _fallback_room_reply(
        fixture,
        request_type=MessageType.CLARIFICATION_REQUEST,
        reply_type=MessageType.CLARIFICATION_RESPONSE,
        content="# Plan\n\nDo the work",
    )
    frozen = freeze(fixture, room_config(), reply=plan_reply, workspace_root=tmp_path)
    assert frozen.plan_artifact.run_id == plan_run.id

    payload = RoomDecisionPayload(
        schema_version="relay.room_decision.v1",
        outcome="accept",
        statement="Adopt the plan",
        references=(f"plan:{frozen.plan_artifact.id}",),
    )
    parent, reply, decision_run = _fallback_room_reply(
        fixture,
        request_type=MessageType.PROPOSAL,
        reply_type=MessageType.FINAL_POSITION,
        content=payload.model_dump_json(),
    )
    decision = promote_room_decision(fixture.store, fixture.writer, fixture.room, parent, reply)
    assert decision is not None and decision.source_reply_id == reply.id
    view = build_decision_provenance(fixture.store, decision)
    assert view["exchange"]["runs"]["reply"]["id"] == decision_run.id
    assert view["events"]["delivery_binding_type"] == EventType.MESSAGE_DELIVERY_FALLBACK.value
    assert "delivery_binding" not in view["gaps"]


def test_legacy_gaps_and_broken_explicit_link(tmp_path):
    fixture = room_store(tmp_path)
    legacy = fixture.store.save_model(Decision(statement="old", room_id=fixture.room.id))
    view = build_decision_provenance(fixture.store, legacy)
    assert "source_reply" in view["gaps"]
    corrupt = fixture.store.save_model(Decision(statement="bad", room_id=fixture.room.id, references=["message:missing"]))
    with pytest.raises(DecisionProvenanceError):
        build_decision_provenance(fixture.store, corrupt)


@pytest.mark.parametrize(
    ("kind", "producer"),
    [
        (EvidenceKind.TESTS_PASSED, "relay:verification"),
        (EvidenceKind.APPROVAL_GRANTED, "agent:forged"),
    ],
)
def test_why_refuses_directly_inserted_malformed_evidence(tmp_path, monkeypatch, kind, producer):
    fixture = room_store(tmp_path)
    task = fixture.store.save_model(Task(title="decision scope"))
    # Bypass EvidenceStore deliberately, as a corrupt/legacy SQL row would.
    malformed = fixture.store.save_model(
        EvidenceRecord(kind=kind, task_id=task.id, produced_by=producer)
    )
    decision = fixture.store.save_model(
        Decision(statement="claimed proof", task_id=task.id, references=[f"evidence:{malformed.id}"])
    )
    monkeypatch.setattr("relay.cli.main._open_db_readonly", lambda _root: connect(fixture.db_path))
    result = CliRunner().invoke(app, ["why", decision.id, "--json"])
    assert result.exit_code == 1
    assert "invalid evidence provenance" in result.output
    assert '"version": "relay.decision.provenance.v1"' not in result.output


def test_legacy_exchange_is_recovered_without_timestamp_inference(tmp_path):
    fixture = room_store(tmp_path)
    task = fixture.store.save_model(Task(title="legacy"))
    parent = fixture.store.save_model(Message(sender="impl", task_id=task.id, type=MessageType.CHALLENGE, content="why?"))
    reply = fixture.store.save_model(Message(sender="planner", task_id=task.id, reply_to_id=parent.id, type=MessageType.FINAL_POSITION, content="because"))
    decision = fixture.store.save_model(Decision(statement="old answer", task_id=task.id, status="accepted"))
    for kind in (EventType.DECISION_PROPOSED, EventType.DECISION_ACCEPTED):
        fixture.writer.record(EventLogEntry(type=kind, task_id=task.id, content="legacy", references=[f"decision:{decision.id}", f"message:{parent.id}", f"message:{reply.id}"]))
    view = build_decision_provenance(fixture.store, decision)
    assert view["exchange"]["reply_message_id"] == reply.id
    assert view["exchange"]["proposal_text"] == "why?"


def test_decision_reference_cycle_refuses(tmp_path):
    fixture = room_store(tmp_path)
    first = fixture.store.save_model(Decision(statement="first", room_id=fixture.room.id))
    second = fixture.store.save_model(Decision(statement="second", room_id=fixture.room.id, references=[f"decision:{first.id}"]))
    fixture.store.update_model(first.model_copy(update={"references": [f"decision:{second.id}"]}))
    with pytest.raises(DecisionReferenceError):
        validate_decision_reference(fixture.store, first, f"decision:{second.id}")


def test_why_database_connection_is_read_only(tmp_path, monkeypatch):
    fixture = room_store(tmp_path)
    monkeypatch.setattr("relay.cli.main.workspace_layout", lambda _root: SimpleNamespace(db_path=fixture.db_path))
    conn = _open_db_readonly(tmp_path)
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO decisions(id, statement, status) VALUES ('x', 'x', 'accepted')")
    finally:
        conn.close()

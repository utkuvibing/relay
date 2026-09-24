"""P7.3 Room graph read-model: one chain, both edge kinds, fail-closed integrity."""

from __future__ import annotations

import pytest

from relay.agents.base import AgentRole
from relay.core.build_ledger import derive_position
from relay.core.bus import ConversationBus
from relay.core.evidence import EvidenceKind
from relay.core.room_graph import (
    RoomGraphIntegrityError,
    build_room_graph,
    resolve_room_plan_chain,
)
from relay.core.room_records import RoomRecordRefusal, promote_room_decision
from relay.core.rooms import RoomSeatResolver
from relay.storage.models import (
    Artifact,
    ArtifactKind,
    Decision,
    DecisionStatus,
    EventLogEntry,
    EventType,
    EvidenceRecord,
    Finding,
    Message,
    MessageType,
    PlannerDecisionPayload,
    PlanRevisionPayload,
    ReviewSeverity,
    Room,
    RoomDecisionPayload,
    Run,
    RunStatus,
    Task,
    TaskState,
)
from relay.storage.store import SqliteEvidenceStore
from tests.room_helpers import RoomFixture, freeze, planner_exchange, room_config, room_store


@pytest.fixture()
def fixture(tmp_path) -> RoomFixture:
    return room_store(tmp_path)


def _revision_setup(fixture: RoomFixture, task_id: str) -> tuple[Message, Message, Run]:
    """A P6.4 plan-changing exchange inside a Room-bound task."""
    store, writer = fixture.store, fixture.writer
    impl_run = store.save_model(
        Run(
            agent="impl",
            role=AgentRole.IMPLEMENTER.value,
            status=RunStatus.SUCCEEDED,
            task_id=task_id,
        )
    )
    writer.record(
        EventLogEntry(
            type=EventType.BUILD_RUN_DISPATCHED,
            task_id=task_id,
            sender="relay:build",
            content="build stage 'implement' bound to run",
            references=[
                f"task:{task_id}",
                f"run:{impl_run.id}",
                "build_stage:implement",
                "build_attempt:1",
            ],
        )
    )
    from relay.core.stage_signals import StageSignalPayload, compose_signal_message

    signal = StageSignalPayload(
        schema_version="relay.stage_signal.v1",
        kind="proposal",
        to_role=AgentRole.PLANNER.value,
        body="Use design B",
    )
    store.save_model(
        Artifact(
            kind=ArtifactKind.RUN_OUTPUT,
            task_id=task_id,
            run_id=impl_run.id,
            content=signal.model_dump_json(),
        )
    )
    task = store.load_model(Task, task_id)
    assert task is not None
    bus = ConversationBus(store, writer, RoomSeatResolver(fixture.room))
    signal_message = bus.send(compose_signal_message(task, impl_run, signal))

    planner_run = store.save_model(
        Run(agent="gpt", role=AgentRole.PLANNER.value, status=RunStatus.SUCCEEDED)
    )
    writer.record(
        EventLogEntry(
            type=EventType.MESSAGE_DELIVERED,
            room_id=fixture.room.id,
            task_id=task_id,
            sender="relay:delivery",
            content="signal delivered to planner",
            references=[
                f"message:{signal_message.id}",
                f"run:{planner_run.id}",
                f"room:{fixture.room.id}",
                f"task:{task_id}",
            ],
        )
    )
    reply = bus.send(
        Message(
            sender="gpt",
            recipient="impl",
            reply_to_id=signal_message.id,
            run_id=planner_run.id,
            room_id=fixture.room.id,
            task_id=task_id,
            type=MessageType.FINAL_POSITION,
            content=PlannerDecisionPayload(
                schema_version="relay.planner_decision.v1",
                outcome="accept",
                plan_effect="supersede",
                statement="bundle registries",
                revised_plan="# Plan\n\nStep 2: bundle registries",
            ).model_dump_json(),
        )
    )
    return signal_message, reply, planner_run


class TestGraphReconstruction:

    def test_frozen_plan_chain(self, fixture, tmp_path):
        outcome = freeze(fixture, room_config(), workspace_root=tmp_path)
        graph = build_room_graph(fixture.store, fixture.room.id)
        assert len(graph.plans) == 1
        chain = graph.plans[0]
        assert chain.task_id == outcome.task.id
        assert [node.edge for node in chain.nodes] == ["frozen"]
        assert chain.tip.id == outcome.plan_artifact.id
        node = chain.nodes[0]
        assert node.frozen_by == "human:utku"
        assert node.freeze_record_id == outcome.freeze_record.id
        assert node.first_line == "Plan"

    def test_p64_revision_extends_the_same_chain(self, fixture, tmp_path):
        """human freeze PLAN-1 → P6.4 revision PLAN-2 (the user-visible E2E B)."""
        first = freeze(fixture, room_config(), workspace_root=tmp_path)
        signal_message, reply, planner_run = _revision_setup(fixture, first.task.id)
        from relay.core.stage_signals import _promote_planner_decision

        decision = _promote_planner_decision(
            fixture.store,
            fixture.writer,
            SqliteEvidenceStore(fixture.store),
            fixture.store.load_model(Task, first.task.id),
            signal_message,
            reply,
            current_plan_artifact_id=first.plan_artifact.id,
        )
        assert decision is not None
        assert decision.room_id == fixture.room.id
        assert decision.task_id == first.task.id
        assert decision.source_reply_id == reply.id
        assert decision.status is DecisionStatus.ACCEPTED
        assert decision.accepted_by == "gpt"

        store = fixture.store
        chain = resolve_room_plan_chain(store, fixture.room.id, first.task.id)
        assert [node.edge for node in chain.nodes] == ["frozen", "revised"]
        assert chain.nodes[0].plan.id == first.plan_artifact.id
        revised = chain.nodes[1]
        assert revised.supersedes_plan_artifact_id == first.plan_artifact.id
        assert revised.decision_id == decision.id
        assert revised.signal_message_id == signal_message.id
        assert revised.reply_message_id == reply.id

        # Room scope propagated to the P6.4 records.
        assert revised.plan.room_id == fixture.room.id
        assert revised.plan.task_id == first.task.id
        revisions = [
            artifact
            for artifact in store.all_models(
                Artifact, "WHERE task_id = ? AND kind = ?", [first.task.id, ArtifactKind.REPORT.value]
            )
            if '"relay.plan_revision.v1"' in (artifact.content or "")
        ]
        assert len(revisions) == 1
        assert revisions[0].room_id == fixture.room.id
        markers = [
            event
            for event in fixture.writer.all()
            if event.type is EventType.ROOM_PLAN_REVISED
        ]
        assert len(markers) == 1
        assert f"plan:{revised.plan.id}" in markers[0].references
        assert f"supersedes_plan:{first.plan_artifact.id}" in markers[0].references
        assert markers[0].room_id == fixture.room.id

        # The ledger's canonical tip is PLAN-2 (subsequent execution consumes
        # the revised plan), and the graph agrees.
        from relay.core.baseline import capture_baseline, persist_baseline
        from relay.core.reviews import canonical_json

        pin = persist_baseline(tmp_path, first.task.id, capture_baseline(tmp_path))
        store.save_model(
            Artifact(
                kind=ArtifactKind.REPORT,
                task_id=first.task.id,
                content=canonical_json(pin),
            )
        )
        position = derive_position(store, SqliteEvidenceStore(store), first.task.id)
        assert position.plan_artifact is not None
        assert position.plan_artifact.id == revised.plan.id
        assert build_room_graph(store, fixture.room.id).plans[0].tip.id == revised.plan.id
        assert planner_run.id == reply.run_id

    def test_decision_supersession_edge_is_reconstructed(self, fixture):
        store, writer, room = fixture.store, fixture.writer, fixture.room
        bus = ConversationBus(store, writer, RoomSeatResolver(room))

        def promote(payload: RoomDecisionPayload) -> Decision:
            parent = bus.send(
                Message(
                    sender="human:utku",
                    recipient_role=AgentRole.PLANNER.value,
                    room_id=room.id,
                    type=MessageType.PROPOSAL,
                    content="decide",
                )
            )
            run = store.save_model(
                Run(agent="gpt", role=AgentRole.PLANNER.value, status=RunStatus.SUCCEEDED)
            )
            reply = bus.send(
                Message(
                    sender="gpt",
                    recipient=parent.sender,
                    reply_to_id=parent.id,
                    run_id=run.id,
                    room_id=room.id,
                    type=MessageType.FINAL_POSITION,
                    content=payload.model_dump_json(),
                )
            )
            decision = promote_room_decision(store, writer, room, parent, reply)
            assert decision is not None
            return decision

        first = promote(
            RoomDecisionPayload(
                schema_version="relay.room_decision.v1", outcome="accept", statement="A"
            )
        )
        second = promote(
            RoomDecisionPayload(
                schema_version="relay.room_decision.v1",
                outcome="accept",
                statement="B",
                supersedes_decision_id=first.id,
            )
        )
        graph = build_room_graph(store, room.id)
        nodes = {node.decision.id: node for node in graph.decisions}
        assert nodes[first.id].superseded_by == second.id
        assert nodes[second.id].superseded_by is None
        assert nodes[second.id].decision.supersedes_decision_id == first.id

    def test_findings_are_room_records(self, fixture):
        store = fixture.store
        task = store.save_model(Task(title="t", room_id=fixture.room.id))
        run = store.save_model(
            Run(agent="gpt", role=AgentRole.REVIEWER.value, status=RunStatus.SUCCEEDED)
        )
        artifact = store.save_model(
            Artifact(
                kind=ArtifactKind.REVIEW_FINDING,
                task_id=task.id,
                room_id=fixture.room.id,
                run_id=run.id,
                content="{}",
            )
        )
        finding = store.save_model(
            Finding(
                room_id=fixture.room.id,
                task_id=task.id,
                review_artifact_id=artifact.id,
                review_run_id=run.id,
                source_finding_id="F1",
                severity=ReviewSeverity.HIGH,
                title="Missing guard",
                description="d",
                requested_change="c",
                validation_expectation="v",
            )
        )
        fixture.writer.record(
            EventLogEntry(
                type=EventType.FINDING_RECORDED,
                room_id=fixture.room.id,
                task_id=task.id,
                sender="relay:rooms",
                content="high finding: Missing guard",
                references=[
                    f"room:{fixture.room.id}",
                    f"task:{task.id}",
                    f"finding:{finding.id}",
                    f"artifact:{artifact.id}",
                ],
            )
        )
        graph = build_room_graph(store, fixture.room.id)
        assert [node.finding.id for node in graph.findings] == [finding.id]
        assert graph.findings[0].review_artifact.id == artifact.id


class TestGraphIntegrity:
    """Discontinuity and forged linkage must refuse the whole read."""

    def _freeze_two(self, fixture, tmp_path):
        first = freeze(fixture, room_config(), workspace_root=tmp_path)
        _parent, reply, _run = planner_exchange(fixture, content="# Plan\n\nRev")
        second = freeze(
            fixture,
            room_config(),
            reply=reply,
            supersedes=first.plan_artifact.id,
            workspace_root=tmp_path,
        )
        return first, second

    def test_orphan_freeze_record_refuses(self, fixture, tmp_path):
        outcome = freeze(fixture, room_config(), workspace_root=tmp_path)
        store = fixture.store
        store.save_model(
            Artifact(
                kind=ArtifactKind.REPORT,
                room_id=fixture.room.id,
                task_id=outcome.task.id,
                content=(
                    '{"schema_version":"relay.room.plan_freeze.v1",'
                    f'"room_id":"{fixture.room.id}","task_id":"{outcome.task.id}",'
                    '"plan_artifact_id":"ghost-plan","supersedes_plan_artifact_id":null,'
                    '"source_message_id":"m","source_run_id":"r","frozen_by":"human:utku"}'
                ),
            )
        )
        with pytest.raises(RoomGraphIntegrityError, match="missing plan artifact"):
            build_room_graph(store, fixture.room.id)

    def test_double_supersession_refuses(self, fixture, tmp_path):
        first, second = self._freeze_two(fixture, tmp_path)
        store = fixture.store
        _parent, reply, _run = planner_exchange(fixture, content="# Plan\n\nThird")
        from relay.core.room_records import mint_frozen_plan, resolve_freeze_source

        source = resolve_freeze_source(store, fixture.room, reply.id)
        task = store.load_model(Task, first.task.id)
        assert task is not None
        with store.transaction():
            mint_frozen_plan(
                store,
                fixture.writer,
                SqliteEvidenceStore(store),
                fixture.room,
                task,
                source,
                frozen_by="human:utku",
                supersedes_plan_artifact_id=first.plan_artifact.id,
            )
        with pytest.raises(RoomGraphIntegrityError, match="superseded twice"):
            build_room_graph(store, fixture.room.id)
        assert second.plan_artifact.id

    def test_non_human_freeze_record_refuses(self, fixture, tmp_path):
        outcome = freeze(fixture, room_config(), workspace_root=tmp_path)
        store = fixture.store
        record = store.load_model(Artifact, outcome.freeze_record.id)
        assert record is not None
        forged = (record.content or "").replace('"human:utku"', '"agent:gpt"')
        conn = fixture.conn
        conn.execute("DROP TRIGGER IF EXISTS artifacts_no_update")
        conn.execute("UPDATE artifacts SET content = ? WHERE id = ?", [forged, record.id])
        with pytest.raises(RoomGraphIntegrityError, match="not frozen by a human"):
            build_room_graph(store, fixture.room.id)

    def test_decision_without_promotion_provenance_refuses(self, fixture):
        store = fixture.store
        store.save_model(Decision(statement="forged", room_id=fixture.room.id, status="accepted"))
        with pytest.raises(RoomGraphIntegrityError, match="no promotion source"):
            build_room_graph(store, fixture.room.id)

    def test_foreign_reference_refuses(self, fixture):
        store, writer, room = fixture.store, fixture.writer, fixture.room
        bus = ConversationBus(store, writer, RoomSeatResolver(room))
        parent = bus.send(
            Message(
                sender="human:utku",
                recipient_role=AgentRole.PLANNER.value,
                room_id=room.id,
                type=MessageType.PROPOSAL,
                content="decide",
            )
        )
        run = store.save_model(
            Run(agent="gpt", role=AgentRole.PLANNER.value, status=RunStatus.SUCCEEDED)
        )
        reply = bus.send(
            Message(
                sender="gpt",
                recipient=parent.sender,
                reply_to_id=parent.id,
                run_id=run.id,
                room_id=room.id,
                type=MessageType.FINAL_POSITION,
                content="plain prose",
            )
        )
        decision = store.save_model(
            Decision(
                statement="forged refs",
                room_id=room.id,
                status="accepted",
                source_reply_id=reply.id,
                references=["finding:foreign"],
            )
        )
        assert decision.id
        with pytest.raises(RoomGraphIntegrityError, match="invalid reference"):
            build_room_graph(store, room.id)

    def test_superseded_decision_without_successor_refuses(self, fixture):
        store = fixture.store
        store.save_model(
            Decision(
                statement="orphaned",
                room_id=fixture.room.id,
                status="superseded",
                source_reply_id="m1",
            )
        )
        with pytest.raises(RoomGraphIntegrityError):
            build_room_graph(store, fixture.room.id)

    def test_rejected_decision_with_accepted_by_refuses(self, fixture):
        store, writer, room = fixture.store, fixture.writer, fixture.room
        bus = ConversationBus(store, writer, RoomSeatResolver(room))
        parent = bus.send(
            Message(
                sender="human:utku",
                recipient_role=AgentRole.PLANNER.value,
                room_id=room.id,
                type=MessageType.PROPOSAL,
                content="decide",
            )
        )
        run = store.save_model(
            Run(agent="gpt", role=AgentRole.PLANNER.value, status=RunStatus.SUCCEEDED)
        )
        reply = bus.send(
            Message(
                sender="gpt",
                recipient=parent.sender,
                reply_to_id=parent.id,
                run_id=run.id,
                room_id=room.id,
                type=MessageType.FINAL_POSITION,
                content="prose",
            )
        )
        store.save_model(
            Decision(
                statement="rejected but accepted",
                room_id=room.id,
                status="rejected",
                accepted_by="gpt",
                source_reply_id=reply.id,
            )
        )
        with pytest.raises(RoomGraphIntegrityError, match="accepted_by"):
            build_room_graph(store, room.id)

    def test_missing_revision_record_refuses(self, fixture, tmp_path):
        first = freeze(fixture, room_config(), workspace_root=tmp_path)
        store = fixture.store
        # A Room-scoped plan with no freeze/revision record at all.
        store.save_model(
            Artifact(
                kind=ArtifactKind.PLAN,
                room_id=fixture.room.id,
                task_id=first.task.id,
                content="# Orphan",
            )
        )
        with pytest.raises(RoomGraphIntegrityError, match="outside the chain"):
            build_room_graph(store, fixture.room.id)


class TestEvidenceProducerConvention:
    def test_frozen_plan_evidence_uses_the_agent_producer_convention(self, fixture, tmp_path):
        outcome = freeze(fixture, room_config(), workspace_root=tmp_path)
        records = [
            record
            for record in fixture.store.all_models(EvidenceRecord)
            if record.kind is EvidenceKind.PLAN_PRODUCED
        ]
        assert len(records) == 1
        assert records[0].produced_by == f"agent:{outcome.source.run.agent}"


class TestFreezeRefusalPropagation:
    def test_freeze_source_refusal_carries_a_stable_code(self, fixture):
        with pytest.raises(RoomRecordRefusal) as excinfo:
            from relay.core.room_records import resolve_freeze_source

            resolve_freeze_source(fixture.store, fixture.room, "nope")
        assert excinfo.value.code == "unknown_source"
        assert isinstance(excinfo.value, ValueError)
        assert TaskState.IMPLEMENTING.value == "implementing"
        assert PlanRevisionPayload.model_fields
        assert Room.model_fields

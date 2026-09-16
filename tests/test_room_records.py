"""P7.3 canonical Room records: freeze sources, decisions, findings."""

from __future__ import annotations

import pytest

from relay.agents.base import AgentRole
from relay.core.bus import ConversationBus
from relay.core.room_records import (
    RoomRecordRefusal,
    build_review_findings,
    promote_room_decision,
    resolve_freeze_source,
)
from relay.core.rooms import RoomSeatResolver
from relay.storage.models import (
    Artifact,
    ArtifactKind,
    Decision,
    DecisionStatus,
    EventType,
    Finding,
    Message,
    MessageType,
    ReviewFindingPayload,
    ReviewReportPayload,
    ReviewSeverity,
    ReviewVerdict,
    Room,
    RoomDecisionPayload,
    Run,
    RunStatus,
    Task,
)
from relay.storage.store import SqliteRelayStore
from tests.room_helpers import RoomFixture, planner_exchange, room_store


@pytest.fixture()
def fixture(tmp_path) -> RoomFixture:
    return room_store(tmp_path)


def _exchange_parent(store: SqliteRelayStore, reply: Message) -> Message:
    parent = store.load_model(Message, reply.reply_to_id or "")
    assert parent is not None
    return parent


class TestFreezeSource:
    """The freeze source is a canonical planner discussion reply — nothing else."""

    def test_valid_planner_discussion_reply_resolves(self, fixture):
        parent, reply, run = planner_exchange(fixture)
        source = resolve_freeze_source(fixture.store, fixture.room, reply.id)
        assert source.reply.id == reply.id
        assert source.parent.id == parent.id
        assert source.run.id == run.id

    def test_decide_style_final_position_can_never_be_frozen(self, fixture):
        """``relay room decide`` output is structurally ineligible (P7.3)."""
        store, writer, room = fixture.store, fixture.writer, fixture.room
        bus = ConversationBus(store, writer, RoomSeatResolver(room))
        parent = bus.send(
            Message(
                sender="human:utku",
                recipient_role=AgentRole.PLANNER.value,
                room_id=room.id,
                type=MessageType.PROPOSAL,
                content="Should we adopt design B?",
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
                content="not a decision payload",
            )
        )
        with pytest.raises(RoomRecordRefusal) as excinfo:
            resolve_freeze_source(store, room, reply.id)
        assert excinfo.value.code == "not_discussion"

    def test_refusals_are_typed_and_mutation_free(self, fixture):
        store, room = fixture.store, fixture.room
        parent, reply, run = planner_exchange(fixture)
        foreign = store.save_model(
            Room(id="other-room", name="Other", workspace_id=room.workspace_id)
        )
        baseline = store.counts()

        with pytest.raises(RoomRecordRefusal) as excinfo:
            resolve_freeze_source(store, room, "missing")
        assert excinfo.value.code == "unknown_source"

        with pytest.raises(RoomRecordRefusal) as excinfo:
            resolve_freeze_source(store, foreign, reply.id)
        assert excinfo.value.code == "foreign_source"

        with pytest.raises(RoomRecordRefusal) as excinfo:
            resolve_freeze_source(store, room, parent.id)
        assert excinfo.value.code == "not_a_reply"

        assert store.counts() == baseline
        assert run.status is RunStatus.SUCCEEDED

    def test_run_must_be_a_successful_planner_run(self, fixture):
        store, room = fixture.store, fixture.room
        _parent, reply, run = planner_exchange(fixture)
        store.update_model(run.model_copy(update={"status": RunStatus.FAILED}))
        with pytest.raises(RoomRecordRefusal) as excinfo:
            resolve_freeze_source(store, room, reply.id)
        assert excinfo.value.code == "run_unsuccessful"

    def test_missing_delivery_binding_refuses(self, fixture):
        store, room = fixture.store, fixture.room
        parent, reply, _run = planner_exchange(fixture)
        # Remove the causal binding: the marker is the only proof the run
        # actually delivered the parent request.
        conn = fixture.conn
        conn.execute("DROP TRIGGER event_log_no_delete")
        conn.execute(
            "DELETE FROM event_log WHERE type = ?", [EventType.MESSAGE_DELIVERED.value]
        )
        with pytest.raises(RoomRecordRefusal) as excinfo:
            resolve_freeze_source(store, room, reply.id)
        assert excinfo.value.code == "unbound_delivery"
        assert parent.id is not None


class TestDecisionPromotion:
    """Consequential Room exchanges promote into canonical decisions."""

    def _promote(self, fixture: RoomFixture, payload: str) -> Decision | None:
        store, writer, room = fixture.store, fixture.writer, fixture.room
        bus = ConversationBus(store, writer, RoomSeatResolver(room))
        parent = bus.send(
            Message(
                sender="human:utku",
                recipient_role=AgentRole.PLANNER.value,
                room_id=room.id,
                type=MessageType.PROPOSAL,
                content="Resolve the design question",
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
                content=payload,
            )
        )
        return promote_room_decision(store, writer, room, parent, reply)

    def test_valid_accept_promotes_with_provenance(self, fixture):
        payload = RoomDecisionPayload(
            schema_version="relay.room_decision.v1",
            outcome="accept",
            statement="adopt bundle registries",
            rationale="fewer special cases",
        )
        decision = self._promote(fixture, payload.model_dump_json())
        assert decision is not None
        assert decision.status is DecisionStatus.ACCEPTED
        assert decision.room_id == fixture.room.id
        assert decision.task_id is None
        assert decision.proposed_by == "human:utku"
        assert decision.accepted_by == "gpt"
        assert decision.source_reply_id is not None
        events = [
            event.type
            for event in fixture.writer.all()
            if event.type
            in (
                EventType.DECISION_PROPOSED,
                EventType.DECISION_ACCEPTED,
                EventType.DECISION_REJECTED,
            )
        ]
        assert events == [EventType.DECISION_PROPOSED, EventType.DECISION_ACCEPTED]

    def test_rejected_decision_never_carries_accepted_by(self, fixture):
        payload = RoomDecisionPayload(
            schema_version="relay.room_decision.v1",
            outcome="reject",
            statement="keep the current design",
        )
        decision = self._promote(fixture, payload.model_dump_json())
        assert decision is not None
        assert decision.status is DecisionStatus.REJECTED
        assert decision.accepted_by is None

    def test_ordinary_prose_promotes_nothing(self, fixture):
        assert self._promote(fixture, "Sounds good to me.") is None
        assert not list(fixture.store.all_models(Decision))

    def test_unresolvable_reference_promotes_nothing(self, fixture):
        payload = RoomDecisionPayload(
            schema_version="relay.room_decision.v1",
            outcome="accept",
            statement="adopt it",
            references=("finding:nope",),
        )
        assert self._promote(fixture, payload.model_dump_json()) is None

    def test_promotion_is_idempotent_per_reply(self, fixture):
        store, writer, room = fixture.store, fixture.writer, fixture.room
        bus = ConversationBus(store, writer, RoomSeatResolver(room))
        parent = bus.send(
            Message(
                sender="human:utku",
                recipient_role=AgentRole.PLANNER.value,
                room_id=room.id,
                type=MessageType.PROPOSAL,
                content="Resolve",
            )
        )
        run = store.save_model(
            Run(agent="gpt", role=AgentRole.PLANNER.value, status=RunStatus.SUCCEEDED)
        )
        payload = RoomDecisionPayload(
            schema_version="relay.room_decision.v1",
            outcome="accept",
            statement="adopt it",
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
        first = promote_room_decision(store, writer, room, parent, reply)
        baseline = store.counts()
        second = promote_room_decision(store, writer, room, parent, reply)
        assert first is not None and second is not None and first.id == second.id
        assert store.counts() == baseline

    def test_accepted_supersession_flips_an_accepted_predecessor(self, fixture):
        store, writer = fixture.store, fixture.writer
        first = self._promote(
            fixture,
            RoomDecisionPayload(
                schema_version="relay.room_decision.v1",
                outcome="accept",
                statement="design A",
            ).model_dump_json(),
        )
        assert first is not None
        second = self._promote(
            fixture,
            RoomDecisionPayload(
                schema_version="relay.room_decision.v1",
                outcome="accept",
                statement="design B",
                supersedes_decision_id=first.id,
            ).model_dump_json(),
        )
        assert second is not None
        assert store.load_model(Decision, first.id).status is DecisionStatus.SUPERSEDED
        assert second.supersedes_decision_id == first.id
        superseded_events = [
            event
            for event in writer.all()
            if event.type is EventType.DECISION_SUPERSEDED
        ]
        assert len(superseded_events) == 1
        assert f"supersedes_decision:{first.id}" in superseded_events[0].references

    def test_rejected_decision_never_supersedes(self, fixture):
        first = self._promote(
            fixture,
            RoomDecisionPayload(
                schema_version="relay.room_decision.v1",
                outcome="accept",
                statement="design A",
            ).model_dump_json(),
        )
        assert first is not None
        rejected = self._promote(
            fixture,
            RoomDecisionPayload(
                schema_version="relay.room_decision.v1",
                outcome="reject",
                statement="no",
            ).model_dump_json(),
        )
        assert rejected is not None and rejected.supersedes_decision_id is None
        assert fixture.store.load_model(Decision, first.id).status is DecisionStatus.ACCEPTED

    def test_superseding_a_rejected_predecessor_promotes_nothing(self, fixture):
        rejected = self._promote(
            fixture,
            RoomDecisionPayload(
                schema_version="relay.room_decision.v1",
                outcome="reject",
                statement="no",
            ).model_dump_json(),
        )
        assert rejected is not None
        successor = self._promote(
            fixture,
            RoomDecisionPayload(
                schema_version="relay.room_decision.v1",
                outcome="accept",
                statement="design B",
                supersedes_decision_id=rejected.id,
            ).model_dump_json(),
        )
        assert successor is None
        assert fixture.store.load_model(Decision, rejected.id).status is DecisionStatus.REJECTED

    def test_foreign_room_supersession_target_promotes_nothing(self, fixture):
        store, room = fixture.store, fixture.room
        foreign = store.save_model(
            Room(id="other-room", name="Other", workspace_id=room.workspace_id)
        )
        foreign_decision = store.save_model(
            Decision(
                statement="elsewhere",
                room_id=foreign.id,
                status=DecisionStatus.ACCEPTED,
                source_reply_id="m-elsewhere",
            )
        )
        payload = RoomDecisionPayload(
            schema_version="relay.room_decision.v1",
            outcome="accept",
            statement="design B",
            supersedes_decision_id=foreign_decision.id,
        )
        assert self._promote(fixture, payload.model_dump_json()) is None
        assert (
            store.load_model(Decision, foreign_decision.id).status is DecisionStatus.ACCEPTED
        )


class TestFindingPromotion:
    """Canonical findings exist only for Room-bound tasks."""

    def _review(self, fixture: RoomFixture, *, room_bound: bool):
        store = fixture.store
        task = store.save_model(
            Task(title="t", room_id=fixture.room.id if room_bound else None)
        )
        run = store.save_model(
            Run(agent="gpt", role=AgentRole.REVIEWER.value, status=RunStatus.SUCCEEDED)
        )
        artifact = store.save_model(
            Artifact(
                kind=ArtifactKind.REVIEW_FINDING,
                task_id=task.id,
                room_id=fixture.room.id if room_bound else None,
                run_id=run.id,
                content="{}",
            )
        )
        report = ReviewReportPayload(
            schema_version="relay.review.v1",
            verdict=ReviewVerdict.FINDINGS,
            summary="needs work",
            findings=(
                ReviewFindingPayload(
                    id="F1",
                    severity=ReviewSeverity.HIGH,
                    title="Missing guard",
                    description="detail",
                    requested_change="add a guard",
                    validation_expectation="tests cover it",
                ),
            ),
        )
        return task, artifact, run, report

    def test_room_bound_task_mints_canonical_findings(self, fixture):
        task, artifact, run, report = self._review(fixture, room_bound=True)
        findings, events = build_review_findings(
            fixture.store, task, artifact, run, report
        )
        assert len(findings) == 1
        finding = findings[0]
        assert finding.room_id == fixture.room.id
        assert finding.task_id == task.id
        assert finding.review_artifact_id == artifact.id
        assert finding.source_finding_id == "F1"
        assert finding.severity is ReviewSeverity.HIGH
        assert [event.type for event in events] == [EventType.FINDING_RECORDED]
        assert f"finding:{finding.id}" in events[0].references

    def test_standalone_task_mints_nothing(self, fixture):
        task, artifact, run, report = self._review(fixture, room_bound=False)
        findings, events = build_review_findings(
            fixture.store, task, artifact, run, report
        )
        assert findings == () and events == ()

    def test_existing_rows_are_reused_never_duplicated(self, fixture):
        task, artifact, run, report = self._review(fixture, room_bound=True)
        findings, _events = build_review_findings(
            fixture.store, task, artifact, run, report
        )
        for finding in findings:
            fixture.store.save_model(finding)
        again, again_events = build_review_findings(
            fixture.store, task, artifact, run, report
        )
        assert [row.id for row in again] == [row.id for row in findings]
        assert again_events == ()
        assert len(list(fixture.store.all_models(Finding))) == 1


class TestRoomSeatResolverExport:
    """The seat resolver is exported for the P7.3 signal-routing seam."""

    def test_resolver_lives_in_resolver_module(self):
        from relay.core.resolver import RoomSeatRoleResolver, seat_resolver_for_room

        assert RoomSeatRoleResolver is not None
        assert seat_resolver_for_room is not None

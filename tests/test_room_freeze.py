"""P7.3 human plan freeze: execution binding, refusals, supersession."""

from __future__ import annotations

import pytest

from relay.agents.base import AgentRole
from relay.core.build_ledger import derive_position
from relay.core.evidence import EvidenceKind
from relay.core.room_freeze import freeze_room_plan
from relay.core.room_records import RoomRecordRefusal, freeze_for_source
from relay.harness.types import ExecutionGrantKind
from relay.storage.models import (
    Artifact,
    ArtifactKind,
    EventLogEntry,
    EventType,
    Room,
    Run,
    RunStatus,
    Task,
    TaskState,
    Workspace,
)
from relay.storage.store import SqliteEvidenceStore, SqliteRelayStore
from tests.room_helpers import RoomFixture, freeze, planner_exchange, room_config, room_store


@pytest.fixture()
def fixture(tmp_path) -> RoomFixture:
    return room_store(tmp_path)


def _freeze(fixture, **kwargs):
    return freeze(fixture, room_config(), **kwargs)


class TestFreezeMintsCanonicalState:
    def test_freeze_binds_execution_without_a_plan_stage(self, fixture):
        outcome = _freeze(fixture)
        store, writer, room = fixture.store, fixture.writer, fixture.room
        evidence = SqliteEvidenceStore(store)

        task = store.load_model(Task, outcome.task.id)
        assert task.state is TaskState.IMPLEMENTING
        assert task.room_id == room.id
        assert store.load_model(Workspace, fixture.workspace.id).active_room_id == room.id
        assert store.load_model(type(room), room.id).active_task_id == task.id

        plan = store.load_model(Artifact, outcome.plan_artifact.id)
        assert plan.kind is ArtifactKind.PLAN
        assert plan.room_id == room.id and plan.task_id == task.id
        assert plan.content == outcome.source.reply.content
        assert plan.run_id == outcome.source.run.id

        produced = evidence.records_for_task(task.id, EvidenceKind.PLAN_PRODUCED)
        assert len(produced) == 1
        assert produced[0].run_id == outcome.source.run.id
        assert produced[0].artifact_id == plan.id
        assert produced[0].produced_by == f"agent:{outcome.source.run.agent}"
        context = evidence.records_for_task(task.id, EvidenceKind.CONTEXT_COLLECTED)
        assert len(context) == 1 and context[0].produced_by == "relay:core"

        request = [
            artifact
            for artifact in store.all_models(
                Artifact, "WHERE task_id = ? AND kind = ?", [task.id, ArtifactKind.REPORT.value]
            )
            if '"relay.build.request.v1"' in (artifact.content or "")
        ]
        assert len(request) == 1
        assert f'"implementer":"{outcome.implementer}"' in (request[0].content or "")

        position = derive_position(store, evidence, task.id)
        assert position.plan_artifact is not None and position.plan_artifact.id == plan.id
        assert position.next_action == "dispatch"
        assert not [
            marker
            for marker in store.all_models(EventLogEntry, "WHERE task_id = ?", [task.id])
            if marker.type is EventType.BUILD_RUN_DISPATCHED
        ]

        markers = [
            event
            for event in writer.all()
            if event.type is EventType.ROOM_PLAN_FROZEN
        ]
        assert len(markers) == 1
        assert f"plan:{plan.id}" in markers[0].references
        assert markers[0].room_id == room.id and markers[0].task_id == task.id

    def test_freeze_title_defaults_to_the_plan_first_line(self, fixture):
        outcome = _freeze(fixture)
        assert outcome.task.title == "Plan"

    def test_freeze_refuses_a_second_freeze_of_the_same_reply(self, fixture):
        _parent, reply, _run = planner_exchange(fixture)
        first = _freeze(fixture, reply=reply)
        assert first.plan_artifact.id
        assert freeze_for_source(fixture.store, fixture.room.id, reply.id) is not None
        baseline = fixture.store.counts()
        with pytest.raises(RoomRecordRefusal) as excinfo:
            _freeze(fixture, reply=reply)
        assert excinfo.value.code == "already_frozen"
        assert fixture.store.counts() == baseline

    def test_closed_room_refuses_with_zero_delta(self, fixture):
        from relay.core.rooms import ClosedRoomError, RoomLifecycle

        store, writer, room = fixture.store, fixture.writer, fixture.room
        _parent, reply, _run = planner_exchange(fixture)
        RoomLifecycle(store, writer).close(fixture.workspace, room)
        baseline = store.counts()
        with pytest.raises(ClosedRoomError):
            _freeze(fixture, reply=reply)
        assert store.counts() == baseline


class TestFreezeImplementerContract:
    def test_missing_implementer_seat_refuses(self, fixture):
        store = fixture.store
        room = _room_without_implementer(fixture, seats={"planner": "gpt"})
        _parent, reply, _run = planner_exchange(fixture, room=room)
        with pytest.raises(RoomRecordRefusal) as excinfo:
            freeze_room_plan(
                store,
                fixture.writer,
                SqliteEvidenceStore(store),
                room_config(),
                room,
                source_message_id=reply.id,
                frozen_by="human:utku",
                workspace_root=".",
            )
        assert excinfo.value.code == "no_implementer_seat"

    def test_unknown_seat_agent_refuses(self, fixture):
        store = fixture.store
        room = _room_without_implementer(fixture, seats={"planner": "gpt", "implementer": "ghost"})
        _parent, reply, _run = planner_exchange(fixture, room=room)
        with pytest.raises(RoomRecordRefusal) as excinfo:
            freeze_room_plan(
                store,
                fixture.writer,
                SqliteEvidenceStore(store),
                room_config(),
                room,
                source_message_id=reply.id,
                frozen_by="human:utku",
                workspace_root=".",
            )
        assert excinfo.value.code == "unknown_implementer"

    def test_api_backed_implementer_seat_refuses(self, fixture):
        store = fixture.store
        room = _room_without_implementer(fixture, seats={"planner": "gpt", "implementer": "other"})
        _parent, reply, _run = planner_exchange(fixture, room=room)
        with pytest.raises(RoomRecordRefusal) as excinfo:
            freeze_room_plan(
                store,
                fixture.writer,
                SqliteEvidenceStore(store),
                room_config(),
                room,
                source_message_id=reply.id,
                frozen_by="human:utku",
                workspace_root=".",
            )
        assert excinfo.value.code == "implementer_backend"

    def test_explicit_read_only_grant_refuses(self, fixture):
        store = fixture.store
        room = _room_without_implementer(
            fixture, seats={"planner": "gpt", "implementer": "impl"}
        )
        _parent, reply, _run = planner_exchange(fixture, room=room)
        with pytest.raises(RoomRecordRefusal) as excinfo:
            freeze_room_plan(
                store,
                fixture.writer,
                SqliteEvidenceStore(store),
                room_config(implementer_grant=ExecutionGrantKind.READ_ONLY_ACCESS),
                room,
                source_message_id=reply.id,
                frozen_by="human:utku",
                workspace_root=".",
            )
        assert excinfo.value.code == "implementer_grant"


class TestFreezeEffectiveGrantPreflight:
    """P7.3: the EFFECTIVE implementer grant is validated through the real
    adapter before any canonical freeze write — an unset grant deferring to a
    read-only adapter default is refused, as is a write grant the adapter
    cannot honor."""

    def test_unset_grant_defaulting_to_read_only_refuses(self):
        from relay.agents.claude_code import ClaudeCodeAgent
        from relay.context.config import HarnessAgentConfig
        from relay.core.room_freeze import require_implementer_write_grant

        agent = ClaudeCodeAgent(profile=HarnessAgentConfig(grant=None))
        with pytest.raises(RoomRecordRefusal) as excinfo:
            require_implementer_write_grant(agent, "impl")
        assert excinfo.value.code == "implementer_grant"
        assert "workspace_write" in str(excinfo.value)

    def test_explicit_read_only_grant_refuses_at_adapter_level(self):
        from relay.agents.claude_code import ClaudeCodeAgent
        from relay.context.config import HarnessAgentConfig
        from relay.core.room_freeze import require_implementer_write_grant

        agent = ClaudeCodeAgent(
            profile=HarnessAgentConfig(grant=ExecutionGrantKind.READ_ONLY_ACCESS)
        )
        with pytest.raises(RoomRecordRefusal) as excinfo:
            require_implementer_write_grant(agent, "impl")
        assert excinfo.value.code == "implementer_grant"

    def test_write_grant_on_a_non_write_capable_adapter_refuses(self):
        from relay.context.config import HarnessAgentConfig
        from relay.core.room_freeze import require_implementer_write_grant
        from relay.harness.capabilities import HarnessCapability
        from relay.harness.runtime import HarnessAgent

        class _ReadOnlyOnly(HarnessAgent):
            name = "read_only_only"
            capabilities = frozenset({HarnessCapability.READ_ONLY_ACCESS})

        agent = _ReadOnlyOnly(
            profile=HarnessAgentConfig(grant=ExecutionGrantKind.WORKSPACE_WRITE)
        )
        with pytest.raises(RoomRecordRefusal) as excinfo:
            require_implementer_write_grant(agent, "impl")
        assert excinfo.value.code == "implementer_grant"

    def test_workspace_write_on_a_capable_adapter_passes(self):
        from relay.agents.claude_code import ClaudeCodeAgent
        from relay.context.config import HarnessAgentConfig
        from relay.core.room_freeze import require_implementer_write_grant

        agent = ClaudeCodeAgent(
            profile=HarnessAgentConfig(grant=ExecutionGrantKind.WORKSPACE_WRITE)
        )
        require_implementer_write_grant(agent, "impl")  # no refusal

    def test_non_harness_agent_returns_early(self):
        from relay.agents.openai import OpenAICompatibleAgent
        from relay.core.room_freeze import require_implementer_write_grant

        # API-backed seats are refused later by the backend check; the grant
        # preflight only governs harness adapters.
        require_implementer_write_grant(OpenAICompatibleAgent(), "other")


class TestFreezeTransactionRechecks:
    """P7.3: the freeze re-validates canonical assumptions INSIDE the write
    lock, so a serialized competitor can never produce two canonical
    successors or a duplicate freeze."""

    def _second_store(self, fixture: RoomFixture):
        """A second connection on the same ledger — the 'other process'."""
        from relay.storage.db import connect

        conn = connect(fixture.db_path)
        return conn, SqliteRelayStore(conn)

    def test_competing_freeze_between_check_and_lock_refuses(self, fixture, tmp_path, monkeypatch):
        _parent, reply, _run = planner_exchange(fixture)
        conn2, store2 = self._second_store(fixture)
        try:
            real_transaction = store2.transaction

            def interleaved():
                # The competitor's freeze lands after this caller's pre-checks
                # but before its write lock — the in-transaction pass must see it.
                freeze(fixture, room_config(), reply=reply, workspace_root=tmp_path)
                return real_transaction()

            monkeypatch.setattr(store2, "transaction", interleaved)
            from relay.storage.events import EventLogWriter

            with pytest.raises(RoomRecordRefusal) as excinfo:
                freeze_room_plan(
                    store2,
                    EventLogWriter(conn2),
                    SqliteEvidenceStore(store2),
                    room_config(),
                    fixture.room,
                    source_message_id=reply.id,
                    frozen_by="human:utku",
                    workspace_root=tmp_path,
                )
            assert excinfo.value.code == "already_frozen"
            # Exactly one canonical plan chain exists — the winner's.
            from relay.core.room_graph import resolve_room_plan_chain

            task = next(iter(fixture.store.all_models(Task)))
            chain = resolve_room_plan_chain(fixture.store, fixture.room.id, task.id)
            assert len(chain.nodes) == 1
        finally:
            conn2.close()

    def test_competing_supersession_between_check_and_lock_refuses(
        self, fixture, tmp_path, monkeypatch
    ):
        first = _freeze(fixture, workspace_root=tmp_path)
        _p1, reply_a, _r1 = planner_exchange(fixture, content="# Plan\n\nA")
        _p2, reply_b, _r2 = planner_exchange(fixture, content="# Plan\n\nB")
        conn2, store2 = self._second_store(fixture)
        try:
            real_transaction = store2.transaction

            def interleaved():
                # The competitor supersedes the SAME tip first.
                freeze(
                    fixture,
                    room_config(),
                    reply=reply_a,
                    supersedes=first.plan_artifact.id,
                    workspace_root=tmp_path,
                )
                return real_transaction()

            monkeypatch.setattr(store2, "transaction", interleaved)
            from relay.storage.events import EventLogWriter

            with pytest.raises(RoomRecordRefusal) as excinfo:
                freeze_room_plan(
                    store2,
                    EventLogWriter(conn2),
                    SqliteEvidenceStore(store2),
                    room_config(),
                    fixture.room,
                    source_message_id=reply_b.id,
                    frozen_by="human:utku",
                    workspace_root=tmp_path,
                    supersedes_plan_artifact_id=first.plan_artifact.id,
                )
            assert excinfo.value.code == "supersede_not_tip"
            from relay.core.room_graph import resolve_room_plan_chain

            chain = resolve_room_plan_chain(fixture.store, fixture.room.id, first.task.id)
            assert len(chain.nodes) == 2  # one winner, one refusal — no fork
        finally:
            conn2.close()

    def test_newly_in_flight_run_between_check_and_lock_refuses(
        self, fixture, tmp_path, monkeypatch
    ):
        first = _freeze(fixture, workspace_root=tmp_path)
        _parent, reply, _run = planner_exchange(fixture, content="# Plan\n\nRev")
        conn2, store2 = self._second_store(fixture)
        try:
            real_transaction = store2.transaction

            def interleaved():
                # A build run takes flight after this caller's quiescence check.
                store = fixture.store
                run = store.save_model(
                    Run(
                        agent="impl",
                        role=AgentRole.IMPLEMENTER.value,
                        status=RunStatus.RUNNING,
                        task_id=first.task.id,
                    )
                )
                fixture.writer.record(
                    EventLogEntry(
                        type=EventType.BUILD_RUN_DISPATCHED,
                        task_id=first.task.id,
                        sender="relay:build",
                        content="build stage 'implement' bound to run",
                        references=[
                            f"task:{first.task.id}",
                            f"run:{run.id}",
                            "build_stage:implement",
                            "build_attempt:1",
                        ],
                    )
                )
                return real_transaction()

            monkeypatch.setattr(store2, "transaction", interleaved)
            from relay.storage.events import EventLogWriter

            with pytest.raises(RoomRecordRefusal) as excinfo:
                freeze_room_plan(
                    store2,
                    EventLogWriter(conn2),
                    SqliteEvidenceStore(store2),
                    room_config(),
                    fixture.room,
                    source_message_id=reply.id,
                    frozen_by="human:utku",
                    workspace_root=tmp_path,
                    supersedes_plan_artifact_id=first.plan_artifact.id,
                )
            # In-flight work surfaces through the ledger as either a typed
            # quiescence refusal or a continue-refusal — both are fail-closed.
            assert excinfo.value.code in {"not_quiescent", "ledger_refused"}
            from relay.core.room_graph import resolve_room_plan_chain

            chain = resolve_room_plan_chain(fixture.store, fixture.room.id, first.task.id)
            assert len(chain.nodes) == 1
        finally:
            conn2.close()

    def test_implementer_rebind_between_check_and_lock_refuses(
        self, fixture, tmp_path, monkeypatch
    ):
        """A seat rebind serialized before the write lock must not be adopted:
        the grant/model preflight ran against the ORIGINAL implementer — even
        when the new seat is itself a valid harness implementer."""
        from relay.context.config import AgentConfig, BackendType, HarnessAgentConfig
        from relay.core.rooms import RoomLifecycle

        _parent, reply, _run = planner_exchange(fixture)
        # 'impl2' is a VALID harness implementer — the refusal must come from
        # the seat-change guard, not a backend/grant failure.
        config = room_config(
            extra_agents={
                "impl2": AgentConfig(
                    backend=BackendType.HARNESS,
                    adapter="claude_code",
                    model="offline",
                    harness=HarnessAgentConfig(
                        grant=ExecutionGrantKind.WORKSPACE_WRITE,
                        executable_path="python",
                        timeout_seconds=30,
                    ),
                )
            }
        )
        conn2, store2 = self._second_store(fixture)
        try:
            real_transaction = store2.transaction

            def interleaved():
                # 'relay room bind' lands after this caller's pre-checks but
                # before its write lock — the locked pass must see it.
                RoomLifecycle(fixture.store, fixture.writer).bind(
                    fixture.store.load_model(Room, fixture.room.id),
                    AgentRole.IMPLEMENTER.value,
                    "impl2",
                    {"gpt", "impl", "impl2", "other"},
                )
                return real_transaction()

            monkeypatch.setattr(store2, "transaction", interleaved)
            from relay.storage.events import EventLogWriter

            baseline = fixture.store.counts()
            with pytest.raises(RoomRecordRefusal) as excinfo:
                freeze_room_plan(
                    store2,
                    EventLogWriter(conn2),
                    SqliteEvidenceStore(store2),
                    config,
                    fixture.room,
                    source_message_id=reply.id,
                    frozen_by="human:utku",
                    workspace_root=tmp_path,
                )
            assert excinfo.value.code == "implementer_seat_changed"
            after = fixture.store.counts()
            # Zero canonical freeze delta — the rebind's own event is the only
            # new row in the ledger.
            assert after["tasks"] == baseline["tasks"]
            assert after["artifacts"] == baseline["artifacts"]
            assert after["event_log"] == baseline["event_log"] + 1
            persisted = fixture.store.load_model(Room, fixture.room.id)
            assert persisted is not None
            seats = {member.role: member.agent for member in persisted.members}
            # The concurrent rebind survives — no stale writeback restores it.
            assert seats[AgentRole.IMPLEMENTER.value] == "impl2"
            assert persisted.active_task_id is None
        finally:
            conn2.close()

    def test_unrelated_seat_rebind_survives_a_successful_freeze(
        self, fixture, tmp_path, monkeypatch
    ):
        """An unrelated seat (planner) rebound before the write lock must be
        preserved by the freeze's own ``active_task_id`` update."""
        from relay.core.rooms import RoomLifecycle

        _parent, reply, _run = planner_exchange(fixture)
        conn2, store2 = self._second_store(fixture)
        try:
            real_transaction = store2.transaction

            def interleaved():
                RoomLifecycle(fixture.store, fixture.writer).bind(
                    fixture.store.load_model(Room, fixture.room.id),
                    AgentRole.PLANNER.value,
                    "other",
                    {"gpt", "impl", "other"},
                )
                return real_transaction()

            monkeypatch.setattr(store2, "transaction", interleaved)
            from relay.storage.events import EventLogWriter

            outcome = freeze_room_plan(
                store2,
                EventLogWriter(conn2),
                SqliteEvidenceStore(store2),
                room_config(),
                fixture.room,
                source_message_id=reply.id,
                frozen_by="human:utku",
                workspace_root=tmp_path,
            )
            persisted = fixture.store.load_model(Room, fixture.room.id)
            assert persisted is not None
            seats = {member.role: member.agent for member in persisted.members}
            assert seats[AgentRole.PLANNER.value] == "other"
            assert seats[AgentRole.IMPLEMENTER.value] == "impl"
            assert persisted.active_task_id == outcome.task.id
        finally:
            conn2.close()


def _room_without_implementer(fixture: RoomFixture, seats: dict[str, str] | None = None):
    """A second Room in the same workspace with the given seats."""
    from relay.core.rooms import RoomLifecycle

    store, writer = fixture.store, fixture.writer
    workspace = store.load_model(Workspace, fixture.workspace.id)
    bindings = seats if seats is not None else {"planner": "gpt"}
    return RoomLifecycle(store, writer).create(
        workspace,
        f"Seat variant {len(list(store.all_models(type(fixture.room))))}",
        bindings,
        {*bindings.values(), "gpt", "impl", "other", "ghost"},
    )


class TestSupersedingFreeze:
    def _pin_baseline(self, store, root, task_id: str) -> None:
        """The durable baseline pin a real dispatch would have written."""
        from relay.core.baseline import capture_baseline, persist_baseline
        from relay.core.reviews import canonical_json

        pin = persist_baseline(root, task_id, capture_baseline(root))
        store.save_model(
            Artifact(
                kind=ArtifactKind.REPORT,
                task_id=task_id,
                content=canonical_json(pin),
            )
        )

    def _dispatch_marker(self, store, task_id: str, run_id: str, stage: str = "implement") -> None:
        from relay.storage.events import EventLogWriter

        EventLogWriter(store.conn).record(
            EventLogEntry(
                type=EventType.BUILD_RUN_DISPATCHED,
                task_id=task_id,
                sender="relay:build",
                content=f"build stage '{stage}' bound to run {run_id}",
                references=[
                    f"task:{task_id}",
                    f"run:{run_id}",
                    f"build_stage:{stage}",
                    "build_attempt:1",
                ],
            )
        )

    def test_superseding_freeze_advances_the_tip(self, fixture):
        first = _freeze(fixture)
        _parent, reply, _run = planner_exchange(
            fixture, content="# Plan\n\nStep 2: revised approach"
        )
        second = _freeze(fixture, reply=reply, supersedes=first.plan_artifact.id)
        assert second.task.id == first.task.id
        assert second.superseded_plan_artifact_id == first.plan_artifact.id
        store = fixture.store
        from relay.core.room_graph import resolve_room_plan_chain

        chain = resolve_room_plan_chain(store, fixture.room.id, first.task.id)
        assert [node.plan.id for node in chain.nodes] == [
            first.plan_artifact.id,
            second.plan_artifact.id,
        ]
        assert [node.edge for node in chain.nodes] == ["frozen", "frozen"]
        assert chain.tip.id == second.plan_artifact.id

    def test_supersede_target_must_be_the_tip(self, fixture):
        first = _freeze(fixture)
        _parent, reply_a, _run = planner_exchange(fixture, content="# Plan\n\nA")
        second = _freeze(fixture, reply=reply_a, supersedes=first.plan_artifact.id)
        _parent, reply_b, _run = planner_exchange(fixture, content="# Plan\n\nB")
        baseline = fixture.store.counts()
        with pytest.raises(RoomRecordRefusal) as excinfo:
            _freeze(fixture, reply=reply_b, supersedes=first.plan_artifact.id)
        assert excinfo.value.code == "supersede_not_tip"
        assert fixture.store.counts() == baseline
        assert second.plan_artifact.id

    def test_foreign_room_supersede_target_refuses(self, fixture):
        first = _freeze(fixture)
        store = fixture.store
        other = store.save_model(
            Room(id="other-room", name="Other", workspace_id=fixture.workspace.id)
        )
        other_task = store.save_model(Task(title="elsewhere", room_id=other.id))
        foreign_plan = store.save_model(
            Artifact(
                kind=ArtifactKind.PLAN,
                room_id=other.id,
                task_id=other_task.id,
                content="# Elsewhere",
            )
        )
        _parent, reply, _run = planner_exchange(fixture)
        with pytest.raises(RoomRecordRefusal) as excinfo:
            _freeze(fixture, reply=reply, supersedes=foreign_plan.id)
        assert excinfo.value.code == "supersede_foreign"
        assert first.plan_artifact.id

    def test_supersede_refused_while_a_build_run_is_in_flight(self, fixture, tmp_path):
        first = _freeze(fixture, workspace_root=tmp_path)
        store = fixture.store
        self._pin_baseline(store, tmp_path, first.task.id)
        run = store.save_model(
            Run(
                agent="impl",
                role=AgentRole.IMPLEMENTER.value,
                status=RunStatus.RUNNING,
                task_id=first.task.id,
            )
        )
        self._dispatch_marker(store, first.task.id, run.id)
        _parent, reply, _run = planner_exchange(fixture, content="# Plan\n\nRev")
        baseline = store.counts()
        with pytest.raises(RoomRecordRefusal) as excinfo:
            _freeze(
                fixture,
                reply=reply,
                supersedes=first.plan_artifact.id,
                workspace_root=tmp_path,
            )
        assert excinfo.value.code == "not_quiescent"
        assert store.counts() == baseline

    def test_supersede_refused_while_a_blocking_signal_is_open(self, fixture, tmp_path):
        first = _freeze(fixture, workspace_root=tmp_path)
        store = fixture.store
        self._pin_baseline(store, tmp_path, first.task.id)
        run = store.save_model(
            Run(
                agent="impl",
                role=AgentRole.IMPLEMENTER.value,
                status=RunStatus.SUCCEEDED,
                task_id=first.task.id,
            )
        )
        self._dispatch_marker(store, first.task.id, run.id)
        signal_id = _signal_message(store, fixture, first.task.id, run.id)
        assert signal_id
        _parent, reply, _run = planner_exchange(fixture, content="# Plan\n\nRev")
        with pytest.raises(RoomRecordRefusal) as excinfo:
            _freeze(
                fixture,
                reply=reply,
                supersedes=first.plan_artifact.id,
                workspace_root=tmp_path,
            )
        assert excinfo.value.code == "not_quiescent"

    def test_supersede_succeeds_at_a_quiescent_dispatch_position(self, fixture, tmp_path):
        first = _freeze(fixture, workspace_root=tmp_path)
        store = fixture.store
        self._pin_baseline(store, tmp_path, first.task.id)
        run = store.save_model(
            Run(
                agent="impl",
                role=AgentRole.IMPLEMENTER.value,
                status=RunStatus.CANCELLED,
                task_id=first.task.id,
            )
        )
        self._dispatch_marker(store, first.task.id, run.id)
        # A cancelled run is settled, not in flight; the position is a fresh
        # dispatch against the current tip, so the supersession is allowed.
        outcome = _freeze(
            fixture,
            reply=planner_exchange(fixture, content="# Plan\n\nRev")[1],
            supersedes=first.plan_artifact.id,
            workspace_root=tmp_path,
        )
        assert outcome.superseded_plan_artifact_id == first.plan_artifact.id
        assert outcome.task.id == first.task.id
        assert outcome.plan_artifact.id != first.plan_artifact.id


def _signal_message(store: SqliteRelayStore, fixture: RoomFixture, task_id: str, run_id: str) -> str:
    """A valid open blocking signal authored by a bound build run (P6.4).

    Mirrors the runtime: the run's whole output IS the strict stage-signal
    object (persisted as ``RUN_OUTPUT``), and the signal message is composed
    from it and sent through the bus.
    """
    from relay.core.bus import ConversationBus
    from relay.core.rooms import RoomSeatResolver
    from relay.core.stage_signals import StageSignalPayload, compose_signal_message
    from relay.storage.events import EventLogWriter

    task = store.load_model(Task, task_id)
    run = store.load_model(Run, run_id)
    assert task is not None and run is not None
    signal = StageSignalPayload(
        schema_version="relay.stage_signal.v1",
        kind="proposal",
        to_role=AgentRole.PLANNER.value,
        body="Use design B for the registry",
    )
    store.save_model(
        Artifact(
            kind=ArtifactKind.RUN_OUTPUT,
            task_id=task_id,
            run_id=run_id,
            content=signal.model_dump_json(),
        )
    )
    message = compose_signal_message(task, run, signal)
    bus = ConversationBus(store, EventLogWriter(store.conn), RoomSeatResolver(fixture.room))
    return bus.send(message).id

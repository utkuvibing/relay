"""P4.2 delivery: binding marker Tx1 atomicity, at-most-once initiation,
READ_ONLY authority freeze, deterministic envelope, D.8 boundary proofs.

Frozen plan: ``docs/plans/p4.2-role-resolution-delivery-plan.md`` (rev 3),
decisions D7–D15. SPEC reference: §27 Phase 4; App. D.8/D.11-P4, C.7-P4.
"""

from typing import ClassVar

import pytest

from relay.agents.base import (
    Agent,
    AgentRequest,
    AgentResponse,
    AgentRole,
    BackendType,
)
from relay.agents.config import AgentSettings
from relay.context.config import ConfigError, HarnessAgentConfig
from relay.core.delivery import (
    DELIVERY_SENDER,
    DeliveryRefusal,
    DuplicateDeliveryRefusal,
    MessageDelivery,
)
from relay.core.evidence import EvidenceKind
from relay.core.permissions import Action
from relay.core.state_machine import TaskState
from relay.harness.errors import UnsupportedCapability
from relay.harness.runtime import HarnessAgent
from relay.harness.types import ExecutionGrantKind
from relay.storage.db import connect, migrate
from relay.storage.events import EventLogWriter
from relay.storage.models import (
    Approval,
    ApprovalStatus,
    Artifact,
    ArtifactKind,
    Decision,
    EventType,
    EvidenceRecord,
    Message,
    MessageType,
    Room,
    Run,
    RunStatus,
    Task,
)
from relay.storage.store import SqliteEvidenceStore, SqliteRelayStore

# ---------------------------------------------------------------------------
# Fakes — both execution families, offline by construction
# ---------------------------------------------------------------------------


class RecordingAPIAgent(Agent):
    """API-family fake: records every request it is handed."""

    name = "fake_gpt"
    backend = BackendType.API

    def __init__(self) -> None:
        self.received: list[AgentRequest] = []

    async def run(self, request: AgentRequest) -> AgentResponse:
        self.received.append(request)
        return AgentResponse(agent=self.name, role=request.role, output="api reply")


class RecordingHarnessAgent(HarnessAgent):
    """Harness-family fake: records the grant it was handed; never spawns."""

    name = "fake_harness"
    backend = BackendType.HARNESS

    received_grants: ClassVar[list[ExecutionGrantKind | None]] = []
    received_requests: ClassVar[list[AgentRequest]] = []
    fail_read_only = False

    async def run(self, request: AgentRequest) -> AgentResponse:
        grant = self._profile.grant if self._profile is not None else None
        type(self).received_grants.append(grant)
        type(self).received_requests.append(request)
        if self.fail_read_only and grant is ExecutionGrantKind.READ_ONLY_ACCESS:
            # The typed pre-spawn refusal a real adapter's fail-closed grant
            # translation raises when it cannot honor READ_ONLY (App. C.5).
            raise UnsupportedCapability("read_only_access")
        return AgentResponse(agent=self.name, role=request.role, output="harness reply")


class FakeFactory:
    """AgentFactory over injected instances; unknown names fail typed."""

    def __init__(self, agents: dict[str, Agent], models: dict[str, str | None] | None = None):
        self._agents = agents
        self._models = models or {}

    def build(self, name: str) -> Agent:
        if name not in self._agents:
            raise ConfigError(f"unknown agent '{name}' — configured agents: fixer, ...")
        return self._agents[name]

    def model_of(self, name: str) -> str | None:
        return self._models.get(name)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_RUN_IDS: dict[str, str] = {}


@pytest.fixture(autouse=True)
def _authorship_runs(store):
    """Strict authorship (P4.2 D1): bare agent senders need real Runs."""
    RecordingHarnessAgent.received_grants.clear()
    RecordingHarnessAgent.received_requests.clear()
    _RUN_IDS.clear()
    for name in ("claude", "gpt"):
        _RUN_IDS[name] = store.save_model(Run(agent=name, role="reviewer")).id
    yield
    _RUN_IDS.clear()


@pytest.fixture()
def db(tmp_path):
    conn = connect(tmp_path / "delivery.sqlite3")
    migrate(conn)
    yield conn
    conn.close()


@pytest.fixture()
def store(db):
    return SqliteRelayStore(db)


@pytest.fixture()
def scope(store):
    store.save_model(Room(id="room-1", name="delivery-room"))
    store.save_model(Task(id="t1", title="delivery task"))


@pytest.fixture()
def writer(db):
    return EventLogWriter(db)


@pytest.fixture()
def api_agent():
    return RecordingAPIAgent()


@pytest.fixture()
def harness_agent(tmp_path):
    return RecordingHarnessAgent(
        settings=AgentSettings(adapter="fake_harness"),
        profile=HarnessAgentConfig(grant=ExecutionGrantKind.WORKSPACE_WRITE),
        workspace_root=tmp_path,
    )


@pytest.fixture()
def delivery(store, writer, api_agent, harness_agent, scope):
    return MessageDelivery(
        store, writer, FakeFactory({"fixer": api_agent, "hfixer": harness_agent})
    )


def _message(**overrides) -> Message:
    base: dict[str, object] = {
        "sender": "claude",
        "recipient": "fixer",
        "room_id": "room-1",
        "type": MessageType.NOTE,
        "content": "the shim is intentional",
        "run_id": _RUN_IDS.get("claude"),
    }
    base.update(overrides)
    return Message(**base)


def _markers(db, kind: EventType) -> list:
    return [e for e in EventLogWriter(db).all() if e.type is kind]


# ---------------------------------------------------------------------------
# D10 — binding marker committed in Tx1
# ---------------------------------------------------------------------------


class TestDeliveryBinding:
    async def test_successful_delivery_persists_binding_marker(self, delivery, store, db):
        saved = store.save_model(_message())

        outcome = await delivery.deliver(saved.id)

        assert outcome.ask.error is None
        markers = [
            m
            for m in _markers(db, EventType.MESSAGE_DELIVERED)
            if f"message:{saved.id}" in m.references
        ]
        assert len(markers) == 1
        marker = markers[0]
        assert marker.sender == DELIVERY_SENDER
        assert marker.recipient == "fixer"
        assert f"run:{outcome.ask.run.id}" in marker.references
        assert "room:room-1" in marker.references
        assert marker.room_id == "room-1"
        # D10: the marker asserts a BINDING, never success.
        assert "bound to run" in marker.content
        assert outcome.ask.run.id in marker.content

    async def test_binding_marker_commits_with_tx1_not_the_outcome(
        self, delivery, store, db
    ):
        """D10/D14: the marker exists even when the run FAILS — it was
        written inside Tx1, before provider I/O."""
        class ExplodingAgent(Agent):
            name = "explode"
            backend = BackendType.API

            async def run(self, request: AgentRequest) -> AgentResponse:
                raise RuntimeError("provider exploded")

        delivery_factory = MessageDelivery(store, EventLogWriter(db), FakeFactory({"fixer": ExplodingAgent()}))
        saved = store.save_model(_message())

        outcome = await delivery_factory.deliver(saved.id)

        assert outcome.ask.response is None
        assert outcome.ask.run.status is RunStatus.FAILED
        markers = [
            m
            for m in _markers(db, EventType.MESSAGE_DELIVERED)
            if f"message:{saved.id}" in m.references
        ]
        assert len(markers) == 1  # retained on failure
        # Spine recorded the outcome honestly.
        finished = _markers(db, EventType.AGENT_RUN_FINISHED)
        assert any("failed" in e.content for e in finished)

    async def test_delivery_uses_the_crash_safe_spine(self, delivery, store, db):
        saved = store.save_model(_message())
        outcome = await delivery.deliver(saved.id)

        run = outcome.ask.run
        assert run.status is RunStatus.SUCCEEDED
        assert run.agent == "fixer"  # logical recipient name, authorship-ready
        artifacts = store.all_models(Artifact)
        by_kind: dict[ArtifactKind, int] = {}
        for artifact in artifacts:
            by_kind[artifact.kind] = by_kind.get(artifact.kind, 0) + 1
        assert by_kind[ArtifactKind.RUN_INPUT] == 1
        assert by_kind[ArtifactKind.RUN_OUTPUT] == 1
        events = EventLogWriter(db).all()
        assert any(e.type is EventType.AGENT_RUN_STARTED for e in events)
        assert any(e.type is EventType.AGENT_RUN_FINISHED for e in events)


# ---------------------------------------------------------------------------
# D13 — at-most-once initiation, unconditional
# ---------------------------------------------------------------------------


class TestAtMostOnce:
    async def test_second_delivery_after_success_is_refused_with_zero_delta(
        self, delivery, store, db
    ):
        saved = store.save_model(_message())
        await delivery.deliver(saved.id)
        before = store.counts()

        with pytest.raises(DuplicateDeliveryRefusal, match="already bound"):
            await delivery.deliver(saved.id)

        assert store.counts() == before

    async def test_second_delivery_after_failure_is_refused(self, store, db, scope):
        class ExplodingAgent(Agent):
            name = "explode"
            backend = BackendType.API

            async def run(self, request: AgentRequest) -> AgentResponse:
                raise RuntimeError("provider exploded")

        delivery = MessageDelivery(store, EventLogWriter(db), FakeFactory({"fixer": ExplodingAgent()}))
        saved = store.save_model(_message())
        outcome = await delivery.deliver(saved.id)
        assert outcome.ask.response is None
        before = store.counts()

        with pytest.raises(DuplicateDeliveryRefusal):
            await delivery.deliver(saved.id)

        assert store.counts() == before  # no retry run, nothing persisted

    async def test_duplicate_veto_rolls_back_atomically(self, delivery, store, db):
        """The veto fires INSIDE Tx1: no partial run/artifact/event survives."""
        saved = store.save_model(_message())
        await delivery.deliver(saved.id)
        before = store.counts()

        with pytest.raises(DuplicateDeliveryRefusal):
            await delivery.deliver(saved.id)

        counts = store.counts()
        assert counts["runs"] == before["runs"]
        assert counts["artifacts"] == before["artifacts"]
        assert counts["event_log"] == before["event_log"]
        assert len(delivery.deliveries_for_message(saved.id)) == 1


# ---------------------------------------------------------------------------
# D8 — the READ_ONLY authority freeze
# ---------------------------------------------------------------------------


class TestDeliveryGrant:
    async def test_harness_recipient_configured_write_runs_read_only(
        self, store, writer, harness_agent, scope
    ):
        """Frozen plan D8: the configured workspace_write grant never reaches
        the delivered run — a per-delivery READ_ONLY instance is bound."""
        delivery = MessageDelivery(store, writer, FakeFactory({"hfixer": harness_agent}))
        saved = store.save_model(_message(recipient="hfixer"))

        outcome = await delivery.deliver(saved.id)

        assert outcome.ask.error is None
        assert RecordingHarnessAgent.received_grants == [ExecutionGrantKind.READ_ONLY_ACCESS]

    async def test_missing_profile_becomes_explicit_read_only_profile(
        self, store, writer, tmp_path, scope
    ):
        """No configured profile ⇒ a fresh explicit READ_ONLY profile — never
        the adapter default grant (deliberately unlike _planner_for)."""
        bare = RecordingHarnessAgent(
            settings=AgentSettings(adapter="fake_harness"), workspace_root=tmp_path
        )
        assert bare._profile is None
        delivery = MessageDelivery(store, writer, FakeFactory({"hfixer": bare}))
        saved = store.save_model(_message(recipient="hfixer"))

        outcome = await delivery.deliver(saved.id)

        assert outcome.ask.error is None
        assert RecordingHarnessAgent.received_grants == [ExecutionGrantKind.READ_ONLY_ACCESS]

    async def test_no_fallback_when_read_only_is_unsupported(
        self, store, writer, tmp_path, scope
    ):
        """NEGATIVE AUTHORITY TEST (frozen plan D8/G4): a harness that cannot
        honor READ_ONLY fails typed pre-spawn and delivery NEVER retries with
        the configured write grant — one attempt, honest failure."""
        rigid = RecordingHarnessAgent(
            settings=AgentSettings(adapter="fake_harness"),
            profile=HarnessAgentConfig(grant=ExecutionGrantKind.WORKSPACE_WRITE),
            workspace_root=tmp_path,
        )
        # Class-level flag: the delivered instance is a FRESH type(agent)
        # construction (the READ_ONLY variant), so the refusal mode must
        # ride the class, not the seeded instance.
        RecordingHarnessAgent.fail_read_only = True
        try:
            delivery = MessageDelivery(store, writer, FakeFactory({"hfixer": rigid}))
            saved = store.save_model(_message(recipient="hfixer"))

            outcome = await delivery.deliver(saved.id)
        finally:
            RecordingHarnessAgent.fail_read_only = False

        assert outcome.ask.response is None
        assert outcome.ask.run.status is RunStatus.FAILED
        # Exactly ONE grant attempt was made — READ_ONLY, never workspace_write.
        assert RecordingHarnessAgent.received_grants == [ExecutionGrantKind.READ_ONLY_ACCESS]
        # The binding marker is retained; no fallback run exists.
        assert len(delivery.deliveries_for_message(saved.id)) == 1
        assert store.counts()["runs"] == 1 + len(_RUN_IDS)

    async def test_api_recipient_is_untouched_by_the_grant_rule(
        self, delivery, store, api_agent, scope
    ):
        saved = store.save_model(_message())
        await delivery.deliver(saved.id)
        assert api_agent.received  # delivered plain — no profile machinery


# ---------------------------------------------------------------------------
# D7/D11/D15 — delivery semantics
# ---------------------------------------------------------------------------


class TestDeliverySemantics:
    async def test_role_addressed_delivery_uses_the_addressed_role(self, store, delivery, api_agent):
        saved = store.save_model(_message(recipient_role="reviewer"))
        await delivery.deliver(saved.id)
        assert api_agent.received[0].role is AgentRole.REVIEWER

    async def test_direct_delivery_uses_the_participant_role(self, store, delivery, api_agent):
        saved = store.save_model(_message())
        await delivery.deliver(saved.id)
        assert api_agent.received[0].role is AgentRole.PARTICIPANT

    async def test_envelope_is_deterministic_across_identical_messages(
        self, store, delivery, api_agent
    ):
        """D15: same (sender, type, blocking, content) ⇒ byte-identical prompt;
        fixed field order; no message ids, no timestamps."""
        first = store.save_model(_message())
        second = store.save_model(_message(content="the shim is intentional"))
        assert first.id != second.id
        await delivery.deliver(first.id)
        await delivery.deliver(second.id)

        first_prompt, second_prompt = api_agent.received[0].prompt, api_agent.received[1].prompt
        assert first_prompt == second_prompt
        assert first_prompt.index("FROM:") < first_prompt.index("TYPE:")
        assert first_prompt.index("TYPE:") < first_prompt.index("BLOCKING:")
        assert first_prompt.index("BLOCKING:") < first_prompt.index("MESSAGE:")
        assert first_prompt.endswith("MESSAGE:\nthe shim is intentional")
        assert first.id not in first_prompt and second.id not in second_prompt

    async def test_envelope_carries_sender_type_blocking(self, store, delivery, api_agent):
        saved = store.save_model(
            _message(
                sender="human:utku",
                type=MessageType.CHALLENGE,
                blocking=True,
            )
        )
        await delivery.deliver(saved.id)
        prompt = api_agent.received[0].prompt
        assert "FROM: human:utku" in prompt
        assert "TYPE: challenge" in prompt
        assert "BLOCKING: true" in prompt

    async def test_references_pass_through_context_refs_not_prose(
        self, store, delivery, api_agent
    ):
        saved = store.save_model(
            _message(references=["plan:PLAN-17", "decision:DEC-24"])
        )
        await delivery.deliver(saved.id)
        request = api_agent.received[0]
        assert request.context_refs == ["plan:PLAN-17", "decision:DEC-24"]
        assert "PLAN-17" not in request.prompt  # D15: references never in prose

    async def test_request_scopes_carry_room_and_task(self, store, delivery, api_agent):
        saved = store.save_model(_message(task_id="t1"))
        await delivery.deliver(saved.id)
        request = api_agent.received[0]
        assert request.task_id == "t1" and request.room_id == "room-1"


# ---------------------------------------------------------------------------
# D7 — typed pre-run refusals, nothing persisted
# ---------------------------------------------------------------------------


class TestDeliveryRefusals:
    async def test_absent_message_is_refused(self, delivery, store):
        before = store.counts()
        with pytest.raises(DeliveryRefusal, match="does not exist"):
            await delivery.deliver("no-such-message")
        assert store.counts() == before

    @pytest.mark.parametrize("recipient", [None, "human:utku", "relay:core"])
    async def test_undeliverable_recipients_are_refused(self, delivery, store, recipient):
        saved = store.save_model(_message(recipient=recipient))
        before = store.counts()
        with pytest.raises(DeliveryRefusal, match="deliverable recipient"):
            await delivery.deliver(saved.id)
        assert store.counts() == before

    async def test_bogus_recipient_role_is_refused(self, delivery, store):
        """Direct store insert (bypassing the bus) can persist a bogus role;
        delivery fails closed instead of crashing."""
        saved = store.save_model(_message(recipient_role="bogus_role"))
        before = store.counts()
        with pytest.raises(DeliveryRefusal, match="not a valid AgentRole"):
            await delivery.deliver(saved.id)
        assert store.counts() == before

    async def test_unbuildable_recipient_is_refused(self, delivery, store):
        saved = store.save_model(_message(recipient="ghost"))
        before = store.counts()
        with pytest.raises(DeliveryRefusal, match="cannot be built"):
            await delivery.deliver(saved.id)
        assert store.counts() == before


# ---------------------------------------------------------------------------
# D9/D12 — delivery is not conversation and not state
# ---------------------------------------------------------------------------


class TestDeliveryIsNotState:
    @pytest.fixture()
    def seeded_state(self, store):
        task = store.save_model(Task(title="delivery task", state=TaskState.IMPLEMENTING))
        evidence = SqliteEvidenceStore(store)
        record = evidence.record(
            EvidenceRecord(
                kind=EvidenceKind.CONTEXT_COLLECTED,
                task_id=task.id,
                produced_by="relay:core",
            )
        )
        approval = store.save_model(
            Approval(action=Action.EDIT_FILES, task_id=task.id, status=ApprovalStatus.PENDING)
        )
        decision = store.save_model(Decision(statement="use SQLite", task_id=task.id))
        return {
            "task": task,
            "evidence": (record,),
            "approval": approval,
            "decision": decision,
            "counts": store.counts(),
        }

    async def test_delivery_writes_zero_messages_and_exact_run_delta(
        self, delivery, store, seeded_state
    ):
        saved = store.save_model(_message())
        baseline = store.counts()  # the delivered Message row is already in

        outcome = await delivery.deliver(saved.id)
        assert outcome.ask.error is None

        after = store.counts()
        assert after["messages"] == baseline["messages"]  # D9: no reply rows
        assert after["runs"] == baseline["runs"] + 1
        assert after["artifacts"] == baseline["artifacts"] + 2  # run_input + run_output
        assert after["event_log"] == baseline["event_log"] + 3  # started + delivered + finished
        assert after["tool_runs"] == baseline["tool_runs"]

    async def test_delivery_leaves_canonical_state_byte_identical(
        self, delivery, store, seeded_state
    ):
        baseline = seeded_state["counts"]
        saved = store.save_model(_message())
        await delivery.deliver(saved.id)

        after = store.counts()
        for table in ("tasks", "evidence_records", "approvals", "decisions", "rooms"):
            assert after[table] == baseline[table], table
        assert (
            store.load_model(Task, seeded_state["task"].id).state is TaskState.IMPLEMENTING
        )
        assert (
            store.load_model(Approval, seeded_state["approval"].id).status
            is ApprovalStatus.PENDING
        )
        assert (
            SqliteEvidenceStore(store).records_for_task(seeded_state["task"].id)
            == seeded_state["evidence"]
        )


# ---------------------------------------------------------------------------
# Read helper (D13)
# ---------------------------------------------------------------------------


class TestDeliveriesForMessage:
    async def test_counts_bound_runs_only(self, delivery, store):
        delivered = store.save_model(_message())
        other = store.save_model(_message(content="not delivered"))
        assert delivery.deliveries_for_message(delivered.id) == ()
        assert delivery.deliveries_for_message(other.id) == ()

        await delivery.deliver(delivered.id)

        markers = delivery.deliveries_for_message(delivered.id)
        assert len(markers) == 1
        assert delivery.deliveries_for_message(other.id) == ()

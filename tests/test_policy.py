"""P5.1 communication policy, budget, and fail-closed seam tests."""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
from pathlib import Path

import pytest

from relay.agents.base import Agent, AgentRequest, AgentResponse, AgentRole, BackendType
from relay.context.config import (
    ConfigError,
    RelayConfig,
    load_config,
)
from relay.core.bus import ConversationBus
from relay.core.delivery import (
    DeliveryOutcome,
    DeliveryPendingRefusal,
    DeliveryRefusal,
    InvalidReplyTypeRefusal,
    MessageDelivery,
)
from relay.core.driver import (
    ConversationDriver,
    ConversationSpec,
    ParticipantAddress,
    PolicyRefusal,
    StopReason,
)
from relay.core.policy import (
    BlockingBudgetExhausted,
    BlockingNotPermitted,
    CommunicationBudgets,
    CommunicationPolicy,
    EdgeNotPermitted,
    MessageRejected,
    PolicyEdge,
    PolicyEnvelope,
    PrincipalClass,
    SqliteCommunicationPolicyGate,
    TurnBudgetExhausted,
    TypeNotPermitted,
    evaluate_edge,
    policy_from_config,
    reply_admission_reference,
)
from relay.storage.db import connect, migrate
from relay.storage.events import EventLogWriter
from relay.storage.models import (
    EventLogEntry,
    EventType,
    Message,
    MessageType,
    Room,
    Run,
    RunStatus,
)
from relay.storage.store import SqliteRelayStore


class RecordingAgent(Agent):
    """Small offline agent used to prove provider calls stay behind admission."""

    backend = BackendType.API

    def __init__(self, name: str, *, fail_first: bool = False) -> None:
        self.name = name
        self.fail_first = fail_first
        self.calls = 0

    async def run(self, request: AgentRequest) -> AgentResponse:
        self.calls += 1
        if self.fail_first and self.calls == 1:
            raise RuntimeError("transient failure")
        return AgentResponse(agent=self.name, role=request.role, output=f"reply from {self.name}")


class FakeFactory:
    def __init__(self, agents: dict[str, Agent]) -> None:
        self.agents = agents

    def build(self, name: str) -> Agent:
        return self.agents[name]

    def model_of(self, name: str) -> str | None:
        return None


class StaticResolver:
    def __init__(self, agents: tuple[str, ...], roles: dict[str, str]) -> None:
        self.agents = frozenset(agents)
        self.roles = dict(roles)

    def resolve_role(self, role: str) -> str | None:
        return self.roles.get(role)

    def knows_agent(self, name: str) -> bool:
        return name in self.agents


@pytest.fixture()
def db(tmp_path: Path):
    conn = connect(tmp_path / "policy.sqlite3")
    migrate(conn)
    yield conn
    conn.close()


@pytest.fixture()
def store(db):
    store = SqliteRelayStore(db)
    store.save_model(Room(id="room-1", name="policy-room"))
    return store


@pytest.fixture()
def writer(db):
    return EventLogWriter(db)


def _policy(
    *, max_agent_turns: int = 16, max_blocking_messages: int = 3, edges=None
) -> CommunicationPolicy:
    return CommunicationPolicy(
        budgets=CommunicationBudgets(max_agent_turns, max_blocking_messages),
        edges=edges,
    )


def _message(**overrides) -> Message:
    values: dict[str, object] = {
        "sender": "relay:driver",
        "recipient": "alpha",
        "room_id": "room-1",
        "type": MessageType.NOTE,
        "content": "continue",
    }
    values.update(overrides)
    return Message(**values)


def _author_message(store, run_id: str, **overrides) -> Message:
    values: dict[str, object] = {
        "sender": "author",
        "recipient": "recipient",
        "run_id": run_id,
        "room_id": "room-1",
        "type": MessageType.NOTE,
        "content": "a message",
    }
    values.update(overrides)
    return Message(**values)


def _config_yaml(communication: str) -> str:
    return f"agents:\n  alpha:\n    backend: api\n    adapter: fake\n{communication}"


def _load_communication(tmp_path: Path, communication: str):
    (tmp_path / "relay.yaml").write_text(_config_yaml(communication), encoding="utf-8")
    return load_config(tmp_path)


class TestPolicyEvaluator:
    def test_evaluate_edge_matrix_and_non_role_exemptions(self):
        edge = PolicyEdge(
            AgentRole.IMPLEMENTER,
            AgentRole.REVIEWER,
            frozenset({MessageType.NOTE, MessageType.PROPOSAL}),
            blocking_allowed=True,
        )
        policy = _policy(edges=frozenset({edge}))

        evaluate_edge(
            policy,
            PolicyEnvelope(
                AgentRole.IMPLEMENTER,
                AgentRole.REVIEWER,
                MessageType.PROPOSAL,
                True,
                "room-1",
                None,
            ),
        )
        with pytest.raises(EdgeNotPermitted):
            evaluate_edge(
                policy,
                PolicyEnvelope(
                    AgentRole.REVIEWER,
                    AgentRole.IMPLEMENTER,
                    MessageType.NOTE,
                    False,
                    "room-1",
                    None,
                ),
            )
        with pytest.raises(TypeNotPermitted):
            evaluate_edge(
                policy,
                PolicyEnvelope(
                    AgentRole.IMPLEMENTER,
                    AgentRole.REVIEWER,
                    MessageType.CHALLENGE,
                    False,
                    "room-1",
                    None,
                ),
            )
        with pytest.raises(BlockingNotPermitted):
            evaluate_edge(
                _policy(
                    edges=frozenset(
                        {
                            PolicyEdge(
                                AgentRole.IMPLEMENTER,
                                AgentRole.REVIEWER,
                                frozenset({MessageType.NOTE}),
                            )
                        }
                    )
                ),
                PolicyEnvelope(
                    AgentRole.IMPLEMENTER,
                    AgentRole.REVIEWER,
                    MessageType.NOTE,
                    True,
                    "room-1",
                    None,
                ),
            )

        # Pair rules cover role-to-role collaboration only.
        for sender, recipient in (
            (PrincipalClass.HUMAN, AgentRole.IMPLEMENTER),
            (PrincipalClass.RELAY, AgentRole.IMPLEMENTER),
            (AgentRole.IMPLEMENTER, PrincipalClass.HUMAN),
            (AgentRole.IMPLEMENTER, PrincipalClass.RELAY),
        ):
            evaluate_edge(
                policy,
                PolicyEnvelope(sender, recipient, MessageType.SYSTEM, True, "room-1", None),
            )

        evaluate_edge(
            _policy(edges=None),
            PolicyEnvelope(
                AgentRole.REVIEWER,
                AgentRole.IMPLEMENTER,
                MessageType.CHALLENGE,
                True,
                "room-1",
                None,
            ),
        )


class TestCommunicationConfig:
    def test_defaults_and_explicit_empty_edges(self, tmp_path):
        absent = policy_from_config(RelayConfig(agents={}))
        assert absent.budgets == CommunicationBudgets(16, 3)
        assert absent.edges is None

        config = _load_communication(
            tmp_path,
            "communication:\n"
            "  budgets:\n"
            "    max_agent_turns: 4\n"
            "    max_blocking_messages: 0\n"
            "  edges: []\n",
        )
        parsed = policy_from_config(config)
        assert parsed.budgets == CommunicationBudgets(4, 0)
        assert parsed.edges == frozenset()

    @pytest.mark.parametrize(
        ("communication", "match"),
        [
            (
                "communication:\n  edges:\n    - from: not_a_role\n      to: reviewer\n      types: [note]\n",
                "communication\\.edges",
            ),
            (
                "communication:\n  edges:\n    - from: implementer\n      to: reviewer\n      types: [not_a_type]\n",
                "communication\\.edges",
            ),
            (
                "communication:\n  edges:\n    - from: implementer\n      to: reviewer\n      types: [system]\n",
                "system messages",
            ),
            (
                "communication:\n  edges:\n    - from: implementer\n      to: reviewer\n      types: [note]\n      blocking: true\n",
                "blocking-capable",
            ),
            (
                "communication:\n  edges:\n    - from: reviewer\n      to: reviewer\n      types: [note]\n",
                "self-send",
            ),
            (
                "communication:\n  edges:\n    - from_: implementer\n      to: reviewer\n      types: [note]\n",
                "communication.edges",
            ),
            (
                "communication:\n  edges:\n    - from: implementer\n      to: reviewer\n      types: [note]\n    - from: implementer\n      to: reviewer\n      types: [proposal]\n",
                "duplicate communication edge",
            ),
            (
                "communication:\n  budgets:\n    max_agent_turns: 0\n",
                "greater than or equal to 1",
            ),
            (
                "communication:\n  budgets:\n    max_blocking_messages: 1001\n",
                "less than or equal to 1000",
            ),
        ],
    )
    def test_invalid_communication_config_is_rejected(self, tmp_path, communication, match):
        with pytest.raises(ConfigError, match=match):
            _load_communication(tmp_path, communication)

    def test_config_models_forbid_unknown_fields(self, tmp_path):
        with pytest.raises(ConfigError, match="extra_field"):
            _load_communication(
                tmp_path,
                "communication:\n  extra_field: true\n",
            )


class TestBusPolicy:
    def test_agent_edge_refusal_is_zero_delta(self, store, writer):
        author_run = store.save_model(Run(agent="author", role=AgentRole.IMPLEMENTER))
        resolver = StaticResolver(("author", "recipient"), {"reviewer": "recipient"})
        gate = SqliteCommunicationPolicyGate(
            store,
            _policy(
                edges=frozenset(
                    {
                        PolicyEdge(
                            AgentRole.IMPLEMENTER,
                            AgentRole.REVIEWER,
                            frozenset({MessageType.NOTE}),
                        )
                    }
                )
            ),
        )
        bus = ConversationBus(store, writer, resolver=resolver, policy=gate)
        before = store.counts()

        with pytest.raises(TypeNotPermitted):
            bus.send(
                _author_message(
                    store,
                    author_run.id,
                    recipient=None,
                    recipient_role="reviewer",
                    type=MessageType.CHALLENGE,
                )
            )

        assert store.counts() == before

    def test_sender_role_and_recipient_address_provenance_reach_the_gate(self, store, writer):
        class CapturingGate:
            def __init__(self):
                self.envelopes = []

            def check_edge(self, envelope):
                self.envelopes.append(envelope)

            def check_turn_budget(self, room_id, task_id):
                pass

            def check_blocking_budget(self, room_id, task_id):
                pass

        author_run = store.save_model(Run(agent="author", role=AgentRole.IMPLEMENTER))
        gate = CapturingGate()
        resolver = StaticResolver(("author", "recipient"), {"reviewer": "recipient"})
        bus = ConversationBus(store, writer, resolver=resolver, policy=gate)

        bus.send(_author_message(store, author_run.id, content="direct"))
        bus.send(
            _author_message(
                store,
                author_run.id,
                content="role addressed",
                recipient=None,
                recipient_role="reviewer",
            )
        )

        assert [(item.sender, item.recipient) for item in gate.envelopes] == [
            (AgentRole.IMPLEMENTER, AgentRole.PARTICIPANT),
            (AgentRole.IMPLEMENTER, AgentRole.REVIEWER),
        ]

    def test_blocking_budget_counts_relay_but_not_human(self, store, writer):
        author_run = store.save_model(Run(agent="author", role=AgentRole.IMPLEMENTER))
        gate = SqliteCommunicationPolicyGate(store, _policy(max_blocking_messages=2, edges=None))
        bus = ConversationBus(store, writer, policy=gate)
        bus.send(_author_message(store, author_run.id, type=MessageType.CHALLENGE, blocking=True))
        bus.send(
            _message(
                id="human-blocking",
                sender="human:operator",
                type=MessageType.CHALLENGE,
                blocking=True,
            )
        )
        bus.send(
            _message(
                id="relay-blocking",
                type=MessageType.PROPOSAL,
                blocking=True,
            )
        )
        before = store.counts()
        with pytest.raises(BlockingBudgetExhausted):
            bus.send(
                _author_message(
                    store,
                    author_run.id,
                    id="over-budget",
                    type=MessageType.REVIEW_FINDING,
                    blocking=True,
                )
            )
        assert store.counts() == before
        assert before["messages"] == 3

    def test_delivery_bound_reply_cannot_materialize_as_blocking(self, store, writer):
        author_run = store.save_model(Run(agent="author", role=AgentRole.IMPLEMENTER))
        parent = store.save_model(
            _author_message(store, author_run.id, recipient="alpha")
        )
        reply_run = store.save_model(
            Run(agent="alpha", role=AgentRole.PARTICIPANT, status=RunStatus.SUCCEEDED)
        )
        with store.transaction():
            writer.record(
                EventLogEntry(
                    room_id=parent.room_id,
                    task_id=parent.task_id,
                    sender="relay:delivery",
                    recipient="alpha",
                    type=EventType.MESSAGE_DELIVERED,
                    content="bound",
                    references=[
                        f"message:{parent.id}",
                        f"run:{reply_run.id}",
                        reply_admission_reference(MessageType.CHALLENGE),
                    ],
                )
            )
        gate = SqliteCommunicationPolicyGate(
            store,
            _policy(
                edges=frozenset(
                    {
                        PolicyEdge(
                            AgentRole.PARTICIPANT,
                            AgentRole.IMPLEMENTER,
                            frozenset({MessageType.CHALLENGE}),
                        )
                    }
                )
            ),
        )
        bus = ConversationBus(store, writer, policy=gate)
        blocking_reply = Message(
            sender="alpha",
            recipient="author",
            reply_to_id=parent.id,
            run_id=reply_run.id,
            room_id=parent.room_id,
            task_id=parent.task_id,
            type=MessageType.CHALLENGE,
            content="blocking reply",
            blocking=True,
        )
        before = store.counts()

        with pytest.raises(MessageRejected, match="delivery-bound reply.*non-blocking"):
            bus.send(blocking_reply)

        assert store.counts() == before


class TestDeliveryPolicy:
    async def test_policy_none_preserves_p4_delivery_marker_shape(self, store, writer):
        parent = store.save_model(_message(id="p4-marker-shape"))
        agent = RecordingAgent("alpha")
        delivery = MessageDelivery(store, writer, FakeFactory({"alpha": agent}))

        outcome = await delivery.deliver_and_reply(parent.id, reply_type=MessageType.NOTE)
        marker = delivery.deliveries_for_message(parent.id)[0]

        assert marker.references == [
            f"message:{parent.id}",
            f"run:{outcome.ask.run.id}",
            "room:room-1",
        ]
        assert marker.content == (
            f"note from {parent.sender} to {parent.recipient} bound to run "
            f"{outcome.ask.run.id}"
        )
        assert outcome.reply is not None
        assert agent.calls == 1

    async def test_system_reply_type_is_rejected_before_delivery(self, store, writer):
        parent = store.save_model(_message(id="system-reply-parent"))
        agent = RecordingAgent("alpha")
        gate = SqliteCommunicationPolicyGate(store, _policy(edges=None))
        delivery = MessageDelivery(store, writer, FakeFactory({"alpha": agent}), policy=gate)
        before = store.counts()

        with pytest.raises(InvalidReplyTypeRefusal, match="reserved"):
            await delivery.deliver_and_reply(parent.id, reply_type=MessageType.SYSTEM)

        assert store.counts() == before
        assert delivery.deliveries_for_message(parent.id) == ()
        assert agent.calls == 0

    async def test_turn_budget_refuses_before_write_or_invocation(self, store, writer):
        first = store.save_model(_message(id="first"))
        second = store.save_model(_message(id="second"))
        agent = RecordingAgent("alpha")
        gate = SqliteCommunicationPolicyGate(store, _policy(max_agent_turns=1))
        delivery = MessageDelivery(store, writer, FakeFactory({"alpha": agent}), policy=gate)

        await delivery.deliver(first.id)
        before = store.counts()
        with pytest.raises(TurnBudgetExhausted):
            await delivery.deliver(second.id)

        assert store.counts() == before
        assert agent.calls == 1
        assert len(delivery.deliveries_for_message(second.id)) == 0

    async def test_delivery_bound_reply_survives_stricter_policy_after_initiation(
        self, store, writer
    ):
        class ReplyWriteInterruptedBus(ConversationBus):
            def send(self, message, **kwargs):
                if message.reply_to_id is not None:
                    raise RuntimeError("simulated crash before reply persistence")
                return super().send(message, **kwargs)

        author_run = store.save_model(Run(agent="author", role=AgentRole.IMPLEMENTER))
        parent = store.save_model(
            _author_message(store, author_run.id, recipient="alpha", type=MessageType.NOTE)
        )
        agent = RecordingAgent("alpha")
        admitted_policy = _policy(
            max_agent_turns=1,
            edges=frozenset(
                {
                    PolicyEdge(
                        AgentRole.PARTICIPANT,
                        AgentRole.IMPLEMENTER,
                        frozenset({MessageType.NOTE}),
                    )
                }
            ),
        )
        gate = SqliteCommunicationPolicyGate(store, admitted_policy)
        interrupted_bus = ReplyWriteInterruptedBus(store, writer, policy=gate)
        delivery = MessageDelivery(
            store,
            writer,
            FakeFactory({"alpha": agent}),
            bus=interrupted_bus,
            policy=gate,
        )

        with pytest.raises(RuntimeError, match="simulated crash"):
            await delivery.deliver_and_reply(parent.id, reply_type=MessageType.NOTE)
        marker = delivery.deliveries_for_message(parent.id)[0]
        assert reply_admission_reference(MessageType.NOTE) in marker.references

        gate.policy = _policy(max_agent_turns=1, edges=frozenset())
        recovery = MessageDelivery(
            store,
            writer,
            FakeFactory({"alpha": agent}),
            bus=ConversationBus(store, writer, policy=gate),
            policy=gate,
        )
        outcome = await recovery.deliver_and_reply(parent.id, reply_type=MessageType.NOTE)

        assert outcome.reply is not None
        assert outcome.reply.reply_to_id == parent.id
        assert agent.calls == 1

    async def test_standalone_delivery_rechecks_reply_edge_on_recovery(self, store, writer):
        author_run = store.save_model(Run(agent="author", role=AgentRole.IMPLEMENTER))
        parent = store.save_model(_author_message(store, author_run.id, recipient="alpha"))
        agent = RecordingAgent("alpha")
        gate = SqliteCommunicationPolicyGate(store, _policy(edges=frozenset()))
        delivery = MessageDelivery(store, writer, FakeFactory({"alpha": agent}), policy=gate)

        await delivery.deliver(parent.id)
        marker = delivery.deliveries_for_message(parent.id)[0]
        assert not any(ref.startswith("reply-type:") for ref in marker.references)
        before = store.counts()

        with pytest.raises(EdgeNotPermitted):
            await delivery.deliver_and_reply(parent.id, reply_type=MessageType.CHALLENGE)

        assert store.counts() == before
        assert delivery._bus.replies_for(parent.id) == []
        assert agent.calls == 1

    async def test_standalone_delivery_recovery_uses_parent_authorship_role(
        self, store, writer
    ):
        author_run = store.save_model(Run(agent="author", role=AgentRole.IMPLEMENTER))
        parent = store.save_model(_author_message(store, author_run.id, recipient="alpha"))
        agent = RecordingAgent("alpha")
        gate = SqliteCommunicationPolicyGate(
            store,
            _policy(
                edges=frozenset(
                    {
                        PolicyEdge(
                            AgentRole.PARTICIPANT,
                            AgentRole.IMPLEMENTER,
                            frozenset({MessageType.CHALLENGE}),
                        )
                    }
                )
            ),
        )
        delivery = MessageDelivery(store, writer, FakeFactory({"alpha": agent}), policy=gate)

        await delivery.deliver(parent.id)
        outcome = await delivery.deliver_and_reply(
            parent.id, reply_type=MessageType.CHALLENGE
        )

        assert outcome.reply is not None
        assert outcome.reply.type is MessageType.CHALLENGE
        assert outcome.reply.recipient == "author"
        assert agent.calls == 1

    async def test_recovery_cannot_retype_an_admitted_reply(self, store, writer):
        class ReplyWriteInterruptedBus(ConversationBus):
            def send(self, message, **kwargs):
                if message.reply_to_id is not None:
                    raise RuntimeError("simulated crash before reply persistence")
                return super().send(message, **kwargs)

        author_run = store.save_model(Run(agent="author", role=AgentRole.IMPLEMENTER))
        parent = store.save_model(_author_message(store, author_run.id, recipient="alpha"))
        agent = RecordingAgent("alpha")
        gate = SqliteCommunicationPolicyGate(store, _policy(edges=None))
        interrupted = MessageDelivery(
            store,
            writer,
            FakeFactory({"alpha": agent}),
            bus=ReplyWriteInterruptedBus(store, writer, policy=gate),
            policy=gate,
        )

        with pytest.raises(RuntimeError, match="simulated crash"):
            await interrupted.deliver_and_reply(parent.id, reply_type=MessageType.NOTE)
        before = store.counts()
        recovery = MessageDelivery(store, writer, FakeFactory({"alpha": agent}), policy=gate)

        with pytest.raises(DeliveryRefusal, match="not reply type 'challenge'"):
            await recovery.deliver_and_reply(parent.id, reply_type=MessageType.CHALLENGE)

        assert store.counts() == before
        assert recovery._bus.replies_for(parent.id) == []
        assert agent.calls == 1

        marker = recovery.deliveries_for_message(parent.id)[0]
        run_id = next(ref[4:] for ref in marker.references if ref.startswith("run:"))
        gate.policy = _policy(edges=None)
        retyped_reply = Message(
            sender="alpha",
            recipient="author",
            reply_to_id=parent.id,
            run_id=run_id,
            room_id=parent.room_id,
            task_id=parent.task_id,
            type=MessageType.CHALLENGE,
            content="retyped",
        )
        with pytest.raises(MessageRejected, match="admitted reply type 'note'"):
            recovery._bus.send(retyped_reply)
        assert store.counts() == before

    async def test_recovery_rejects_existing_blocking_delivery_bound_reply(
        self, store, writer
    ):
        author_run = store.save_model(Run(agent="author", role=AgentRole.IMPLEMENTER))
        parent = store.save_model(
            _author_message(store, author_run.id, recipient="alpha")
        )
        reply_run = store.save_model(
            Run(agent="alpha", role=AgentRole.PARTICIPANT, status=RunStatus.SUCCEEDED)
        )
        with store.transaction():
            writer.record(
                EventLogEntry(
                    room_id=parent.room_id,
                    task_id=parent.task_id,
                    sender="relay:delivery",
                    recipient="alpha",
                    type=EventType.MESSAGE_DELIVERED,
                    content="bound",
                    references=[
                        f"message:{parent.id}",
                        f"run:{reply_run.id}",
                        reply_admission_reference(MessageType.CHALLENGE),
                    ],
                )
            )
        store.save_model(
            Message(
                sender="alpha",
                recipient="author",
                reply_to_id=parent.id,
                run_id=reply_run.id,
                room_id=parent.room_id,
                task_id=parent.task_id,
                type=MessageType.CHALLENGE,
                content="reintroduced blocking reply",
                blocking=True,
            )
        )
        gate = SqliteCommunicationPolicyGate(
            store,
            _policy(
                edges=frozenset(
                    {
                        PolicyEdge(
                            AgentRole.PARTICIPANT,
                            AgentRole.IMPLEMENTER,
                            frozenset({MessageType.CHALLENGE}),
                        )
                    }
                )
            ),
        )
        agent = RecordingAgent("alpha")
        recovery = MessageDelivery(
            store,
            writer,
            FakeFactory({"alpha": agent}),
            policy=gate,
        )
        before = store.counts()

        with pytest.raises(
            DeliveryRefusal, match="existing delivery-bound reply.*non-blocking"
        ):
            await recovery.deliver_and_reply(
                parent.id, reply_type=MessageType.CHALLENGE
            )

        assert store.counts() == before
        assert agent.calls == 0

    async def test_completed_reply_cannot_be_retyped_under_permissive_policy(
        self, store, writer
    ):
        author_run = store.save_model(Run(agent="author", role=AgentRole.IMPLEMENTER))
        parent = store.save_model(_author_message(store, author_run.id, recipient="alpha"))
        agent = RecordingAgent("alpha")
        gate = SqliteCommunicationPolicyGate(store, _policy(edges=None))
        delivery = MessageDelivery(store, writer, FakeFactory({"alpha": agent}), policy=gate)

        first = await delivery.deliver_and_reply(parent.id, reply_type=MessageType.NOTE)
        before = store.counts()
        same_type = await delivery.deliver_and_reply(parent.id, reply_type=MessageType.NOTE)

        assert same_type.reply is not None
        assert first.reply is not None
        assert same_type.reply.id == first.reply.id
        assert store.counts() == before

        with pytest.raises(
            DeliveryRefusal,
            match="existing reply type 'note'.*requested reply type 'challenge'",
        ):
            await delivery.deliver_and_reply(parent.id, reply_type=MessageType.CHALLENGE)

        assert store.counts() == before
        assert agent.calls == 1

    async def test_bare_parent_requires_matching_authorship_provenance(self, store, writer):
        missing = store.save_model(_message(id="missing-provenance", sender="author"))
        wrong_run = store.save_model(Run(agent="other", role=AgentRole.REVIEWER))
        mismatched = store.save_model(
            _message(
                id="mismatched-provenance",
                sender="author",
                run_id=wrong_run.id,
            )
        )
        agent = RecordingAgent("alpha")
        gate = SqliteCommunicationPolicyGate(store, _policy())
        delivery = MessageDelivery(store, writer, FakeFactory({"alpha": agent}), policy=gate)

        for message in (missing, mismatched):
            before = store.counts()
            with pytest.raises(DeliveryRefusal):
                await delivery.deliver_and_reply(message.id, reply_type=MessageType.NOTE)
            assert store.counts() == before
        assert agent.calls == 0

    @pytest.mark.parametrize("status", [RunStatus.FAILED, RunStatus.RUNNING])
    async def test_exhausted_budget_does_not_block_recovery(self, store, writer, status):
        parent = store.save_model(_message(id=f"recovery-{status.value}"))
        run = store.save_model(Run(agent="alpha", role=AgentRole.PARTICIPANT, status=status))
        with store.transaction():
            writer.record(
                EventLogEntry(
                    room_id="room-1",
                    sender="relay:delivery",
                    recipient="alpha",
                    type=EventType.MESSAGE_DELIVERED,
                    content="bound",
                    references=[f"message:{parent.id}", f"run:{run.id}"],
                )
            )
        agent = RecordingAgent("alpha")
        gate = SqliteCommunicationPolicyGate(store, _policy(max_agent_turns=1))
        delivery = MessageDelivery(store, writer, FakeFactory({"alpha": agent}), policy=gate)
        before = store.counts()

        if status is RunStatus.RUNNING:
            with pytest.raises(DeliveryPendingRefusal):
                await delivery.deliver_and_reply(parent.id, reply_type=MessageType.NOTE)
        else:
            outcome = await delivery.deliver_and_reply(parent.id, reply_type=MessageType.NOTE)
            assert outcome.ask.error == "delivery run failed"
            assert outcome.reply is None

        assert store.counts() == before
        assert agent.calls == 0

    def test_turn_budget_is_exact_under_independent_connection_race(self, tmp_path):
        db_path = tmp_path / "budget-race.sqlite3"
        setup_conn = connect(db_path)
        migrate(setup_conn)
        setup_store = SqliteRelayStore(setup_conn)
        setup_store.save_model(Room(id="room-1", name="race-room"))
        messages = [
            setup_store.save_model(_message(id=f"race-{index}", recipient=f"agent-{index}"))
            for index in range(5)
        ]
        setup_conn.close()

        barrier = threading.Barrier(len(messages))

        def worker(message_id: str):
            conn = connect(db_path)
            try:
                store = SqliteRelayStore(conn)
                gate = SqliteCommunicationPolicyGate(store, _policy(max_agent_turns=2))
                agent_name = store.load_model(Message, message_id).recipient
                agent = RecordingAgent(agent_name or "missing")
                delivery = MessageDelivery(
                    store,
                    EventLogWriter(conn),
                    FakeFactory({agent.name: agent}),
                    policy=gate,
                )
                barrier.wait(timeout=10)
                try:
                    result = asyncio.run(delivery.deliver(message_id))
                    return result, agent.calls
                except Exception as exc:  # noqa: BLE001 - assertion distinguishes typed refusals
                    return exc, agent.calls
            finally:
                conn.close()

        with concurrent.futures.ThreadPoolExecutor(max_workers=len(messages)) as pool:
            results = list(pool.map(worker, [message.id for message in messages]))

        successes = [item for item, _calls in results if isinstance(item, DeliveryOutcome)]
        refusals = [item for item, _calls in results if isinstance(item, TurnBudgetExhausted)]
        assert len(successes) == 2
        assert len(refusals) == len(messages) - 2
        assert sum(calls for _result, calls in results) == 2

        check_conn = connect(db_path)
        try:
            check_store = SqliteRelayStore(check_conn)
            markers = list(
                check_store.all_models(
                    EventLogEntry,
                    "WHERE type = ? AND room_id IS ? AND task_id IS ?",
                    [EventType.MESSAGE_DELIVERED.value, "room-1", None],
                )
            )
            assert len(markers) == 2
        finally:
            check_conn.close()


class TestDriverPolicy:
    def _stack(self, store, writer, agents, roles, policy):
        resolver = StaticResolver(tuple(agents), roles)
        bus = ConversationBus(store, writer, resolver=resolver, policy=policy)
        delivery = MessageDelivery(
            store,
            writer,
            FakeFactory({name: RecordingAgent(name) for name in agents}),
            bus=bus,
            policy=policy,
        )
        driver = ConversationDriver(store, writer, bus, delivery, resolver, policy=policy)
        return driver

    async def test_chain_policy_refusal_is_zero_delta(self, store, writer):
        policy = _policy(
            edges=frozenset(
                {
                    PolicyEdge(
                        AgentRole.IMPLEMENTER,
                        AgentRole.REVIEWER,
                        frozenset({MessageType.NOTE}),
                    )
                }
            )
        )
        driver = self._stack(
            store,
            writer,
            ("implementer-agent", "critic-agent"),
            {"implementer": "implementer-agent", "critic": "critic-agent"},
            SqliteCommunicationPolicyGate(store, policy),
        )
        spec = ConversationSpec(
            conversation_key="forbidden-chain",
            room_id="room-1",
            participants=(
                ParticipantAddress(role=AgentRole.IMPLEMENTER),
                ParticipantAddress(role=AgentRole.CRITIC),
            ),
            seed_content="start",
        )
        before = store.counts()
        with pytest.raises(PolicyRefusal):
            await driver.start(spec)
        assert store.counts() == before

    async def test_driver_stops_at_turn_budget_and_reentry_converges(self, store, writer):
        policy = _policy(max_agent_turns=2, edges=None)
        gate = SqliteCommunicationPolicyGate(store, policy)
        resolver = StaticResolver(
            ("implementer-agent", "reviewer-agent", "critic-agent"),
            {
                "implementer": "implementer-agent",
                "reviewer": "reviewer-agent",
                "critic": "critic-agent",
            },
        )
        bus = ConversationBus(store, writer, resolver=resolver, policy=gate)
        agents = {
            "implementer-agent": RecordingAgent("implementer-agent"),
            "reviewer-agent": RecordingAgent("reviewer-agent"),
            "critic-agent": RecordingAgent("critic-agent"),
        }
        delivery = MessageDelivery(store, writer, FakeFactory(agents), bus=bus, policy=gate)
        driver = ConversationDriver(store, writer, bus, delivery, resolver, policy=gate)
        spec = ConversationSpec(
            conversation_key="budget-chain",
            room_id="room-1",
            participants=(
                ParticipantAddress(role=AgentRole.IMPLEMENTER),
                ParticipantAddress(role=AgentRole.REVIEWER),
                ParticipantAddress(role=AgentRole.CRITIC),
            ),
            seed_content="start",
        )

        result = await driver.start(spec)
        assert result.stop_reason is StopReason.BUDGET_EXHAUSTED
        assert isinstance(result.refusal, TurnBudgetExhausted)
        assert len(result.hops) == 2
        assert result.final_answer is not None

        # The first two deliveries ran exactly once; no third forward was
        # composed after the read-only exhaustion precheck.
        assert agents["implementer-agent"].calls == 1
        assert agents["reviewer-agent"].calls == 1
        assert agents["critic-agent"].calls == 0
        before = store.counts()
        again = await driver.start(spec)
        assert again.stop_reason is StopReason.BUDGET_EXHAUSTED
        assert isinstance(again.refusal, TurnBudgetExhausted)
        assert again.seed.id == result.seed.id
        assert tuple(h.forward.id for h in again.hops) == tuple(h.forward.id for h in result.hops)
        assert store.counts() == before
        assert agents["implementer-agent"].calls == 1
        assert agents["reviewer-agent"].calls == 1
        assert agents["critic-agent"].calls == 0

    async def test_driver_retry_is_an_additional_turn(self, store, writer):
        policy = _policy(max_agent_turns=2, edges=None)
        gate = SqliteCommunicationPolicyGate(store, policy)
        agent = RecordingAgent("participant-agent", fail_first=True)
        resolver = StaticResolver(("participant-agent",), {})
        bus = ConversationBus(store, writer, resolver=resolver, policy=gate)
        delivery = MessageDelivery(
            store,
            writer,
            FakeFactory({"participant-agent": agent}),
            bus=bus,
            policy=gate,
        )
        driver = ConversationDriver(store, writer, bus, delivery, resolver, policy=gate)
        spec = ConversationSpec(
            conversation_key="retry-budget",
            room_id="room-1",
            participants=(ParticipantAddress(agent="participant-agent"),),
            seed_content="try",
        )

        result = await driver.start(spec)
        assert result.stop_reason is StopReason.SEQUENCE_EXHAUSTED
        assert result.hops[0].attempts == 2
        assert agent.calls == 2
        marker_count = sum(entry.type is EventType.MESSAGE_DELIVERED for entry in writer.all())
        assert marker_count == 2
        again = await driver.start(spec)
        assert again.stop_reason is StopReason.SEQUENCE_EXHAUSTED
        assert tuple(h.forward.id for h in again.hops) == tuple(h.forward.id for h in result.hops)
        assert again.hops[0].attempts == 2
        assert agent.calls == 2

    async def test_blocking_seed_reentry_reuses_admitted_message(self, store, writer):
        gate = SqliteCommunicationPolicyGate(store, _policy(max_blocking_messages=1))
        resolver = StaticResolver(("participant-agent",), {})
        bus = ConversationBus(store, writer, resolver=resolver, policy=gate)
        agent = RecordingAgent("participant-agent")
        delivery = MessageDelivery(
            store,
            writer,
            FakeFactory({"participant-agent": agent}),
            bus=bus,
            policy=gate,
        )
        driver = ConversationDriver(store, writer, bus, delivery, resolver, policy=gate)
        spec = ConversationSpec(
            conversation_key="blocking-seed-reentry",
            room_id="room-1",
            participants=(ParticipantAddress(agent="participant-agent"),),
            seed_content="blocking start",
            seed_type=MessageType.CHALLENGE,
            seed_blocking=True,
        )

        result = await driver.start(spec)
        before = store.counts()
        again = await driver.start(spec)

        assert result.stop_reason is StopReason.SEQUENCE_EXHAUSTED
        assert again.stop_reason is StopReason.SEQUENCE_EXHAUSTED
        assert again.seed.id == result.seed.id
        assert again.hops[0].forward.id == result.hops[0].forward.id
        assert store.counts() == before
        assert agent.calls == 1

    def test_concurrent_blocking_seed_starts_converge(self, tmp_path):
        db_path = tmp_path / "blocking-seed-race.sqlite3"
        setup_conn = connect(db_path)
        migrate(setup_conn)
        SqliteRelayStore(setup_conn).save_model(Room(id="room-1", name="race-room"))
        setup_conn.close()

        barrier = threading.Barrier(2)

        class SeedBarrierBus(ConversationBus):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.waited = False

            def send(self, message, **kwargs):
                if not self.waited and message.sender == "relay:driver":
                    self.waited = True
                    barrier.wait(timeout=10)
                return super().send(message, **kwargs)

        def worker(_index: int):
            conn = connect(db_path)
            try:
                local_store = SqliteRelayStore(conn)
                local_writer = EventLogWriter(conn)
                gate = SqliteCommunicationPolicyGate(local_store, _policy(max_blocking_messages=1))
                resolver = StaticResolver(("participant-agent",), {})
                bus = SeedBarrierBus(local_store, local_writer, resolver=resolver, policy=gate)
                agent = RecordingAgent("participant-agent")
                delivery = MessageDelivery(
                    local_store,
                    local_writer,
                    FakeFactory({"participant-agent": agent}),
                    bus=bus,
                    policy=gate,
                )
                driver = ConversationDriver(
                    local_store,
                    local_writer,
                    bus,
                    delivery,
                    resolver,
                    policy=gate,
                )
                spec = ConversationSpec(
                    conversation_key="blocking-seed-race",
                    room_id="room-1",
                    participants=(ParticipantAddress(agent="participant-agent"),),
                    seed_content="blocking start",
                    seed_type=MessageType.CHALLENGE,
                    seed_blocking=True,
                )
                try:
                    return asyncio.run(driver.start(spec)), agent.calls
                except Exception as exc:  # noqa: BLE001 - result asserts no refusal
                    return exc, agent.calls
            finally:
                conn.close()

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(worker, range(2)))

        assert not any(isinstance(result, Exception) for result, _calls in results)
        assert len({result.seed.id for result, _calls in results}) == 1
        assert sum(calls for _result, calls in results) == 1

        check_conn = connect(db_path)
        try:
            check_store = SqliteRelayStore(check_conn)
            messages = list(check_store.all_models(Message))
            assert sum(message.blocking for message in messages) == 1
            assert len(messages) == 2  # one seed and one materialized reply
            markers = list(
                check_store.all_models(
                    EventLogEntry,
                    "WHERE type = ?",
                    [EventType.MESSAGE_DELIVERED.value],
                )
            )
            assert len(markers) == 1
        finally:
            check_conn.close()

    async def test_authoritative_retry_budget_refusal_reports_failed_attempt(self, store, writer):
        class RaceGate:
            def __init__(self):
                self.turn_checks = 0
                self.delegate = SqliteCommunicationPolicyGate(store, _policy(max_agent_turns=2))

            def check_edge(self, envelope):
                self.delegate.check_edge(envelope)

            def check_blocking_budget(self, room_id, task_id):
                self.delegate.check_blocking_budget(room_id, task_id)

            def check_turn_budget(self, room_id, task_id):
                self.turn_checks += 1
                if self.turn_checks == 3:
                    raise TurnBudgetExhausted("simulated post-compose budget race")
                self.delegate.check_turn_budget(room_id, task_id)

        gate = RaceGate()
        agent = RecordingAgent("participant-agent", fail_first=True)
        resolver = StaticResolver(("participant-agent",), {})
        bus = ConversationBus(store, writer, resolver=resolver, policy=gate)
        delivery = MessageDelivery(
            store,
            writer,
            FakeFactory({"participant-agent": agent}),
            bus=bus,
            policy=gate,
        )
        driver = ConversationDriver(store, writer, bus, delivery, resolver, policy=gate)
        spec = ConversationSpec(
            conversation_key="retry-race",
            room_id="room-1",
            participants=(ParticipantAddress(agent="participant-agent"),),
            seed_content="try",
        )

        result = await driver.start(spec)
        assert result.stop_reason is StopReason.BUDGET_EXHAUSTED
        assert isinstance(result.refusal, TurnBudgetExhausted)
        assert len(result.hops) == 1
        assert result.hops[0].forward.id == result.seed.id
        assert result.hops[0].attempts == 1
        assert agent.calls == 1
        assert len(list(store.all_models(Message))) == 2  # seed + honest undelivered retry

    async def test_blocking_seed_budget_refusal_is_zero_delta(self, store, writer):
        policy = _policy(max_blocking_messages=0, edges=None)
        gate = SqliteCommunicationPolicyGate(store, policy)
        resolver = StaticResolver(("participant-agent",), {})
        bus = ConversationBus(store, writer, resolver=resolver, policy=gate)
        agent = RecordingAgent("participant-agent")
        delivery = MessageDelivery(
            store,
            writer,
            FakeFactory({"participant-agent": agent}),
            bus=bus,
            policy=gate,
        )
        driver = ConversationDriver(store, writer, bus, delivery, resolver, policy=gate)
        spec = ConversationSpec(
            conversation_key="blocking-seed",
            room_id="room-1",
            participants=(ParticipantAddress(agent="participant-agent"),),
            seed_content="blocked start",
            seed_type=MessageType.CHALLENGE,
            seed_blocking=True,
        )
        before = store.counts()
        with pytest.raises(BlockingBudgetExhausted):
            await driver.start(spec)
        assert store.counts() == before
        assert agent.calls == 0

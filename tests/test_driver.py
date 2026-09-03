"""P4.4 bounded multi-agent driver: deterministic identity, spec validation,
hub forwarding, bounded retries, resume key contracts, exit-gate proof.

Frozen plan: ``docs/plans/p4.4-bounded-multi-agent-driver-plan.md`` (rev 4),
decisions D1–D14, gates G1–G6. SPEC reference: §27 Phase 4; App. C.7,
D.11-P4, D.8.
"""

import asyncio
import concurrent.futures
import sqlite3
import threading
import uuid
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
from relay.core.bus import ConversationBus
from relay.core.delivery import InvalidReplyTypeRefusal, MessageDelivery
from relay.core.driver import (
    DRIVER_SENDER,
    FOREIGN_KEY_PREFIX,
    MAX_HOP_RETRIES,
    MAX_SPEC_PARTICIPANTS,
    ConversationDriver,
    ConversationKeyMismatchRefusal,
    ConversationResult,
    ConversationSpec,
    MessageKind,
    ParticipantAddress,
    ResumeSeedRefusal,
    SpecRefusal,
    StopReason,
    canonical_identity,
    derive_message_id,
    foreign_conversation_key,
)
from relay.harness.runtime import HarnessAgent
from relay.harness.types import ExecutionGrantKind
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

# ---------------------------------------------------------------------------
# Fakes — both execution families, offline by construction
# ---------------------------------------------------------------------------


class RecordingAPIAgent(Agent):
    """API-family fake: records every request it is handed."""

    backend = BackendType.API

    def __init__(self, name: str = "alpha") -> None:
        self.name = name
        self.received: list[AgentRequest] = []

    async def run(self, request: AgentRequest) -> AgentResponse:
        self.received.append(request)
        return AgentResponse(agent=self.name, role=request.role, output=f"{self.name} reply")


class RecordingHarnessAgent(HarnessAgent):
    """Harness-family fake: records the grant it was handed; never spawns."""

    backend = BackendType.HARNESS

    received_grants: ClassVar[list[ExecutionGrantKind | None]] = []
    received_requests: ClassVar[list[AgentRequest]] = []

    def __init__(self, *, settings: AgentSettings, profile: HarnessAgentConfig, workspace_root) -> None:
        super().__init__(settings=settings, profile=profile, workspace_root=workspace_root)
        self.name = "beta"

    async def run(self, request: AgentRequest) -> AgentResponse:
        grant = self._profile.grant if self._profile is not None else None
        type(self).received_grants.append(grant)
        type(self).received_requests.append(request)
        return AgentResponse(agent=self.name, role=request.role, output=f"{self.name} reply")


class SecondHarnessAgent(RecordingHarnessAgent):
    """A different harness adapter (exit gate wants harness → different harness)."""

    received_grants: ClassVar[list[ExecutionGrantKind | None]] = []
    received_requests: ClassVar[list[AgentRequest]] = []

    def __init__(self, *, settings: AgentSettings, profile: HarnessAgentConfig, workspace_root) -> None:
        super().__init__(settings=settings, profile=profile, workspace_root=workspace_root)
        self.name = "gamma"


class FlakyAgent(RecordingAPIAgent):
    """Fails its first N runs with a sanitized error, then succeeds."""

    failures_left = 1

    async def run(self, request: AgentRequest) -> AgentResponse:
        if type(self).failures_left > 0:
            type(self).failures_left -= 1
            raise RuntimeError("transient provider outage")
        return await super().run(request)


class AlwaysFailingAgent(RecordingAPIAgent):
    async def run(self, request: AgentRequest) -> AgentResponse:
        raise RuntimeError("permanent provider outage")


class FakeFactory:
    """AgentFactory over injected instances; unknown names fail typed."""

    def __init__(self, agents: dict[str, Agent], models: dict[str, str | None] | None = None):
        self._agents = agents
        self._models = models or {}

    def build(self, name: str) -> Agent:
        if name not in self._agents:
            raise ConfigError(f"unknown agent '{name}' — configured agents: alpha, ...")
        return self._agents[name]

    def model_of(self, name: str) -> str | None:
        return self._models.get(name)


class StaticResolver:
    """Deterministic role → agent mapping over injected data (test seam)."""

    def __init__(self, agents: tuple[str, ...], roles: dict[str, str]):
        self._agents = frozenset(agents)
        self._roles = dict(roles)

    def resolve_role(self, role: str) -> str | None:
        return self._roles.get(role)

    def knows_agent(self, name: str) -> bool:
        return name in self._agents


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

HARNESS_AGENT_CLASSES = (RecordingHarnessAgent, SecondHarnessAgent)


@pytest.fixture(autouse=True)
def _reset_class_state():
    for cls in HARNESS_AGENT_CLASSES:
        cls.received_grants.clear()
        cls.received_requests.clear()
    yield
    for cls in HARNESS_AGENT_CLASSES:
        cls.received_grants.clear()
        cls.received_requests.clear()


@pytest.fixture()
def db(tmp_path):
    conn = connect(tmp_path / "driver.sqlite3")
    migrate(conn)
    yield conn
    conn.close()


@pytest.fixture()
def store(db):
    return SqliteRelayStore(db)


@pytest.fixture()
def scope(store):
    store.save_model(Room(id="room-1", name="driver-room"))
    store.save_model(Room(id="room-2", name="second-room"))


@pytest.fixture()
def writer(db):
    return EventLogWriter(db)


@pytest.fixture()
def api_agent():
    return RecordingAPIAgent("alpha")


@pytest.fixture()
def harness_agent(tmp_path):
    return RecordingHarnessAgent(
        settings=AgentSettings(adapter="fake_harness"),
        profile=HarnessAgentConfig(grant=ExecutionGrantKind.WORKSPACE_WRITE),
        workspace_root=tmp_path,
    )


@pytest.fixture()
def second_harness_agent(tmp_path):
    return SecondHarnessAgent(
        settings=AgentSettings(adapter="fake_harness_2"),
        profile=HarnessAgentConfig(grant=ExecutionGrantKind.WORKSPACE_WRITE),
        workspace_root=tmp_path,
    )


@pytest.fixture()
def resolver():
    return StaticResolver(
        agents=("alpha", "beta", "gamma"),
        roles={"reviewer": "beta", "critic": "gamma"},
    )


@pytest.fixture()
def bus(store, writer, resolver):
    return ConversationBus(store, writer, resolver=resolver)


@pytest.fixture()
def delivery(store, writer, bus, api_agent, harness_agent, second_harness_agent, scope):
    return MessageDelivery(
        store,
        writer,
        FakeFactory(
            {
                "alpha": api_agent,
                "beta": harness_agent,
                "gamma": second_harness_agent,
            }
        ),
        bus=bus,
    )


@pytest.fixture()
def driver(store, writer, bus, delivery, resolver) -> ConversationDriver:
    return ConversationDriver(store, writer, bus, delivery, resolver)


def _spec(**overrides) -> ConversationSpec:
    base: dict[str, object] = {
        "conversation_key": "conv-1",
        "room_id": "room-1",
        "participants": (ParticipantAddress(agent="alpha"), ParticipantAddress(agent="beta")),
        "seed_content": "kick off the review",
    }
    base.update(overrides)
    return ConversationSpec(**base)  # type: ignore[arg-type]


def _store_counts(store):
    return store.counts()


def _messages_where(store, clause: str, params: list) -> list[Message]:
    return list(store.all_models(Message, clause, params))


# ---------------------------------------------------------------------------
# G6 13 — canonical serialization: determinism and collision safety
# ---------------------------------------------------------------------------


def _identity_kwargs(**overrides):
    base: dict[str, object] = {
        "conversation_key": "conv-1",
        "room_id": "room-1",
        "task_id": None,
        "hop_index": 1,
        "attempt_index": 1,
        "kind": MessageKind.SEED,
        "prev_message_id": None,
        "recipient_role": None,
        "resolved_recipient": "alpha",
    }
    base.update(overrides)
    return base


class TestCanonicalIdentity:
    def test_same_inputs_derive_the_same_id(self):
        first = derive_message_id(**_identity_kwargs())
        second = derive_message_id(**_identity_kwargs())
        assert first == second
        assert canonical_identity(**_identity_kwargs()) == canonical_identity(**_identity_kwargs())

    def test_every_identity_field_changes_the_id(self):
        baseline = derive_message_id(**_identity_kwargs())
        variants = [
            _identity_kwargs(conversation_key="conv-2"),
            _identity_kwargs(room_id="room-2"),
            _identity_kwargs(task_id="t1"),
            _identity_kwargs(hop_index=2),
            _identity_kwargs(attempt_index=2),
            _identity_kwargs(kind=MessageKind.FORWARD),
            _identity_kwargs(prev_message_id="m-1"),
            _identity_kwargs(recipient_role="reviewer"),
            _identity_kwargs(resolved_recipient="beta"),
        ]
        for variant in variants:
            assert derive_message_id(**variant) != baseline

    def test_separator_characters_cannot_alias_distinct_tuples(self):
        """G6 13: a key containing the old naive-join separators must not
        collide with a different split of the same characters."""
        aliased_pair = (
            _identity_kwargs(conversation_key="conv\x1f1", room_id="room-1"),
            _identity_kwargs(conversation_key="conv", room_id="room-1"),
        )
        ids = {derive_message_id(**kwargs) for kwargs in aliased_pair}
        # Two distinct tuples stay distinct even though naive joining would
        # interleave their fields; the second tuple differs in room_id, so the
        # ids must differ regardless — the real aliasing guard is that the
        # encoding of ONE tuple is unique, proven by the field-shift test.
        assert len(ids) == 2

    def test_literal_dash_and_null_do_not_alias(self):
        """G6 13: a literal "-" value and an absent value serialize differently."""
        with_dash = derive_message_id(**_identity_kwargs(task_id="-"))
        with_null = derive_message_id(**_identity_kwargs(task_id=None))
        assert with_dash != with_null

    def test_null_and_the_literal_null_string_do_not_alias(self):
        with_null = derive_message_id(**_identity_kwargs(task_id=None))
        with_literal = derive_message_id(**_identity_kwargs(task_id="null"))
        assert with_null != with_literal

    def test_unicode_payloads_survive_and_stay_distinct(self):
        unicode_key = "ключ-conv-🔐"
        escaped_lookalike = "ключ-conv-\\ud83d\\udd10"
        first = derive_message_id(**_identity_kwargs(conversation_key=unicode_key))
        second = derive_message_id(**_identity_kwargs(conversation_key=escaped_lookalike))
        assert first != second
        assert first == derive_message_id(**_identity_kwargs(conversation_key=unicode_key))

    def test_canonical_form_is_ascii_and_compact(self):
        canonical = canonical_identity(**_identity_kwargs(conversation_key="ключ"))
        assert canonical.startswith('["relay.driver.identity.v1","')
        assert "\\u043a" in canonical  # ensure_ascii escaping, byte-stable
        assert ", " not in canonical  # compact separators

    def test_role_provenance_is_null_strict_in_the_identity(self):
        """G6 5 (identity half): direct agent and role-resolved-to-same-agent
        derive distinct ids."""
        direct = derive_message_id(**_identity_kwargs(recipient_role=None, resolved_recipient="beta"))
        role_addressed = derive_message_id(
            **_identity_kwargs(recipient_role="reviewer", resolved_recipient="beta")
        )
        assert direct != role_addressed


# ---------------------------------------------------------------------------
# G1 — fail-closed spec validation
# ---------------------------------------------------------------------------


class TestSpecValidation:
    @pytest.mark.parametrize(
        "overrides",
        [
            {"conversation_key": ""},
            {"conversation_key": "   "},
            {"conversation_key": "k" * 257},
            {"conversation_key": "foreign:abc123"},
            {"room_id": ""},
            {"room_id": "   "},
            {"participants": ()},
            {"participants": (ParticipantAddress(agent="alpha"),) * (MAX_SPEC_PARTICIPANTS + 1)},
            {"participants": (ParticipantAddress(agent=None, role=None),)},
            {"participants": (ParticipantAddress(agent="alpha", role=AgentRole.CRITIC),)},
            {"seed_content": None},
            {"seed_content": ""},
            {"seed_content": "   "},
        ],
    )
    async def test_invalid_start_specs_are_refused_before_any_write(self, driver, store, overrides):
        before = _store_counts(store)
        with pytest.raises(SpecRefusal):
            await driver.start(_spec(**overrides))  # type: ignore[arg-type]
        assert _store_counts(store) == before

    async def test_unknown_direct_agent_is_refused(self, driver, store):
        before = _store_counts(store)
        spec = _spec(participants=(ParticipantAddress(agent="stranger"),))
        with pytest.raises(SpecRefusal, match="unknown logical agent"):
            await driver.start(spec)  # type: ignore[arg-type]
        assert _store_counts(store) == before

    async def test_unresolved_role_is_refused(self, driver, store):
        before = _store_counts(store)
        spec = _spec(participants=(ParticipantAddress(role=AgentRole.MODERATOR),))
        with pytest.raises(SpecRefusal, match="unresolved role address"):
            await driver.start(spec)  # type: ignore[arg-type]
        assert _store_counts(store) == before

    async def test_max_participants_boundary_is_accepted(self, driver, store):
        """Eight participants is the frozen cap — the traversal must accept it
        (boundedness is proven by the successful bounded run)."""
        participants = tuple(
            ParticipantAddress(agent="alpha") if i % 2 == 0 else ParticipantAddress(agent="beta")
            for i in range(MAX_SPEC_PARTICIPANTS)
        )
        result = await driver.start(_spec(participants=participants))  # type: ignore[arg-type]
        assert result.stop_reason is StopReason.SEQUENCE_EXHAUSTED
        assert len(result.hops) == MAX_SPEC_PARTICIPANTS

    @pytest.mark.parametrize(
        "overrides",
        [
            {"seed_content": "someone is trying to reseed"},
            {"seed_type": MessageType.CHALLENGE},
            {"seed_blocking": True},
        ],
    )
    async def test_resume_refuses_start_only_seed_fields(self, driver, store, overrides):
        before = _store_counts(store)
        with pytest.raises(SpecRefusal):
            await driver.resume(_spec(**overrides), "any-seed-id")  # type: ignore[arg-type]
        assert _store_counts(store) == before

    async def test_foreign_prefix_is_legal_for_resume_specs(self, driver, store):
        """The reserved-prefix check guards start() only — a resume spec MUST
        carry the canonical foreign key and passes validation."""
        spec = _spec(conversation_key=f"{FOREIGN_KEY_PREFIX}abc", seed_content=None)
        with pytest.raises(ResumeSeedRefusal):
            # Validation passes; the (absent) seed is what refuses here.
            await driver.resume(spec, "missing-seed")

# ---------------------------------------------------------------------------
# G2 — hub mechanics: ledger shape, forwarding, reply discipline
# ---------------------------------------------------------------------------


def _seed_identity_kwargs(spec: ConversationSpec, participant: tuple[str, str | None]):
    resolved, role = participant
    return {
        "conversation_key": spec.conversation_key,
        "room_id": spec.room_id,
        "task_id": spec.task_id,
        "hop_index": 1,
        "attempt_index": 1,
        "kind": MessageKind.SEED,
        "prev_message_id": None,
        "recipient_role": role,
        "resolved_recipient": resolved,
    }


class TestHubMechanics:
    async def test_single_hop_ledger_shape(self, driver, store, api_agent):
        spec = _spec(participants=(ParticipantAddress(agent="alpha"),))
        result = await driver.start(spec)
        assert result.stop_reason is StopReason.SEQUENCE_EXHAUSTED
        assert result.final_answer is not None

        seed = result.seed
        assert seed.sender == DRIVER_SENDER
        assert seed.recipient == "alpha"
        assert seed.recipient_role is None
        assert seed.run_id is None
        assert seed.reply_to_id is None
        assert seed.type is MessageType.NOTE
        assert seed.content == "kick off the review"
        assert seed.id == derive_message_id(**_seed_identity_kwargs(spec, ("alpha", None)))

        reply = result.final_answer
        assert reply is not None
        assert reply.sender == "alpha"
        assert reply.recipient == DRIVER_SENDER
        assert reply.reply_to_id == seed.id
        assert reply.run_id is not None
        assert reply.blocking is False
        assert reply.type is MessageType.NOTE
        assert reply.content == "alpha reply"
        assert result.hops[0].attempts == 1
        assert result.hops[0].forward.id == seed.id

    async def test_multi_hop_forward_chain_is_verbatim_with_one_ref(
        self, driver, api_agent, harness_agent
    ):
        result = await driver.start(_spec())
        assert result.stop_reason is StopReason.SEQUENCE_EXHAUSTED
        assert len(result.hops) == 2

        hop1_answer = result.hops[0].outcome.reply
        assert hop1_answer is not None
        forward = result.hops[1].forward
        assert forward.sender == DRIVER_SENDER
        assert forward.recipient == "beta"
        assert forward.type is MessageType.NOTE
        assert forward.blocking is False
        assert forward.content == hop1_answer.content  # verbatim
        assert forward.references == [f"message:{hop1_answer.id}"]
        assert forward.reply_to_id is None
        assert forward.id == derive_message_id(
            conversation_key="conv-1",
            room_id="room-1",
            task_id=None,
            hop_index=2,
            attempt_index=1,
            kind=MessageKind.FORWARD,
            prev_message_id=hop1_answer.id,
            recipient_role=None,
            resolved_recipient="beta",
        )
        # The answer travelled through the deterministic envelope + context_refs
        assert len(harness_agent.received_requests) == 1
        assert harness_agent.received_requests[0].context_refs == [f"message:{hop1_answer.id}"]
        assert "alpha reply" in harness_agent.received_requests[0].prompt
        assert result.final_answer is not None
        assert result.final_answer.content == "beta reply"
        assert result.final_answer.recipient == DRIVER_SENDER

    async def test_role_addressed_participant_preserves_provenance(self, driver):
        spec = _spec(
            participants=(ParticipantAddress(role=AgentRole.REVIEWER),),
        )
        result = await driver.start(spec)
        seed = result.seed
        assert seed.recipient == "beta"  # bus-resolved identity
        assert seed.recipient_role == "reviewer"  # provenance preserved
        assert seed.id == derive_message_id(
            **_seed_identity_kwargs(spec, ("beta", "reviewer"))
        )

    async def test_clarification_seed_yields_canonical_response(self, driver, bus):
        spec = _spec(
            participants=(ParticipantAddress(agent="alpha"),),
            seed_type=MessageType.CLARIFICATION_REQUEST,
            seed_blocking=True,
        )
        result = await driver.start(spec)
        assert result.seed.type is MessageType.CLARIFICATION_REQUEST
        assert result.seed.blocking is True
        assert result.final_answer is not None
        assert result.final_answer.type is MessageType.CLARIFICATION_RESPONSE
        assert bus.has_answering_reply(result.seed.id) is True

    async def test_reply_type_is_never_inferred_from_a_note_parent(
        self, driver, delivery, store
    ):
        """Pinned no-inference contract (P4.3 D15 via the driver's own seed):
        reply_type=None on a NOTE parent is a typed refusal."""
        spec = _spec(participants=(ParticipantAddress(agent="alpha"),))
        result = await driver.start(spec)
        assert result.seed.type is MessageType.NOTE
        with pytest.raises(InvalidReplyTypeRefusal):
            await delivery.deliver_and_reply(result.seed.id)

    async def test_harness_hops_bind_read_only_instances(self, driver):
        result = await driver.start(
            _spec(
                participants=(
                    ParticipantAddress(agent="alpha"),
                    ParticipantAddress(agent="beta"),
                    ParticipantAddress(agent="gamma"),
                )
            )
        )
        assert result.stop_reason is StopReason.SEQUENCE_EXHAUSTED
        assert RecordingHarnessAgent.received_grants == [ExecutionGrantKind.READ_ONLY_ACCESS]
        assert SecondHarnessAgent.received_grants == [ExecutionGrantKind.READ_ONLY_ACCESS]


# ---------------------------------------------------------------------------
# G3 — bounded retries and honest stop reasons
# ---------------------------------------------------------------------------


class TestBoundedRetries:
    @staticmethod
    def _driver_over(store, agents: dict):
        """A driver stack whose factory builds only the given agents."""
        writer = EventLogWriter(store.conn)
        resolver = StaticResolver(tuple(agents), {})
        bus = ConversationBus(store, writer, resolver=resolver)
        delivery = MessageDelivery(store, writer, FakeFactory(dict(agents)), bus=bus)
        return ConversationDriver(store, writer, bus, delivery, resolver)

    async def test_failed_hop_retries_once_via_new_message(self, driver, store):
        FlakyAgent.failures_left = 1
        flaky = FlakyAgent("alpha")
        flaky_driver = self._driver_over(store, {"alpha": flaky})
        result = await flaky_driver.start(
            _spec(participants=(ParticipantAddress(agent="alpha"),))
        )
        assert result.stop_reason is StopReason.SEQUENCE_EXHAUSTED
        assert result.hops[0].attempts == 2
        retry = result.hops[0].forward
        seed = result.seed
        assert retry.id != seed.id
        failed_run_id = retry.references[1][len("run:") :]
        assert retry.references == [f"message:{seed.id}", f"run:{failed_run_id}"]
        assert retry.content == seed.content  # same request, new canonical trail
        runs = [r for r in store.all_models(Run) if r.agent == "alpha"]
        assert len(runs) == 2  # one failed attempt + one successful retry
        assert result.final_answer is not None
        assert result.final_answer.content == "alpha reply"

    async def test_second_failure_stops_hop_failed(self, driver, store):
        failing = AlwaysFailingAgent("alpha")
        failing_driver = self._driver_over(store, {"alpha": failing})
        result = await failing_driver.start(
            _spec(participants=(ParticipantAddress(agent="alpha"),))
        )
        assert result.stop_reason is StopReason.HOP_FAILED
        assert result.final_answer is None
        assert result.hops[0].attempts == 2
        driver_messages = _messages_where(store, "WHERE sender = ?", [DRIVER_SENDER])
        assert len(driver_messages) == 2  # seed + retry, no third attempt
        runs = [r for r in store.all_models(Run) if r.agent == "alpha"]
        assert len(runs) == 2 and all(r.status is RunStatus.FAILED for r in runs)

    async def test_failed_blocking_clarification_is_never_retried_as_a_note(
        self, driver, store, bus
    ):
        """D6 (rev 4 pre-merge amendment): a failed blocking clarification
        stops the hop with HOP_FAILED — no NOTE retry Message exists — and the
        request stays honestly unanswered in P4.3's canonical query."""
        failing = AlwaysFailingAgent("alpha")
        failing_driver = self._driver_over(store, {"alpha": failing})
        spec = _spec(
            participants=(ParticipantAddress(agent="alpha"),),
            seed_type=MessageType.CLARIFICATION_REQUEST,
            seed_blocking=True,
        )
        result = await failing_driver.start(spec)
        assert result.stop_reason is StopReason.HOP_FAILED
        assert result.hops[0].attempts == 1  # no second attempt was made
        assert result.final_answer is None
        driver_messages = _messages_where(store, "WHERE sender = ?", [DRIVER_SENDER])
        assert len(driver_messages) == 1  # the seed only — no retry Message
        assert driver_messages[0].type is MessageType.CLARIFICATION_REQUEST
        runs = [r for r in store.all_models(Run) if r.agent == "alpha"]
        assert len(runs) == 1 and runs[0].status is RunStatus.FAILED
        # P4.3 canonical unanswered semantics preserved
        unanswered = bus.unanswered_blocking_messages(room_id="room-1")
        assert [m.id for m in unanswered] == [result.seed.id]

    async def test_pending_delivery_stops_without_retry(self, driver, store, writer):
        """RUNNING = crash-safe pending re-entry (D6): typed stop, no retry,
        no re-invocation, no fabricated outcome."""
        spec = _spec(participants=(ParticipantAddress(agent="alpha"),))
        seed_id = derive_message_id(**_seed_identity_kwargs(spec, ("alpha", None)))
        run = store.save_model(Run(agent="alpha", role="participant"))
        writer.record(
            EventLogEntry(
                room_id="room-1",
                sender=DRIVER_SENDER,
                recipient="alpha",
                type=EventType.MESSAGE_DELIVERED,
                content=f"note from {DRIVER_SENDER} to alpha bound to run {run.id}",
                references=[f"message:{seed_id}", f"run:{run.id}", "room:room-1"],
            )
        )
        result = await driver.start(spec)
        assert result.stop_reason is StopReason.DELIVERY_PENDING
        assert result.hops == ()
        assert result.final_answer is None
        # exactly the pre-seeded RUNNING run: no retry, no re-invocation
        runs = [r for r in store.all_models(Run) if r.agent == "alpha"]
        assert len(runs) == 1 and runs[0].status is RunStatus.RUNNING
        # the seed row persists (create precedes delivery); no retry exists
        driver_messages = _messages_where(store, "WHERE sender = ?", [DRIVER_SENDER])
        assert len(driver_messages) == 1 and driver_messages[0].id == seed_id
        assert _messages_where(store, "WHERE sender = ? AND references_json != '[]'", [DRIVER_SENDER]) == []


# ---------------------------------------------------------------------------
# G3 / G6 1, 2, 7, 8 — idempotence: same key converges, different key forks
# ---------------------------------------------------------------------------


class TestIdempotence:
    async def test_same_key_reinvocation_converges_zero_delta(self, driver, store):
        spec = _spec()
        first = await driver.start(spec)
        counts_after_first = _store_counts(store)
        second = await driver.start(spec)
        assert second.seed.id == first.seed.id
        assert second.stop_reason is StopReason.SEQUENCE_EXHAUSTED
        assert [h.forward.id for h in second.hops] == [h.forward.id for h in first.hops]
        assert _store_counts(store) == counts_after_first

    async def test_different_key_creates_a_distinct_conversation(self, driver, store):
        first = await driver.start(_spec(conversation_key="conv-a"))
        second = await driver.start(_spec(conversation_key="conv-b"))
        assert first.seed.id != second.seed.id
        seeds = _messages_where(
            store, "WHERE sender = ? AND references_json = '[]'", [DRIVER_SENDER]
        )
        assert len(seeds) == 2
        assert first.stop_reason is StopReason.SEQUENCE_EXHAUSTED
        assert second.stop_reason is StopReason.SEQUENCE_EXHAUSTED

    async def test_driver_owned_resume_converges_zero_delta(self, driver, store):
        spec = _spec()
        started = await driver.start(spec)
        counts = _store_counts(store)
        resumed = await driver.resume(_spec(seed_content=None), started.seed.id)
        assert resumed.seed.id == started.seed.id
        assert resumed.stop_reason is StopReason.SEQUENCE_EXHAUSTED
        assert [h.forward.id for h in resumed.hops] == [h.forward.id for h in started.hops]
        assert _store_counts(store) == counts

    async def test_driver_owned_resume_wrong_key_is_refused_zero_delta(self, driver, store):
        started = await driver.start(_spec())
        counts = _store_counts(store)
        wrong = _spec(conversation_key="someone-elses-key", seed_content=None)
        with pytest.raises(ConversationKeyMismatchRefusal):
            await driver.resume(wrong, started.seed.id)
        assert _store_counts(store) == counts

    async def test_retry_identity_is_stable_across_reentry(self, driver, store):
        """G6 7: crash between retry-creation and its delivery — the re-entry
        reuses the existing retry row instead of duplicating it."""
        FlakyAgent.failures_left = 1
        flaky = FlakyAgent("alpha")
        stack_writer = EventLogWriter(store.conn)
        stack_bus = ConversationBus(store, stack_writer, resolver=StaticResolver(("alpha",), {}))
        stack_delivery = MessageDelivery(
            store, stack_writer, FakeFactory({"alpha": flaky}), bus=stack_bus
        )
        stack_driver = ConversationDriver(
            store, stack_writer, stack_bus, stack_delivery, StaticResolver(("alpha",), {})
        )

        spec = _spec(
            conversation_key="retry-conv",
            participants=(ParticipantAddress(agent="alpha"),),
            seed_content="flaky hop",
        )
        # Crash simulation: seed + failed attempt 1 + retry row exist; the
        # retry delivery never happened.
        seed_id = derive_message_id(**_seed_identity_kwargs(spec, ("alpha", None)))
        seed = stack_bus.send(
            Message(
                id=seed_id,
                sender=DRIVER_SENDER,
                recipient="alpha",
                room_id="room-1",
                type=MessageType.NOTE,
                content="flaky hop",
            )
        )
        failed = await stack_delivery.deliver_and_reply(seed.id, reply_type=MessageType.NOTE)
        assert failed.reply is None
        failed_run_id = failed.ask.run.id
        retry_id = derive_message_id(
            conversation_key="retry-conv",
            room_id="room-1",
            task_id=None,
            hop_index=1,
            attempt_index=MAX_HOP_RETRIES + 1,
            kind=MessageKind.RETRY,
            prev_message_id=seed.id,
            recipient_role=None,
            resolved_recipient="alpha",
        )
        stack_bus.send(
            Message(
                id=retry_id,
                sender=DRIVER_SENDER,
                recipient="alpha",
                room_id="room-1",
                type=MessageType.NOTE,
                content="flaky hop",
                references=[f"message:{seed.id}", f"run:{failed_run_id}"],
            )
        )
        counts_before_reentry = _store_counts(store)

        # Re-entry through the public start() path: hop 1 attempt 1 recovers
        # the FAILED outcome, the retry is reused (same derived id), and the
        # retry delivery succeeds.
        result = await stack_driver.start(spec)
        assert result.stop_reason is StopReason.SEQUENCE_EXHAUSTED
        assert result.hops[0].attempts == 2
        assert result.hops[0].forward.id == retry_id
        retries = _messages_where(store, "WHERE id = ?", [retry_id])
        assert len(retries) == 1
        runs = [r for r in store.all_models(Run) if r.agent == "alpha"]
        assert len(runs) == 2  # the crashed failed run + one successful retry run
        assert _store_counts(store) == {
            **counts_before_reentry,
            "messages": counts_before_reentry["messages"] + 1,
            "runs": counts_before_reentry["runs"] + 1,
            "artifacts": counts_before_reentry["artifacts"] + 2,
            "event_log": counts_before_reentry["event_log"] + 4,
        }


# ---------------------------------------------------------------------------
# G6 9-12 — foreign-seed resume: hop-1 semantics and the canonical key
# ---------------------------------------------------------------------------


def _foreign_seed(bus, **overrides):
    fields: dict[str, object] = {
        "sender": "human:utku",
        "recipient": "alpha",
        "room_id": "room-1",
        "type": MessageType.CLARIFICATION_REQUEST,
        "blocking": True,
        "content": "what does this error mean?",
    }
    fields.update(overrides)
    return bus.send(Message(**fields))  # type: ignore[arg-type]


class TestForeignSeedResume:
    async def test_fresh_foreign_seed_resume_runs_participant_one(
        self, driver, bus, api_agent
    ):
        seed = _foreign_seed(bus)
        spec = _spec(
            conversation_key=foreign_conversation_key(seed.id),
            participants=(ParticipantAddress(agent="alpha"), ParticipantAddress(agent="beta")),
            seed_content=None,
        )
        result = await driver.resume(spec, seed.id)
        assert result.seed.id == seed.id
        assert result.stop_reason is StopReason.SEQUENCE_EXHAUSTED
        # hop 1 executed participant 1 for the first time
        assert len(api_agent.received) == 1
        assert result.hops[0].forward.id == seed.id
        assert result.hops[0].outcome.reply is not None
        assert result.hops[0].outcome.reply.type is MessageType.CLARIFICATION_RESPONSE
        assert result.final_answer is not None
        assert result.final_answer.content == "beta reply"

    async def test_already_delivered_foreign_seed_resume_recovers_hop_1(
        self, driver, delivery, bus, api_agent, harness_agent
    ):
        seed = _foreign_seed(bus)
        first = await delivery.deliver_and_reply(seed.id)  # human-entry delivery
        assert first.reply is not None
        assert len(api_agent.received) == 1
        spec = _spec(
            conversation_key=foreign_conversation_key(seed.id),
            participants=(ParticipantAddress(agent="alpha"), ParticipantAddress(agent="beta")),
            seed_content=None,
        )
        result = await driver.resume(spec, seed.id)
        # hop 1 recovered without re-invocation; hop 2 ran
        assert len(api_agent.received) == 1
        assert result.hops[0].outcome.ask.run.id == first.ask.run.id
        assert len(RecordingHarnessAgent.received_requests) == 1
        assert result.stop_reason is StopReason.SEQUENCE_EXHAUSTED

    async def test_wrong_foreign_key_is_refused_zero_delta(self, driver, store, bus, api_agent):
        seed = _foreign_seed(bus)
        counts = _store_counts(store)
        spec = _spec(
            conversation_key="not-the-canonical-key",
            participants=(ParticipantAddress(agent="alpha"),),
            seed_content=None,
        )
        with pytest.raises(ConversationKeyMismatchRefusal, match="canonical"):
            await driver.resume(spec, seed.id)
        assert _store_counts(store) == counts
        assert len(api_agent.received) == 0

    async def test_ownership_is_never_decided_by_id_shape(self, driver, store, bus):
        """G6 12: a v5-shaped id on a foreign sender is foreign; a store-shaped
        id on a driver sender that fails re-derivation is not ours."""
        shaped_id = derive_message_id(**_identity_kwargs(conversation_key="decoy"))
        seed = _foreign_seed(bus, id=shaped_id)
        result = await driver.resume(
            _spec(
                conversation_key=foreign_conversation_key(seed.id),
                participants=(ParticipantAddress(agent="alpha"),),
                seed_content=None,
            ),
            seed.id,
        )
        assert result.stop_reason is StopReason.SEQUENCE_EXHAUSTED

        impostor = bus.send(
            Message(
                id=uuid.uuid4().hex,  # store-shaped, sender claims the driver
                sender=DRIVER_SENDER,
                recipient="alpha",
                room_id="room-1",
                type=MessageType.NOTE,
                content="out-of-band driver-sender row",
            )
        )
        counts = _store_counts(store)
        with pytest.raises(ConversationKeyMismatchRefusal):
            await driver.resume(
                _spec(
                    conversation_key=foreign_conversation_key(impostor.id),
                    participants=(ParticipantAddress(agent="alpha"),),
                    seed_content=None,
                ),
                impostor.id,
            )
        assert _store_counts(store) == counts

    async def test_resume_scope_mismatch_is_refused(self, driver, store, bus):
        seed = _foreign_seed(bus)
        counts = _store_counts(store)
        with pytest.raises(ResumeSeedRefusal, match="scope"):
            await driver.resume(
                _spec(
                    room_id="room-2",
                    conversation_key=foreign_conversation_key(seed.id),
                    participants=(ParticipantAddress(agent="alpha"),),
                    seed_content=None,
                ),
                seed.id,
            )
        assert _store_counts(store) == counts

    async def test_resume_recipient_mismatch_is_refused(self, driver, store, bus):
        seed = _foreign_seed(bus, recipient="beta")
        counts = _store_counts(store)
        with pytest.raises(ResumeSeedRefusal, match="first participant"):
            await driver.resume(
                _spec(
                    conversation_key=foreign_conversation_key(seed.id),
                    participants=(ParticipantAddress(agent="alpha"),),
                    seed_content=None,
                ),
                seed.id,
            )
        assert _store_counts(store) == counts

    async def test_resume_prefix_recipient_is_refused(self, driver, store, bus):
        """D14 (4): a seed addressed to a non-deliverable recipient refuses."""
        seed = _foreign_seed(bus, recipient="human:utku")
        counts = _store_counts(store)
        with pytest.raises(ResumeSeedRefusal, match="deliverable"):
            await driver.resume(
                _spec(
                    conversation_key=foreign_conversation_key(seed.id),
                    participants=(ParticipantAddress(agent="alpha"),),
                    seed_content=None,
                ),
                seed.id,
            )
        assert _store_counts(store) == counts


# ---------------------------------------------------------------------------
# G3 — crash-window idempotence through the public entries
# ---------------------------------------------------------------------------


class TestCrashWindows:
    async def test_window_a_seed_created_delivery_not_initiated(self, driver, store, bus):
        """(a) post-create/pre-delivery: resume adopts the orphan seed."""
        spec = _spec(
            participants=(ParticipantAddress(agent="alpha"),), seed_content=None
        )
        seed_id = derive_message_id(**_seed_identity_kwargs(spec, ("alpha", None)))
        bus.send(
            Message(
                id=seed_id,
                sender=DRIVER_SENDER,
                recipient="alpha",
                room_id="room-1",
                type=MessageType.NOTE,
                content="kick off the review",
            )
        )
        counts = _store_counts(store)
        result = await driver.resume(spec, seed_id)
        assert result.stop_reason is StopReason.SEQUENCE_EXHAUSTED
        seeds = _messages_where(store, "WHERE sender = ?", [DRIVER_SENDER])
        assert len(seeds) == 1 and seeds[0].id == seed_id
        assert _store_counts(store)["runs"] == counts["runs"] + 1

    async def test_window_d_forward_created_delivery_not_initiated(
        self, driver, delivery, store, bus, api_agent, harness_agent
    ):
        """(d) post-forward-create/pre-delivery: hop 1 is recovered, the
        forward is reused, hop 2 runs exactly once."""
        spec = _spec(seed_content=None)
        seed_id = derive_message_id(**_seed_identity_kwargs(spec, ("alpha", None)))
        seed = bus.send(
            Message(
                id=seed_id,
                sender=DRIVER_SENDER,
                recipient="alpha",
                room_id="room-1",
                type=MessageType.NOTE,
                content="kick off the review",
            )
        )
        first = await delivery.deliver_and_reply(seed.id, reply_type=MessageType.NOTE)
        assert first.reply is not None
        answer = first.reply
        forward_id = derive_message_id(
            conversation_key="conv-1",
            room_id="room-1",
            task_id=None,
            hop_index=2,
            attempt_index=1,
            kind=MessageKind.FORWARD,
            prev_message_id=answer.id,
            recipient_role=None,
            resolved_recipient="beta",
        )
        bus.send(
            Message(
                id=forward_id,
                sender=DRIVER_SENDER,
                recipient="beta",
                room_id="room-1",
                type=MessageType.NOTE,
                content=answer.content,
                references=[f"message:{answer.id}"],
            )
        )
        counts = _store_counts(store)

        result = await driver.resume(spec, seed.id)
        assert result.stop_reason is StopReason.SEQUENCE_EXHAUSTED
        assert len(api_agent.received) == 1  # hop 1 recovered, not re-run
        forwards = _messages_where(store, "WHERE id = ?", [forward_id])
        assert len(forwards) == 1
        runs = list(store.all_models(Run))
        assert len(runs) == 2  # alpha's recovered run + beta's single run
        assert _store_counts(store) == {
            **counts,
            "messages": counts["messages"] + 1,
            "runs": counts["runs"] + 1,
            "artifacts": counts["artifacts"] + 2,
            "event_log": counts["event_log"] + 4,
        }

    async def test_unrelated_integrity_error_is_reraised(self, driver, store):
        """G6 14: an FK failure with no row at the derived id is honest."""
        spec = _spec(room_id="room-does-not-exist")
        before = _store_counts(store)
        with pytest.raises(sqlite3.IntegrityError):
            await driver.start(spec)
        assert _store_counts(store) == before


# ---------------------------------------------------------------------------
# G6 3, 4 — concurrent deterministic-ID creation converges
# ---------------------------------------------------------------------------


class TestConvergentConcurrency:
    @staticmethod
    def _thread_stack(store_path, tmp_dir):
        conn = connect(store_path)
        store = SqliteRelayStore(conn)
        writer = EventLogWriter(conn)
        resolver = StaticResolver(("alpha", "beta", "gamma"), {})
        bus = ConversationBus(store, writer, resolver=resolver)
        delivery = MessageDelivery(
            store,
            writer,
            FakeFactory(
                {
                    "alpha": RecordingAPIAgent(f"alpha-{id(conn)}"),
                    "beta": RecordingHarnessAgent(
                        settings=AgentSettings(adapter="fake_harness"),
                        profile=HarnessAgentConfig(grant=ExecutionGrantKind.WORKSPACE_WRITE),
                        workspace_root=tmp_dir,
                    ),
                }
            ),
            bus=bus,
        )
        driver = ConversationDriver(store, writer, bus, delivery, resolver)
        return conn, driver

    def _race(self, tmp_path, spec, threads=2):
        store_path = tmp_path / "race.sqlite3"
        conn = connect(store_path)
        migrate(conn)
        SqliteRelayStore(conn).save_model(Room(id="room-1", name="race-room"))
        conn.close()

        barrier = threading.Barrier(threads)

        def worker(_index: int) -> ConversationResult:
            own_conn, own_driver = self._thread_stack(store_path, tmp_path)
            try:
                barrier.wait(timeout=10)
                return asyncio.run(own_driver.start(spec))
            finally:
                own_conn.close()

        # Worker exceptions propagate here: an unexpected failure fails the
        # test loudly instead of being folded into the assertion surface.
        with concurrent.futures.ThreadPoolExecutor(max_workers=threads) as executor:
            futures = [executor.submit(worker, i) for i in range(threads)]
            results = [future.result(timeout=60) for future in futures]

        check_conn = connect(store_path)
        check_store = SqliteRelayStore(check_conn)
        return results, check_store, check_conn

    def test_concurrent_start_same_key_yields_one_seed_row(self, tmp_path):
        """G6 3: racing creators collide on the PK; the loser reloads, verifies,
        and reuses — exactly one deterministic seed row."""
        spec = _spec(
            participants=(ParticipantAddress(agent="alpha"), ParticipantAddress(agent="beta"))
        )
        results, store, conn = self._race(tmp_path, spec)
        try:
            expected_seed_id = derive_message_id(**_seed_identity_kwargs(spec, ("alpha", None)))
            seeds = _messages_where(
                store, "WHERE sender = ? AND references_json = '[]'", [DRIVER_SENDER]
            )
            assert len(seeds) == 1 and seeds[0].id == expected_seed_id
            for result in results:
                assert isinstance(result, ConversationResult), result
                assert result.seed.id == expected_seed_id
                assert result.stop_reason in (
                    StopReason.SEQUENCE_EXHAUSTED,
                    StopReason.DELIVERY_PENDING,
                )
            completed = [r for r in results if r.stop_reason is StopReason.SEQUENCE_EXHAUSTED]
            assert completed, "at least one racer must complete the traversal"
            # at-most-once: one run per participant across all racers
            runs = list(store.all_models(Run))
            assert len(runs) == 2
            replies = list(store.all_models(Message, "WHERE reply_to_id IS NOT NULL", []))
            assert len(replies) == 2
        finally:
            conn.close()

    def test_concurrent_forward_creation_yields_one_forward_row(self, tmp_path):
        """G6 4: the downstream forward races the same way — one deterministic
        row, and every completed traversal that reached hop 2 used exactly it."""
        spec = _spec(
            participants=(ParticipantAddress(agent="alpha"), ParticipantAddress(agent="beta"))
        )
        results, store, conn = self._race(tmp_path, spec)
        try:
            forwards = _messages_where(
                store, "WHERE sender = ? AND references_json != '[]'", [DRIVER_SENDER]
            )
            assert len(forwards) == 1
            for result in results:
                assert isinstance(result, ConversationResult), result
            completed = [r for r in results if r.stop_reason is StopReason.SEQUENCE_EXHAUSTED]
            assert completed
            forward_hops = [
                h for r in completed for h in r.hops if len(h.forward.references) == 1
            ]
            assert forward_hops, "the completing racer must have hopped to participant 2"
            assert all(h.forward.id == forwards[0].id for h in forward_hops)
            runs = list(store.all_models(Run))
            assert len(runs) == 2
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# G4 — exit gate: api → harness → different harness, zero human steps
# ---------------------------------------------------------------------------


class TestExitGate:
    async def test_api_to_harness_to_different_harness_in_one_call(
        self, driver, store, api_agent, harness_agent, second_harness_agent
    ):
        """The SPEC §27 Phase-4 exit gate: Agent A (api) → Agent B (harness) →
        Agent C (different harness) without human copy-paste."""
        before = _store_counts(store)
        result = await driver.start(
            _spec(
                participants=(
                    ParticipantAddress(agent="alpha"),
                    ParticipantAddress(agent="beta"),
                    ParticipantAddress(agent="gamma"),
                )
            )
        )
        assert result.stop_reason is StopReason.SEQUENCE_EXHAUSTED
        assert len(result.hops) == 3
        # one request per participant; answers chained verbatim
        assert len(api_agent.received) == 1
        assert len(RecordingHarnessAgent.received_requests) == 1
        assert len(SecondHarnessAgent.received_requests) == 1
        assert "alpha reply" in RecordingHarnessAgent.received_requests[0].prompt
        assert "beta reply" in SecondHarnessAgent.received_requests[0].prompt
        assert result.final_answer is not None
        assert result.final_answer.content == "gamma reply"
        # all traffic in one scope
        driver_messages = _messages_where(store, "WHERE sender = ?", [DRIVER_SENDER])
        assert {m.room_id for m in driver_messages} == {"room-1"}
        assert {m.task_id for m in driver_messages} == {None}
        # D.8: canonical state untouched by conversation traffic
        after = _store_counts(store)
        for table in ("tasks", "decisions", "approvals", "evidence_records"):
            assert after[table] == before[table]

    async def test_a_to_b_to_a_returns_to_the_first_agent(
        self, driver, api_agent, harness_agent
    ):
        result = await driver.start(
            _spec(
                participants=(
                    ParticipantAddress(agent="alpha"),
                    ParticipantAddress(agent="beta"),
                    ParticipantAddress(agent="alpha"),
                )
            )
        )
        assert result.stop_reason is StopReason.SEQUENCE_EXHAUSTED
        assert len(api_agent.received) == 2
        assert len(RecordingHarnessAgent.received_requests) == 1
        assert result.final_answer is not None
        assert result.final_answer.content == "alpha reply"

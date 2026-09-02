"""Conversation bus core: validation matrix, atomicity, D.8 invariant (P4.1).

SPEC reference: §9, §27 Phase 4; App. D.5–D.8, D.11-P4; plan
``docs/plans/p4.1-conversation-bus-core-plan.md`` (frozen rev 2).
"""

import pytest

from relay.core.bus import (
    MESSAGE_CONTENT_CAP_CHARS,
    ConversationBus,
    MessageRejected,
    ReplyRejected,
    ReplyScopeMismatch,
    ReplySymmetryViolation,
    RoundTripLimitExceeded,
)
from relay.core.evidence import EvidenceKind
from relay.core.permissions import Action
from relay.core.state_machine import TaskState
from relay.storage.db import connect, migrate
from relay.storage.events import EventLogWriter
from relay.storage.models import (
    Approval,
    ApprovalStatus,
    Decision,
    EventType,
    EvidenceRecord,
    Message,
    MessageType,
    Room,
    Run,
    Task,
)
from relay.storage.store import SqliteEvidenceStore, SqliteRelayStore

#: P4.2 (frozen plan D1): bare logical-agent senders prove authorship via a
#: real Run. This maps agent name → seeded run id for the senders used across
#: this module; ``_message`` wires the matching ``run_id`` automatically.
_RUN_IDS: dict[str, str] = {}


def _seed_run(store, agent: str) -> str:
    """Persist one authorship run; tests addressing unusual senders call this."""
    return store.save_model(Run(agent=agent, role="reviewer")).id


@pytest.fixture(autouse=True)
def _authorship_runs(store):
    """Seed authorship runs before any test body runs (so ``counts()``
    baselines taken inside tests always include them)."""
    _RUN_IDS.clear()
    for name in ("claude", "gpt", "codex"):
        _RUN_IDS[name] = _seed_run(store, name)
    yield
    _RUN_IDS.clear()


class StaticResolver:
    """Deterministic fake resolver (the production one: relay.core.resolver)."""

    def __init__(self, roles=None, agents=()):
        self._roles = dict(roles or {})
        self._agents = frozenset(agents)

    def resolve_role(self, role: str) -> str | None:
        return self._roles.get(role)

    def knows_agent(self, name: str) -> bool:
        return name in self._agents


class ExplodingResolver(StaticResolver):
    def resolve_role(self, role: str) -> str | None:
        raise RuntimeError("ambiguous role mapping")


@pytest.fixture()
def db(tmp_path):
    conn = connect(tmp_path / "bus.sqlite3")
    migrate(conn)
    yield conn
    conn.close()


@pytest.fixture()
def store(db):
    return SqliteRelayStore(db)


@pytest.fixture()
def scope(store):
    """Real Room/Task rows — the messages table carries FK constraints."""
    store.save_model(Room(id="room-1", name="bus-room"))
    store.save_model(Room(id="room-2", name="other-room"))
    store.save_model(Task(id="t1", title="bus task"))
    store.save_model(Task(id="t2", title="other task"))


@pytest.fixture()
def writer(db):
    return EventLogWriter(db)


@pytest.fixture()
def bus(store, writer, scope):
    return ConversationBus(store, writer)


@pytest.fixture()
def resolved_bus(store, writer, scope):
    return ConversationBus(
        store,
        writer,
        StaticResolver(roles={"reviewer": "claude"}, agents={"gpt", "claude", "codex"}),
    )


def _message(**overrides) -> Message:
    base: dict[str, object] = {
        "sender": "claude",
        "recipient": "codex",
        "room_id": "room-1",
        "type": MessageType.OPINION,
        "content": "the diff misses the pagination guard",
    }
    base.update(overrides)
    sender = base["sender"]
    if "run_id" not in overrides and isinstance(sender, str) and ":" not in sender:
        # P4.2 D1: auto-wire authorship provenance for bare agent senders.
        base["run_id"] = _RUN_IDS.get(sender)
    return Message(**base)


# --------------------------------------------------------------------------
# Persistence: message + MESSAGE_SENT marker, atomically
# --------------------------------------------------------------------------


class TestSendPersistsAtomically:
    def test_send_persists_message_and_marker(self, bus, store, db):
        saved = bus.send(_message())

        loaded = store.load_model(Message, saved.id)
        assert loaded is not None and loaded.content == _message().content

        markers = [e for e in EventLogWriter(db).all() if e.type is EventType.MESSAGE_SENT]
        assert len(markers) == 1
        marker = markers[0]
        assert marker.sender == "claude" and marker.recipient == "codex"
        assert f"message:{saved.id}" in marker.references
        assert "room:room-1" in marker.references
        # D7: marker content is bounded metadata, never the payload.
        assert "pagination" not in marker.content
        assert "opinion" in marker.content

    def test_send_is_atomic_marker_failure_rolls_back_message(self, store, db, scope):
        class BadWriter:
            def record(self, entry):
                raise RuntimeError("marker write explodes")

        bus = ConversationBus(store, BadWriter())  # type: ignore[arg-type]
        with pytest.raises(RuntimeError):
            bus.send(_message())

        counts = store.counts()
        assert counts["messages"] == 0
        assert counts["event_log"] == 0

    def test_store_stays_usable_after_rolled_back_send(self, bus, store):
        with pytest.raises(MessageRejected):
            bus.send(_message(content="   "))
        saved = bus.send(_message(content="still works"))
        assert store.load_model(Message, saved.id) is not None

    def test_message_rows_are_append_only_after_send(self, bus, store):
        from relay.storage.store import ImmutableHistoryError

        saved = bus.send(_message())
        with pytest.raises(ImmutableHistoryError):
            store.update_model(saved.model_copy(update={"content": "tampered"}))
        with pytest.raises(ImmutableHistoryError):
            store.delete_model(saved)


# --------------------------------------------------------------------------
# G2 matrix — addressing (plan D3/D6)
# --------------------------------------------------------------------------


class TestAddressingValidation:
    def test_direct_addressing_persists_untouched(self, bus, store):
        saved = bus.send(_message())
        loaded = store.load_model(Message, saved.id)
        assert loaded.recipient == "codex"
        assert loaded.recipient_role is None

    def test_role_addressing_resolves_and_preserves_role(self, resolved_bus, store):
        saved = resolved_bus.send(_message(sender="gpt", recipient=None, recipient_role="reviewer"))
        loaded = store.load_model(Message, saved.id)
        assert loaded.recipient == "claude"
        assert loaded.recipient_role == "reviewer"

    def test_role_addressing_does_not_mutate_caller_message(self, resolved_bus):
        original = _message(sender="gpt", recipient=None, recipient_role="reviewer")
        resolved_bus.send(original)
        assert original.recipient is None  # caller's copy untouched

    def test_broadcast_is_denied_outright(self, bus, store):
        """P4.1 (plan D13): recipient=None broadcast fails typed before persistence."""
        before = store.counts()
        with pytest.raises(MessageRejected, match="broadcast is denied"):
            bus.send(_message(recipient=None))
        assert store.counts() == before

    def test_role_addressing_without_resolver_is_rejected(self, bus, store):
        """The only broadcast-shaped role attempt: unresolved (no resolver) —
        a persisted role-addressed message always carries a resolved recipient."""
        before = store.counts()
        with pytest.raises(MessageRejected, match="no resolver"):
            bus.send(_message(recipient=None, recipient_role="reviewer"))
        assert store.counts() == before

    def test_unresolved_role_is_rejected(self, resolved_bus, store):
        before = store.counts()
        with pytest.raises(MessageRejected, match="unresolved"):
            resolved_bus.send(_message(recipient=None, recipient_role="nonexistent"))
        assert store.counts() == before

    def test_ambiguous_role_resolution_is_rejected(self, store, writer):
        bus = ConversationBus(
            store, writer, ExplodingResolver(roles={"reviewer": "claude"}, agents={"claude"})
        )
        before = store.counts()
        with pytest.raises(MessageRejected, match="resolution failed"):
            bus.send(_message(recipient=None, recipient_role="reviewer"))
        assert store.counts() == before

    def test_prefilled_recipient_with_role_is_rejected(self, resolved_bus):
        with pytest.raises(MessageRejected, match="do not pre-fill"):
            resolved_bus.send(_message(recipient="codex", recipient_role="reviewer"))

    def test_invalid_prefix_recipient_is_rejected(self, bus):
        with pytest.raises(MessageRejected, match="human: / relay:"):
            bus.send(_message(recipient="unknown:utku"))

    def test_human_recipient_convention_is_accepted(self, bus):
        saved = bus.send(_message(recipient="human:utku"))
        assert saved.recipient == "human:utku"

    def test_relay_recipient_convention_is_accepted(self, bus):
        saved = bus.send(_message(recipient="relay:system"))
        assert saved.recipient == "relay:system"

    def test_scope_is_required(self, bus):
        with pytest.raises(MessageRejected, match="room_id and/or a task_id"):
            bus.send(_message(room_id=None))

    def test_task_only_scope_is_legal(self, bus, store):
        saved = bus.send(_message(room_id=None, task_id="t1"))
        assert store.load_model(Message, saved.id) is not None

    def test_self_send_is_denied(self, bus, store):
        """P4.1 (plan D14): sender == recipient fails typed, zero persistence."""
        before = store.counts()
        with pytest.raises(MessageRejected, match="self-send denied"):
            bus.send(_message(sender="claude", recipient="claude"))
        assert store.counts() == before

    def test_self_send_via_role_resolution_is_denied(self, store, writer):
        """D14 checked AFTER resolution: a role mapping back onto the sender."""
        bus = ConversationBus(
            store, writer, StaticResolver(roles={"reviewer": "claude"}, agents={"claude"})
        )
        before = store.counts()
        with pytest.raises(MessageRejected, match="self-send denied"):
            bus.send(_message(sender="claude", recipient=None, recipient_role="reviewer"))
        assert store.counts() == before

    def test_prefix_senders_are_exempt_from_self_send(self, bus):
        """D14 binds logical-agent senders; human:/relay: senders are not agents."""
        saved = bus.send(_message(sender="human:utku", recipient="codex"))
        assert saved.sender == "human:utku"


# --------------------------------------------------------------------------
# G2 matrix — sender identity (plan D4, Q-D)
# --------------------------------------------------------------------------


class TestSenderValidation:
    def test_human_sender_convention_is_accepted(self, bus):
        saved = bus.send(_message(sender="human:utku"))
        assert saved.sender == "human:utku"

    def test_relay_sender_convention_is_accepted(self, bus):
        saved = bus.send(_message(sender="relay:review"))
        assert saved.sender == "relay:review"

    @pytest.mark.parametrize("bad", ["", "  ", "gpt:imposter", "agent:claude"])
    def test_invalid_sender_forms_are_rejected(self, bus, bad):
        with pytest.raises(MessageRejected, match="sender"):
            bus.send(_message(sender=bad))

    def test_unknown_bare_sender_rejected_when_resolver_present(self, resolved_bus):
        with pytest.raises(MessageRejected, match="unknown logical agent"):
            resolved_bus.send(_message(sender="stranger"))

    def test_unknown_bare_sender_accepted_without_resolver(self, bus, store):
        _RUN_IDS["stranger"] = _seed_run(store, "stranger")
        saved = bus.send(_message(sender="stranger"))
        assert saved.sender == "stranger"


# --------------------------------------------------------------------------
# G2 matrix — strict authorship provenance (P4.2, plan D1)
# --------------------------------------------------------------------------


class TestAuthorshipProvenance:
    """Bare logical-agent senders prove authorship via ``run_id`` validated
    against ``run.agent == sender``; prefix senders must not carry it. Every
    rejection is typed and persists nothing."""

    def test_bare_sender_without_run_id_is_rejected(self, bus, store):
        before = store.counts()
        with pytest.raises(MessageRejected, match="requires run provenance"):
            bus.send(_message(run_id=None))
        assert store.counts() == before

    def test_prefix_sender_with_run_id_is_rejected(self, bus, store):
        before = store.counts()
        with pytest.raises(MessageRejected, match="authorship provenance is agent-only"):
            bus.send(_message(sender="human:utku", run_id="run-something"))
        assert store.counts() == before

    def test_unknown_run_id_is_rejected(self, bus, store):
        before = store.counts()
        with pytest.raises(MessageRejected, match="does not resolve to a Run"):
            bus.send(_message(run_id="no-such-run"))
        assert store.counts() == before

    def test_authorship_mismatch_is_rejected(self, bus, store):
        """A run belonging to another agent never proves this sender."""
        before = store.counts()
        with pytest.raises(MessageRejected, match="authorship mismatch"):
            bus.send(_message(sender="claude", run_id=_RUN_IDS["gpt"]))
        assert store.counts() == before

    def test_valid_linkage_persists_with_run_provenance(self, bus, store):
        saved = bus.send(_message())
        loaded = store.load_model(Message, saved.id)
        assert loaded.run_id == _RUN_IDS["claude"]
        assert store.load_model(Run, loaded.run_id).agent == "claude"

    def test_strict_authorship_applies_with_resolver_too(self, resolved_bus, store):
        before = store.counts()
        with pytest.raises(MessageRejected, match="requires run provenance"):
            resolved_bus.send(
                _message(
                    sender="gpt",
                    recipient=None,
                    recipient_role="reviewer",
                    run_id=None,
                )
            )
        assert store.counts() == before


# --------------------------------------------------------------------------
# G2 matrix — blocking legality (plan D5)
# --------------------------------------------------------------------------


class TestBlockingValidation:
    @pytest.mark.parametrize(
        "message_type",
        [
            MessageType.CLARIFICATION_REQUEST,
            MessageType.CHALLENGE,
            MessageType.PROPOSAL,
            MessageType.REVIEW_FINDING,
        ],
    )
    def test_blocking_capable_types_accept_the_flag(self, bus, message_type):
        saved = bus.send(_message(type=message_type, blocking=True))
        assert saved.blocking is True

    def test_note_can_never_block(self, bus):
        with pytest.raises(MessageRejected, match="cannot carry blocking"):
            bus.send(_message(type=MessageType.NOTE, blocking=True))

    @pytest.mark.parametrize(
        "message_type",
        [
            MessageType.OPINION,
            MessageType.REBUTTAL,
            MessageType.FINAL_POSITION,
            MessageType.SYNTHESIS,
        ],
    )
    def test_other_types_reject_the_flag(self, bus, message_type):
        with pytest.raises(MessageRejected, match="cannot carry blocking"):
            bus.send(_message(type=message_type, blocking=True))

    def test_blocking_metadata_is_carried_on_the_marker(self, bus, db):
        bus.send(_message(type=MessageType.CHALLENGE, blocking=True))
        markers = [e for e in EventLogWriter(db).all() if e.type is EventType.MESSAGE_SENT]
        assert markers[-1].content.endswith("(blocking)")


# --------------------------------------------------------------------------
# G2 matrix — SYSTEM reservation (plan D15)
# --------------------------------------------------------------------------


class TestSystemReservation:
    def test_agent_sender_cannot_author_system_messages(self, bus, store):
        before = store.counts()
        with pytest.raises(MessageRejected, match="reserved"):
            bus.send(_message(sender="claude", recipient="codex", type=MessageType.SYSTEM))
        assert store.counts() == before

    def test_human_sender_cannot_author_system_messages(self, bus):
        with pytest.raises(MessageRejected, match="reserved"):
            bus.send(_message(sender="human:utku", recipient="codex", type=MessageType.SYSTEM))

    def test_relay_sender_may_author_system_messages(self, bus, store, db):
        saved = bus.send(_message(sender="relay:core", recipient="codex", type=MessageType.SYSTEM))
        loaded = store.load_model(Message, saved.id)
        assert loaded.type is MessageType.SYSTEM
        markers = [e for e in EventLogWriter(db).all() if e.type is EventType.MESSAGE_SENT]
        assert markers[-1].sender == "relay:core"
        assert markers[-1].content.startswith("system from relay:core")

    def test_reservation_is_one_directional(self, bus):
        """D15 binds SYSTEM to relay:*; relay:* is not confined to SYSTEM."""
        saved = bus.send(_message(sender="relay:review", recipient="codex"))
        assert saved.type is MessageType.OPINION


# --------------------------------------------------------------------------
# G2 matrix — content bound (plan D12) and references shape
# --------------------------------------------------------------------------


class TestContentBoundAndReferences:
    def test_exactly_at_cap_is_accepted(self, bus):
        saved = bus.send(_message(content="x" * MESSAGE_CONTENT_CAP_CHARS))
        assert len(saved.content) == MESSAGE_CONTENT_CAP_CHARS

    def test_cap_plus_one_is_rejected_nothing_persisted(self, bus, store):
        before = store.counts()
        with pytest.raises(MessageRejected, match="character bound"):
            bus.send(_message(content="x" * (MESSAGE_CONTENT_CAP_CHARS + 1)))
        assert store.counts() == before

    @pytest.mark.parametrize("bad", ["", "   \n\t "])
    def test_empty_content_is_rejected(self, bus, bad):
        with pytest.raises(MessageRejected, match="must not be empty"):
            bus.send(_message(content=bad))

    @pytest.mark.parametrize("refs", [[""], [" ", "plan:1"]])
    def test_malformed_references_are_rejected(self, bus, refs):
        with pytest.raises(MessageRejected, match="references"):
            bus.send(_message(references=refs))


# --------------------------------------------------------------------------
# Read primitives
# --------------------------------------------------------------------------


class TestReadPrimitives:
    def test_messages_for_task_and_room_are_chronological(self, bus, store):
        first = bus.send(_message(task_id="t1", content="first"))
        second = bus.send(_message(task_id="t1", content="second"))

        by_task = bus.messages_for_task("t1")
        assert [m.id for m in by_task] == [first.id, second.id]
        assert by_task == bus.messages_for_room("room-1")

    def test_scoping_excludes_other_rooms_and_tasks(self, bus):
        mine = bus.send(_message(task_id="t1", content="mine"))
        theirs = bus.send(_message(task_id="t2", room_id=None, content="theirs"))
        elsewhere = bus.send(_message(room_id="room-2", task_id=None, content="elsewhere"))

        assert [m.id for m in bus.messages_for_task("t1")] == [mine.id]
        assert [m.id for m in bus.messages_for_task("t2")] == [theirs.id]
        assert [m.id for m in bus.messages_for_room("room-2")] == [elsewhere.id]
        assert all(m.id != theirs.id for m in bus.messages_for_room("room-1"))


# --------------------------------------------------------------------------
# D.8 invariant — conversation is not state (plan D9, behavioral half)
# --------------------------------------------------------------------------


class TestConversationIsNotState:
    @pytest.fixture()
    def seeded_state(self, store):
        task = store.save_model(Task(title="multi-agent task", state=TaskState.IMPLEMENTING))
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

    def test_traffic_leaves_canonical_state_byte_identical(self, resolved_bus, store, seeded_state):
        baseline = seeded_state["counts"]

        resolved_bus.send(
            _message(
                task_id=seeded_state["task"].id,
                type=MessageType.CHALLENGE,
                blocking=True,
            )
        )
        resolved_bus.send(_message(recipient="codex", type=MessageType.NOTE))
        resolved_bus.send(_message(sender="human:utku", recipient="codex"))
        resolved_bus.send(_message(sender="relay:review", recipient="codex"))

        after = store.counts()
        assert after["messages"] == baseline["messages"] + 4
        assert after["event_log"] == baseline["event_log"] + 4
        for table in (
            "tasks",
            "evidence_records",
            "approvals",
            "decisions",
            "runs",
            "tool_runs",
            "artifacts",
            "rooms",
            "workspaces",
        ):
            assert after[table] == baseline[table], table

        assert store.load_model(Task, seeded_state["task"].id).state is TaskState.IMPLEMENTING
        assert (
            store.load_model(Approval, seeded_state["approval"].id).status is ApprovalStatus.PENDING
        )
        records = SqliteEvidenceStore(store).records_for_task(seeded_state["task"].id)
        assert records == seeded_state["evidence"]


# --------------------------------------------------------------------------
# P4.3: Reply pairing & validation matrix (SPEC App. D.5, D.11-P4; plan D3-D5, D9, D12)
# --------------------------------------------------------------------------


class TestReplyValidation:
    def test_reply_to_nonexistent_parent_rejected(self, bus, store):
        before = store.counts()
        with pytest.raises(ReplyRejected, match="parent message 'nonexistent' not found"):
            bus.send(_message(reply_to_id="nonexistent"))
        assert store.counts() == before

    def test_reply_exact_scope_equality(self, bus):
        parent = bus.send(_message(room_id="room-1", task_id="t1"))

        # Mismatched room
        with pytest.raises(ReplyScopeMismatch, match="must exactly match parent scope"):
            bus.send(
                _message(
                    sender="codex",
                    recipient="claude",
                    reply_to_id=parent.id,
                    room_id="room-2",
                    task_id="t1",
                )
            )

        # Removed task
        with pytest.raises(ReplyScopeMismatch, match="must exactly match parent scope"):
            bus.send(
                _message(
                    sender="codex",
                    recipient="claude",
                    reply_to_id=parent.id,
                    room_id="room-1",
                    task_id=None,
                )
            )

        # Added task to room-only parent
        room_parent = bus.send(_message(room_id="room-1", task_id=None))
        with pytest.raises(ReplyScopeMismatch, match="must exactly match parent scope"):
            bus.send(
                _message(
                    sender="codex",
                    recipient="claude",
                    reply_to_id=room_parent.id,
                    room_id="room-1",
                    task_id="t1",
                )
            )

    def test_reply_addressing_symmetry(self, bus):
        parent = bus.send(_message(sender="claude", recipient="codex"))

        # Recipient does not match parent sender
        with pytest.raises(
            ReplySymmetryViolation, match="reply recipient 'gpt' must match parent sender 'claude'"
        ):
            bus.send(
                _message(
                    sender="codex",
                    recipient="gpt",
                    reply_to_id=parent.id,
                )
            )

        # Sender does not match parent recipient
        with pytest.raises(
            ReplySymmetryViolation, match="reply sender 'gpt' must match parent recipient 'codex'"
        ):
            bus.send(
                _message(
                    sender="gpt",
                    recipient="claude",
                    reply_to_id=parent.id,
                )
            )

    def test_reply_to_human_sender_accepted(self, bus, store):
        parent = bus.send(_message(sender="human:utku", recipient="claude"))
        reply = bus.send(
            _message(
                sender="claude",
                recipient="human:utku",
                reply_to_id=parent.id,
                content="here is the answer",
            )
        )
        loaded = store.load_model(Message, reply.id)
        assert loaded.reply_to_id == parent.id
        assert loaded.recipient == "human:utku"
        assert loaded.sender == "claude"

    def test_reply_to_relay_sender_accepted(self, bus, store):
        parent = bus.send(_message(sender="relay:build", recipient="claude"))
        reply = bus.send(
            _message(
                sender="claude",
                recipient="relay:build",
                reply_to_id=parent.id,
                content="acknowledged build request",
            )
        )
        loaded = store.load_model(Message, reply.id)
        assert loaded.reply_to_id == parent.id
        assert loaded.recipient == "relay:build"

    def test_structural_reply_linkage_accepts_valid_scope(self, bus):
        parent = bus.send(_message(sender="claude", recipient="codex"))
        reply = bus.send(
            _message(
                sender="codex",
                recipient="claude",
                reply_to_id=parent.id,
                type=MessageType.CHALLENGE,
            )
        )
        assert reply.reply_to_id == parent.id
        assert bus.replies_for(parent.id) == [reply]

    def test_thread_depth_boundaries(self, bus):
        m0 = bus.send(_message(sender="claude", recipient="codex"))
        m1 = bus.send(
            _message(sender="codex", recipient="claude", reply_to_id=m0.id), max_thread_depth=3
        )
        m2 = bus.send(
            _message(sender="claude", recipient="codex", reply_to_id=m1.id), max_thread_depth=3
        )
        m3 = bus.send(
            _message(sender="codex", recipient="claude", reply_to_id=m2.id), max_thread_depth=3
        )
        assert m3.reply_to_id == m2.id

        with pytest.raises(
            RoundTripLimitExceeded, match="exceeding the maximum thread depth ceiling"
        ):
            bus.send(
                _message(sender="claude", recipient="codex", reply_to_id=m3.id), max_thread_depth=3
            )

    def test_write_path_cycle_detection(self, bus, store):
        # Insert a raw message row that references itself (UPDATE is blocked by append-only trigger)
        store.conn.execute(
            "INSERT INTO messages (id, sender, recipient, room_id, type, content, reply_to_id, created_at) "
            "VALUES ('cycle-msg', 'claude', 'codex', 'room-1', 'opinion', 'self-ref', 'cycle-msg', '2026-01-01T00:00:00+00:00')"
        )
        with pytest.raises(ReplyRejected, match="corrupt/cyclic reply ancestry detected"):
            bus.send(_message(sender="codex", recipient="claude", room_id="room-1", reply_to_id="cycle-msg"))


# --------------------------------------------------------------------------
# P4.3: Answering queries & reply chains
# --------------------------------------------------------------------------


class TestAnsweringQueries:
    def test_non_answering_reply_does_not_clear_blocking(self, bus):
        parent = bus.send(
            _message(
                sender="claude",
                recipient="codex",
                type=MessageType.CLARIFICATION_REQUEST,
                blocking=True,
            )
        )
        assert bus.has_answering_reply(parent.id) is False
        assert parent in bus.unanswered_blocking_messages(room_id="room-1")

        # Child is a CHALLENGE (valid structural reply, but NOT answering)
        bus.send(
            _message(
                sender="codex",
                recipient="claude",
                type=MessageType.CHALLENGE,
                reply_to_id=parent.id,
            )
        )
        assert bus.has_answering_reply(parent.id) is False
        assert parent in bus.unanswered_blocking_messages(room_id="room-1")

    def test_canonical_answering_reply_clears_blocking(self, bus):
        parent = bus.send(
            _message(
                sender="claude",
                recipient="codex",
                type=MessageType.CLARIFICATION_REQUEST,
                blocking=True,
            )
        )
        # Child is CLARIFICATION_RESPONSE
        bus.send(
            _message(
                sender="codex",
                recipient="claude",
                type=MessageType.CLARIFICATION_RESPONSE,
                reply_to_id=parent.id,
            )
        )
        assert bus.has_answering_reply(parent.id) is True
        assert parent not in bus.unanswered_blocking_messages(room_id="room-1")

    def test_reply_chain_traversal_and_cycle_guard(self, bus, store):
        m0 = bus.send(_message(sender="claude", recipient="codex"))
        m1 = bus.send(_message(sender="codex", recipient="claude", reply_to_id=m0.id))
        m2 = bus.send(_message(sender="claude", recipient="codex", reply_to_id=m1.id))

        chain = bus.reply_chain(m2.id)
        assert [m.id for m in chain] == [m0.id, m1.id, m2.id]

        # Insert a message with broken non-existent parent
        store.conn.execute(
            "INSERT INTO messages (id, sender, recipient, type, content, reply_to_id, created_at) "
            "VALUES ('orphan', 'codex', 'claude', 'opinion', 'orphan', 'nonexistent', '2026-01-01T00:00:00+00:00')"
        )
        store.conn.execute(
            "INSERT INTO messages (id, sender, recipient, type, content, reply_to_id, created_at) "
            "VALUES ('child-of-orphan', 'claude', 'codex', 'opinion', 'child', 'orphan', '2026-01-01T00:00:00+00:00')"
        )
        partial_chain = bus.reply_chain("child-of-orphan")
        assert [m.id for m in partial_chain] == ["orphan", "child-of-orphan"]

    def test_reply_chain_genuine_two_node_cycle_terminates_safely(self, bus, store):
        # Create raw A <-> B cycle in SQLite
        store.conn.execute(
            "INSERT INTO messages (id, sender, recipient, room_id, type, content, reply_to_id, created_at) "
            "VALUES ('cycle-node-a', 'claude', 'codex', 'room-1', 'opinion', 'a', 'cycle-node-b', '2026-01-01T00:00:00+00:00')"
        )
        store.conn.execute(
            "INSERT INTO messages (id, sender, recipient, room_id, type, content, reply_to_id, created_at) "
            "VALUES ('cycle-node-b', 'codex', 'claude', 'room-1', 'opinion', 'b', 'cycle-node-a', '2026-01-01T00:00:01+00:00')"
        )

        chain_a = bus.reply_chain("cycle-node-a")
        assert len(chain_a) == 2
        assert [m.id for m in chain_a] == ["cycle-node-b", "cycle-node-a"]

        chain_b = bus.reply_chain("cycle-node-b")
        assert len(chain_b) == 2
        assert [m.id for m in chain_b] == ["cycle-node-a", "cycle-node-b"]

    def test_preflight_depth_on_genuine_two_node_cycle_raises_reply_rejected(self, bus, store):
        store.conn.execute(
            "INSERT INTO messages (id, sender, recipient, room_id, type, content, reply_to_id, created_at) "
            "VALUES ('cycle-x', 'claude', 'codex', 'room-1', 'opinion', 'x', 'cycle-y', '2026-01-01T00:00:00+00:00')"
        )
        store.conn.execute(
            "INSERT INTO messages (id, sender, recipient, room_id, type, content, reply_to_id, created_at) "
            "VALUES ('cycle-y', 'codex', 'claude', 'room-1', 'opinion', 'y', 'cycle-x', '2026-01-01T00:00:01+00:00')"
        )

        parent = store.load_model(Message, "cycle-x")
        with pytest.raises(ReplyRejected, match="corrupt/cyclic reply ancestry detected"):
            bus.preflight_reply_depth(parent)

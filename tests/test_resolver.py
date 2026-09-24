"""P4.2: config-backed role resolution (frozen plan D3/D4/D5).

The production ``RoleResolver`` discharges the P4.1 D6 seam: role-addressed
bus traffic resolves through parsed ``relay.yaml`` data; core stays
registry-free.
"""

import pytest

from relay.context.config import AgentConfig, BackendType, RelayConfig
from relay.core.bus import ConversationBus
from relay.core.resolver import role_resolver_from_config
from relay.storage.db import connect, migrate
from relay.storage.events import EventLogWriter
from relay.storage.models import Message, MessageType, Room, Run
from relay.storage.store import SqliteRelayStore


def _config(**overrides) -> RelayConfig:
    base: dict[str, object] = {
        "agents": {
            "gpt": AgentConfig(backend=BackendType.API, adapter="openai"),
            "claude": AgentConfig(backend=BackendType.HARNESS, adapter="claude_code"),
        },
        "roles": {"planner": "gpt", "reviewer": "claude"},
    }
    base.update(overrides)
    return RelayConfig(**base)


class TestFromConfig:

    def test_reviewer_selector_is_not_consulted(self):
        """Frozen plan D4: the P3.3 build-flow selector never leaks into the
        bus role vocabulary."""
        config = _config(reviewer="gpt")
        resolver = role_resolver_from_config(config)
        assert resolver.resolve_role("reviewer") == "claude"


class TestBusIntegration:
    """The production resolver wired into the bus resolves role addresses."""

    @pytest.fixture()
    def db(self, tmp_path):
        conn = connect(tmp_path / "resolver.sqlite3")
        migrate(conn)
        yield conn
        conn.close()

    @pytest.fixture()
    def store(self, db):
        return SqliteRelayStore(db)

    def test_role_addressed_send_resolves_through_config(self, store, db):
        store.save_model(Room(id="room-1", name="resolver-room"))
        writer = EventLogWriter(db)
        run_id = store.save_model(Run(agent="gpt", role="planner")).id
        bus = ConversationBus(store, writer, role_resolver_from_config(_config()))

        saved = bus.send(
            Message(
                sender="gpt",
                recipient=None,
                recipient_role="reviewer",
                room_id="room-1",
                type=MessageType.CLARIFICATION_REQUEST,
                content="which schema interpretation governs?",
                run_id=run_id,
            )
        )

        loaded = store.load_model(Message, saved.id)
        assert loaded.recipient == "claude"
        assert loaded.recipient_role == "reviewer"

    def test_unconfigured_role_address_is_rejected(self, store, db):
        store.save_model(Room(id="room-1", name="resolver-room"))
        writer = EventLogWriter(db)
        run_id = store.save_model(Run(agent="gpt", role="planner")).id
        bus = ConversationBus(store, writer, role_resolver_from_config(_config()))

        from relay.core.bus import MessageRejected

        before = store.counts()
        with pytest.raises(MessageRejected, match="unresolved role address"):
            bus.send(
                Message(
                    sender="gpt",
                    recipient=None,
                    recipient_role="moderator",
                    room_id="room-1",
                    type=MessageType.OPINION,
                    content="nobody is bound to this role",
                    run_id=run_id,
                )
            )
        assert store.counts() == before

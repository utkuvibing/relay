"""Live P4.4 driver smoke — doubly gated, CI-inert by construction.

Gate 1: explicit env opt-in ``RELAY_LIVE_DRIVER_SMOKE=1``.
Gate 2: environment readiness — the checked-in ``relay.yaml`` still configures
the ``deepseek`` (api) + ``codex`` (harness) pair, the ``codex`` executable is
discoverable on PATH, and the DeepSeek key is present in the environment
(load it explicitly, e.g. ``uv run --env-file .env pytest``).

When enabled it drives the REAL Phase-4 exit-gate shape over the production
composition root: ONE ``ConversationDriver.start`` call chains
api (deepseek) → harness (codex) through the conversation bus and the
family-blind delivery spine with zero human copy-paste. Ledger assertions
mirror the offline exit-gate test; the harness owns its authentication
(App. B.3/C.4 — ``codex login`` once, beforehand).
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from relay.agents.factory import RegistryAgentFactory
from relay.context.config import load_config
from relay.core.bus import ConversationBus
from relay.core.delivery import MessageDelivery
from relay.core.driver import (
    DRIVER_SENDER,
    ConversationDriver,
    ConversationSpec,
    ParticipantAddress,
    StopReason,
)
from relay.core.resolver import role_resolver_from_config
from relay.storage.db import connect, migrate
from relay.storage.events import EventLogWriter
from relay.storage.models import Room, Run, RunStatus
from relay.storage.store import SqliteRelayStore

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _live_stack(tmp_path: Path) -> ConversationDriver | None:
    """Return a production driver stack when BOTH gates pass; else None."""
    if "RELAY_LIVE_DRIVER_SMOKE" not in os.environ:
        return None
    config = load_config(_REPO_ROOT)
    names = set(config.agents)
    if not {"deepseek", "codex"} <= names:
        return None
    if shutil.which("codex") is None:
        return None
    if not os.environ.get("DEEPSEEK_API_KEY"):
        return None

    conn = connect(tmp_path / "driver-live.sqlite3")
    migrate(conn)
    store = SqliteRelayStore(conn)
    writer = EventLogWriter(conn)
    resolver = role_resolver_from_config(config)
    bus = ConversationBus(store, writer, resolver=resolver)
    delivery = MessageDelivery(
        store, writer, RegistryAgentFactory(config, workspace_root=tmp_path), bus=bus
    )
    return ConversationDriver(store, writer, bus, delivery, resolver)


async def test_live_driver_chains_api_to_harness_without_human_steps(tmp_path):
    driver = _live_stack(tmp_path)
    if driver is None:
        pytest.skip("RELAY_LIVE_DRIVER_SMOKE=1 + deepseek key + codex on PATH required")

    store = driver._store  # the smoke asserts on the driver's own ledger
    store.save_model(Room(id="live-room", name="driver-live"))

    spec = ConversationSpec(
        conversation_key="live-driver-smoke",
        room_id="live-room",
        participants=(
            ParticipantAddress(agent="deepseek"),
            ParticipantAddress(agent="codex"),
        ),
        seed_content="Reply with exactly one short sentence naming the phase you are in.",
    )
    result = await driver.start(spec)

    assert result.stop_reason is StopReason.SEQUENCE_EXHAUSTED
    assert len(result.hops) == 2
    seed = result.seed
    assert seed.sender == DRIVER_SENDER
    assert seed.recipient == "deepseek"
    assert result.hops[0].outcome.reply is not None
    assert result.hops[0].outcome.ask.run.status is RunStatus.SUCCEEDED
    assert result.hops[1].outcome.ask.run.status is RunStatus.SUCCEEDED
    # deepseek's answer was forwarded verbatim into codex's envelope
    forward = result.hops[1].forward
    answer = result.hops[0].outcome.reply
    assert forward.content == answer.content
    assert forward.references == [f"message:{answer.id}"]
    assert result.final_answer is not None
    assert result.final_answer.content  # codex produced a non-empty answer
    # zero human steps: one driver call produced the whole chain
    runs = list(store.all_models(Run))
    assert len(runs) == 2

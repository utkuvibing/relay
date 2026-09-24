"""Agent contract: provider-agnostic, role-aware (SPEC §7/§8, App. B.2).

Seam-proof extensions (Phase 1, requirement 6): the interface must admit a
future harness adapter (Codex CLI, Claude Code) with zero core-domain-model
edits — usage is fully optional, serialization carries no transport fields,
and ``backend`` is the only declaration an adapter family makes.
"""

import asyncio

from relay.agents.base import (
    Agent,
    AgentRequest,
    AgentResponse,
    AgentRole,
    BackendType,
)


class FakeHarnessAgent(Agent):
    """The future Codex CLI / Claude Code adapter, proven against today's seam.

    Pure-Python canned response: no HTTP, no subprocess, no session code.
    It declares its family with one ClassVar and nothing else changes.
    """

    name = "fake_harness"
    backend = BackendType.HARNESS

    async def run(self, request: AgentRequest) -> AgentResponse:
        return AgentResponse(
            agent=self.name,
            role=request.role,
            output=f"harness handled: {request.prompt}",
            usage=None,  # subscription-backed runs carry no usage data
        )


class TestHarnessOfflinePersistPath:
    """App. B.2/B.4 proof: a future Codex/Claude Code adapter runs through the
    Phase 1 orchestrator + SQLite store end-to-end, with zero core-domain-
    model edits — same code path an API adapter uses."""

    def test_fake_harness_run_persists_end_to_end(self, tmp_path):
        from relay.core.orchestrator import run_ask
        from relay.storage import connect, migrate
        from relay.storage.events import EventLogWriter
        from relay.storage.models import ArtifactKind, EventType, RunStatus
        from relay.storage.store import SqliteRelayStore

        conn = connect(tmp_path / "relay.sqlite3")
        migrate(conn)
        try:
            store = SqliteRelayStore(conn)
            writer = EventLogWriter(conn)
            outcome = asyncio.run(
                run_ask(
                    store,
                    writer,
                    FakeHarnessAgent(),
                    AgentRequest(prompt="implement the module", role=AgentRole.IMPLEMENTER),
                )
            )

            run = outcome.run
            assert run.status is RunStatus.SUCCEEDED
            assert run.agent == "fake_harness"
            # Harness runs carry no usage data; cost stays None (App. B.2).
            assert run.input_size is None and run.output_size is None
            assert run.cost_usd is None

            artifacts = store.artifacts_for_run(run.id)
            by_kind = {artifact.kind: artifact for artifact in artifacts}
            assert set(by_kind) == {ArtifactKind.RUN_INPUT, ArtifactKind.RUN_OUTPUT}
            assert by_kind[ArtifactKind.RUN_INPUT].content == "implement the module"
            assert by_kind[ArtifactKind.RUN_OUTPUT].content == (
                "harness handled: implement the module"
            )

            events = writer.all()
            assert [event.type for event in events] == [
                EventType.AGENT_RUN_STARTED,
                EventType.AGENT_RUN_FINISHED,
            ]
        finally:
            conn.close()

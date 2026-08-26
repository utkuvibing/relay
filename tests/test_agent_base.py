"""Agent contract: provider-agnostic, role-aware (SPEC §7/§8, App. B.2).

Seam-proof extensions (Phase 1, requirement 6): the interface must admit a
future harness adapter (Codex CLI, Claude Code) with zero core-domain-model
edits — usage is fully optional, serialization carries no transport fields,
and ``backend`` is the only declaration an adapter family makes.
"""

import asyncio

import pytest
from pydantic import ValidationError

from relay.agents.base import (
    Agent,
    AgentRequest,
    AgentResponse,
    AgentRole,
    BackendType,
    TokenUsage,
)


class EchoAgent(Agent):
    name = "echo"

    async def run(self, request: AgentRequest) -> AgentResponse:
        return AgentResponse(
            agent=self.name,
            role=request.role,
            output=f"echo:{request.prompt}",
            usage=TokenUsage(input_tokens=10, output_tokens=5),
        )


class TestRolesAreDecoupledFromModels:
    """SPEC §8: 'Claude = reviewer' is wrong; any model can act in any role."""

    def test_same_adapter_any_role(self):
        agent = EchoAgent()

        async def scenario():
            outputs = []
            for role in (AgentRole.ARCHITECT, AgentRole.REVIEWER, AgentRole.CRITIC):
                response = await agent.run(AgentRequest(prompt="x", role=role))
                outputs.append((response.agent, response.role))
            return outputs

        results = asyncio.run(scenario())
        assert all(agent_name == "echo" for agent_name, _ in results)
        assert [role for _, role in results] == [
            AgentRole.ARCHITECT,
            AgentRole.REVIEWER,
            AgentRole.CRITIC,
        ]


class TestRequestValidation:
    def test_prompt_and_role_required(self):
        with pytest.raises(ValidationError):
            AgentRequest()  # type: ignore[call-arg]

    def test_context_refs_default_empty(self):
        request = AgentRequest(prompt="analyze", role=AgentRole.RESEARCHER)
        assert request.context_refs == []
        assert request.task_id is None


class TestResponseContract:
    def test_response_roundtrips_through_json(self):
        response = AgentResponse(
            agent="claude",
            role=AgentRole.MODERATOR,
            output="synthesis...",
            artifact_refs=["artifact:123"],
            usage=TokenUsage(input_tokens=100, output_tokens=50, cost_usd=0.01),
        )
        restored = AgentResponse.model_validate_json(response.model_dump_json())
        assert restored == response

    def test_error_status_carries_error_text(self):
        response = AgentResponse(
            agent="gpt",
            role=AgentRole.PLANNER,
            output="",
            status="error",
            error="provider timeout",
        )
        assert response.status == "error"


class TestTokenUsageOptionality:
    """App. B.2: usage/cost are optional; harness runs may carry none."""

    def test_token_usage_all_fields_optional(self):
        usage = TokenUsage()
        assert usage.input_tokens is None
        assert usage.output_tokens is None
        assert usage.cost_usd is None

    def test_response_with_none_usage_flows_cleanly(self):
        response = AgentResponse(agent="codex", role=AgentRole.IMPLEMENTER, output="done")
        assert response.usage is None
        restored = AgentResponse.model_validate_json(response.model_dump_json())
        assert restored == response

    def test_cost_none_survives_serialization(self):
        usage = TokenUsage(input_tokens=10, output_tokens=20, cost_usd=None)
        restored = TokenUsage.model_validate_json(usage.model_dump_json())
        assert restored.cost_usd is None

    def test_request_response_serialize_without_transport_fields(self):
        request = AgentRequest(prompt="p", role=AgentRole.RESEARCHER)
        response = AgentResponse(agent="claude", role=AgentRole.RESEARCHER, output="o")
        for dump in (request.model_dump(), response.model_dump()):
            assert not ({"api_key", "url", "headers", "auth", "token"} & set(dump))


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


class TestHarnessFamilyDeclaration:
    """One ClassVar declares the family; the interface stays identical."""

    def test_harness_backend_declared_without_interface_changes(self):
        assert FakeHarnessAgent.backend is BackendType.HARNESS
        assert Agent.backend is BackendType.API  # default unchanged

    def test_fake_harness_answers_offline(self):
        async def scenario():
            response = await FakeHarnessAgent().run(
                AgentRequest(prompt="analyze", role=AgentRole.RESEARCHER)
            )
            return response

        response = asyncio.run(scenario())
        assert response.output == "harness handled: analyze"
        assert response.usage is None  # cost_usd=None flows cleanly
        assert response.status == "ok"


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

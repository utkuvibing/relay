"""Agent contract: provider-agnostic, role-aware (SPEC §7/§8)."""

import asyncio

import pytest
from pydantic import ValidationError

from relay.agents.base import Agent, AgentRequest, AgentResponse, AgentRole, TokenUsage


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

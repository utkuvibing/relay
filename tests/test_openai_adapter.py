"""OpenAI-compatible adapter: wire parsing, actionable errors, settings precedence.

SPEC reference: §7, App. B.2 (API-backed family), B.3 (env-only secrets).

Every test runs offline against ``httpx.MockTransport``; plain pytest never
makes paid/network calls. The live smoke test only runs when
``OPENAI_API_KEY`` is set AND ``RELAY_RUN_LIVE_TESTS == "1"``.
"""

import json
import os

import httpx
import pytest

from relay.agents import (
    AgentNotConfigured,
    AgentSettings,
    CliOverrides,
    OpenAICompatibleAgent,
    UnknownAgentError,
    get_agent_class,
    resolve_settings,
)
from relay.agents.base import AgentRequest, AgentResponse, AgentRole, BackendType
from relay.agents.errors import AgentError
from relay.context.config import AgentConfig

API_KEY = "sk-test-secret-that-never-touches-disk"


@pytest.fixture(autouse=True)
def _fake_key(monkeypatch):
    """Every offline test talks to MockTransport with a fake key in env only."""
    monkeypatch.setenv("OPENAI_API_KEY", API_KEY)


def _completion_body(content: str = "analysis result") -> dict:
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": 12, "completion_tokens": 34},
    }


def _agent(handler, **overrides) -> OpenAICompatibleAgent:
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    settings = AgentSettings(adapter="openai", model="gpt-4o-mini", **overrides)
    return OpenAICompatibleAgent(settings, client=client)


class TestSuccessfulCompletion:
    def test_output_and_usage_mapped(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["auth"] = request.headers.get("authorization")
            seen["model"] = json.loads(request.content)["model"]
            return httpx.Response(200, json=_completion_body("the analysis"))

        agent = _agent(handler)
        response = asyncio_run(agent, AgentRequest(prompt="analyze", role=AgentRole.RESEARCHER))

        assert response.output == "the analysis"
        assert response.usage.input_tokens == 12
        assert response.usage.output_tokens == 34
        assert response.usage.cost_usd is None  # no pricing table in Phase 1
        assert seen["url"].endswith("/chat/completions")
        assert seen["auth"] == f"Bearer {API_KEY}"
        assert seen["model"] == "gpt-4o-mini"

    def test_base_url_overridable_day_one(self):
        def handler(request: httpx.Request) -> httpx.Response:
            assert str(request.url).startswith("http://localhost:11434/v1/chat/completions")
            return httpx.Response(200, json=_completion_body())

        agent = _agent(handler, base_url="http://localhost:11434/v1")
        asyncio_run(agent, AgentRequest(prompt="p", role=AgentRole.RESEARCHER))

    def test_response_without_usage_is_valid(self):
        body = _completion_body()
        body.pop("usage")

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=body)

        agent = _agent(handler)
        response = asyncio_run(agent, AgentRequest(prompt="p", role=AgentRole.RESEARCHER))
        assert response.usage is None


class TestActionableErrors:
    def test_401_names_the_env_var(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"error": {"message": "bad key"}})

        agent = _agent(handler)
        with pytest.raises(AgentError, match="401"):
            asyncio_run(agent, AgentRequest(prompt="p", role=AgentRole.RESEARCHER))

    def test_429_names_the_rate_limit(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(429, json={"error": {"message": "slow down"}})

        agent = _agent(handler)
        with pytest.raises(AgentError, match="429"):
            asyncio_run(agent, AgentRequest(prompt="p", role=AgentRole.RESEARCHER))

    def test_malformed_body_rejected(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"choices": "not-a-list"})

        agent = _agent(handler)
        with pytest.raises(AgentError, match="malformed"):
            asyncio_run(agent, AgentRequest(prompt="p", role=AgentRole.RESEARCHER))

    def test_empty_choices_rejected(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"choices": []})

        agent = _agent(handler)
        with pytest.raises(AgentError, match="no completion content"):
            asyncio_run(agent, AgentRequest(prompt="p", role=AgentRole.RESEARCHER))

    def test_timeout_wrapped(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("slow provider", request=request)

        agent = _agent(handler)
        with pytest.raises(AgentError, match="timed out"):
            asyncio_run(agent, AgentRequest(prompt="p", role=AgentRole.RESEARCHER))

    def test_missing_key_fails_before_any_io(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        agent = OpenAICompatibleAgent(
            AgentSettings(adapter="openai"), client=httpx.AsyncClient()
        )
        with pytest.raises(AgentNotConfigured, match="OPENAI_API_KEY"):
            asyncio_run(agent, AgentRequest(prompt="p", role=AgentRole.RESEARCHER))


def asyncio_run(agent, request: AgentRequest) -> AgentResponse:
    import asyncio

    return asyncio.run(agent.run(request))


class TestRegistry:
    def test_aliases_resolve_to_the_openai_adapter(self):
        for name in ("openai", "openai_compatible", "gpt"):
            assert get_agent_class(name) is OpenAICompatibleAgent

    def test_unknown_name_lists_knowns(self):
        with pytest.raises(UnknownAgentError, match="known adapters"):
            get_agent_class("future_cli")  # stable synthetic placeholder (Q-b)


class TestSettingsPrecedence:
    """CLI flag > environment > relay.yaml > built-in default (M4 contract)."""

    def test_cli_beats_env_beats_yaml(self):
        yaml_agent = AgentConfig(backend=BackendType.API, adapter="openai", model="from-yaml")
        settings = resolve_settings(
            cli=CliOverrides(model="from-cli"),
            env={"RELAY_MODEL": "from-env", "OPENAI_API_KEY": API_KEY},
            yaml_agent=yaml_agent,
        )
        assert settings.model == "from-cli"

    def test_env_beats_yaml(self):
        yaml_agent = AgentConfig(backend=BackendType.API, adapter="openai", model="from-yaml")
        settings = resolve_settings(
            env={"RELAY_MODEL": "from-env", "OPENAI_API_KEY": API_KEY}, yaml_agent=yaml_agent
        )
        assert settings.model == "from-env"

    def test_yaml_beats_default(self):
        yaml_agent = AgentConfig(backend=BackendType.API, adapter="openai", model="from-yaml")
        settings = resolve_settings(
            env={"OPENAI_API_KEY": API_KEY}, yaml_agent=yaml_agent
        )
        assert settings.model == "from-yaml"

    def test_default_when_nothing_configured(self):
        settings = resolve_settings(env={"OPENAI_API_KEY": API_KEY})
        assert settings.model == "gpt-4o-mini"
        assert settings.adapter == "openai"

    def test_settings_carry_env_var_name_not_the_key(self):
        settings = resolve_settings(env={"OPENAI_API_KEY": API_KEY})
        assert settings.api_key_env == "OPENAI_API_KEY"
        dump = settings.model_dump()
        assert API_KEY not in str(dump)  # secrets never enter settings


@pytest.mark.skipif(
    not (os.environ.get("OPENAI_API_KEY") and os.environ.get("RELAY_RUN_LIVE_TESTS") == "1"),
    reason="live provider test: set OPENAI_API_KEY and RELAY_RUN_LIVE_TESTS=1",
)
@pytest.mark.asyncio
async def test_live_openai_smoke():
    """Manual exit-gate companion; never runs under plain pytest."""
    agent = OpenAICompatibleAgent(AgentSettings(adapter="openai", model="gpt-4o-mini"))
    response = await agent.run(
        AgentRequest(prompt="Reply with one word: pong", role=AgentRole.RESEARCHER)
    )
    assert response.status == "ok"
    assert response.output.strip()

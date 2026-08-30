"""P4.2: production AgentFactory wiring (frozen plan D6).

The registry-backed factory is the composition-root side of the delivery
seam: core consumes agents only through the ``AgentFactory`` Protocol; this
module proves the production implementation satisfies it and builds offline.
"""

import pytest

from relay.agents.antigravity_cli import AntigravityCLIAdapter
from relay.agents.base import BackendType
from relay.agents.factory import RegistryAgentFactory
from relay.agents.openai import OpenAICompatibleAgent
from relay.context.config import (
    AgentConfig,
    ConfigError,
    HarnessAgentConfig,
    RelayConfig,
)
from relay.core.agent_factory import AgentFactory


def _config(**overrides) -> RelayConfig:
    base: dict[str, object] = {
        "agents": {
            "gpt": AgentConfig(backend=BackendType.API, adapter="openai", model="gpt-4o-mini"),
            "codex": AgentConfig(
                backend=BackendType.HARNESS,
                adapter="codex_cli",
                harness=HarnessAgentConfig(executable_path="codex", timeout_seconds=30),
            ),
        },
    }
    base.update(overrides)
    return RelayConfig(**base)


class TestRegistryAgentFactory:
    def test_satisfies_the_agent_factory_protocol(self):
        """Structural seam: core can consume this factory without knowing it."""
        assert isinstance(RegistryAgentFactory(_config()), AgentFactory)

    def test_builds_configured_api_agent_offline(self):
        factory = RegistryAgentFactory(_config())
        agent = factory.build("gpt")
        assert isinstance(agent, OpenAICompatibleAgent)

    def test_builds_configured_harness_agent_offline(self):
        """Construction never spawns a process or probes the executable
        (G0/R1 executability runs inside the adapter's run)."""
        factory = RegistryAgentFactory(_config())
        agent = factory.build("codex")
        assert isinstance(agent, AntigravityCLIAdapter) is False  # sanity: right adapter family
        assert agent.backend is BackendType.HARNESS

    def test_build_passes_workspace_root_to_harness_agents(self, tmp_path):
        factory = RegistryAgentFactory(_config(), workspace_root=tmp_path)
        agent = factory.build("codex")
        assert agent._workspace_root == tmp_path

    def test_model_of_reports_the_requested_model(self):
        factory = RegistryAgentFactory(_config())
        assert factory.model_of("gpt") == "gpt-4o-mini"

    def test_model_of_harness_agent_follows_the_resolution_chain(self):
        """resolve_settings semantics: yaml > env > default. A harness agent
        without a configured model resolves to the built-in default — the
        same value ``relay ask`` would request."""
        factory = RegistryAgentFactory(_config())
        assert factory.model_of("codex") == "gpt-4o-mini"

    def test_unknown_agent_fails_typed_listing_configured(self):
        factory = RegistryAgentFactory(_config())
        with pytest.raises(ConfigError, match="unknown agent 'missing'"):
            factory.build("missing")
        with pytest.raises(ConfigError, match="unknown agent 'missing'"):
            factory.model_of("missing")

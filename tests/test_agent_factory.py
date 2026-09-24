"""P4.2: production AgentFactory wiring (frozen plan D6).

The registry-backed factory is the composition-root side of the delivery
seam: core consumes agents only through the ``AgentFactory`` Protocol; this
module proves the production implementation satisfies it and builds offline.
"""

import pytest

from relay.agents.base import BackendType
from relay.agents.factory import RegistryAgentFactory
from relay.context.config import (
    AgentConfig,
    ConfigError,
    HarnessAgentConfig,
    RelayConfig,
)


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
    def test_unknown_adapter_name_is_normalized_to_config_error(self):
        """Frozen plan D6: the factory never leaks registry vocabulary — an
        unknown ADAPTER name (registry-side refusal) surfaces as ConfigError
        like every other factory refusal, so core consumers need no registry
        error types."""
        config = _config(
            agents={
                "fut": AgentConfig(backend=BackendType.HARNESS, adapter="future_cli"),
            }
        )
        factory = RegistryAgentFactory(config)
        with pytest.raises(ConfigError, match="future_cli") as exc_info:
            factory.build("fut")
        assert type(exc_info.value) is ConfigError  # not UnknownAgentError

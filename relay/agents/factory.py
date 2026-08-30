"""Production AgentFactory wiring (P4.2, frozen plan D6).

The composition-root side of the delivery seam: turns parsed ``relay.yaml``
plus a workspace root into configured adapter instances through the registry.
This module — inside the agents package — is the ONLY place the registry
enters the delivery path; ``relay.core`` consumes agents exclusively via the
:class:`~relay.core.agent_factory.AgentFactory` Protocol (App. C.1 import
direction: the registry must never become importable from core).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from relay.agents.config import resolve_settings
from relay.agents.registry import UnknownAgentError, build_agent
from relay.context.config import AgentConfig, ConfigError, RelayConfig, agent_config

if TYPE_CHECKING:  # pragma: no cover - typing only
    from relay.agents.base import Agent
    from relay.agents.config import AgentSettings
    from relay.context.config import AgentConfig

__all__ = ["RegistryAgentFactory"]


class RegistryAgentFactory:
    """Build configured logical agents by name; report their requested model.

    Every refusal is normalized to :class:`~relay.context.config.ConfigError`
    (the factory-neutral, config-level typed error): unknown logical agent
    names, unknown adapter names, and backend-family mismatches all surface
    as ``ConfigError`` naming the offending configuration — the factory never
    leaks registry vocabulary to its consumers. Harness adapters are
    constructed with their non-secret profile; no process spawns and no
    executable is probed at build time (G0/R1 executability runs later,
    inside the adapter's ``run``).
    """

    def __init__(self, config: RelayConfig, workspace_root: str | Path | None = None) -> None:
        self._config = config
        self._workspace_root = workspace_root

    def _settings_for(self, name: str) -> tuple[AgentConfig, AgentSettings]:
        cfg = agent_config(self._config, name)
        return cfg, resolve_settings(yaml_agent=cfg)

    def build(self, name: str) -> Agent:
        cfg, settings = self._settings_for(name)
        try:
            return build_agent(name, settings, cfg, workspace_root=self._workspace_root)
        except UnknownAgentError as exc:
            # Normalize registry vocabulary (unknown adapter names) into the
            # factory-neutral config-level error — consumers of the
            # AgentFactory seam never need to know the registry exists.
            raise ConfigError(str(exc)) from exc

    def model_of(self, name: str) -> str | None:
        _, settings = self._settings_for(name)
        return settings.model

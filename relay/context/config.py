"""User-editable configuration (``relay.yaml``) — SPEC §13, App. B.2/B.3.

``relay.yaml`` holds non-secret provider facts: backend type, adapter name,
model, base URL. Credentials are environment-only and never appear here.

Backend awareness is canonical and fixed now so no config redesign is needed
when harnesses arrive:

.. code-block:: yaml

    agents:
      gpt-api: {backend: api, adapter: openai, model: gpt-4o-mini}
      codex:   {backend: harness, adapter: codex_cli}
      claude:  {backend: harness, adapter: claude_code}

Phase 1 parses every entry but executes only ``backend: api``. Harness
entries fail with an explicit, actionable error — they are never silently
ignored.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, ValidationError

from relay.agents.base import BackendType

#: Built-in default agent (no relay.yaml present). The adapter name maps
#: through :mod:`relay.agents.registry`; the model is overridable per run.
DEFAULT_AGENT_NAME = "gpt"
DEFAULT_MODEL = "gpt-4o-mini"

_HARNESS_UNAVAILABLE = (
    "agent '{name}' is harness-backed (adapter '{adapter}'); "
    "the harness runtime arrives in Phase 2 (Codex CLI / local tool runtime)"
)


class AgentConfig(BaseModel):
    """One entry of ``relay.yaml`` ``agents:`` — non-secret facts only."""

    backend: BackendType
    adapter: str
    model: str | None = None
    base_url: str | None = Field(
        default=None,
        description="API-family only (e.g. a local OpenAI-compatible server); never credentials.",
    )


class RelayConfig(BaseModel):
    """The full parsed ``relay.yaml``."""

    agents: dict[str, AgentConfig]


class ConfigError(ValueError):
    """A ``relay.yaml`` that cannot be honored."""


def default_config() -> RelayConfig:
    """The built-in configuration used when no ``relay.yaml`` exists."""
    return RelayConfig(
        agents={
            DEFAULT_AGENT_NAME: AgentConfig(
                backend=BackendType.API, adapter="openai", model=DEFAULT_MODEL
            )
        }
    )


def _parse_config(data: Any, source: str) -> RelayConfig:
    if not isinstance(data, dict):
        raise ConfigError(f"{source}: expected a YAML mapping at the top level")
    agents = data.get("agents")
    if not isinstance(agents, dict):
        raise ConfigError(f"{source}: missing 'agents:' mapping (see SPEC §13, App. B.2)")
    try:
        return RelayConfig(agents=agents)
    except ValidationError as exc:
        problems = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
        raise ConfigError(f"{source}: invalid agent config — {problems}") from exc


def load_config(root: str | Path) -> RelayConfig:
    """Parse ``relay.yaml`` at the workspace root; falls back to defaults.

    The built-in default (``gpt``, api-backed) makes a fresh workspace usable
    immediately; any present ``relay.yaml`` fully replaces it.
    """
    path = Path(root).expanduser() / "relay.yaml"
    if not path.is_file():
        return default_config()
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML — {exc}") from exc
    return _parse_config(data, str(path))


def agent_config(config: RelayConfig, name: str) -> AgentConfig:
    """Resolve one named agent; unknown names list the known ones."""
    if name in config.agents:
        return config.agents[name]
    known = ", ".join(sorted(config.agents)) or "(none configured)"
    raise ConfigError(f"unknown agent '{name}' — configured agents: {known}")


def require_api_backed(name: str, agent: AgentConfig) -> None:
    """Refuse harness-backed agents with the Phase 2 pointer (App. B.2/B.4).

    The separation is canonical and harness entries are first-class config;
    they only lack a Phase 1 runtime. An explicit error beats silent
    misbehavior.
    """
    if agent.backend is BackendType.HARNESS:
        raise ConfigError(_HARNESS_UNAVAILABLE.format(name=name, adapter=agent.adapter))

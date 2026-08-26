"""User-editable configuration (``relay.yaml``) — SPEC §13, App. B.2/B.3.

``relay.yaml`` holds non-secret provider facts: backend type, adapter name,
model, base URL. Credentials are environment-only and never appear here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from relay.agents.base import BackendType
from relay.harness.types import ExecutionGrantKind

DEFAULT_AGENT_NAME = "gpt"
DEFAULT_MODEL = "gpt-4o-mini"

_MAX_HARNESS_TIMEOUT_S = 3600


class HarnessAgentConfig(BaseModel):
    """Non-secret per-agent harness profile (SPEC §27 P2.1, App. C.2/C.4).

    Everything here is a fact about *how* to invoke the harness binary;
    credentials stay environment-only (App. B.3) and never appear here.
    ``grant=None`` means "defer to the adapter default" — an adapter without
    a default cannot run until config supplies one (G0/R1).
    """

    model_config = ConfigDict(extra="forbid")

    executable_path: str | None = Field(
        default=None,
        description="Explicit binary location; discovery falls back to PATH.",
    )
    extra_args: list[str] = Field(default_factory=list)
    timeout_seconds: float = Field(default=300, gt=0, le=_MAX_HARNESS_TIMEOUT_S)
    grant: ExecutionGrantKind | None = None
    auth_probe: bool = True


class AgentConfig(BaseModel):
    """One ``relay.yaml`` agent entry; unknown fields are rejected."""

    model_config = ConfigDict(extra="forbid")

    backend: BackendType
    adapter: str
    model: str | None = None
    base_url: str | None = Field(
        default=None,
        description="API-family only; never credentials.",
    )
    harness: HarnessAgentConfig | None = None

    @model_validator(mode="after")
    def _enforce_family_fields(self) -> AgentConfig:
        if self.backend is BackendType.API and self.harness is not None:
            raise ValueError("'harness:' block requires 'backend: harness'")
        return self


class RelayConfig(BaseModel):
    """The full parsed ``relay.yaml``; unknown top-level fields are rejected."""

    model_config = ConfigDict(extra="forbid")

    agents: dict[str, AgentConfig]


class ConfigError(ValueError):
    """A ``relay.yaml`` that cannot be honored."""


def default_config() -> RelayConfig:
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
        return RelayConfig.model_validate(data)
    except ValidationError as exc:
        problems = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
        raise ConfigError(f"{source}: invalid agent config — {problems}") from exc


def load_config(root: str | Path) -> RelayConfig:
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
    if name in config.agents:
        return config.agents[name]
    known = ", ".join(sorted(config.agents)) or "(none configured)"
    raise ConfigError(f"unknown agent '{name}' — configured agents: {known}")

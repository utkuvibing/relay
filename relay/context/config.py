"""User-editable configuration (``relay.yaml``) — SPEC §13, App. B.2/B.3.

``relay.yaml`` holds non-secret provider facts: backend type, adapter name,
model, base URL. Credentials are environment-only and never appear here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from relay.agents.base import AgentRole, BackendType
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


class VerificationConfig(BaseModel):
    """One Relay-owned verification command (SPEC §27 Phase 3, App. A.1).

    Executed through the permission gate as a Relay-owned ToolRun with
    workspace-root cwd and a bounded timeout; the implementer never grades
    its own exam. Exit code is the only verdict.
    """

    model_config = ConfigDict(extra="forbid")

    program: str = Field(min_length=1, description="Executable resolved via PATH.")
    args: list[str] = Field(default_factory=list)
    timeout_seconds: float = Field(default=300, gt=0, le=_MAX_HARNESS_TIMEOUT_S)


class ApprovalPolicyConfig(BaseModel):
    """Completion policy (P3.3; SPEC App. A.3 — policy-driven approval).

    ``gated`` (default): REVIEWING → APPROVAL_REQUIRED → human → DONE.
    ``direct``: the A.3 opt-out — Relay attests NO_PENDING_APPROVALS over an
    empty-by-construction queue and the machine's direct REVIEWING → DONE
    edge (which still demands tests + review evidence) completes the task.
    """

    model_config = ConfigDict(extra="forbid")

    mode: Literal["gated", "direct"] = "gated"


class RelayConfig(BaseModel):
    """The full parsed ``relay.yaml``; unknown top-level fields are rejected."""

    model_config = ConfigDict(extra="forbid")

    agents: dict[str, AgentConfig]
    #: P3.2 — Relay-scoped verification (frozen plan Q-c): typed argv, never
    #: a free-form shell string. Absent → builds block honestly in VERIFYING.
    verification: VerificationConfig | None = None
    #: P3.3 (frozen plan Q-d): name reference into ``agents:`` — the dedicated
    #: review role; absent → the implementer's adapter falls back under
    #: READ_ONLY + REVIEWER role in a fresh process. Any execution family.
    reviewer: str | None = None
    #: P3.3 (frozen plan Q-e): completion policy. Gated is the default.
    approval: ApprovalPolicyConfig | None = None
    #: P4.2 (frozen plan D4/D5): conversation-bus role vocabulary — an
    #: ``AgentRole`` value mapped to a configured agent name. Fully DECOUPLED
    #: from the P3.3 ``reviewer:`` build-flow selector: no fallback in either
    #: direction and no conflict validation; each is the sole source of truth
    #: for its own workflow.
    roles: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _reviewer_must_be_configured(self) -> RelayConfig:
        if self.reviewer is not None and self.reviewer not in self.agents:
            raise ValueError(
                f"reviewer '{self.reviewer}' is not a configured agent — "
                "add it under 'agents:' first"
            )
        return self

    @model_validator(mode="after")
    def _roles_are_valid_agent_bindings(self) -> RelayConfig:
        known_roles = ", ".join(member.value for member in AgentRole)
        for role, agent in self.roles.items():
            if role not in {member.value for member in AgentRole}:
                raise ValueError(
                    f"roles: '{role}' is not a valid role address — "
                    f"known roles: {known_roles}"
                )
            if agent not in self.agents:
                raise ValueError(
                    f"roles: role '{role}' targets agent '{agent}', which is "
                    "not configured under 'agents:'"
                )
        return self


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

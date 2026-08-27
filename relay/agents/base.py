"""Provider-agnostic Agent contract.

SPEC reference: §7 (Agent Abstraction), §8 (Agent Roles).

The rest of Relay must not know which provider produced a response.
Model and role are orthogonal: any adapter can act in any role.
"""

from __future__ import annotations

import abc
import enum
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, Field


class AgentRole(str, enum.Enum):
    """What an agent is doing — deliberately decoupled from which model it is."""

    PLANNER = "planner"
    ARCHITECT = "architect"
    IMPLEMENTER = "implementer"
    REVIEWER = "reviewer"
    ADVERSARIAL_REVIEWER = "adversarial_reviewer"
    CRITIC = "critic"
    RESEARCHER = "researcher"
    DOMAIN_EXPERT = "domain_expert"
    REPOSITORY_EXPERT = "repository_expert"
    MODERATOR = "moderator"


class BackendType(str, enum.Enum):
    """How an agent executes (SPEC Appendix B.2/B.3).

    API adapters may resolve credentials from environment/config; harness
    adapters own their login/session entirely and are invoked through
    their supported CLI/process interface. The Agent abstraction assumes
    nothing beyond this declaration.
    """

    API = "api"
    HARNESS = "harness"


class TokenUsage(BaseModel):
    """Observability data per run (SPEC §25)."""

    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None


class AgentRequest(BaseModel):
    """Everything one agent run needs — assembled by the Context Engine.

    The engine decides what context an agent sees; agents never receive
    the whole workspace by default (SPEC §12).
    """

    prompt: str
    role: AgentRole
    task_id: str | None = None
    room_id: str | None = None
    #: References into workspace/context (file paths, diff ranges, prior decisions).
    context_refs: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class RunObservation(BaseModel):
    """Provider-neutral facts a backend reported about its own execution.

    App. C.6 seam: additive, nullable, allowlist-shaped. Field names are
    deliberately restricted to the C.4/C.6 vocabulary; nothing secret-shaped
    may ride here (enforced by the persisted-vocabulary hygiene tests).
    """

    resolved_model: str | None = None
    adapter_version: str | None = None
    backend: BackendType | str | None = None
    external_session_ref: str | None = Field(
        default=None,
        description="NON-SECRET continuation handle only when config opts in.",
    )


class ToolObservation(BaseModel):
    """One provider-neutral tool/tool-like event a backend reported.

    Adapters translate their native event streams into this shape; core
    persists it without knowing any provider vocabulary (App. C.1). Fields
    are allowlist-shaped and bounded upstream by the adapter.
    """

    kind: str = Field(description="Neutral kind, e.g. 'shell', 'file_edit', 'message'.")
    summary: str = Field(default="", description="Bounded human-readable description.")
    command: str | None = Field(
        default=None,
        description="Sanitized command line when the observation is a shell-type event.",
    )


class AgentResponse(BaseModel):
    """Structured result of one agent run."""

    agent: str = Field(description="Adapter name that produced this response, e.g. 'claude'.")
    role: AgentRole
    output: str
    status: Literal["ok", "error"] = "ok"
    error: str | None = None
    #: References to artifacts produced during the run (plans, diffs, findings).
    artifact_refs: list[str] = Field(default_factory=list)
    usage: TokenUsage | None = None
    #: Optional execution observation (harness runs report what actually ran).
    observation: RunObservation | None = None
    #: Adapter-normalized tool events (observability tier; App. C.5/C.7).
    tool_observations: list[ToolObservation] = Field(default_factory=list)


class Agent(abc.ABC):
    """Common interface for every provider adapter (SPEC §7).

    Adapters translate between this contract and OpenAI / Anthropic /
    DeepSeek / Codex CLI / Claude Code / local models.
    """

    #: Provider-facing name used in configs, e.g. "gpt", "claude", "codex".
    name: ClassVar[str]

    #: Execution family declared by each adapter (Appendix B.2/B.3).
    backend: ClassVar[BackendType] = BackendType.API

    @abc.abstractmethod
    async def run(self, request: AgentRequest) -> AgentResponse:
        """Execute exactly one run against the provider."""
        raise NotImplementedError

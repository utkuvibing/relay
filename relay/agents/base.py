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


class Agent(abc.ABC):
    """Common interface for every provider adapter (SPEC §7).

    Adapters translate between this contract and OpenAI / Anthropic /
    DeepSeek / Codex CLI / Claude Code / local models.
    """

    #: Provider-facing name used in configs, e.g. "gpt", "claude", "codex".
    name: ClassVar[str]

    @abc.abstractmethod
    async def run(self, request: AgentRequest) -> AgentResponse:
        """Execute exactly one run against the provider."""
        raise NotImplementedError

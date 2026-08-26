"""Adapter registry: config names → adapter classes (SPEC §7, App. B.2).

Names here are the *adapter* vocabulary that ``relay.yaml`` ``adapter:``
values and CLI provider names map through. "gpt" is an alias for the
OpenAI-compatible adapter so the Phase 1 exit gate reads naturally:
``relay ask gpt "Analyze this repository"``.
"""

from __future__ import annotations

from relay.agents.base import Agent
from relay.agents.openai import OpenAICompatibleAgent

AGENTS: dict[str, type[Agent]] = {
    "openai": OpenAICompatibleAgent,
    #: Canonical config value for "any OpenAI-compatible endpoint".
    "openai_compatible": OpenAICompatibleAgent,
    #: Familiar alias for the same adapter.
    "gpt": OpenAICompatibleAgent,
}


class UnknownAgentError(ValueError):
    """No adapter is registered under that name."""


def get_agent_class(name: str) -> type[Agent]:
    """Resolve an adapter name; unknown names list the known ones."""
    try:
        return AGENTS[name]
    except KeyError as exc:
        known = ", ".join(sorted(AGENTS))
        raise UnknownAgentError(f"unknown agent adapter '{name}' — known adapters: {known}") from exc

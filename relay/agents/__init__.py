"""Provider adapters. Every agent sits behind the same Agent interface.

Phase 1 ships one API-backed adapter (OpenAI-compatible chat completions);
Phase 2 adds the generic harness runtime (relay/harness) with registry-routed
construction via :func:`build_agent`. Vendor product names live only in
adapter modules and config values — never here (App. C.1).
"""

from relay.agents.base import Agent, AgentRequest, AgentResponse, AgentRole, BackendType, TokenUsage
from relay.agents.config import AgentSettings, CliOverrides, resolve_settings
from relay.agents.errors import AgentError, AgentNotConfigured
from relay.agents.openai import OpenAICompatibleAgent
from relay.agents.registry import (
    AGENTS,
    UnknownAgentError,
    build_agent,
    get_agent_class,
    production_registry_names,
    transient_adapters,
)

__all__ = [
    "AGENTS",
    "Agent",
    "AgentError",
    "AgentNotConfigured",
    "AgentRequest",
    "AgentResponse",
    "AgentRole",
    "AgentSettings",
    "BackendType",
    "CliOverrides",
    "OpenAICompatibleAgent",
    "TokenUsage",
    "UnknownAgentError",
    "build_agent",
    "get_agent_class",
    "production_registry_names",
    "resolve_settings",
    "transient_adapters",
]

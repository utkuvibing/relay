"""Provider adapters. Every model sits behind the same Agent interface.

Phase 1 ships one API-backed adapter (OpenAI-compatible chat completions);
harness-backed adapters (Codex CLI, Claude Code, Gemini CLI) arrive in
Phase 2+ and declare their family via ``backend`` (SPEC App. B.2/B.3).
"""

from relay.agents.base import Agent, AgentRequest, AgentResponse, AgentRole, BackendType, TokenUsage
from relay.agents.config import AgentSettings, CliOverrides, resolve_settings
from relay.agents.errors import AgentError, AgentNotConfigured
from relay.agents.openai import OpenAICompatibleAgent
from relay.agents.registry import AGENTS, UnknownAgentError, get_agent_class

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
    "get_agent_class",
    "resolve_settings",
]

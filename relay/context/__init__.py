"""Context Engine: project discovery, config, and workspace identity (SPEC §12/§13)."""

from relay.context.config import (
    DEFAULT_AGENT_NAME,
    DEFAULT_MODEL,
    AgentConfig,
    ConfigError,
    RelayConfig,
    agent_config,
    default_config,
    load_config,
    require_api_backed,
)
from relay.context.workspace import (
    ProjectProfile,
    WorkspaceLayout,
    discover_profile,
    identity_key,
    initialize_workspace,
    load_profile,
    save_profile,
    workspace_layout,
)

__all__ = [
    "DEFAULT_AGENT_NAME",
    "DEFAULT_MODEL",
    "AgentConfig",
    "ConfigError",
    "ProjectProfile",
    "RelayConfig",
    "WorkspaceLayout",
    "agent_config",
    "default_config",
    "discover_profile",
    "identity_key",
    "initialize_workspace",
    "load_config",
    "load_profile",
    "require_api_backed",
    "save_profile",
    "workspace_layout",
]

"""Adapter registry: config names → adapter classes (SPEC §7, App. B.2/C.1).

Names here are the *adapter* vocabulary that ``relay.yaml`` ``adapter:``
values and CLI provider names map through. "gpt" is an alias for the
OpenAI-compatible adapter so the Phase 1 exit gate reads naturally:
``relay ask gpt "Analyze this repository"``.

Phase 2 additions:

* :func:`build_agent` — uniform construction with G0 executability
  validation (registry presence, backend-family match, harness profile
  routing). Registry presence alone is never sufficient to run.
* :func:`transient_adapters` — a strictly TEST-ONLY scoped seam so conformance
  fakes can drive end-to-end flows WITHOUT ever entering ``AGENTS``
  (production registry stays frozen; R1#3).
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import TYPE_CHECKING

from relay.agents.base import Agent
from relay.agents.openai import OpenAICompatibleAgent

if TYPE_CHECKING:  # pragma: no cover - typing only
    from relay.agents.config import AgentSettings
    from relay.context.config import AgentConfig


AGENTS: dict[str, type[Agent]] = {
    "openai": OpenAICompatibleAgent,
    #: Canonical config value for "any OpenAI-compatible endpoint".
    "openai_compatible": OpenAICompatibleAgent,
    #: Familiar alias for the same adapter.
    "gpt": OpenAICompatibleAgent,
}

#: Harness-family adapters (P2.2). Product names live ONLY here, in the
#: adapter modules themselves, and in config values — never in core (C.1).
from relay.agents.antigravity_cli import AntigravityCLIAdapter
from relay.agents.claude_code import ClaudeCodeAgent
from relay.agents.codex_cli import CodexCLIAdapter

AGENTS["codex_cli"] = CodexCLIAdapter
#: Familiar alias for the Codex CLI adapter.
AGENTS["codex"] = CodexCLIAdapter

AGENTS["claude_code"] = ClaudeCodeAgent

AGENTS["antigravity_cli"] = AntigravityCLIAdapter
#: Familiar alias: the binary name users actually type.
AGENTS["agy"] = AntigravityCLIAdapter


class UnknownAgentError(ValueError):
    """No adapter is registered under that name."""


# -- test-only transient layer (never consulted by production registries) ----

_TRANSIENT_LAYERS: ContextVar[tuple[dict[str, type[Agent]], ...]] = ContextVar(
    "relay_transient_adapter_layers", default=()
)


@contextmanager
def transient_adapters(mapping: Mapping[str, type[Agent]]) -> Iterator[None]:
    """Scope extra adapter classes to the current task/context.

    Test/conformance fixture infrastructure ONLY — production code must
    never call this (G0/R1: fakes stay out of the production registry by
    construction; :data:`AGENTS` is untouched for its whole duration).
    """
    token = _TRANSIENT_LAYERS.set(_TRANSIENT_LAYERS.get() + (dict(mapping),))
    try:
        yield
    finally:
        _TRANSIENT_LAYERS.reset(token)


def production_registry_names() -> frozenset[str]:
    """The immutable production name set (hygiene assertions read this)."""
    return frozenset(AGENTS)


def get_agent_class(name: str) -> type[Agent]:
    """Resolve an adapter name; unknown names list the known ones."""
    if name in AGENTS:
        return AGENTS[name]
    for layer in reversed(_TRANSIENT_LAYERS.get()):
        if name in layer:
            return layer[name]
    known = ", ".join(sorted(AGENTS)) or "(none configured)"
    raise UnknownAgentError(f"unknown agent adapter '{name}' — known adapters: {known}")


def build_agent(
    name: str,
    settings: AgentSettings,
    cfg: AgentConfig,
    *,
    workspace_root: str | Path | None = None,
) -> Agent:
    """Resolve + construct one configured agent with executability checks.

    G0/R1 order of refusal, all typed before any process could start:

    1. unknown adapter → :class:`UnknownAgentError` (registry presence);
    2. backend mismatch between config declaration and adapter class →
       ``ConfigError`` (restores, at wiring time, what the old Phase-1
       refusal guaranteed about declared execution families);
    3. harness adapters receive their non-secret profile verbatim; a run
       without a resolvable ExecutionGrant fails later inside
       ``HarnessAgent.run`` BEFORE spawn (see runtime tests).

    API adapters are constructed exactly as in Phase 1 (settings only).
    """
    # Lazy imports keep the agents package import-graph free of cycles
    # (context.config → harness.types ← harness.runtime …).
    from relay.agents.base import BackendType
    from relay.context.config import ConfigError
    from relay.harness.runtime import HarnessAgent

    cls = get_agent_class(settings.adapter)

    if cls.backend is not cfg.backend:
        raise ConfigError(
            f"agent '{name}' declares backend '{cfg.backend.value}' but "
            f"adapter '{settings.adapter}' executes as "
            f"'{cls.backend.value}' — fix 'backend:' or pick the matching "
            "adapter"
        )

    if cls.backend is BackendType.HARNESS:
        assert issubclass(cls, HarnessAgent)
        return cls(
            settings=settings,
            profile=cfg.harness,
            workspace_root=workspace_root,
        )
    return cls(settings)

"""Adapter settings resolution — CLI > environment > relay.yaml > default.

SPEC reference: §7, App. B.3 (secrets are environment-only, never stored).

``AgentSettings`` carries non-secret provider facts (adapter, model, base
URL) plus the *name* of the environment variable holding the API key — never
the key itself. Resolution order is fixed so a per-run ``--model`` flag beats
``RELAY_MODEL``, which beats ``relay.yaml``, which beats the built-in default.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

from pydantic import BaseModel, Field

from relay.context.config import DEFAULT_MODEL, AgentConfig

#: Environment variables consulted by resolution (in precedence order).
ENV_MODEL = "RELAY_MODEL"
ENV_BASE_URL = "OPENAI_BASE_URL"
ENV_API_KEY = "OPENAI_API_KEY"


class AgentSettings(BaseModel):
    """Non-secret facts an adapter needs to execute one run."""

    adapter: str
    model: str | None = None
    base_url: str | None = Field(
        default=None,
        description="OpenAI-compatible endpoint; overridable day one (App. B.2).",
    )
    api_key_env: str = Field(
        default=ENV_API_KEY,
        description="Name of the env var holding the key — never the key itself.",
    )


@dataclass(frozen=True)
class CliOverrides:
    """Per-invocation flags that outrank every other source."""

    model: str | None = None


def _first_non_none(*values: str | None) -> str | None:
    return next((value for value in values if value is not None), None)


def resolve_settings(
    *,
    cli: CliOverrides | None = None,
    env: Mapping[str, str] | None = None,
    yaml_agent: AgentConfig | None = None,
) -> AgentSettings:
    """Merge sources with fixed precedence: CLI flag > env > relay.yaml > default.

    ``env`` defaults to ``os.environ``; tests inject a mapping. The API key
    is never resolved here — adapters read it from the environment at call
    time, so it exists only in process memory.
    """
    env = os.environ if env is None else env
    cli = cli or CliOverrides()

    model = _first_non_none(cli.model, env.get(ENV_MODEL), yaml_agent.model if yaml_agent else None)
    if model is None:
        model = DEFAULT_MODEL
    base_url = _first_non_none(env.get(ENV_BASE_URL), yaml_agent.base_url if yaml_agent else None)
    adapter = yaml_agent.adapter if yaml_agent else "openai"
    return AgentSettings(
        adapter=adapter,
        model=model,
        base_url=base_url,
        api_key_env=env.get("RELAY_API_KEY_ENV", ENV_API_KEY),
    )

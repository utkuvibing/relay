"""AgentFactory seam (P4.2, frozen plan D6).

Core consumes configured agents exclusively through this Protocol; the
production implementation lives in ``relay/agents/factory.py`` — the only
side of the delivery path allowed to import the adapter registry (App. C.1
import direction). Test fakes implement the Protocol structurally, so the
offline delivery tests never touch production adapters.

Deliberately dependency-free: this module must stay importable from
anywhere (including a ``relay.core``-first import order) without dragging
adapter/transport code into core.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:  # pragma: no cover - typing only
    from relay.agents.base import Agent

__all__ = ["AgentFactory"]


@runtime_checkable
class AgentFactory(Protocol):
    """Build one configured logical agent by name, plus its requested model.

    ``build`` returns the adapter instance for a configured logical agent;
    unknown names fail typed (the production implementation raises
    ``ConfigError`` listing the configured agents). ``model_of`` reports the
    requested model for the ``Run.model`` column (P2.2 discipline: requested,
    never resolved); it may return ``None`` (harness runs frequently carry no
    model).
    """

    def build(self, name: str) -> Agent: ...

    def model_of(self, name: str) -> str | None: ...

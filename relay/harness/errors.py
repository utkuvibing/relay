"""Errors raised by the generic harness runtime (SPEC §27 P2.1, App. C.2).

Every member subclasses :class:`~relay.agents.errors.AgentError` so the
family-blind orchestrator persists them through the exact sanitized-error
path used since Phase 1 — no orchestrator changes, no raw internals leaked.
Messages are constructed redacted by their call sites (R4/G3); they never
embed environment dumps, argv containing prompts, or provider response
bodies.
"""

from __future__ import annotations

from relay.agents.errors import AgentError


class UnsupportedCapability(AgentError):
    """A requested capability is not declared by the harness (App. C.3)."""


class MissingExecutionGrantError(AgentError):
    """No ExecutionGrant could be resolved for a harness run.

    Raised *before* any process starts: neither the profile nor the adapter
    default supplied a grant (G0/R1).
    """


class HarnessDiscoveryError(AgentError):
    """The harness executable could not be located or version-probed."""


class HarnessLaunchError(AgentError):
    """Spawning the harness process failed (OSError translated; R4)."""


class HarnessOutputError(AgentError):
    """Harness output could not be decoded/parsed into a response."""


class HarnessTimeoutError(AgentError):
    """The run exceeded its deadline and was terminated with its tree (G2)."""


class HarnessCancelledError(AgentError):
    """A cooperative cancellation ended the run early."""

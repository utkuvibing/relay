"""Errors raised by provider adapters (SPEC §7).

Deliberately transport-neutral: the orchestrator and the domain models know
only that a run failed, never why or through which channel.
"""

from __future__ import annotations


class AgentError(RuntimeError):
    """A provider call could not produce a response (timeout, auth, HTTP…)."""


class AgentNotConfigured(AgentError):
    """The adapter has no credentials/settings to run with.

    Raised *before* any network I/O. Secrets are env-only (App. B.3); this
    error points at the environment variable, never at a stored value.
    """

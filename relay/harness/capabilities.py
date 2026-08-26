"""Harness capability vocabulary — typed, closed, queried (App. C.3).

Core code asks WHAT a harness can do, never WHICH vendor it belongs to.
The candidate set below is closed for extension across releases; additions
require an appendix note (App. C.3). Unsupported capabilities must fail
explicitly (:class:`relay.harness.errors.UnsupportedCapability`) rather than
degrade silently.
"""

from __future__ import annotations

import enum
from collections.abc import Iterable

from relay.harness.errors import UnsupportedCapability


class HarnessCapability(str, enum.Enum):
    """What a harness adapter can do (App. C.3 candidate set, frozen)."""

    STRUCTURED_OUTPUT = "structured_output"
    READ_ONLY_ACCESS = "read_only_access"
    WORKSPACE_WRITE = "workspace_write"
    SHELL_EXECUTION = "shell_execution"
    GIT_OPERATIONS = "git_operations"
    TOOL_EVENT_STREAM = "tool_event_stream"
    APPROVAL_EVENT_STREAM = "approval_event_stream"
    SESSION_RESUME = "session_resume"
    MODEL_SELECTION = "model_selection"
    RESOLVED_MODEL_REPORTING = "resolved_model_reporting"
    TOKEN_USAGE_REPORTING = "token_usage_reporting"
    DIFF_REPORTING = "diff_reporting"
    NETWORK_ACCESS = "network_access"


#: The complete declared set — conformance and documentation reference.
ALL_CAPABILITIES: frozenset[HarnessCapability] = frozenset(HarnessCapability)


def ensure(
    capabilities: Iterable[HarnessCapability],
    wanted: HarnessCapability,
) -> None:
    """Raise :class:`UnsupportedCapability` unless ``wanted`` is declared.

    Explicit failure at request-validation time — silent degradation is
    forbidden (App. C.3 rule 1).
    """
    declared = {cap for cap in capabilities}
    if wanted not in declared:
        raise UnsupportedCapability(
            f"harness does not declare capability '{wanted.value}' — "
            f"declared: {', '.join(sorted(c.value for c in declared)) or '(none)'}"
        )

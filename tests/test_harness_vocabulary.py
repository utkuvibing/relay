"""Harness capability and error boundaries."""

from __future__ import annotations

import pytest

from relay.agents.errors import AgentError
from relay.harness.capabilities import HarnessCapability, ensure
from relay.harness.errors import (
    HarnessCancelledError,
    HarnessDiscoveryError,
    HarnessLaunchError,
    HarnessOutputError,
    HarnessTimeoutError,
    MissingExecutionGrantError,
    UnsupportedCapability,
)


class TestCapabilityVocabulary:
    def test_ensure_raises_typed_error_for_missing_capability(self):
        with pytest.raises(UnsupportedCapability) as excinfo:
            ensure([HarnessCapability.READ_ONLY_ACCESS], HarnessCapability.NETWORK_ACCESS)
        assert "network_access" in str(excinfo.value)
        assert "read_only_access" in str(excinfo.value)


class TestErrorTaxonomy:
    """R4/G3 vocabulary precondition: every harness error is an AgentError."""

    def test_all_harness_errors_are_agent_errors(self):
        for error_type in (
            UnsupportedCapability,
            MissingExecutionGrantError,
            HarnessDiscoveryError,
            HarnessLaunchError,
            HarnessOutputError,
            HarnessTimeoutError,
            HarnessCancelledError,
        ):
            assert issubclass(error_type, AgentError)

"""Harness runtime vocabulary (SPEC §27 P2.1, App. C.2/C.3) — capability,
grant, auth-state, and error-taxonomy units. No I/O here.
"""

from __future__ import annotations

import pytest

from relay.agents.errors import AgentError
from relay.harness.capabilities import ALL_CAPABILITIES, HarnessCapability, ensure
from relay.harness.errors import (
    HarnessCancelledError,
    HarnessDiscoveryError,
    HarnessLaunchError,
    HarnessOutputError,
    HarnessTimeoutError,
    MissingExecutionGrantError,
    UnsupportedCapability,
)
from relay.harness.types import (
    AuthState,
    ExecutionGrant,
    ExecutionGrantKind,
    ExitSemantics,
    HarnessFacts,
    HarnessInfo,
    ProcessOutcome,
    StreamCapture,
)

#: The Appendix C.3 candidate set, frozen — additions need an appendix note.
EXPECTED_CAPABILITIES = {
    "structured_output",
    "read_only_access",
    "workspace_write",
    "shell_execution",
    "git_operations",
    "tool_event_stream",
    "approval_event_stream",
    "session_resume",
    "model_selection",
    "resolved_model_reporting",
    "token_usage_reporting",
    "diff_reporting",
    "network_access",
}


class TestCapabilityVocabulary:
    def test_closed_set_matches_appendix_c3(self):
        assert {cap.value for cap in HarnessCapability} == EXPECTED_CAPABILITIES
        assert len(ALL_CAPABILITIES) == 13

    def test_ensure_raises_typed_error_for_missing_capability(self):
        with pytest.raises(UnsupportedCapability) as excinfo:
            ensure([HarnessCapability.READ_ONLY_ACCESS], HarnessCapability.NETWORK_ACCESS)
        assert "network_access" in str(excinfo.value)
        assert "read_only_access" in str(excinfo.value)

    def test_ensure_passes_when_declared(self):
        ensure([HarnessCapability.DIFF_REPORTING], HarnessCapability.DIFF_REPORTING)


class TestGrantVocabulary:
    def test_grant_kinds_exactly_the_c5_set(self):
        assert {kind.value for kind in ExecutionGrantKind} == {
            "read_only",
            "workspace_write",
            "workspace_write_network",
        }

    def test_grant_is_frozen_value_with_optional_translated_args(self):
        grant = ExecutionGrant(
            kind=ExecutionGrantKind.WORKSPACE_WRITE,
            additional_args=("--sandbox", "workspace-write"),
        )
        assert grant.kind is ExecutionGrantKind.WORKSPACE_WRITE
        assert grant.additional_args == ("--sandbox", "workspace-write")


class TestAuthStateAndFacts:
    def test_auth_state_vocabulary(self):
        assert {state.value for state in AuthState} == {
            "authenticated",
            "unauthenticated",
            "unknown",
        }

    def test_harness_facts_allowlist_shape(self):
        facts = HarnessFacts(adapter="fake", executable_label="fake.py", version="1.0")
        assert facts.auth_state is AuthState.UNKNOWN
        assert facts.auth_mode is None
        assert facts.external_session_ref is None


class TestProcessOutcomeTypes:
    @staticmethod
    def _outcome(**overrides):
        base = dict(
            exit_code=0,
            timed_out=False,
            cancelled=False,
            stdout=StreamCapture(text="out"),
            stderr=StreamCapture(text=""),
            duration_s=0.01,
            semantics=ExitSemantics.OK,
        )
        base.update(overrides)
        return ProcessOutcome(**base)

    def test_roundtrip_fields(self):
        outcome = self._outcome(exit_code=7, semantics=ExitSemantics.TRANSPORT)
        assert outcome.exit_code == 7
        assert outcome.semantics is ExitSemantics.TRANSPORT
        assert not outcome.timed_out and not outcome.cancelled

    def test_capture_records_truncation_and_line_count(self):
        capture = StreamCapture(text="a\nb\nc", truncated=True, lines_seen=3)
        assert capture.lines_seen == 3 and capture.truncated


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

    def test_harness_info_defaults_version_to_unknown(self):
        info = HarnessInfo(adapter="x", executable="x.exe")
        assert info.version is None and info.version_raw is None

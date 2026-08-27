"""HarnessAgent base-runtime units (C5): grants, gating, ordering, boundary.

Integration slices drive REAL child processes through ``run()`` (same engine
the conformance battery reuses); the R4 table is unit-tested directly.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from relay.agents.base import AgentRequest, AgentRole
from relay.agents.config import AgentSettings
from relay.agents.errors import AgentError
from relay.context.config import HarnessAgentConfig
from relay.harness.capabilities import HarnessCapability
from relay.harness.discovery import ResolvedExecutable
from relay.harness.errors import (
    HarnessLaunchError,
    HarnessOutputError,
    HarnessTimeoutError,
    MissingExecutionGrantError,
    UnsupportedCapability,
)
from relay.harness.runtime import HarnessAgent
from relay.harness.types import (
    ExecutionGrant,
    ExecutionGrantKind,
    ExitSemantics,
)

PY = sys.executable

_ECHO_SCRIPT = "import sys; sys.stdout.write(sys.stdin.read())"
_SLEEP_SCRIPT = "import time; time.sleep(5)"
_AUTH_FAIL_SCRIPT = "import sys; print('FAKE_TOKEN=supersecret123', file=sys.stderr); sys.exit(9)"


class EchoHarness(HarnessAgent):
    """Concrete double executing real Python snippets via the engine."""

    name = "echo_test"
    capabilities = frozenset(
        {
            HarnessCapability.READ_ONLY_ACCESS,
            HarnessCapability.WORKSPACE_WRITE,
            HarnessCapability.NETWORK_ACCESS,
        }
    )

    def invocation_argv(self, resolved):
        return (resolved.command, "-c", _ECHO_SCRIPT)


def _agent(
    tmp_path: Path,
    cls: type[HarnessAgent] = EchoHarness,
    *,
    profile: HarnessAgentConfig | None = None,
) -> HarnessAgent:
    profile = profile or HarnessAgentConfig(executable_path=str(PY), timeout_seconds=20)
    return cls(
        settings=AgentSettings(adapter="echo_test"),
        profile=profile,
        workspace_root=tmp_path,
    )


class TestGrants:
    def test_profile_grant_beats_adapter_default(self, tmp_path):
        profile = HarnessAgentConfig(
            executable_path=str(PY),
            grant=ExecutionGrantKind.WORKSPACE_WRITE,
        )
        agent = _agent(tmp_path, profile=profile)
        assert agent.resolve_grant().kind is ExecutionGrantKind.WORKSPACE_WRITE

    def test_missing_everywhere_raises_typed_error_before_spawn(self, tmp_path):
        class NoDefault(EchoHarness):
            name = "no_default"
            default_grant = None

        agent = _agent(tmp_path, NoDefault, profile=HarnessAgentConfig(executable_path=str(PY)))
        with pytest.raises(MissingExecutionGrantError) as excinfo:
            agent.resolve_grant()
        assert excinfo.value.args[0].startswith("no_default:")

    def test_g0_no_process_spawns_when_grant_missing(self, tmp_path, monkeypatch):
        """R1#2 proof: typed failure occurs before any spawn attempt."""
        spawned = []

        import relay.harness.process as process_module

        original = process_module.execute

        async def _spy(spec):
            spawned.append(spec.argv)
            return await original(spec)

        monkeypatch.setattr(process_module, "execute", _spy)

        class NoDefault(EchoHarness):
            name = "no_default2"
            default_grant = None

        agent = _agent(tmp_path, NoDefault, profile=HarnessAgentConfig(executable_path=str(PY)))
        with pytest.raises(MissingExecutionGrantError):
            __import__("asyncio").run(
                agent.run(AgentRequest(prompt="x", role=AgentRole.RESEARCHER))
            )
        assert spawned == []


class TestCapabilityGating:
    def test_workspace_grant_requires_declared_capability(self, tmp_path):
        class ReadOnlyAdapter(EchoHarness):
            name = "ro_adapter"
            capabilities = frozenset({HarnessCapability.READ_ONLY_ACCESS})

        agent = _agent(tmp_path, ReadOnlyAdapter)
        grant = agent.resolve_grant(ExecutionGrantKind.WORKSPACE_WRITE)
        with pytest.raises(UnsupportedCapability):
            agent._check_grant_capabilities(grant)

    def test_network_grant_requires_network_capability(self, tmp_path):
        class NoNet(EchoHarness):
            name = "no_net"
            capabilities = frozenset(
                {HarnessCapability.READ_ONLY_ACCESS, HarnessCapability.WORKSPACE_WRITE}
            )

        agent = _agent(tmp_path, NoNet)
        grant = agent.resolve_grant(ExecutionGrantKind.WORKSPACE_WRITE_NETWORK)
        with pytest.raises(UnsupportedCapability):
            agent._check_grant_capabilities(grant)

    def test_read_only_needs_read_only_declaration(self, tmp_path):
        class Blind(EchoHarness):
            name = "blind"
            capabilities = frozenset()

        agent = _agent(tmp_path, Blind)
        with pytest.raises(UnsupportedCapability):
            agent._check_grant_capabilities(agent.resolve_grant())


class TestArgvOrdering:
    def test_canonical_order_invocation_profile_then_grant(self, tmp_path):
        agent = _agent(
            tmp_path,
            profile=HarnessAgentConfig(
                executable_path=str(PY),
                extra_args=["--profile-flag"],
                grant=ExecutionGrantKind.WORKSPACE_WRITE,
            ),
        )
        resolved = ResolvedExecutable(command=PY, source="explicit_path")
        grant = ExecutionGrant(
            kind=ExecutionGrantKind.WORKSPACE_WRITE,
            additional_args=("--grant-flag", "value"),
        )
        argv = agent.compose_argv(resolved, grant)
        # Deterministic contract: invocation · profile.extra_args · grant flags;
        # the prompt NEVER appears in argv (stdin channel instead).
        assert argv == (PY, "-c", _ECHO_SCRIPT, "--profile-flag", "--grant-flag", "value")


class TestRunIntegration:
    async def test_happy_path_prompt_over_stdin(self, tmp_path):
        response = await _agent(tmp_path).run(
            AgentRequest(prompt="relay-prompt-echo", role=AgentRole.IMPLEMENTER)
        )
        assert response.output == "relay-prompt-echo"
        assert response.agent == "echo_test"
        assert response.role is AgentRole.IMPLEMENTER

    async def test_timeout_maps_to_typed_sanitized_error(self, tmp_path):
        class TimeoutEcho(EchoHarness):
            name = "timeout_test"

            def invocation_argv(self, resolved):
                return (resolved.command, "-c", _SLEEP_SCRIPT)

        agent = _agent(
            tmp_path,
            TimeoutEcho,
            profile=HarnessAgentConfig(executable_path=str(PY), timeout_seconds=0.5),
        )
        with pytest.raises(HarnessTimeoutError) as excinfo:
            await agent.run(AgentRequest(prompt="x", role=AgentRole.RESEARCHER))
        message = str(excinfo.value)
        assert "exceeded" in message and "terminated" in message
        assert "time.sleep" not in message  # internal code never leaks

    async def test_auth_exit_semantics_hint_with_redaction(self, tmp_path):
        class AuthExiter(EchoHarness):
            name = "auth_exiter"

            def invocation_argv(self, resolved):
                return (resolved.command, "-c", _AUTH_FAIL_SCRIPT)

            def classify_exit(self, exit_code):
                return {9: ExitSemantics.AUTH}.get(exit_code, super().classify_exit(exit_code))

        agent = _agent(tmp_path, AuthExiter)
        with pytest.raises(HarnessOutputError) as excinfo:
            await agent.run(AgentRequest(prompt="x", role=AgentRole.RESEARCHER))
        message = str(excinfo.value)
        assert "authentication failed" in message
        assert "supersecret123" not in message  # redacted (credential-shaped)


class TestErrorBoundaryTable:
    def test_translate_table_membership_and_sanitization(self):
        cases = [
            (FileNotFoundError("classified /home/secrets/nope"), HarnessLaunchError),
            (PermissionError("denied"), HarnessLaunchError),
            (UnicodeDecodeError("utf-8", b"", 0, 1, "bad"), HarnessOutputError),
            (TimeoutError(), HarnessTimeoutError),
            (RuntimeError("mystery internals"), AgentError),
        ]
        for raw, expected_type in cases:
            translated = HarnessAgent._translate_exception("fake", raw)
            assert isinstance(translated, expected_type)
            # Sanitized messages never embed non-empty raw exception text.
            if str(raw):
                assert str(raw) not in str(translated)

    async def test_raw_parser_exception_cannot_escape_run(self, tmp_path):
        class BrokenParser(EchoHarness):
            name = "broken_parser"

            def parse_output(self, stdout_text, stderr_text):
                raise ValueError("raw internals: secret-value-42")

        agent = _agent(tmp_path, BrokenParser)
        with pytest.raises(AgentError) as excinfo:
            await agent.run(AgentRequest(prompt="x", role=AgentRole.RESEARCHER))
        message = str(excinfo.value)
        assert "failed unexpectedly" in message
        assert "ValueError" not in message
        assert "secret-value-42" not in message


class TestBackendDeclaration:
    def test_backend_is_harness_on_the_base_itself(self):
        from relay.agents.base import BackendType

        assert HarnessAgent.backend is BackendType.HARNESS

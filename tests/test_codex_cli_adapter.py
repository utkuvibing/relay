"""CodexCLIAdapter (P2.2) — unit + offline conformance battery membership.

The adapter's *profile* (capabilities, grant translation, conflict/self env,
failure vocabulary, JSONL parser) is exercised through the identical offline
battery the P2.1 fakes run: an embedded codex-shaped child script stands in
for the vendor binary (offline discipline — mirrors OpenAI MockTransport).
"""

from __future__ import annotations

import json

import pytest

from relay.agents.base import AgentRequest, AgentRole, BackendType
from relay.agents.codex_cli import CodexCLIAdapter
from relay.agents.config import AgentSettings
from relay.agents.registry import AGENTS, get_agent_class
from relay.context.config import HarnessAgentConfig
from relay.harness.capabilities import ALL_CAPABILITIES, HarnessCapability
from relay.harness.conformance import default_factory_for, run_battery
from relay.harness.env_policy import DEFAULT_CONFLICT_VARIABLES, build_child_env
from relay.harness.errors import HarnessOutputError
from relay.harness.runtime import HarnessAgent
from relay.harness.types import ExecutionGrantKind, ExitSemantics

# ---------------------------------------------------------------------------
# Vocabulary / declaration surface
# ---------------------------------------------------------------------------


class TestDeclaration:
    def test_registered_under_canonical_and_alias_names(self):
        assert get_agent_class("codex_cli") is CodexCLIAdapter
        assert get_agent_class("codex") is CodexCLIAdapter
        assert AGENTS["codex_cli"] is CodexCLIAdapter

    def test_backend_is_harness_family(self):
        assert CodexCLIAdapter.backend is BackendType.HARNESS
        assert issubclass(CodexCLIAdapter, HarnessAgent)

    def test_capabilities_subset_of_frozen_vocabulary(self):
        declared = CodexCLIAdapter({}).capabilities_set()
        assert declared <= set(ALL_CAPABILITIES)
        # Honest-declaration posture: no approval mediation exists on exec;
        # diffs are extracted Relay-side; resume seam unconsumed.
        assert HarnessCapability.APPROVAL_EVENT_STREAM not in declared
        assert HarnessCapability.DIFF_REPORTING not in declared
        assert HarnessCapability.SESSION_RESUME not in declared
        assert HarnessCapability.RESOLVED_MODEL_REPORTING not in declared
        # The C.5 tiers this adapter translates must be declared.
        assert HarnessCapability.READ_ONLY_ACCESS in declared
        assert HarnessCapability.WORKSPACE_WRITE in declared

    def test_default_grant_is_read_only(self):
        agent = CodexCLIAdapter({})
        grant = agent.resolve_grant(None)
        assert grant.kind is ExecutionGrantKind.READ_ONLY_ACCESS


class TestGrantTranslation:
    def test_read_only_maps_to_sandbox_read_only(self):
        agent = CodexCLIAdapter({})
        assert agent.grant_arguments(ExecutionGrantKind.READ_ONLY_ACCESS) == (
            "--sandbox",
            "read-only",
        )

    def test_workspace_write_maps_to_sandbox_workspace_write(self):
        agent = CodexCLIAdapter({})
        assert agent.grant_arguments(ExecutionGrantKind.WORKSPACE_WRITE) == (
            "--sandbox",
            "workspace-write",
        )

    def test_network_tier_adds_config_toggle(self):
        agent = CodexCLIAdapter({})
        args = agent.grant_arguments(ExecutionGrantKind.WORKSPACE_WRITE_NETWORK)
        assert ("--sandbox", "workspace-write") == tuple(args[:2])
        assert "sandbox_workspace_write.network_access=true" in " ".join(args)

    def test_grant_flags_flow_into_compose_argv_tail(self):
        agent = CodexCLIAdapter(AgentSettings(adapter="codex_cli"))
        resolved = _fake_resolved()
        argv = agent.compose_argv(resolved, agent.resolve_grant())
        assert argv[:4] == (resolved.command, "exec", "--json", "-")
        assert argv[-2:] == ("--sandbox", "read-only")


class TestInvocation:
    def test_prompt_never_rides_argv_and_stdin_marker_present(self):
        agent = CodexCLIAdapter(AgentSettings(adapter="codex_cli"))
        argv = agent.invocation_argv(_fake_resolved())
        assert argv == ("codex.exe", "exec", "--json", "-")
        assert not any("secret-prompt" in part for part in argv)

    def test_model_selection_rides_argv_when_requested(self):
        settings = AgentSettings(adapter="codex_cli", model="gpt-5.6-codex")
        agent = CodexCLIAdapter(settings)
        argv = agent.invocation_argv(_fake_resolved())
        assert ("--model", "gpt-5.6-codex") == tuple(argv[-2:])

    def test_profile_extra_args_insert_before_grant_flags(self):
        profile = HarnessAgentConfig(extra_args=["--profile", "relay-ci"])
        agent = CodexCLIAdapter(
            AgentSettings(adapter="codex_cli"),
            profile=profile,
            workspace_root=".",
        )
        argv = agent.compose_argv(
            _fake_resolved(), agent.resolve_grant(ExecutionGrantKind.WORKSPACE_WRITE)
        )
        assert argv.index("--profile") > argv.index("-")
        assert argv[-2:] == ("--sandbox", "workspace-write")


# ---------------------------------------------------------------------------
# Environment policy (C.4)
# ---------------------------------------------------------------------------


class TestEnvironmentPolicy:
    def test_codex_key_route_is_in_the_conflict_union(self):
        union = CodexCLIAdapter({}).conflict_variables()
        assert {"CODEX_API_KEY"} <= union
        assert DEFAULT_CONFLICT_VARIABLES <= union

    def test_codex_home_self_whitelist_applies_to_this_adapter_only(self):
        parent = {
            "PATH": "/usr/bin",
            "CODEX_HOME": r"C:\users\u\.codex",
            "OPENAI_API_KEY": "sk-parent-key",
            "ANTHROPIC_API_KEY": "anti-key",
        }
        codex_env = build_child_env(
            parent,
            conflict_variables=CodexCLIAdapter({}).conflict_variables(),
            self_allowed=CodexCLIAdapter.self_allowed_env,
        )
        assert codex_env["CODEX_HOME"] == r"C:\users\u\.codex"
        assert "OPENAI_API_KEY" not in codex_env
        assert "ANTHROPIC_API_KEY" not in codex_env

    def test_other_adapters_still_have_codex_home_stripped(self):
        """The self-whitelist must not leak into OTHER adapters' children."""
        other = build_child_env(
            {"PATH": "/usr/bin", "CODEX_HOME": r"C:\users\u\.codex"},
            conflict_variables=CodexCLIAdapter({}).conflict_variables(),
            self_allowed=frozenset(),  # a different adapter whitelists nothing
        )
        assert "CODEX_HOME" not in other


# ---------------------------------------------------------------------------
# Structured output parsing (exec --json)
# ---------------------------------------------------------------------------


def _event_stream() -> str:
    return "\n".join(
        [
            json.dumps({"type": "thread.started", "thread_id": "0199a213-thread"}),
            json.dumps({"type": "turn.started"}),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"id": "item_1", "type": "command_execution"},
                }
            ),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"id": "item_2", "type": "agent_message", "text": "final answer here"},
                }
            ),
            json.dumps(
                {
                    "type": "turn.completed",
                    "usage": {"input_tokens": 24763, "output_tokens": 122},
                }
            ),
        ]
    )


class TestParseOutput:
    def test_final_agent_message_wins_and_usage_captured(self):
        agent = CodexCLIAdapter({})
        output = agent.parse_output(_event_stream(), "")
        assert output == "final answer here"
        usage = agent.response_usage()
        assert usage is not None
        assert usage.input_tokens == 24763
        assert usage.output_tokens == 122
        assert usage.cost_usd is None  # subscription billing: never priced

    def test_unknown_event_types_are_tolerated(self):
        agent = CodexCLIAdapter({})
        stream = "\n".join(
            [
                json.dumps({"type": "thread.started", "thread_id": "t"}),
                json.dumps({"type": "future.event", "payload": 1}),
                json.dumps({"type": "item.completed", "item": {"id": "i", "type": "web_search"}}),
                json.dumps({"type": "item.completed", "item": {"id": "f", "type": "agent_message", "text": "ok"}}),
                json.dumps({"type": "turn.completed"}),
            ]
        )
        assert agent.parse_output(stream, "") == "ok"

    def test_error_event_fails_typed(self):
        agent = CodexCLIAdapter({})
        stream = "\n".join(
            [
                json.dumps({"type": "thread.started"}),
                json.dumps({"type": "error", "message": "usage limit exceeded"}),
            ]
        )
        with pytest.raises(HarnessOutputError):
            agent.parse_output(stream, "")

    def test_empty_stream_fails_typed(self):
        agent = CodexCLIAdapter({})
        with pytest.raises(HarnessOutputError):
            agent.parse_output("", "")

    def test_events_without_final_message_fail_typed(self):
        agent = CodexCLIAdapter({})
        stream = json.dumps({"type": "turn.started"})
        with pytest.raises(HarnessOutputError):
            agent.parse_output(stream, "")

    def test_malformed_line_fails_typed_without_raw_leak(self):
        agent = CodexCLIAdapter({})
        with pytest.raises(HarnessOutputError, match="could not be decoded"):
            agent.parse_output('{"type": BROKEN', "")
        try:
            agent.parse_output('{"type": BROKEN sk-live-oops', "")
        except HarnessOutputError as exc:
            assert "sk-live-oops" not in str(exc)

    def test_non_dict_json_line_fails_typed(self):
        agent = CodexCLIAdapter({})
        with pytest.raises(HarnessOutputError):
            agent.parse_output("[1, 2, 3]", "")

    def test_stderr_cred_shapes_do_not_ride_into_success_output(self):
        # parse_output consumes stdout only; stderr noise never joins output.
        agent = CodexCLIAdapter({})
        assert agent.parse_output(_event_stream(), "OPENAI_API_KEY=leak-attempt") == (
            "final answer here"
        )


# ---------------------------------------------------------------------------
# Offline conformance battery via codex-shaped child (no vendor binary)
# ---------------------------------------------------------------------------


_CODEX_SRC = r'''
import json, os, sys
data = sys.stdin.read()
if "--version" in sys.argv:
    print("codex-cli 0.55.0"); sys.exit(0)
if data.startswith("#ENVCHECK#"):
    names = [line[5:] for line in data.splitlines() if line.startswith("NAME=")]
    print(json.dumps({"type": "thread.started", "thread_id": "envprobe"}))
    print(json.dumps({
        "type": "item.completed",
        "item": {"id": "e", "type": "agent_message", "text":
                 "; ".join(n + "=" + os.environ.get(n, "__MISSING__") for n in names)},
    }))
    print(json.dumps({"type": "turn.completed"}))
    sys.exit(0)
mode = sys.argv[sys.argv.index("--mode") + 1] if "--mode" in sys.argv else ""
if mode == "relay-conformance-usage":
    print("CODEX_CONFORMANCE=usage-fodder", file=sys.stderr); sys.exit(3)
if mode == "relay-conformance-auth":
    print("CODEX_CONFORMANCE=auth-leak-attempt", file=sys.stderr); sys.exit(4)
if mode == "malfault":
    print('{"type": BROKEN')
    sys.exit(0)
first_line = data.strip().splitlines()[0] if data.strip() else "empty"
print(json.dumps({"type": "thread.started", "thread_id": "abc"}))
print(json.dumps({"type": "item.completed",
                  "item": {"id": "m", "type": "agent_message",
                           "text": "codex-ok echo=%s cwd=%s" % (first_line, os.getcwd())}}))
print(json.dumps({"type": "turn.completed",
                  "usage": {"input_tokens": 10, "output_tokens": 5}}))
'''


class _CodexHooks:
    """Instance-level carrier for CodexCLIAdapter's own instance state."""

    def __init__(self, name: str, settings):
        self.name = name
        self._settings = settings
        self._info = None
        self._last_input_tokens = None
        self._last_output_tokens = None
        self._last_thread_id = None


class CodexShapedFixture(HarnessAgent):
    """Offline stand-in binding the REAL adapter's hooks to a local child.

    Deliberately reuses CodexCLIAdapter's declarations and parser (not
    copies), so a green battery certifies the shipping profile itself.
    """

    name = "codex_conformance_fixture"
    capabilities = CodexCLIAdapter.capabilities
    extra_conflict_variables = CodexCLIAdapter.extra_conflict_variables
    self_allowed_env = CodexCLIAdapter.self_allowed_env

    #: The fixture advertises its OWN fault vocabulary (its child script has
    #: deterministic numerics), exercising B05 against this profile. The real
    #: adapter ships failure_modes=() — see its class comment.
    failure_modes = (
        ("relay-conformance-usage", "harness exited abnormally"),
        ("relay-conformance-auth", "harness exited abnormally"),
    )

    def __init__(self, settings=None, *, profile=None, workspace_root=None):
        super().__init__(
            settings=settings, profile=profile, workspace_root=workspace_root
        )
        self.__hooks = _CodexHooks(self.name, self._settings)

    def invocation_argv(self, resolved):
        return (resolved.command, "-c", _CODEX_SRC)

    def classify_exit(self, exit_code):
        return CodexCLIAdapter.classify_exit(self.__hooks, exit_code)

    def grant_arguments(self, kind):
        return CodexCLIAdapter.grant_arguments(self.__hooks, kind)

    def parse_output(self, stdout_text, stderr_text):
        result = CodexCLIAdapter.parse_output(self.__hooks, stdout_text, stderr_text)
        self._last_input_tokens = self.__hooks._last_input_tokens
        self._last_output_tokens = self.__hooks._last_output_tokens
        self._last_thread_id = self.__hooks._last_thread_id
        return result

    def response_usage(self):
        if getattr(self, "_last_input_tokens", None) is None and (
            getattr(self, "_last_output_tokens", None) is None
        ):
            return None
        from relay.agents.base import TokenUsage

        return TokenUsage(
            input_tokens=self._last_input_tokens,
            output_tokens=self._last_output_tokens,
            cost_usd=None,
        )

    def run_observation(self):
        from relay.agents.base import RunObservation

        info = getattr(self, "_info", None)
        return RunObservation(
            resolved_model=self._settings.model,
            adapter_version=info.version if info else None,
            backend="harness",
        )


def _fake_resolved():
    from relay.harness.discovery import ResolvedExecutable

    return ResolvedExecutable(command="codex.exe", source="explicit_path")


# ---------------------------------------------------------------------------
# Offline conformance battery via codex-shaped child (no vendor binary)
# ---------------------------------------------------------------------------


def test_full_battery_on_codex_shaped_fixture(tmp_path):
    report = run_battery(default_factory_for(CodexShapedFixture), tmp_path)
    if not report.passed:
        pytest.fail("codex fixture failed conformance:\n" + report.summary())


def test_battery_rejects_a_broken_codex_profile(tmp_path):
    class LiarFixture(CodexShapedFixture):
        name = "codex_liar_fixture"

        def classify_exit(self, exit_code):
            return ExitSemantics.OK  # lies about failures → B05 catches it

    report = run_battery(default_factory_for(LiarFixture), tmp_path)
    assert not report.passed
    assert any(name.startswith("B05") for name in {c.name for c in report.failures()})


class TestRegistryHygiene:
    def test_conformance_fixture_never_enters_production_registry(self):
        assert "codex_conformance_fixture" not in AGENTS
        assert "codex_cli" in AGENTS and "codex" in AGENTS


# ---------------------------------------------------------------------------
# Live smoke — doubly gated, never runs offline or in CI (no secrets wired)
# ---------------------------------------------------------------------------


def _live_codex_available() -> bool:
    import os
    import shutil

    return bool(
        os.environ.get("RELAY_RUN_LIVE_TESTS") == "1"
        and (shutil.which("codex") or os.environ.get("RELAY_CODEX_PATH"))
    )


@pytest.mark.skipif(
    not _live_codex_available(),
    reason="live harness test: set RELAY_RUN_LIVE_TESTS=1 with a logged-in codex on PATH",
)
@pytest.mark.asyncio
async def test_live_codex_read_only_smoke(tmp_path):
    """Manual exit-gate companion; one trivial read-only ask."""
    profile = HarnessAgentConfig(
        executable_path=None,  # discover from PATH
        timeout_seconds=120.0,
        grant=ExecutionGrantKind.READ_ONLY_ACCESS,
    )
    agent = CodexCLIAdapter(
        AgentSettings(adapter="codex_cli"),
        profile=profile,
        workspace_root=tmp_path,
    )
    info = await agent.discover()
    assert info.version  # discovered + probed
    response = await agent.run(
        AgentRequest(prompt="Reply with exactly: RELAY_SMOKE_OK", role=AgentRole.RESEARCHER)
    )
    assert response.status == "ok"
    assert response.output

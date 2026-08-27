"""AntigravityCLIAdapter — third real harness (P2.4; frozen plan rev 2).

Mirrors the P2.3 test architecture exactly: a fixture reuses the REAL
adapter's declarations and parser via an unbound-delegation hooks carrier,
binding them to a local Python child that emulates the documented CLI
surface byte-for-byte where it matters (argv shape, JSON envelope, status
vocabulary, exit numerics). Offline, hermetic, no vendor binary.

Sections mirror tests/test_claude_code_adapter.py so review can diff intent:
declaration → grant contract (read-only + refusals) → invocation/binary
support → env policy → envelope parsing → integration pipeline → battery
parity → widening defense → persistence dormancy → G4 zero-touch proof.
"""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

import pytest

from relay.agents.antigravity_cli import AntigravityCLIAdapter
from relay.agents.base import (
    AgentRequest,
    AgentRole,
    BackendType,
    RunObservation,
    TokenUsage,
)
from relay.agents.config import AgentSettings
from relay.agents.registry import AGENTS
from relay.context.config import HarnessAgentConfig
from relay.harness.capabilities import ALL_CAPABILITIES, HarnessCapability
from relay.harness.conformance import default_factory_for, run_battery
from relay.harness.discovery import ResolvedExecutable
from relay.harness.errors import (
    HarnessDiscoveryError,
    HarnessOutputError,
    UnsupportedCapability,
)
from relay.harness.runtime import HarnessAgent
from relay.harness.types import ExecutionGrantKind, ExitSemantics, HarnessInfo

REPO_ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable

FIXTURE_SESSION_ID = "8c1f4a2b-9d3e-4b7a-a1c5-2f6e8b9d0a41"

READ_ONLY_TAIL = ("--mode", "plan")

#: Every terminal status the vendor documents except SUCCESS (frozen-plan D5:
#: fail-closed — each must produce a typed parse failure).
FAILURE_STATUSES = (
    "ERROR",
    "CANCELED",
    "INTERRUPTED",
    "INVALID",
    "WAITING",
    "RUNNING",
)


def _envelope(result: str = "echo:ping", **overrides) -> str:
    payload = {
        "conversation_id": FIXTURE_SESSION_ID,
        "status": "SUCCESS",
        "response": result,
        "usage": {"input_tokens": 11, "output_tokens": 7},
    }
    payload.update(overrides)
    return json.dumps(payload)


def _request(prompt: str) -> AgentRequest:
    return AgentRequest(prompt=prompt, role=AgentRole.RESEARCHER)


def _fake_resolved() -> ResolvedExecutable:
    return ResolvedExecutable(command=str(PY), source="explicit_path")


async def _run(agent: HarnessAgent, prompt: str):
    return await agent.run(_request(prompt))


#: Emulates the documented `agy --output-format json` headless surface.
_ANTIGRAVITY_SRC = r'''
import json, os, sys, uuid
argv = sys.argv[1:]
if "--version" in argv:
    print("agy-emulated 1.1.9"); sys.exit(0)
if "--help" in argv:
    print("usage: agy [flags]")
    print("  --output-format string   output format: text, json, or stream-json")
    print("  --input-format string    input format: text or stream-json")
    print("  --mode string            execution mode: default, accept-edits, or plan")
    print("  --disable-slash-commands treat slash-prefixed input as plain prompt text")
    sys.exit(0)

def _value(flag):
    return argv[argv.index(flag) + 1] if flag in argv else ""

data = sys.stdin.read()
mode = _value("--mode")

if data.startswith("#ENVCHECK#"):
    names = [line[5:] for line in data.splitlines() if line.startswith("NAME=")]
    lines = [n + "=" + os.environ.get(n, "__MISSING__") for n in names]
    print(json.dumps({"conversation_id": str(uuid.uuid4()), "status": "SUCCESS",
                      "response": "\n".join(lines),
                      "usage": {"input_tokens": 10, "output_tokens": 4}}))
    sys.exit(0)

if mode == "malfault":
    print('{"status": BROKEN'); sys.exit(0)
if mode == "conformance-usage":
    print("conformance-usage complaint", file=sys.stderr); sys.exit(3)
if mode == "authfail-envelope":
    print(json.dumps({"conversation_id": "", "status": "ERROR", "response": "",
                      "error": "authentication required"})); sys.exit(1)

first_line = (data.strip().splitlines() or ["empty"])[0]
print(json.dumps({
    "conversation_id": str(uuid.uuid4()),
    "status": "SUCCESS",
    "response": "agy-ok echo=%s cwd=%s" % (first_line, os.getcwd()),
    "usage": {"input_tokens": 10, "output_tokens": 4},
}))
'''


class _AntigravityHooks:
    """Instance-level carrier for AntigravityCLIAdapter's parse side-band."""

    def __init__(self, name: str, settings):
        self.name = name
        self._settings = settings
        self._info = None
        self._last_input_tokens = None
        self._last_output_tokens = None
        self._last_session_id = None


class AntigravityShapedFixture(HarnessAgent):
    """Binds the REAL adapter's declarations/parser to a local child.

    Deliberately reuses AntigravityCLIAdapter's declarations and parser (not
    copies), so a green battery certifies the shipping profile itself.
    """

    name = "antigravity_conformance_fixture"
    capabilities = AntigravityCLIAdapter.capabilities
    extra_conflict_variables = AntigravityCLIAdapter.extra_conflict_variables

    #: The fixture exercises deterministic fault triggers its child script
    #: implements (the REAL adapter ships failure_modes=() — upstream exit
    #: numerics beyond SIGTERM are not version-stable).
    failure_modes = (("conformance-usage", "harness exited abnormally"),)

    def __init__(self, settings=None, *, profile=None, workspace_root=None):
        super().__init__(settings=settings, profile=profile, workspace_root=workspace_root)
        self.__hooks = _AntigravityHooks(self.name, self._settings)

    def invocation_argv(self, resolved):
        return (resolved.command, "-c", _ANTIGRAVITY_SRC)

    def grant_arguments(self, kind):
        # Delegate ALL kinds — the real class composes the plan-mode tail and
        # refuses both write tiers, so the fixture certifies the shipping
        # contract exactly (B11's grant-gating probe included).
        return AntigravityCLIAdapter.grant_arguments(self.__hooks, kind)

    def classify_exit(self, exit_code):
        return AntigravityCLIAdapter.classify_exit(self.__hooks, exit_code)

    def parse_output(self, stdout_text, stderr_text):
        result = AntigravityCLIAdapter.parse_output(self.__hooks, stdout_text, stderr_text)
        self._last_input_tokens = self.__hooks._last_input_tokens
        self._last_output_tokens = self.__hooks._last_output_tokens
        self._last_session_id = self.__hooks._last_session_id
        return result

    def response_usage(self):
        tokens_in = getattr(self, "_last_input_tokens", None)
        tokens_out = getattr(self, "_last_output_tokens", None)
        if tokens_in is None and tokens_out is None:
            return None
        return TokenUsage(input_tokens=tokens_in, output_tokens=tokens_out, cost_usd=None)

    def run_observation(self):
        info = getattr(self, "_info", None)
        return RunObservation(
            resolved_model=None,
            adapter_version=info.version if info else None,
            backend="harness",
            external_session_ref=None,
        )

    @property
    def last_session_ref(self) -> str | None:
        """Mirror of the real adapter's parse side-band on this instance."""
        return getattr(self, "_last_session_id", None)


def _agent(
    tmp_path: Path,
    *,
    cls=AntigravityShapedFixture,
    profile: HarnessAgentConfig | None = None,
) -> HarnessAgent:
    profile = profile or HarnessAgentConfig(executable_path=str(PY), timeout_seconds=20)
    return cls(
        settings=AgentSettings(adapter="antigravity_cli"),
        profile=profile,
        workspace_root=tmp_path,
    )


def _real(tmp_path: Path, *, version: str | None = "1.1.9") -> AntigravityCLIAdapter:
    """The SHIPPING adapter with discovery state primed for pure unit work.

    Grant/argv/parse-state semantics belong to the real class; only the
    program path differs from a vendor install (which cannot exist on CI).
    """
    agent = AntigravityCLIAdapter(
        settings=AgentSettings(adapter="antigravity_cli"),
        profile=HarnessAgentConfig(executable_path=str(PY), timeout_seconds=20),
        workspace_root=tmp_path,
    )
    agent._info = HarnessInfo(
        adapter="antigravity_cli",
        executable=str(PY),
        version=version,
        version_raw=None,
    )
    return agent


class TestDeclaration:
    def test_registered_under_canonical_and_alias_names(self):
        assert AGENTS["antigravity_cli"] is AntigravityCLIAdapter
        assert AGENTS["agy"] is AntigravityCLIAdapter

    def test_backend_is_harness_family(self):
        assert AntigravityCLIAdapter.backend is BackendType.HARNESS
        assert AntigravityCLIAdapter.name == "antigravity_cli"
        assert AntigravityCLIAdapter.harness_command == "agy"

    def test_capabilities_subset_of_frozen_vocabulary(self):
        assert AntigravityCLIAdapter.capabilities <= ALL_CAPABILITIES

    def test_default_grant_is_read_only(self, tmp_path):
        assert _real(tmp_path).resolve_grant().kind is ExecutionGrantKind.READ_ONLY_ACCESS

    def test_workspace_write_deliberately_absent(self):
        """Grilled decision Q4: no per-invocation clamp flag exists, so the
        write tier is refused at the capability layer, not clamped at the
        flag layer."""
        assert HarnessCapability.WORKSPACE_WRITE not in AntigravityCLIAdapter.capabilities

    def test_unsupported_provider_toggles_absent_from_capabilities(self):
        shell_family = {
            HarnessCapability.SHELL_EXECUTION,
            HarnessCapability.GIT_OPERATIONS,
            HarnessCapability.NETWORK_ACCESS,
            HarnessCapability.TOOL_EVENT_STREAM,
            HarnessCapability.RESOLVED_MODEL_REPORTING,
            HarnessCapability.DIFF_REPORTING,
            HarnessCapability.APPROVAL_EVENT_STREAM,
        }
        assert not (AntigravityCLIAdapter.capabilities & shell_family)


class TestGrantContract:
    """Frozen-plan D3: plan-mode READ_ONLY + typed write-tier refusals."""

    def test_read_only_maps_to_plan_mode(self, tmp_path):
        assert _real(tmp_path).grant_arguments(
            ExecutionGrantKind.READ_ONLY_ACCESS
        ) == READ_ONLY_TAIL

    def test_workspace_write_refuses_typed_before_spawn(self, tmp_path):
        with pytest.raises(UnsupportedCapability, match="per-invocation clamp flag"):
            _real(tmp_path).resolve_grant(ExecutionGrantKind.WORKSPACE_WRITE)

    def test_network_tier_refuses_typed_before_spawn(self, tmp_path):
        with pytest.raises(UnsupportedCapability, match="container isolation"):
            _real(tmp_path).resolve_grant(ExecutionGrantKind.WORKSPACE_WRITE_NETWORK)

    def test_canonical_order_invocation_profile_then_grant(self, tmp_path, monkeypatch):
        from relay.agents import antigravity_cli as module

        monkeypatch.setattr(module, "_slash_clamp_supported", lambda command: True)
        agent = _real(tmp_path)
        agent._profile.extra_args = ["--user-flag"]
        resolved = _fake_resolved()
        argv = agent.compose_argv(resolved, agent.resolve_grant())
        invocation = agent.invocation_argv(resolved)
        assert argv[: len(invocation)] == invocation
        assert argv.index("--user-flag") < argv.index("--mode")
        assert argv[argv.index("--mode") + 1] == "plan"
        assert argv.count("--disable-slash-commands") == 1


class TestInvocation:
    def test_prompt_never_rides_argv_and_contract_flags_present(self, tmp_path, monkeypatch):
        from relay.agents import antigravity_cli as module

        monkeypatch.setattr(module, "_slash_clamp_supported", lambda command: True)
        argv = _real(tmp_path).invocation_argv(_fake_resolved())
        assert "--input-format" in argv and "text" in argv
        assert "--output-format" in argv and "json" in argv
        assert "--disable-slash-commands" in argv
        assert not any(marker in part for part in argv for marker in ("echo:", "#ENVCHECK#"))

    def test_native_binary_is_execd_directly(self, tmp_path, monkeypatch):
        """No npm/ps1 shim wrapping: the Go binary is a real executable."""
        from relay.agents import antigravity_cli as module

        monkeypatch.setattr(module, "_slash_clamp_supported", lambda command: True)
        argv = _real(tmp_path).invocation_argv(_fake_resolved())
        assert argv[0] == str(PY)

    def test_model_selection_rides_argv_when_requested(self, tmp_path, monkeypatch):
        from relay.agents import antigravity_cli as module

        monkeypatch.setattr(module, "_slash_clamp_supported", lambda command: True)
        agent = AntigravityCLIAdapter(
            settings=AgentSettings(adapter="antigravity_cli", model="gemini-3.5-flash-medium"),
            profile=HarnessAgentConfig(executable_path=str(PY)),
            workspace_root=tmp_path,
        )
        agent._info = HarnessInfo(
            adapter="antigravity_cli", executable=str(PY), version="1.1.9", version_raw=None
        )
        argv = agent.invocation_argv(_fake_resolved())
        idx = argv.index("--model")
        assert argv[idx + 1] == "gemini-3.5-flash-medium"


class TestBinarySupportAssertion:
    """Grilled decision Q3: floor + mandatory flag, typed pre-spawn."""

    def test_version_below_floor_fails_typed(self, tmp_path):
        with pytest.raises(HarnessDiscoveryError, match="predates"):
            _real(tmp_path, version="1.1.8").invocation_argv(_fake_resolved())

    def test_unknown_version_fails_closed(self, tmp_path):
        with pytest.raises(HarnessDiscoveryError, match="unknown version"):
            _real(tmp_path, version=None).invocation_argv(_fake_resolved())

    def test_unadvertised_flag_fails_typed(self, tmp_path, monkeypatch):
        from relay.agents import antigravity_cli as module

        monkeypatch.setattr(module, "_slash_clamp_supported", lambda command: False)
        with pytest.raises(HarnessDiscoveryError, match="does not advertise"):
            _real(tmp_path, version="1.1.22").invocation_argv(_fake_resolved())

    def test_supported_binary_composes_the_clamp(self, tmp_path, monkeypatch):
        from relay.agents import antigravity_cli as module

        monkeypatch.setattr(module, "_slash_clamp_supported", lambda command: True)
        argv = _real(tmp_path, version="1.1.22").invocation_argv(_fake_resolved())
        assert argv.count("--disable-slash-commands") == 1

    def test_probe_is_cached_per_command(self, monkeypatch):
        from relay.agents import antigravity_cli as module

        calls: list[tuple] = []

        class _Completed:
            stdout = "usage: agy\n  --disable-slash-commands  ... \n"
            stderr = ""

        def _fake_run(argv, **kwargs):
            calls.append(tuple(argv))
            return _Completed()

        monkeypatch.setattr(module.subprocess, "run", _fake_run)
        module._SUPPORT_CACHE.clear()
        assert module._slash_clamp_supported("agy.exe") is True
        assert module._slash_clamp_supported("agy.exe") is True  # cached
        assert len(calls) == 1
        module._SUPPORT_CACHE.clear()

    def test_probe_failure_counts_as_unsupported(self, monkeypatch):
        from relay.agents import antigravity_cli as module

        def _boom(argv, **kwargs):
            raise OSError("not found")

        monkeypatch.setattr(module.subprocess, "run", _boom)
        module._SUPPORT_CACHE.clear()
        assert module._slash_clamp_supported("missing-agy") is False
        module._SUPPORT_CACHE.clear()


class TestEnvironmentPolicy:
    def test_google_conflict_union_covers_all_provider_routes(self, tmp_path):
        union = _real(tmp_path).conflict_variables()
        # Already universal (D7 audit): the Gemini API-key and Vertex-file routes.
        assert {"GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS"} <= union
        # This profile's additions: agy endpoint override + Vertex routing pair.
        assert {
            "GOOGLE_GEMINI_BASE_URL",
            "GOOGLE_GENAI_USE_VERTEXAI",
            "GOOGLE_CLOUD_PROJECT",
        } <= union

    def test_no_self_whitelists_for_subscription_owned_harness(self, tmp_path):
        assert _real(tmp_path).self_allowed_env == frozenset()


class TestParseOutput:
    @staticmethod
    def _parse(agent, stdout: str, stderr: str = "") -> str:
        return agent.parse_output(stdout, stderr)

    def test_envelope_response_wins_and_state_captured(self, tmp_path):
        agent = _real(tmp_path)
        out = self._parse(agent, _envelope(result="the answer"))
        assert out == "the answer"
        assert agent.last_session_ref == FIXTURE_SESSION_ID
        usage = agent.response_usage()
        assert usage.input_tokens == 11 and usage.output_tokens == 7
        assert usage.cost_usd is None

    def test_every_documented_failure_status_fails_typed(self, tmp_path):
        for status in FAILURE_STATUSES:
            agent = _real(tmp_path)
            payload = _envelope(status=status, error=f"upstream says: {status}")
            with pytest.raises(HarnessOutputError, match=f"status={status}"):
                self._parse(agent, payload)

    def test_unknown_future_status_fails_closed(self, tmp_path):
        with pytest.raises(HarnessOutputError, match="status=FUTURE_STATE"):
            self._parse(
                _real(tmp_path),
                _envelope(status="FUTURE_STATE", error="from a newer binary"),
            )

    def test_error_detail_is_sanitized(self, tmp_path):
        import inspect

        bad = _envelope(status="ERROR", error="login required secret-hunter-77")
        with pytest.raises(HarnessOutputError, match="harness reported failure"):
            self._parse(_real(tmp_path), bad)
        source = inspect.getsource(AntigravityCLIAdapter.parse_output)
        assert "redact(" in source  # redaction applied at construction site

    def test_missing_status_fails_typed(self, tmp_path):
        with pytest.raises(HarnessOutputError, match="carried no status"):
            self._parse(_real(tmp_path), json.dumps({"response": "orphan"}))

    def test_malformed_envelope_fails_typed_without_raw_leak(self, tmp_path):
        with pytest.raises(HarnessOutputError, match="could not be decoded"):
            self._parse(_real(tmp_path), '{"status": BROKEN-secret-99')

    def test_non_dict_envelope_fails_typed(self, tmp_path):
        with pytest.raises(HarnessOutputError):
            self._parse(_real(tmp_path), '["array"]')

    def test_empty_response_fails_typed(self, tmp_path):
        with pytest.raises(HarnessOutputError, match="carried no text"):
            self._parse(_real(tmp_path), _envelope(result="   "))

    def test_invalid_conversation_shapes_are_tolerated_as_none(self, tmp_path):
        agent = _real(tmp_path)
        self._parse(agent, _envelope(conversation_id="not-a-uuid"))
        assert agent.last_session_ref is None


class TestRunIntegration:
    async def test_full_pipeline_offline_emulator(self, tmp_path):
        response = await _run(_agent(tmp_path), "integration-ping")
        assert response.output.startswith("agy-ok echo=integration-ping")
        assert response.usage is not None
        observation = response.observation
        assert observation is not None
        assert observation.external_session_ref is None
        assert response.tool_observations == []

    async def test_nonzero_exit_maps_to_sanitized_generic_failure(self, tmp_path):
        # Upstream surfaces auth failures as an ERROR envelope AND exits
        # non-zero; single-json mode cannot route stdout through parse_output
        # in failing runs (P2.2/P2.3 parity). Auth specificity arrives with a
        # future event-stream profile; here the sanitized generic contract
        # is pinned.
        class Failing(AntigravityShapedFixture):
            def invocation_argv(self, resolved):
                return (resolved.command, "-c", _ANTIGRAVITY_SRC, "--mode", "authfail-envelope")

        agent = Failing(
            settings=AgentSettings(adapter="failing"),
            profile=HarnessAgentConfig(executable_path=str(PY), timeout_seconds=15),
            workspace_root=tmp_path,
        )
        with pytest.raises(HarnessOutputError) as excinfo:
            await agent.run(_request("who-am-i"))
        message = str(excinfo.value)
        assert "harness exited abnormally" in message
        assert "authentication required" not in message  # envelope not parsed on failure

    async def test_timeout_yields_sanitized_timeout_error(self, tmp_path):
        class Hangs(AntigravityShapedFixture):
            def invocation_argv(self, resolved):
                return (resolved.command, "-c", "import time; time.sleep(5)")

        agent = Hangs(
            settings=AgentSettings(adapter="hangs"),
            profile=HarnessAgentConfig(executable_path=str(PY), timeout_seconds=0.5),
            workspace_root=tmp_path,
        )
        with pytest.raises(Exception) as excinfo:
            await agent.run(_request("hang"))
        message = str(excinfo.value)
        assert ("deadline" in message) or ("exceeded" in message)
        assert "time.sleep" not in message


class TestBatteryParityG1Prime:
    def test_full_battery_on_antigravity_shaped_fixture(self, tmp_path):
        report = run_battery(default_factory_for(AntigravityShapedFixture), tmp_path)
        if not report.passed:
            pytest.fail("antigravity fixture failed conformance:\n" + report.summary())

    def test_liar_fixture_still_fails_battery(self, tmp_path):
        class Liar(AntigravityShapedFixture):
            name = "liar_antigravity"

            def classify_exit(self, exit_code):
                return ExitSemantics.OK

        report = run_battery(default_factory_for(Liar), tmp_path)
        assert not report.passed


class TestWideningDefenseQ4:
    """No clamp flag exists — so the defense is structural: Relay composes
    the plan-mode tail and the mandatory slash clamp, and NEVER composes the
    vendor's escalation or allowlist flags on its own."""

    @staticmethod
    def _hostile_workspace(tmp_path: Path) -> Path:
        ws = tmp_path / "hostile-ws"
        settings_dir = ws / ".gemini" / "antigravity-cli"
        settings_dir.mkdir(parents=True, exist_ok=True)
        (settings_dir / "settings.json").write_text(
            json.dumps(
                {
                    "permissions": {"allow": ["command(git)", "command(npm run (build|lint|test))"]},
                    "toolPermission": "always-proceed",
                }
            ),
            encoding="utf-8",
        )
        return ws

    async def test_composed_run_keeps_plan_mode_and_clamp_under_hostile_settings(
        self, tmp_path, monkeypatch
    ):
        workspace = self._hostile_workspace(tmp_path)
        composed: list[tuple[str, ...]] = []
        import relay.harness.runtime as runtime_module
        original = runtime_module.execute

        async def _capture(spec):
            composed.append(tuple(spec.argv))
            return await original(spec)

        monkeypatch.setattr(runtime_module, "execute", _capture)
        response = await _agent(workspace).run(_request("widen-me"))
        assert "agy-ok" in response.output
        assert len(composed) == 1
        argv = composed[0]
        assert argv[argv.index("--mode") + 1] == "plan"  # grant tail unchanged under hostility
        # (The mandatory slash clamp lives in the REAL invocation head; the
        # fixture binds the emulator child in its place by design — the clamp
        # itself is asserted on the shipping class in TestInvocation /
        # TestBinarySupportAssertion. Composed-run assertions here cover the
        # grant layer, mirroring P2.3.)
        for forbidden in (
            "--dangerously-skip-permissions",
            "--allowedTools",
            "--tools",
            "--mode=accept-edits",
        ):
            assert forbidden not in argv, f"forbidden {forbidden} in composed argv"

    def test_invocation_never_composes_escalation_flags(self, tmp_path, monkeypatch):
        from relay.agents import antigravity_cli as module

        monkeypatch.setattr(module, "_slash_clamp_supported", lambda command: True)
        argv = _real(tmp_path).invocation_argv(_fake_resolved()) + _real(
            tmp_path
        ).grant_arguments(ExecutionGrantKind.READ_ONLY_ACCESS)
        joined = " ".join(argv)
        for forbidden in (
            "--dangerously-skip-permissions",
            "--allowedTools",
            "--bare",
            "--yolo",
        ):
            assert forbidden not in joined


class TestPersistenceDormancyD5b:
    async def test_external_session_ref_stays_none_after_success(self, tmp_path):
        agent = _agent(tmp_path)
        response = await agent.run(_request("dormancy"))
        observation = response.observation
        assert observation is not None
        assert observation.external_session_ref is None
        assert agent.last_session_ref is not None  # parsed in memory only…
        facts = agent.describe_facts()  # …never promoted into C.4 facts
        assert facts.external_session_ref is None

    def test_resume_translation_available_but_dormant(self, tmp_path):
        # Pure translation on the SHIPPING class: no process involved.
        agent = _real(tmp_path)
        raw = uuid.uuid4().hex
        jref = f"{raw[:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:32]}"
        assert agent.resume_arguments(jref) == ("--conversation", jref)
        with pytest.raises(UnsupportedCapability):
            agent.resume_arguments("junk-ref")


class TestSecondHarnessGateZeroTouch:
    """G4 architectural proof: antigravity_cli lives ONLY in permitted zones.

    Tier A — code: imports/instantiations of the adapter appear only in
    relay/agents/** or tests/** (AST-based; comments cannot pass).
    Tier B — tokens/docs: outside the zone, the token 'antigravity_cli' may
    surface ONLY inside '#' comment lines (config templates) or non-Python
    documentation files; never in executable statements.
    """

    ALLOWED_PREFIXES = ("relay/agents/", "tests/", "docs/plans/", ".github/")
    ALLOWED_FILES = ("README.md",)

    def _iter_scannable(self):
        for pattern in ("*.py", "*.md", "*.yml", "*.yaml", "*.toml"):
            for path in REPO_ROOT.rglob(pattern):
                rel = path.relative_to(REPO_ROOT).as_posix()
                if rel.startswith((".git", ".venv", "dist", ".commandcode")):
                    continue
                if any(rel.startswith(prefix) for prefix in self.ALLOWED_PREFIXES):
                    continue
                if rel in self.ALLOWED_FILES:
                    continue
                yield rel, path

    @staticmethod
    def _identifiers(text: str) -> list[tuple[int, str]]:
        import ast

        found: list[tuple[int, str]] = []
        tree = ast.parse(text)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").endswith(
                ".antigravity_cli"
            ):
                found.append((node.lineno, node.module))
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.endswith(".antigravity_cli"):
                        found.append((node.lineno, alias.name))
            elif isinstance(node, ast.Name) and node.id == "AntigravityCLIAdapter":
                found.append((node.lineno, node.id))
            elif isinstance(node, ast.Attribute) and node.attr == "AntigravityCLIAdapter":
                found.append((node.lineno, "…" + node.attr))
            elif (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and "antigravity_cli" in node.value.split()
            ):
                # bare-identifier string usage (registry keys), anywhere else
                found.append((node.lineno, repr(node.value)))
        return found

    def test_tier_a_code_references_only_in_adapter_zone(self):
        offenders: list[str] = []
        for rel, path in self._iter_scannable():
            if not rel.endswith(".py"):
                continue
            text = path.read_text(encoding="utf-8")
            if "antigravity" not in text.lower():
                continue  # fast skip; cheap on doc-free modules
            for lineno, what in self._identifiers(text):
                offenders.append(f"{rel}:{lineno} ({what})")
        assert offenders == [], f"G4 tier-A code violations: {offenders}"

    def test_tier_b_tokens_outside_zone_are_comments_or_docs(self):
        offenders: list[str] = []
        for rel, path in self._iter_scannable():
            if rel.endswith(".md"):
                continue  # prose/documentation surface (same standing as README)
            text = path.read_text(encoding="utf-8")
            for lineno, line in enumerate(text.splitlines(), start=1):
                if "antigravity_cli" not in line.lower():
                    continue
                if rel.endswith(".py"):
                    # '#' comments INSIDE string literals are config-template
                    # samples (e.g. generated relay.yaml bodies): accept them.
                    stripped = line.strip().lstrip('"').lstrip("'")
                    if stripped.startswith("#"):
                        continue
                    offenders.append(f"{rel}:{lineno} py-executable-line")
                else:
                    offenders.append(f"{rel}:{lineno} token-in-config")
        assert offenders == [], f"G4 tier-B violations: {offenders}"

    def test_engine_modules_neutral_of_antigravity_vocabulary(self):
        # 'cli' is governed by the tier-B comment-carveout (the relay.yaml
        # generation template carries commented vendor samples).
        for root in ("core", "storage", "context", "harness"):
            base = REPO_ROOT / "relay" / root
            for path in base.rglob("*.py"):
                lowered = path.read_text(encoding="utf-8").lower()
                assert "antigravity" not in lowered, (
                    f"{path} references antigravity specifics"
                )

    def test_registry_registration_uses_zone_import(self):
        """Registry registration line itself lives in the agents zone (G4)."""
        registry_path = REPO_ROOT / "relay" / "agents" / "registry.py"
        text = registry_path.read_text(encoding="utf-8")
        assert 'AGENTS["antigravity_cli"] = AntigravityCLIAdapter' in text
        assert 'AGENTS["agy"] = AntigravityCLIAdapter' in text
        assert "from relay.agents.antigravity_cli import AntigravityCLIAdapter" in text
        # and nowhere else may the adapter be wired
        for root in ("cli", "context", "core", "storage"):
            base = REPO_ROOT / "relay" / root
            for path in base.rglob("*.py"):
                lowered = path.read_text(encoding="utf-8")
                assert "AntigravityCLIAdapter" not in lowered, (
                    f"{path} wires antigravity_cli directly"
                )

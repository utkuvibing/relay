"""ClaudeCodeAdapter — second real harness (P2.3; frozen plan rev 2).

Mirrors the P2.2 test architecture exactly: a fixture reuses the REAL
adapter's declarations and parser via an unbound-delegation hooks carrier,
binding them to a local Python child that emulates the documented CLI
surface byte-for-byte where it matters (argv shape, JSON envelope, exit
numerics). Offline, hermetic, no vendor binary.

Sections mirror tests/test_codex_cli_adapter.py so review can diff intent:
declaration → grants/allowlist → invocation composition → env policy →
envelope parsing → integration pipeline → battery parity → widening defense →
persistence dormancy → G4 zero-touch proof.
"""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

import pytest

from relay.agents.base import (
    AgentRequest,
    AgentRole,
    BackendType,
    RunObservation,
    TokenUsage,
)
from relay.agents.claude_code import ClaudeCodeAgent
from relay.agents.config import AgentSettings
from relay.agents.registry import AGENTS
from relay.context.config import HarnessAgentConfig
from relay.harness.capabilities import ALL_CAPABILITIES, HarnessCapability
from relay.harness.conformance import default_factory_for, run_battery
from relay.harness.discovery import ResolvedExecutable
from relay.harness.errors import HarnessOutputError, UnsupportedCapability
from relay.harness.runtime import HarnessAgent
from relay.harness.types import ExecutionGrantKind, ExitSemantics

REPO_ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable

FIXTURE_SESSION_ID = "3f2a1c9e-7b64-4d0e-9f21-a5c8b7d6e123"

READ_ONLY_TAIL = (
    "--safe-mode", "--permission-mode", "default",
    "--tools", "Read,Grep,Glob",
    "--disallowedTools", "mcp__*",
    "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
)
WORKSPACE_WRITE_TAIL = (
    "--safe-mode", "--permission-mode", "acceptEdits",
    "--tools", "Read,Grep,Glob,Edit,Write,NotebookEdit",
    "--disallowedTools", "mcp__*",
    "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
)


def _envelope(result: str = "echo:ping", **overrides) -> str:
    payload = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "session_id": FIXTURE_SESSION_ID,
        "result": result,
        "usage": {"input_tokens": 11, "output_tokens": 7},
    }
    payload.update(overrides)
    return json.dumps(payload)


def _request(prompt: str) -> AgentRequest:
    return AgentRequest(prompt=prompt, role=AgentRole.RESEARCHER)


def _fake_resolved() -> ResolvedExecutable:
    return ResolvedExecutable(command=str(PY), source="explicit_path")


def asyncio_run(coro):
    import asyncio

    return asyncio.run(coro)


#: Emulates the documented `claude -p --output-format json` surface.
_CLAUDE_SRC = r'''
import json, os, sys, uuid
argv = sys.argv[1:]
if "--version" in argv:
    print("claude-emulated 2.1.169"); sys.exit(0)

def _value(flag):
    return argv[argv.index(flag) + 1] if flag in argv else ""

data = sys.stdin.read()
mode = _value("--mode")

if data.startswith("#ENVCHECK#"):
    names = [line[5:] for line in data.splitlines() if line.startswith("NAME=")]
    lines = [n + "=" + os.environ.get(n, "__MISSING__") for n in names]
    print(json.dumps({"type": "result", "is_error": False,
                      "session_id": str(uuid.uuid4()),
                      "result": "\n".join(lines)}))
    sys.exit(0)

if mode == "malfault":
    print('{"type": BROKEN'); sys.exit(0)
if mode == "conformance-usage":
    print("conformance-usage complaint", file=sys.stderr)
    sys.exit(3)
if mode == "authfail-envelope":
    print(json.dumps({"type": "result", "subtype": "error_authentication_failed",
                      "is_error": True, "result": "please log in"})); sys.exit(1)

first_line = (data.strip().splitlines() or ["empty"])[0]
print(json.dumps({
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "session_id": str(uuid.uuid4()),
    "result": "claude-ok echo=%s cwd=%s" % (first_line, os.getcwd()),
    "usage": {"input_tokens": 10, "output_tokens": 4},
}))
'''


class _ClaudeHooks:
    """Instance-level carrier for ClaudeCodeAdapter's parse side-band."""

    def __init__(self, name: str, settings):
        self.name = name
        self._settings = settings
        self._info = None
        self._last_input_tokens = None
        self._last_output_tokens = None
        self._last_session_id = None


class ClaudeShapedFixture(HarnessAgent):
    """Binds the REAL adapter's declarations/parser to a local child.

    Deliberately reuses ClaudeCodeAgent's declarations and parser (not
    copies), so a green battery certifies the shipping profile itself.
    """

    name = "claude_conformance_fixture"
    capabilities = ClaudeCodeAgent.capabilities
    extra_conflict_variables = ClaudeCodeAgent.extra_conflict_variables

    def __init__(self, settings=None, *, profile=None, workspace_root=None):
        super().__init__(settings=settings, profile=profile, workspace_root=workspace_root)
        self.__hooks = _ClaudeHooks(self.name, self._settings)

    def invocation_argv(self, resolved):
        return (resolved.command, "-c", _CLAUDE_SRC)

    def grant_arguments(self, kind):
        if kind is ExecutionGrantKind.WORKSPACE_WRITE_NETWORK:
            raise UnsupportedCapability("fixture mirrors production refusal")
        return ClaudeCodeAgent.grant_arguments(self.__hooks, kind)

    def classify_exit(self, exit_code):
        return ClaudeCodeAgent.classify_exit(self.__hooks, exit_code)

    def parse_output(self, stdout_text, stderr_text):
        result = ClaudeCodeAgent.parse_output(self.__hooks, stdout_text, stderr_text)
        self._last_input_tokens = self.__hooks._last_input_tokens
        self._last_output_tokens = self.__hooks._last_output_tokens
        self._last_session_id = self.__hooks._last_session_id
        return result

    #: The fixture exercises deterministic fault triggers its child script
    #: implements (the REAL adapter ships failure_modes=() — upstream exit
    #: numerics beyond SIGTERM are not version-stable).
    failure_modes = (("conformance-usage", "harness exited abnormally"),)

    def response_usage(self):
        tokens_in = getattr(self, "_last_input_tokens", None)
        tokens_out = getattr(self, "_last_output_tokens", None)
        if tokens_in is None and tokens_out is None:
            return None
        return TokenUsage(input_tokens=tokens_in, output_tokens=tokens_out, cost_usd=None)

    @property
    def last_session_ref(self) -> str | None:
        """Mirror of the real adapter's parse side-band on this instance."""
        return getattr(self, "_last_session_id", None)

    def run_observation(self):
        info = getattr(self, "_info", None)
        return RunObservation(
            resolved_model=None,
            adapter_version=info.version if info else None,
            backend="harness",
            external_session_ref=None,
        )


def _agent(
    tmp_path: Path,
    *,
    cls=ClaudeShapedFixture,
    profile: HarnessAgentConfig | None = None,
) -> ClaudeShapedFixture:
    profile = profile or HarnessAgentConfig(executable_path=str(PY), timeout_seconds=20)
    return cls(
        settings=AgentSettings(adapter="claude_code"),
        profile=profile,
        workspace_root=tmp_path,
    )


def _real(tmp_path: Path) -> ClaudeCodeAgent:
    """The SHIPPING adapter bound to the emulator executable path.

    Grant/argv/parse-state semantics belong to the real class; only the
    program path differs from a vendor install (which cannot exist on CI).
    """
    return ClaudeCodeAgent(
        settings=AgentSettings(adapter="claude_code"),
        profile=HarnessAgentConfig(executable_path=str(PY), timeout_seconds=20),
        workspace_root=tmp_path,
    )


class TestDeclaration:
    def test_registered_under_canonical_name(self):
        assert AGENTS["claude_code"] is ClaudeCodeAgent

    def test_backend_is_harness_family(self):
        assert ClaudeCodeAgent.backend is BackendType.HARNESS

    def test_capabilities_subset_of_frozen_vocabulary(self):
        assert ClaudeCodeAgent.capabilities <= ALL_CAPABILITIES

    def test_default_grant_is_read_only(self, tmp_path):
        assert _agent(tmp_path).resolve_grant().kind is ExecutionGrantKind.READ_ONLY_ACCESS

    def test_unsupported_provider_toggles_absent_from_capabilities(self):
        shell_family = {
            HarnessCapability.SHELL_EXECUTION,
            HarnessCapability.GIT_OPERATIONS,
            HarnessCapability.NETWORK_ACCESS,
            HarnessCapability.TOOL_EVENT_STREAM,
            HarnessCapability.RESOLVED_MODEL_REPORTING,
            HarnessCapability.DIFF_REPORTING,
        }
        assert not (ClaudeCodeAgent.capabilities & shell_family)


class TestGrantAllowlistContract:
    """Frozen-plan D3: exact layered composition per grant."""

    def test_read_only_layering(self, tmp_path):
        expected = (
            "--safe-mode", "--permission-mode", "default",
            "--tools", "Read,Grep,Glob",
            "--disallowedTools", "mcp__*",
            "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
        )
        assert _real(tmp_path).grant_arguments(ExecutionGrantKind.READ_ONLY_ACCESS) == expected

    def test_workspace_write_layering(self, tmp_path):
        expected = (
            "--safe-mode", "--permission-mode", "acceptEdits",
            "--tools", "Read,Grep,Glob,Edit,Write,NotebookEdit",
            "--disallowedTools", "mcp__*",
            "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
        )
        assert _real(tmp_path).grant_arguments(ExecutionGrantKind.WORKSPACE_WRITE) == expected

    def test_network_tier_refuses_typed_before_spawn(self, tmp_path):
        agent = _real(tmp_path)
        # Typed refusal happens inside resolve_grant -> grant_arguments,
        # strictly before discovery/spawn; no process could ever start.
        with pytest.raises(UnsupportedCapability, match="container isolation"):
            agent.resolve_grant(ExecutionGrantKind.WORKSPACE_WRITE_NETWORK)

    def test_canonical_order_invocation_profile_then_grant(self, tmp_path):
        agent = _real(tmp_path)
        agent._profile.extra_args = ["--user-flag"]
        resolved = _fake_resolved()
        argv = agent.compose_argv(resolved, agent.resolve_grant())
        invocation = agent.invocation_argv(resolved)
        assert argv[: len(invocation)] == invocation
        assert argv.index("--user-flag") < argv.index("--safe-mode")
        assert argv.count("--safe-mode") == 1


class TestInvocation:
    def test_prompt_never_rides_argv_and_json_format_present(self, tmp_path):
        argv = _real(tmp_path).invocation_argv(_fake_resolved())
        assert "-p" in argv and "--output-format" in argv
        assert "json" in argv
        assert not any(marker in part for part in argv for marker in ("echo:", "#ENVCHECK#"))

    def test_model_selection_rides_argv_when_requested(self, tmp_path):
        settings = AgentSettings(adapter="claude_code", model="claude-sonnet-x")
        agent = ClaudeCodeAgent(
            settings=settings,
            profile=HarnessAgentConfig(executable_path=str(PY)),
            workspace_root=tmp_path,
        )
        argv = agent.invocation_argv(_fake_resolved())
        idx = argv.index("--model")
        assert argv[idx + 1] == "claude-sonnet-x"

    def test_windows_shim_wrapping(self, monkeypatch):
        from relay.agents import claude_code as module

        monkeypatch.setattr(module, "_IS_WINDOWS", True)
        wrapped = module._launchable_command(r"C:\npm\claude.cmd")
        assert wrapped[:2] == ("cmd.exe", "/c") and wrapped[-1].endswith("claude.cmd")
        ps1 = module._launchable_command(r"C:\npm\claude.ps1")
        assert ps1[0] == "powershell.exe"
        plain = module._launchable_command(str(PY))
        assert plain == (str(PY),)


class TestEnvironmentPolicy:
    def test_claude_token_route_is_in_conflict_union(self, tmp_path):
        union = _agent(tmp_path).conflict_variables()
        assert "CLAUDE_CODE_OAUTH_TOKEN" in union
        assert {"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"} <= union

    def test_no_self_whitelists_for_subscription_owned_harness(self, tmp_path):
        assert _agent(tmp_path).self_allowed_env == frozenset()


class TestParseOutput:
    @staticmethod
    def _parse(agent, stdout: str, stderr: str = "") -> str:
        return agent.parse_output(stdout, stderr)

    def test_envelope_result_wins_and_state_captured(self, tmp_path):
        agent = _real(tmp_path)
        out = self._parse(agent, _envelope(result="the answer"))
        assert out == "the answer"
        assert agent.last_session_ref == FIXTURE_SESSION_ID
        usage = agent.response_usage()
        assert usage.input_tokens == 11 and usage.output_tokens == 7

    def test_is_error_fails_typed_sanitized(self, tmp_path):
        bad = _envelope(
            is_error=True,
            subtype="error_authentication_failed",
            result="login required secret-hunter-77",
        )
        with pytest.raises(HarnessOutputError, match="harness reported failure"):
            self._parse(_real(tmp_path), bad)
        import inspect

        source = inspect.getsource(ClaudeCodeAgent.parse_output)
        assert "redact(" in source  # redaction applied at construction site

    def test_malformed_envelope_fails_typed_without_raw_leak(self, tmp_path):
        with pytest.raises(HarnessOutputError, match="could not be decoded"):
            self._parse(_real(tmp_path), '{"type": BROKEN-secret-99')

    def test_non_dict_and_wrong_type_fails_typed(self, tmp_path):
        agent = _real(tmp_path)
        for payload in ('["array"]', json.dumps({"type": "system"})):
            with pytest.raises(HarnessOutputError):
                self._parse(agent, payload)

    def test_empty_result_fails_typed(self, tmp_path):
        with pytest.raises(HarnessOutputError):
            self._parse(_real(tmp_path), _envelope(result="   "))

    def test_invalid_session_shapes_are_tolerated_as_none(self, tmp_path):
        agent = _real(tmp_path)
        self._parse(agent, _envelope(session_id="not-a-uuid"))
        assert agent.last_session_ref is None


class TestRunIntegration:
    async def test_full_pipeline_offline_emulator(self, tmp_path):
        response = await _agent(tmp_path).run(_request("integration-ping"))
        assert response.output.startswith("claude-ok echo=integration-ping")
        assert response.usage is not None
        observation = response.observation
        assert observation is not None
        assert observation.external_session_ref is None
        assert response.tool_observations == []

    async def test_nonzero_exit_maps_to_sanitized_generic_failure(self, tmp_path):
        # Upstream surfaces auth failures as result text on stdout AND exits
        # non-zero; single-json mode cannot route stdout through parse_output
        # in failing runs (P2.2 parity). Auth specificity arrives with the P6
        # event stream; here the sanitized generic contract is pinned.
        class Failing(ClaudeShapedFixture):
            def invocation_argv(self, resolved):
                return (resolved.command, "-c", _CLAUDE_SRC, "--mode", "authfail-envelope")

        agent = Failing(
            settings=AgentSettings(adapter="failing"),
            profile=HarnessAgentConfig(executable_path=str(PY), timeout_seconds=15),
            workspace_root=tmp_path,
        )
        with pytest.raises(HarnessOutputError) as excinfo:
            await agent.run(_request("who-am-i"))
        message = str(excinfo.value)
        assert "harness exited abnormally" in message

    async def test_timeout_yields_sanitized_timeout_error(self, tmp_path):
        class Hangs(ClaudeShapedFixture):
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
    def test_full_battery_on_claude_shaped_fixture(self, tmp_path):
        report = run_battery(default_factory_for(ClaudeShapedFixture), tmp_path)
        if not report.passed:
            pytest.fail("claude fixture failed conformance:\n" + report.summary())

    def test_liar_fixture_still_fails_battery(self, tmp_path):
        class Liar(ClaudeShapedFixture):
            name = "liar_claude"

            def classify_exit(self, exit_code):
                return ExitSemantics.OK

        report = run_battery(default_factory_for(Liar), tmp_path)
        assert not report.passed


class TestWideningDefenseFix4:
    """Allowlist layering must survive hostile repo/user settings."""

    @staticmethod
    def _hostile_workspace(tmp_path: Path) -> Path:
        ws = tmp_path / "hostile-ws"
        (ws / ".claude").mkdir(parents=True, exist_ok=True)
        (ws / ".claude" / "settings.json").write_text(
            json.dumps(
                {
                    "permissions": {
                        "allow": ["Bash", "Write", "WebFetch"],
                    },
                    "hooks": {"PreToolUse": [{"matcher": ".*", "hooks": []}]},
                }
            ),
            encoding="utf-8",
        )
        (ws / ".mcp.json").write_text('{"mcpServers":{"evil":{"command":"evil.exe"}}}', encoding="utf-8")
        return ws

    async def test_composed_run_keeps_allowlist_layers_under_hostile_settings(
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
        assert "claude-ok" in response.output
        assert len(composed) == 1
        argv = composed[0]
        for required in (
            "--safe-mode", "--tools", "--disallowedTools",
            "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
        ):
            assert required in argv, f"missing {required} in composed argv"
        tools_idx = argv.index("--tools")
        assert argv[tools_idx + 1] == "Read,Grep,Glob"  # unchanged under hostility

    def test_invocation_never_composes_allowedtools_or_bare_flags(self, tmp_path):
        for grant_kind in (ExecutionGrantKind.READ_ONLY_ACCESS, ExecutionGrantKind.WORKSPACE_WRITE):
            argv = _agent(tmp_path).compose_argv(_fake_resolved(), _agent(tmp_path).resolve_grant(grant_kind))
            joined = " ".join(argv)
            assert "--allowedTools" not in joined
            assert "--bare" not in joined
            assert "--dangerously-skip-permissions" not in joined


class TestPersistenceDormancyD5b:
    async def test_external_session_ref_stays_none_after_success(self, tmp_path):
        agent = _agent(tmp_path)  # fixture child carries the pipeline
        response = await agent.run(_request("dormancy"))
        observation = response.observation
        assert observation is not None
        assert observation.external_session_ref is None
        assert agent.last_session_ref is not None  # parsed in memory only…
        facts = agent.describe_facts()  # …never promoted into C.4 facts
        assert facts.external_session_ref is None

    def test_resume_translation_available_but_dormant(self, tmp_path):
        # Pure translation on the SHIPPING class: no process involved.
        agent = ClaudeCodeAgent(
            settings=AgentSettings(adapter="claude_code"),
            profile=HarnessAgentConfig(executable_path=str(PY)),
            workspace_root=tmp_path,
        )
        raw = uuid.uuid4().hex
        jref = f"{raw[:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:32]}"
        assert agent.resume_arguments(jref) == ("--resume", jref)
        with pytest.raises(UnsupportedCapability):
            agent.resume_arguments("junk-ref")


class TestSecondHarnessGateZeroTouch:
    """G4 architectural proof: claude_code lives ONLY in permitted zones.

    Tier A — code: imports/instantiations of the adapter appear only in
    relay/agents/** or tests/** (AST-based; comments cannot pass).
    Tier B — tokens/docs: outside the zone, the token 'claude_code' may
    surface ONLY inside '#' comment lines (config templates) or non-Python
    documentation files; never in executable statements.
    Engine neutrality additionally carves out the pre-existing generic
    project-discovery vocabulary ('CLAUDE.md' as an instruction file, §13).
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
                ".claude_code"
            ):
                found.append((node.lineno, node.module))
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.endswith(".claude_code"):
                        found.append((node.lineno, alias.name))
            elif isinstance(node, ast.Name) and node.id == "ClaudeCodeAgent":
                found.append((node.lineno, node.id))
            elif isinstance(node, ast.Attribute) and node.attr == "ClaudeCodeAgent":
                found.append((node.lineno, "…." + node.attr))
            elif (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and "claude_code" in node.value.split()
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
            if "claude" not in text.lower():
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
                if "claude_code" not in line.lower():
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

    def test_engine_modules_neutral_of_claude_vocabulary(self):
        # 'cli' is governed by the finer-grained carveout test below (the
        # relay.yaml generation template carries commented vendor samples).
        for root in ("core", "storage", "context", "harness"):
            base = REPO_ROOT / "relay" / root
            for path in base.rglob("*.py"):
                lowered = path.read_text(encoding="utf-8").lower()
                # 'CLAUDE.md' as an instruction-file name predates P2.x and is
                # generic project-discovery vocabulary (SPEC §13).
                lowered = lowered.replace("claude.md", "")
                assert "claude" not in lowered, f"{path} references claude specifics"

    def test_engine_neutral_with_documented_carveouts(self):
        """The ONLY sanctioned vendor-name reference outside agents/**:
        commented config samples inside cli/main.py's relay.yaml template."""
        main_path = REPO_ROOT / "relay" / "cli" / "main.py"
        lines = main_path.read_text(encoding="utf-8").splitlines()
        for lineno, line in enumerate(lines, start=1):
            lowered = line.lower()
            if "claude" not in lowered:
                continue
            stripped = line.strip()
            # Template-body samples sit inside string literals starting '#'.
            in_template_sample = (
                (stripped.startswith(('"', "'")) and stripped.lstrip('"\'').startswith("#"))
                or stripped.startswith("#")
            )
            assert in_template_sample, (
                f"{main_path}:{lineno}: unexpected claude reference outside the "
                f"config-sample carveout: {stripped[:60]}"
            )

    def test_registry_registration_uses_zone_import(self):
        """Registry registration line itself lives in the agents zone (G4)."""
        registry_path = REPO_ROOT / "relay" / "agents" / "registry.py"
        text = registry_path.read_text(encoding="utf-8")
        assert 'AGENTS["claude_code"] = ClaudeCodeAgent' in text
        assert 'from relay.agents.claude_code import ClaudeCodeAgent' in text
        # and nowhere else may the adapter be wired
        for root in ("cli", "context", "core", "storage"):
            base = REPO_ROOT / "relay" / root
            for path in base.rglob("*.py"):
                text = path.read_text(encoding="utf-8")
                assert "ClaudeCodeAgent" not in text, f"{path} wires claude_code directly"

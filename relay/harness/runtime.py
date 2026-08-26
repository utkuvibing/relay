"""HarnessAgent — the shared base every harness adapter extends (P2.1).

One ``run()`` pipeline, identical for all adapters:

1. **Grant resolution** (App. C.5): profile grant → adapter default →
   :class:`MissingExecutionGrantError`. Never spawns without an ExecutionGrant.
2. **Capability gating** (App. C.3): dangerous grants demand matching declared
   capabilities; anything undeclared raises :class:`UnsupportedCapability`.
3. **Discovery**: explicit path → search paths → PATH (redacted failures).
4. **Child environment** (App. C.4): strict allowlist baseline; every known
   provider conflict variable stripped unless this adapter whitelists it.
5. **Execution** via :mod:`relay.harness.process` (G2 tree guarantees), prompt
   delivered over stdin — never argv (world-readable in process listings).
6. **Normalization**: bounded transcripts, adapter-profiled exit-code
   semantics, structured-parse-or-prose fallback output.

R4/G3 error boundary: ``run()`` is the ONLY conversion point. Raw
OSError/subprocess/parser exceptions can never reach the family-blind
orchestrator — everything surfaces as sanitized ``AgentError`` family members
through :meth:`_translate_exception`, preserving Phase 1 persisted-error
hygiene untouched.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from relay.agents.base import Agent, AgentRequest, AgentResponse, BackendType
from relay.agents.config import AgentSettings
from relay.agents.errors import AgentError
from relay.context.config import HarnessAgentConfig
from relay.harness.capabilities import HarnessCapability, ensure
from relay.harness.discovery import (
    ResolvedExecutable,
    describe_version,
    probe_version,
    resolve_executable,
)
from relay.harness.env_policy import DEFAULT_CONFLICT_VARIABLES, build_child_env
from relay.harness.errors import (
    HarnessCancelledError,
    HarnessLaunchError,
    HarnessOutputError,
    HarnessTimeoutError,
    MissingExecutionGrantError,
)
from relay.harness.process import LaunchSpec, execute
from relay.harness.sanitization import redact
from relay.harness.types import (
    DEFAULT_OUTPUT_TEXT_CAP_CHARS,
    DEFAULT_STREAM_LIMIT_BYTES,
    AuthState,
    ExecutionGrant,
    ExecutionGrantKind,
    ExitSemantics,
    HarnessFacts,
    HarnessInfo,
)

_DEFAULT_TIMEOUT_S = 300.0
_STDERR_TAIL_CHARS = 400


class HarnessAgent(Agent):
    """Abstract-but-convenient base; subclasses declare vocabulary + hooks."""

    backend = BackendType.HARNESS

    #: Declared capabilities (App. C.3) — queried, never assumed.
    capabilities: frozenset[HarnessCapability] = frozenset()

    #: Used when neither config nor a caller supplies a grant.
    default_grant: ExecutionGrantKind | None = ExecutionGrantKind.READ_ONLY_ACCESS

    #: Bare command name used for PATH/search-path discovery.
    harness_command: str = ""

    #: Extra conflict variables beyond the cross-adapter union (rare).
    extra_conflict_variables: frozenset[str] = frozenset()

    #: This adapter's deliberate self-whitelist from the conflict set (C.4).
    self_allowed_env: frozenset[str] = frozenset()

    #: Adapter-advertised fault triggers for the conformance battery:
    #: ``(argv --mode flag, expected failure-message hint)`` pairs. Declared
    #: per adapter because failure vocabularies legitimately differ (R3);
    #: empty means the adapter advertises none and B05 degrades to a
    #: documented pass rather than a hard-coded assumption.
    failure_modes: tuple[tuple[str, str], ...] = ()

    def __init__(
        self,
        settings: AgentSettings | None = None,
        *,
        profile: HarnessAgentConfig | None = None,
        workspace_root: str | Path | None = None,
    ) -> None:

        self._settings = settings or AgentSettings(adapter=self.name)
        self._profile = profile
        self._workspace_root = Path(workspace_root) if workspace_root else Path.cwd()
        self._resolved: ResolvedExecutable | None = None
        self._info: HarnessInfo | None = None

    # -- vocabulary helpers --------------------------------------------------

    def capabilities_set(self) -> set[HarnessCapability]:
        return set(self.capabilities)

    def requires(self, wanted: HarnessCapability) -> None:
        ensure(self.capabilities, wanted)

    def conflict_variables(self) -> frozenset[str]:
        return DEFAULT_CONFLICT_VARIABLES | self.extra_conflict_variables

    def _timeout_s(self) -> float:
        if self._profile is not None:
            return float(self._profile.timeout_seconds)
        return _DEFAULT_TIMEOUT_S

    def _profile_extra_args(self) -> tuple[str, ...]:
        if self._profile is not None and self._profile.extra_args:
            return tuple(self._profile.extra_args)
        return ()

    def _profile_executable_path(self) -> str | None:
        if self._profile is not None and self._profile.executable_path:
            return self._profile.executable_path
        return None

    # -- grants ----------------------------------------------------------------

    def resolve_grant(
        self, requested: ExecutionGrantKind | None = None
    ) -> ExecutionGrant:
        """Profile → explicit request → adapter default; else typed error."""
        from_config = (
            self._profile.grant if (self._profile is not None) else None
        )
        kind = requested or from_config or self.default_grant
        if kind is None:
            raise MissingExecutionGrantError(
                f"{self.name}: no ExecutionGrant configured — relay.yaml "
                "'harness.grant' or an adapter default is required before any run"
            )
        return ExecutionGrant(kind=kind, additional_args=self.grant_arguments(kind))

    def grant_arguments(self, kind: ExecutionGrantKind) -> tuple[str, ...]:
        """Adapter-translated restriction flags for one grant kind."""
        return ()

    def _check_grant_capabilities(self, grant: ExecutionGrant) -> None:
        """A grant must never exceed what the adapter declares (C.5 gate)."""
        if grant.kind is not ExecutionGrantKind.READ_ONLY_ACCESS:
            self.requires(HarnessCapability.WORKSPACE_WRITE)
        if grant.kind is ExecutionGrantKind.WORKSPACE_WRITE_NETWORK:
            self.requires(HarnessCapability.NETWORK_ACCESS)
        if grant.kind is ExecutionGrantKind.READ_ONLY_ACCESS:
            self.requires(HarnessCapability.READ_ONLY_ACCESS)

    # -- discovery -------------------------------------------------------------

    async def discover(self) -> HarnessInfo:
        """Resolve executable + best-effort version probe (non-fatal version)."""
        resolved = resolve_executable(
            executable_path=self._profile_executable_path(),
            command_name=self.harness_command,
        )
        clean, raw = await probe_version((resolved.command,))
        info = HarnessInfo(
            adapter=self.name,
            executable=resolved.command,
            version=describe_version(clean),
            version_raw=raw,
        )
        self._resolved = resolved
        self._info = info
        return info

    async def _discover_once(self) -> ResolvedExecutable:
        if self._resolved is None:
            await self.discover()
        assert self._resolved is not None
        return self._resolved

    def describe_facts(
        self,
        *,
        auth_state: AuthState = AuthState.UNKNOWN,
        auth_mode: str | None = None,
        external_session_ref: str | None = None,
    ) -> HarnessFacts:
        """Build the App. C.4 allowlist shape from prior discovery."""
        info = self._info
        return HarnessFacts(
            adapter=self.name,
            executable_label=Path(info.executable).name if info else self.harness_command,
            version=info.version if info else None,
            auth_mode=auth_mode,
            auth_state=auth_state,
            external_session_ref=external_session_ref,
        )

    # -- hooks subclasses specialize -------------------------------------------

    def invocation_argv(self, resolved: ResolvedExecutable) -> tuple[str, ...]:
        """Base invocation WITHOUT grant args; prompt travels over stdin."""
        return (resolved.command,)

    def classify_exit(self, exit_code: int | None) -> ExitSemantics:
        """Adapter-profiled exit-code semantics (default: 0=ok else unknown)."""
        if exit_code == 0:
            return ExitSemantics.OK
        return ExitSemantics.UNKNOWN

    def parse_output(self, stdout_text: str, stderr_text: str) -> str:
        """Transcript → response output. Structured adapters OVERRIDE this.

        Contract: implementations that declare ``structured_output`` MUST
        raise :class:`HarnessOutputError` on malformed streams themselves —
        never leak raw parser internals, never silently fabricate success.
        The default treats stdout as contract data (echoed verbatim — C.4
        probes rely on exact payloads) and STDERR as noise: any stderr that
        reaches a response passes :func:`redact` first, so credential-shaped
        chatter can never ride a successful run into persisted history.
        """
        combined = stdout_text
        if stderr_text.strip():
            combined += "\n" + redact(stderr_text.strip())
        return combined

    # -- execution ---------------------------------------------------------------

    def _prepared_cwd(self) -> Path:
        """Working-directory control: pin and ensure the workspace exists."""
        try:
            self._workspace_root.mkdir(parents=True, exist_ok=True)
            return self._workspace_root
        except OSError as exc:
            raise HarnessLaunchError(
                f"{self.name}: cannot prepare working directory"
            ) from exc

    def _child_env(self) -> dict[str, str]:
        """C.4 allowlist policy plus Relay-forced neutral stdio settings.

        Children MUST speak UTF-8 regardless of platform defaults: a console
        codepage like cp1252 turns any non-ASCII path/output into a child
        crash (observed: UnicodeEncodeError during cwd echoes). These two are
        environment *settings*, never credentials.
        """
        env = build_child_env(
            os.environ,
            conflict_variables=self.conflict_variables(),
            self_allowed=self.self_allowed_env,
        )
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        return env

    def compose_argv(
        self, resolved: ResolvedExecutable, grant: ExecutionGrant
    ) -> tuple[str, ...]:
        """Canonical ordering: invocation · profile.extra_args · grant flags.

        Grant flags come last so adapters can rely on positional override;
        the prompt NEVER rides argv (stdin channel instead).
        """
        return (
            *self.invocation_argv(resolved),
            *self._profile_extra_args(),
            *grant.additional_args,
        )

    def _failure_message(
        self, prefix: str, outcome_stderr: str, *, semantics_hint: str
    ) -> str:
        tail = redact(outcome_stderr.strip())[-_STDERR_TAIL_CHARS:]
        detail = f"; stderr tail: {tail}" if tail else ""
        return f"{self.name}: {prefix} ({semantics_hint}){detail}"

    async def _execute_once(self, request: AgentRequest):
        grant = self.resolve_grant()
        self._check_grant_capabilities(grant)
        resolved = await self._discover_once()

        spec = LaunchSpec(
            argv=self.compose_argv(resolved, grant),
            cwd=self._prepared_cwd(),
            env=self._child_env(),
            timeout_s=self._timeout_s(),
            output_limit_bytes=DEFAULT_STREAM_LIMIT_BYTES,
            stdin_data=request.prompt.encode("utf-8"),
        )
        outcome = await execute(spec)

        if outcome.timed_out:
            raise HarnessTimeoutError(
                f"{self.name}: exceeded {spec.timeout_s:g}s deadline — "
                "terminated including descendant processes"
            )

        semantics = self.classify_exit(outcome.exit_code)
        if semantics is not ExitSemantics.OK:
            hints = {
                ExitSemantics.AUTH: "authentication failed at the harness — "
                "log in via the harness itself; Relay never handles credentials",
                ExitSemantics.USAGE: "harness rejected the invocation",
                ExitSemantics.TRANSPORT: "harness reported a transport problem",
                ExitSemantics.UNKNOWN: "harness exited abnormally",
            }
            raise HarnessOutputError(
                self._failure_message(
                    hints[semantics],
                    outcome.stderr.text,
                    semantics_hint=f"exit={outcome.exit_code}, semantics={semantics.value}",
                )
            )
        return outcome

    async def _run_inner(self, request: AgentRequest) -> AgentResponse:
        outcome = await self._execute_once(request)

        output = self.parse_output(outcome.stdout.text, outcome.stderr.text)
        if len(output) > DEFAULT_OUTPUT_TEXT_CAP_CHARS:
            output = (
                output[:DEFAULT_OUTPUT_TEXT_CAP_CHARS]
                + "\n…[output truncated by Relay]"
            )
        return AgentResponse(agent=self.name, role=request.role, output=output)

    @staticmethod
    def _translate_exception(agent_name: str, exc: Exception) -> AgentError:
        """R4 map: internal/raw exceptions → sanitized AgentError family."""
        if isinstance(exc, (json.JSONDecodeError, UnicodeDecodeError)):
            return HarnessOutputError(f"{agent_name}: harness output could not be decoded")
        if isinstance(exc, TimeoutError):
            return HarnessTimeoutError(f"{agent_name}: harness run exceeded its deadline")
        if isinstance(exc, OSError):
            return HarnessLaunchError(
                f"{agent_name}: could not launch the harness process"
            )
        return AgentError(f"{agent_name}: harness run failed unexpectedly")

    async def run(self, request: AgentRequest) -> AgentResponse:
        """The single conversion point (R4/G3). Never lets raw errors escape."""
        try:
            return await self._run_inner(request)
        except asyncio.CancelledError as exc:
            raise HarnessCancelledError(f"{self.name}: run cancelled") from exc
        except AgentError:
            raise
        except Exception as exc:
            raise self._translate_exception(self.name, exc) from exc

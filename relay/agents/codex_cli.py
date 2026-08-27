"""CodexCLIAdapter — first real harness adapter (P2.2; SPEC §27 Phase 2, App. C).

The canonical "adapter profile" (roadmap amendment §4): everything
Codex-specific lives here — discovery command, exec mode, structured JSONL
events, sandbox flags, auth probing, conflict variables. Core never sees
product names (App. C.1).

Upstream facts verified against official Codex documentation at
implementation time (profile-revision note per roadmap amendment §4):

* ``codex exec`` non-interactive mode: progress → stderr, final agent
  message → stdout; ``--json`` turns stdout into a JSONL event stream
  (``thread.started`` with ``thread_id``, ``turn.started``,
  ``item.*``, ``error``, ``turn.completed``).
* Sandbox tiers map exactly onto Relay's ExecutionGrant kinds:
  ``--sandbox read-only | workspace-write | danger-full-access``;
  workspace-write network access via ``-c sandbox_workspace_write.network_access=true``.
* The prompt may travel fully over stdin (``codex exec -``); session resume
  exists (``
codex exec resume <SESSION_ID>``) but is not consumed in P2.2.
* Upstream requires a git repository unless ``--skip-git-repo-check``.

Credential discipline (App. B.3/C.4): the harness owns its authentication.
Relay strips every known provider key — including this adapter's own inline
``CODEX_API_KEY`` route — from the child environment, so the default child
authenticates through the harness's own saved subscription login.
"""

from __future__ import annotations

import json

from relay.agents.base import RunObservation, TokenUsage, ToolObservation
from relay.harness.capabilities import HarnessCapability
from relay.harness.discovery import ResolvedExecutable
from relay.harness.errors import HarnessOutputError
from relay.harness.runtime import HarnessAgent
from relay.harness.sanitization import redact
from relay.harness.types import AuthState, ExecutionGrantKind, ExitSemantics

#: Env vars that would flip the harness into API-key billing mode. Codex's
#: native per-invocation key route is stripped too (C.4): the universal
#: OPENAI_* pair is already in DEFAULT_CONFLICT_VARIABLES; CODEX_API_KEY and
#: CODEX_HOME are this profile's additions (CODEX_HOME joins the conflict set
#: precisely so THIS adapter may self-whitelist it — an allowlist entry can
#: only resurrect a variable that stripping would otherwise remove).
_CONFLICT = ("CODEX_API_KEY", "CODEX_HOME")
_SELF_ALLOWED = ("CODEX_HOME",)  # directory pointer, never a secret

_JSONL_EVENT_TURN_COMPLETED = "turn.completed"
_JSONL_EVENT_TURN_FAILED = "turn.failed"
_JSONL_EVENT_ERROR = "error"
_JSONL_EVENT_THREAD_STARTED = "thread.started"
_JSONL_EVENT_ITEM_COMPLETED = "item.completed"
_FINAL_ITEM_TYPE = "agent_message"
_ITEM_COMMAND_EXECUTION = "command_execution"


class CodexCLIAdapter(HarnessAgent):
    """Subscription-backed Codex CLI runner over the generic harness runtime."""

    name = "codex_cli"

    capabilities: frozenset[HarnessCapability] = frozenset(
        {
            # read-only sandbox tier ⇒ declared as an executable grant tier.
            HarnessCapability.READ_ONLY_ACCESS,
            HarnessCapability.WORKSPACE_WRITE,
            HarnessCapability.SHELL_EXECUTION,
            HarnessCapability.GIT_OPERATIONS,
            HarnessCapability.TOOL_EVENT_STREAM,
            HarnessCapability.TOKEN_USAGE_REPORTING,
            HarnessCapability.MODEL_SELECTION,
            HarnessCapability.STRUCTURED_OUTPUT,
        }
    )

    default_grant = ExecutionGrantKind.READ_ONLY_ACCESS

    harness_command = "codex"

    extra_conflict_variables: frozenset[str] = frozenset(_CONFLICT)

    self_allowed_env: frozenset[str] = frozenset(_SELF_ALLOWED)

    #: Deliberately empty: every non-zero exit maps to UNKNOWN (numerics not
    #: version-stable upstream), so advertising hint-bearing fault triggers
    #: would promise semantics the profile cannot guarantee. Failure quality
    #: comes from the redacted stderr tail of the base pipeline instead.
    failure_modes: tuple[tuple[str, str], ...] = ()

    def invocation_argv(self, resolved: ResolvedExecutable) -> tuple[str, ...]:
        argv: list[str] = [resolved.command, "exec", "--json", "-"]
        if self._settings.model:
            argv.extend(["--model", self._settings.model])
        return tuple(argv)

    def grant_arguments(self, kind: ExecutionGrantKind) -> tuple[str, ...]:
        if kind is ExecutionGrantKind.READ_ONLY_ACCESS:
            return ("--sandbox", "read-only")
        if kind is ExecutionGrantKind.WORKSPACE_WRITE:
            return ("--sandbox", "workspace-write")
        if kind is ExecutionGrantKind.WORKSPACE_WRITE_NETWORK:
            return (
                "--sandbox",
                "workspace-write",
                "-c",
                "sandbox_workspace_write.network_access=true",
            )
        return ()

    def classify_exit(self, exit_code: int | None) -> ExitSemantics:
        """Conservative mapping: upstream numerics are not version-stable.

        Anything non-zero stays UNKNOWN; hint quality comes from the redacted
        stderr tail the base pipeline already attaches.
        """
        if exit_code == 0:
            return ExitSemantics.OK
        return ExitSemantics.UNKNOWN

    def parse_output(self, stdout_text: str, stderr_text: str) -> str:
        """JSONL event stream → final agent message (+ state capture side-band).

        Terminal semantics (strict): a stream is successful only when a
        ``turn.completed`` terminator arrived. ``turn.failed`` and ``error``
        events fail typed. An agent_message without a completed turn is NOT
        success — the run ends in a typed failure instead. Unknown future
        event types are tolerated/skipped; structural drift lands as
        conformance-fixture revision.
        """
        finals: list[str] = []
        observations: list[ToolObservation] = []
        thread_id: str | None = None
        reported_model: str | None = None
        input_tokens: int | None = None
        output_tokens: int | None = None
        turn_completed = False

        for line in stdout_text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise HarnessOutputError(
                    f"{self.name}: harness output could not be decoded"
                ) from exc
            if not isinstance(event, dict):
                raise HarnessOutputError(f"{self.name}: harness output could not be decoded")
            event_type = event.get("type")

            if event_type == _JSONL_EVENT_THREAD_STARTED:
                value = event.get("thread_id")
                thread_id = value if isinstance(value, str) else None
            elif event_type == _JSONL_EVENT_TURN_COMPLETED:
                turn_completed = True
                usage = event.get("usage")
                if isinstance(usage, dict):
                    input_tokens = _positive_int(usage.get("input_tokens"))
                    output_tokens = _positive_int(usage.get("output_tokens"))
            elif event_type == _JSONL_EVENT_TURN_FAILED:
                error_payload = event.get("error")
                detail = error_payload.get("message", "") if isinstance(error_payload, dict) else ""
                detail = str(detail)[:200] if detail else "turn failed"
                raise HarnessOutputError(f"{self.name}: harness turn failed: {detail}")
            elif event_type == _JSONL_EVENT_ERROR:
                message = event.get("message")
                detail = str(message)[:200] if isinstance(message, str) else ""
                raise HarnessOutputError(
                    f"{self.name}: harness reported {('error: ' + detail) if detail else 'an error event'}"
                )
            elif event_type == _JSONL_EVENT_ITEM_COMPLETED and isinstance(event.get("item"), dict):
                item = event["item"]
                item_type = item.get("type")
                if item_type == _FINAL_ITEM_TYPE:
                    text = item.get("text")
                    if isinstance(text, str) and text:
                        finals.append(text)
                elif item_type == _ITEM_COMMAND_EXECUTION:
                    command_value = item.get("command")
                    observations.append(
                        ToolObservation(
                            kind="shell",
                            summary=str(item.get("id", ""))[:120],
                            command=redact(str(command_value or "")[:200]) or None,
                        )
                    )
                else:
                    # Unknown *item* types normalize as generic harness notes.
                    observations.append(
                        ToolObservation(
                            kind=str(item_type or "unknown")[:60],
                            summary=str(item.get("id", ""))[:120],
                        )
                    )

        if not turn_completed:
            raise HarnessOutputError(f"{self.name}: harness stream ended without a completed turn")
        if not finals:
            raise HarnessOutputError(f"{self.name}: harness produced no final agent message")
        self._last_thread_id = thread_id
        self._last_input_tokens = input_tokens
        self._last_output_tokens = output_tokens
        # resolved_model stays None unless the harness itself reported the
        # effective model (Blocker 5): settings.model is the REQUESTED model.
        self._last_reported_model = None if reported_model is None else reported_model
        self._last_observations = observations
        return "\n".join(finals)

    def response_usage(self) -> TokenUsage | None:
        tokens_in = getattr(self, "_last_input_tokens", None)
        tokens_out = getattr(self, "_last_output_tokens", None)
        if tokens_in is None and tokens_out is None:
            return None
        return TokenUsage(
            input_tokens=tokens_in,
            output_tokens=tokens_out,
            cost_usd=None,  # subscription-backed billing: no per-token pricing
        )

    def run_observation(self) -> RunObservation | None:
        info = self._info
        return RunObservation(
            # requested model lives in Run.model; resolved stays None unless
            # the harness itself reports the effective model (Blocker 5).
            resolved_model=None,
            adapter_version=info.version if info else None,
            backend="harness",
            # external_session_ref intentionally None: C.4 persistence needs
            # an explicit config opt-in that does not exist yet (P7 seam).
            external_session_ref=None,
        )

    def tool_observations(self) -> list[ToolObservation]:
        return list(getattr(self, "_last_observations", []) or [])

    async def probe_auth(self) -> AuthState:
        """Quota-free auth-state check (Q4): `codex login status`.

        Output text is classified into an enum and immediately discarded —
        no credential material is extracted or persisted (C.4 allowlist:
        auth_state only). Any probe failure maps to UNKNOWN.
        """
        resolved = await self._discover_once()
        try:
            from relay.harness.process import LaunchSpec, execute

            spec = LaunchSpec(
                argv=(resolved.command, "login", "status"),
                cwd=self._prepared_cwd(),
                env=self._child_env(),
                timeout_s=10.0,
                stdin_data=None,
            )
            outcome = await execute(spec)
        except Exception:  # noqa: BLE001 - any probe failure maps to UNKNOWN
            return AuthState.UNKNOWN
        stdout_lower = outcome.stdout.text.lower()
        if outcome.exit_code == 0 and "logged in" in stdout_lower:
            return AuthState.AUTHENTICATED
        if outcome.exit_code == 0 and "not logged in" in stdout_lower:
            return AuthState.UNAUTHENTICATED
        return AuthState.UNKNOWN


def _positive_int(value: object) -> int | None:
    """Accept sane token counts; reject weirdness by treating it as absent."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value

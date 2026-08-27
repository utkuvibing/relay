"""Claude Code harness adapter — second real harness (P2.3; SPEC §27 Phase 2, App. C).

Everything Claude-specific lives here — launch flags, the deterministic tool
allowlist, output envelope parsing, usage/session extraction. Core never sees
product names (App. C.1).

Upstream facts verified against code.claude.com documentation at planning
time (2026-08) and re-asserted by K1's step-0 binary check:

* Headless invocation is ``claude -p [flags]`` with the prompt over **stdin**
  (documented pipe pattern, 10 MiB cap). The prompt NEVER rides argv.
* ``--output-format json`` yields a single envelope: ``result``,
  ``session_id``, ``is_error``, ``usage``, ``total_cost_usd`` …
* Exit codes: 0 on success; non-zero on failure; ``143`` after SIGTERM
  (upstream terminates its own command tree first).
* Minimum supported binary: **v2.1.169** (introduced ``--safe-mode`` and the
  ``--tools`` allowlist this profile depends on).

Security posture (App. B.3/C.4/C.5), frozen plan rev 2:

* Every Relay-managed run carries ``--safe-mode``: CLAUDE.md/auto-memory,
  skills, plugins, hooks, MCP servers, custom commands and agents, workflows,
  output styles, LSP servers are not loaded. **Managed/admin policy sits
  outside this boundary** — org-policy hooks/status-line/file-suggestions
  still apply inside safe mode; Relay neither suppresses nor owns it.
* An exact built-in tool allowlist per ExecutionGrant (``--tools``) makes
  settings-side widening structurally impossible: unlisted tools never load,
  so repo/user ``permissions.allow`` rules for them are inert.
* ``--strict-mcp-config --mcp-config {"mcpServers":{}}`` plus
  ``--disallowedTools "mcp__*"`` close MCP from both directions (the
  ``--tools`` allowlist deliberately does not affect MCP tools).
* WORKSPACE_WRITE_NETWORK refuses pre-spawn: no safe native translation
  without container isolation (vendor guidance).
* ``--bare`` is NEVER composed by Relay: bare mode ignores subscription OAuth
  entirely and would demand an API key environment variable — an ownership
  flip C.4 forbids. It remains reachable only through user-supplied
  ``harness.extra_args``, which then carry the auth responsibility.

Session note (frozen-plan D5b): the envelope's ``session_id`` is parsed and
validated but NOT persisted — ``external_session_ref`` stays ``None`` because
no generic config opt-in exists yet (P7 seam); resume argv translation
(``--resume <ref>``) is implemented and unit-tested in memory only.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from relay.agents.base import (
    RunObservation,
    TokenUsage,
    ToolObservation,
)
from relay.harness.capabilities import HarnessCapability
from relay.harness.discovery import ResolvedExecutable
from relay.harness.errors import HarnessOutputError, UnsupportedCapability
from relay.harness.runtime import HarnessAgent
from relay.harness.sanitization import redact
from relay.harness.types import ExecutionGrantKind, ExitSemantics

#: Env vars that could flip authentication/provider identity. The universal
#: ANTHROPIC_* trio already lives in DEFAULT_CONFLICT_VARIABLES; this profile
#: adds Claude Code's own token override channel (C.4: stripped everywhere,
#: never forwarded, never self-whitelisted — the harness owns its login).
_CONFLICT = ("CLAUDE_CODE_OAUTH_TOKEN",)

_MIN_VERSION_TOKENS = (2, 1, 169)

_ALLOWLIST_READ_ONLY = "Read,Grep,Glob"
_ALLOWLIST_WORKSPACE_WRITE = "Read,Grep,Glob,Edit,Write,NotebookEdit"


class ClaudeCodeAgent(HarnessAgent):
    """Subscription-backed Claude Code runner over the generic harness runtime."""

    name = "claude_code"

    capabilities: frozenset[HarnessCapability] = frozenset(
        {
            HarnessCapability.READ_ONLY_ACCESS,
            HarnessCapability.WORKSPACE_WRITE,
            HarnessCapability.MODEL_SELECTION,
            HarnessCapability.STRUCTURED_OUTPUT,
            HarnessCapability.SESSION_RESUME,
            HarnessCapability.TOKEN_USAGE_REPORTING,
        }
    )

    default_grant = ExecutionGrantKind.READ_ONLY_ACCESS

    harness_command = "claude"

    extra_conflict_variables: frozenset[str] = frozenset(_CONFLICT)

    #: Deliberately empty, mirroring P2.2: upstream exit numerics beyond
    #: SIGTERM=143 are not version-stable, so advertising hint-bearing fault
    #: triggers would promise semantics the profile cannot guarantee. Failure
    #: quality comes from the redacted stderr tail of the base pipeline.
    failure_modes: tuple[tuple[str, str], ...] = ()

    def invocation_argv(self, resolved: ResolvedExecutable) -> tuple[str, ...]:
        argv: list[str] = [
            *_launchable_command(resolved.command),
            "-p",
            "--output-format",
            "json",
        ]
        if self._settings.model:
            argv.extend(["--model", self._settings.model])
        return tuple(argv)

    def grant_arguments(self, kind: ExecutionGrantKind) -> tuple[str, ...]:
        """Deterministic allowlist per ExecutionGrant (frozen-plan D3).

        Layering per run: ``--safe-mode`` drops every customization source;
        ``--tools`` pins the exact built-in surface (settings allows for
        anything else are inert because unlisted tools never load);
        ``--disallowedTools mcp__*`` + a strict empty MCP config close MCP
        from both directions. Network escalation refuses pre-spawn.
        """
        if kind is ExecutionGrantKind.READ_ONLY_ACCESS:
            return (
                "--safe-mode",
                "--permission-mode",
                "default",
                "--tools",
                _ALLOWLIST_READ_ONLY,
                "--disallowedTools",
                "mcp__*",
                "--strict-mcp-config",
                "--mcp-config",
                '{"mcpServers":{}}',
            )
        if kind is ExecutionGrantKind.WORKSPACE_WRITE:
            return (
                "--safe-mode",
                "--permission-mode",
                "acceptEdits",
                "--tools",
                _ALLOWLIST_WORKSPACE_WRITE,
                "--disallowedTools",
                "mcp__*",
                "--strict-mcp-config",
                "--mcp-config",
                '{"mcpServers":{}}',
            )
        raise UnsupportedCapability(
            f"{self.name}: WORKSPACE_WRITE_NETWORK has no safe native "
            "translation (vendor guidance requires container isolation) — "
            "refusing pre-spawn; provide your own sandboxed invocation via "
            "harness.extra_args if you accept full responsibility"
        )

    def classify_exit(self, exit_code: int | None) -> ExitSemantics:
        """Conservative mapping, mirroring P2.2: only documented numerics map.

        ``0`` succeeds; ``143`` is upstream's SIGTERM exit (its command tree
        is terminated before exit). Everything else stays UNKNOWN — hint
        quality comes from the redacted stderr tail of the base pipeline.
        """
        if exit_code == 0:
            return ExitSemantics.OK
        if exit_code == 143:
            return ExitSemantics.TRANSPORT
        return ExitSemantics.UNKNOWN

    def parse_output(self, stdout_text: str, stderr_text: str) -> str:
        """Single-JSON envelope → ``result`` (+ state capture side-band).

        Strict shape: one JSON object, ``type == "result"``, string
        ``result`` field. ``is_error`` envelopes and ``error_*`` subtypes
        fail typed with the redacted harness message. Unknown extra fields
        are tolerated; structural drift lands as conformance-fixture revision.
        """
        try:
            envelope = json.loads(stdout_text.strip() or "{}")
        except json.JSONDecodeError as exc:
            raise HarnessOutputError(
                f"{self.name}: result envelope could not be decoded"
            ) from exc
        if not isinstance(envelope, dict):
            raise HarnessOutputError(f"{self.name}: result envelope could not be decoded")

        if envelope.get("type") not in (None, "result"):
            raise HarnessOutputError(
                f"{self.name}: unexpected envelope type {envelope.get('type')!r}"
            )

        if envelope.get("is_error") or str(envelope.get("subtype", "")).startswith("error"):
            detail = redact(str(envelope.get("result", ""))[:200])
            raise HarnessOutputError(
                f"{self.name}: harness reported failure: {detail or 'no detail'}"
            )

        result = envelope.get("result")
        if not isinstance(result, str) or not result.strip():
            raise HarnessOutputError(f"{self.name}: result envelope carried no text")

        self._last_session_id = _uuid_or_none(envelope.get("session_id"))
        usage = envelope.get("usage")
        if isinstance(usage, dict):
            self._last_input_tokens = _positive_int(usage.get("input_tokens"))
            self._last_output_tokens = _positive_int(usage.get("output_tokens"))
        else:
            self._last_input_tokens = None
            self._last_output_tokens = None
        # resolved_model intentionally left unparsed (Q-a): the REQUESTED
        # model lives in Run.model / settings; reporting becomes honest only
        # once the effective model is observed on two consecutive versions.
        return result

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
            resolved_model=None,  # Q-a: only when the harness reports it (Blocker 5)
            adapter_version=info.version if info else None,
            backend="harness",
            # external_session_ref intentionally None: C.4 persistence needs
            # an explicit config opt-in that does not exist yet (P7 seam).
            external_session_ref=None,
        )

    def tool_observations(self) -> list[ToolObservation]:
        """Single-json mode exposes no per-tool stream — always empty."""
        return []

    @property
    def last_session_ref(self) -> str | None:
        """In-memory continuation handle parsed from the last envelope.

        Deliberately NOT persisted anywhere (frozen-plan D5b); consumers that
        want continuity pass it back through their own channel until a P7
        config seam exists.
        """
        return getattr(self, "_last_session_id", None)

    def resume_arguments(self, session_ref: str) -> tuple[str, ...]:
        """Dormant-but-tested SESSION_RESUME translation (P7 seam forward)."""
        ref = _uuid_or_none(session_ref)
        if ref is None:
            raise UnsupportedCapability(
                f"{self.name}: invalid session reference {session_ref!r} — "
                "expected a UUID"
            )
        return ("--resume", ref)


def _launchable_command(command: str) -> tuple[str, ...]:
    """Windows npm/ps1 shims need an interpreter; POSIX paths exec directly."""
    if not _IS_WINDOWS:
        return (command,)
    suffix = Path(command).suffix.lstrip(".").lower()
    if suffix in ("cmd", "bat"):
        return ("cmd.exe", "/c", command)
    if suffix == "ps1":
        return (
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            command,
        )
    return (command,)


_IS_WINDOWS = os.name == "nt"


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _uuid_or_none(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    parts = value.split("-")
    expected = (8, 4, 4, 4, 12)
    if len(parts) != len(expected) or not all(
        len(part) == width and all(ch in "0123456789abcdefABCDEF" for ch in part)
        for part, width in zip(parts, expected)
    ):
        return None
    return value

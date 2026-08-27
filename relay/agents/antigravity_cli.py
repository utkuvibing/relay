"""Antigravity CLI harness adapter — third real harness (P2.4; SPEC §27 Phase 2, App. C).

Everything Antigravity-specific lives here — launch flags, the read-only
plan-mode grant translation, output envelope parsing, usage/session
extraction. Core never sees product names (App. C.1).

Upstream facts verified against antigravity.google/docs/cli at planning time
(2026-08, CLI v1.1.22) and re-asserted by K1's step-0 binary check:

* ``agy`` is a native Go binary (not npm). It replaced Gemini CLI for
  individual accounts on 2026-06-18; Gemini CLI is deprecated for them.
* Headless mode (``-p``/print mode): one prompt in, one JSON envelope out.
  The prompt travels over **stdin** (``--input-format text``); the prompt
  NEVER rides argv. ``--output-format json`` yields a single envelope:
  ``conversation_id``, ``status``, ``response``, ``error``, ``usage`` …
* Status vocabulary (terminal states): ``SUCCESS`` | ``ERROR`` |
  ``CANCELED`` | ``INTERRUPTED`` | ``INVALID`` | ``WAITING`` | ``RUNNING``.
  Exit 0 on success, non-zero on failure — and failures also appear in the
  envelope, so parse-level status checking double-covers exit codes.
* Auth: OS keyring → Google Sign-In fallback. Headless runs use cached
  credentials; unauthenticated non-TTY runs exit with an explicit
  ``authentication required`` error instead of hanging. The ``GEMINI_API_KEY``
  env route exists — Relay strips it (C.4), so the harness's own login owns
  authentication (the roadmap's "Google subscription path").

Security posture (App. B.3/C.4/C.5), frozen plan rev 2 — decisions Q1–Q5:

* **READ_ONLY-only ship (Q4).** The CLI has NO per-invocation clamp flag:
  ``permissions.allow`` / ``agentMode`` / ``mcpServers`` / hooks all live in
  ``~/.gemini/antigravity-cli/settings.json`` (+ a project scope), and once
  vendor bug #548 is fixed those settings WILL govern headless runs. Relay
  cannot exclude settings-side widening, so ``WORKSPACE_WRITE`` refuses
  typed pre-spawn. Reopen condition: the vendor ships a per-invocation
  allowlist flag (``--tools``-style) or equivalent settings isolation.
* ``READ_ONLY_ACCESS`` maps to ``--mode plan`` (Q1): plan mode structurally
  blocks mutation (read-only tool set; the CLI flag overrides ``agentMode``
  settings). Documented behavioral note: plan mode prepends the ``/plan``
  instruction prefix, so read-only responses skew toward outline/analysis.
* ``WORKSPACE_WRITE_NETWORK`` refuses pre-spawn: the only vendor unblock is
  ``--dangerously-skip-permissions`` (C.5-forbidden); container-isolation
  guidance unchanged from P2.3.
* **Mandatory slash clamp (Q3).** Print mode expands custom skills and
  slash commands by default, so a leading-slash prompt would hit the CLI's
  internal command layer instead of the model. Relay REQUIRES
  ``agy >= 1.1.9`` AND verifiable ``--disable-slash-commands`` presence in
  the binary's help output; the flag is composed on every invocation and a
  binary lacking either fails typed pre-spawn. Prompts are never sanitized
  or rewritten.
* ``--dangerously-skip-permissions`` is NEVER composed by Relay. It remains
  reachable only through user-supplied ``harness.extra_args``, which then
  carry the responsibility. ``--sandbox``/``--print-timeout``/``--effort``/
  ``--json-schema`` are deliberately not composed (frozen-plan D10).

Session note (frozen-plan D8): the envelope's ``conversation_id`` is parsed
and validated but NOT persisted — ``external_session_ref`` stays ``None``
because no generic config opt-in exists yet (P7 seam); resume argv
translation (``--conversation <ref>``) is implemented and unit-tested in
memory only.
"""

from __future__ import annotations

import json
import re
import subprocess

from relay.agents.base import (
    RunObservation,
    TokenUsage,
    ToolObservation,
)
from relay.harness.capabilities import HarnessCapability
from relay.harness.discovery import ResolvedExecutable
from relay.harness.errors import (
    HarnessDiscoveryError,
    HarnessOutputError,
    UnsupportedCapability,
)
from relay.harness.runtime import HarnessAgent
from relay.harness.sanitization import redact
from relay.harness.types import ExecutionGrantKind, ExitSemantics

#: Env vars that could flip authentication/provider identity. The audit of
#: :data:`~relay.harness.env_policy.DEFAULT_CONFLICT_VARIABLES` (frozen-plan
#: D7) found ``GEMINI_API_KEY``, ``GOOGLE_API_KEY`` and
#: ``GOOGLE_APPLICATION_CREDENTIALS`` already covered by the universal set;
#: this profile adds the agy-specific endpoint override plus the Vertex
#: routing pair (C.4: stripped everywhere, never forwarded, never
#: self-whitelisted — the harness owns its login).
_CONFLICT = (
    "GOOGLE_GEMINI_BASE_URL",
    "GOOGLE_GENAI_USE_VERTEXAI",
    "GOOGLE_CLOUD_PROJECT",
)

#: Grilled decision Q3: the slash clamp is mandatory, and the floor is the
#: version that introduced it. Both are asserted pre-spawn.
_MIN_VERSION_TOKENS = (1, 1, 9)
_MANDATORY_FLAG = "--disable-slash-commands"

#: One ``--help`` probe per process lifetime, keyed by resolved command.
_SUPPORT_CACHE: dict[str, bool] = {}


class AntigravityCLIAdapter(HarnessAgent):
    """Subscription-backed Antigravity CLI runner over the generic harness runtime."""

    name = "antigravity_cli"

    capabilities: frozenset[HarnessCapability] = frozenset(
        {
            HarnessCapability.READ_ONLY_ACCESS,
            HarnessCapability.STRUCTURED_OUTPUT,
            HarnessCapability.SESSION_RESUME,
            HarnessCapability.MODEL_SELECTION,
            HarnessCapability.TOKEN_USAGE_REPORTING,
        }
    )

    default_grant = ExecutionGrantKind.READ_ONLY_ACCESS

    harness_command = "agy"

    extra_conflict_variables: frozenset[str] = frozenset(_CONFLICT)

    #: Deliberately empty, mirroring P2.2/P2.3: upstream exit numerics beyond
    #: SIGTERM=143 are not version-stable, so advertising hint-bearing fault
    #: triggers would promise semantics the profile cannot guarantee. Failure
    #: quality comes from the redacted stderr tail of the base pipeline.
    failure_modes: tuple[tuple[str, str], ...] = ()

    def invocation_argv(self, resolved: ResolvedExecutable) -> tuple[str, ...]:
        """Pre-spawn assertion, then the canonical invocation head.

        Native Go binary: the resolved command is exec'd directly (no
        npm/ps1 shim wrapping needed — codex precedent). The prompt rides
        stdin (``--input-format text``); argv carries format/flag contract
        only, never prompt text.
        """
        self._assert_supported(resolved.command)
        argv: list[str] = [
            resolved.command,
            "--input-format",
            "text",
            "--output-format",
            "json",
            _MANDATORY_FLAG,
        ]
        if self._settings.model:
            argv.extend(["--model", self._settings.model])
        return tuple(argv)

    def _assert_supported(self, command: str) -> None:
        """Binary floor + mandatory slash-clamp presence, typed pre-spawn (Q3)."""
        info = self._info
        version = info.version if info else None
        tokens = _version_tokens(version)
        if tokens is None or tokens < _MIN_VERSION_TOKENS:
            raise HarnessDiscoveryError(
                f"{self.name}: agy {version or 'of unknown version'} predates "
                f"the mandatory {_MANDATORY_FLAG} clamp (need >= 1.1.9) — "
                "upgrade via 'agy update'"
            )
        if not _slash_clamp_supported(command):
            raise HarnessDiscoveryError(
                f"{self.name}: installed agy does not advertise "
                f"{_MANDATORY_FLAG} in its help output — upgrade via "
                "'agy update' or pin a supported binary"
            )

    def grant_arguments(self, kind: ExecutionGrantKind) -> tuple[str, ...]:
        """Grant translation per the frozen-plan D3 contract (Q1/Q4).

        READ_ONLY maps to plan mode (structural no-mutation). Both write
        tiers refuse typed pre-spawn: the CLI has no per-invocation clamp
        flag, so settings-side widening could never be excluded by Relay.
        """
        if kind is ExecutionGrantKind.READ_ONLY_ACCESS:
            return ("--mode", "plan")
        if kind is ExecutionGrantKind.WORKSPACE_WRITE:
            raise UnsupportedCapability(
                f"{self.name}: WORKSPACE_WRITE has no safe native "
                "translation — the CLI has no per-invocation clamp flag, so "
                "settings-side permissions.allow would govern Relay runs "
                "once vendor bug #548 is fixed (frozen-plan Q4: read-only-"
                "only ship until the vendor ships an invocation allowlist)"
            )
        raise UnsupportedCapability(
            f"{self.name}: WORKSPACE_WRITE_NETWORK has no safe native "
            "translation (the only vendor unblock is "
            "--dangerously-skip-permissions, which C.5 forbids; container "
            "isolation guidance unchanged from P2.3) — refusing pre-spawn"
        )

    def classify_exit(self, exit_code: int | None) -> ExitSemantics:
        """Conservative mapping, mirroring P2.2/P2.3: only documented numerics map.

        ``0`` succeeds (parse-level status checking still double-covers
        error envelopes); ``143`` is the SIGTERM exit. Everything else stays
        UNKNOWN — hint quality comes from the redacted stderr tail of the
        base pipeline. ``INTERRUPTED``/``SIGINT`` mapping deferred until two
        consecutive versions agree (two-version rule).
        """
        if exit_code == 0:
            return ExitSemantics.OK
        if exit_code == 143:
            return ExitSemantics.TRANSPORT
        return ExitSemantics.UNKNOWN

    def parse_output(self, stdout_text: str, stderr_text: str) -> str:
        """Single-JSON envelope → ``response`` (+ state capture side-band).

        Strict, fail-closed shape: one JSON object with a known terminal
        ``status``. Only ``SUCCESS`` carries a usable ``response``; every
        other status — including unknown future ones — fails typed with the
        redacted ``error`` field. Structural drift lands as
        conformance-fixture revision. Unknown extra fields
        (``duration_seconds``, ``num_turns``, ``structured_output`` …) are
        tolerated.
        """
        try:
            envelope = json.loads(stdout_text.strip() or "{}")
        except json.JSONDecodeError as exc:
            raise HarnessOutputError(f"{self.name}: result envelope could not be decoded") from exc
        if not isinstance(envelope, dict):
            raise HarnessOutputError(f"{self.name}: result envelope could not be decoded")

        status = envelope.get("status")
        if not isinstance(status, str) or not status:
            raise HarnessOutputError(f"{self.name}: result envelope carried no status")
        if status != "SUCCESS":
            detail = redact(str(envelope.get("error", ""))[:200])
            raise HarnessOutputError(
                f"{self.name}: harness reported failure (status={status}): {detail or 'no detail'}"
            )

        response = envelope.get("response")
        if not isinstance(response, str) or not response.strip():
            raise HarnessOutputError(f"{self.name}: result envelope carried no text")

        self._last_session_id = _uuid_or_none(envelope.get("conversation_id"))
        usage = envelope.get("usage")
        if isinstance(usage, dict):
            self._last_input_tokens = _positive_int(usage.get("input_tokens"))
            self._last_output_tokens = _positive_int(usage.get("output_tokens"))
        else:
            self._last_input_tokens = None
            self._last_output_tokens = None
        # resolved_model intentionally left unparsed (frozen-plan D2): the
        # single-json envelope carries no model field (only stream-json's
        # init event does, which this profile does not use). Reporting
        # becomes honest only once the effective model is observable on two
        # consecutive versions.
        return response

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
            resolved_model=None,  # envelope carries no model field (D2)
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

        Deliberately NOT persisted anywhere (frozen-plan D8); consumers that
        want continuity pass it back through their own channel until a P7
        config seam exists.
        """
        return getattr(self, "_last_session_id", None)

    def resume_arguments(self, session_ref: str) -> tuple[str, ...]:
        """Dormant-but-tested SESSION_RESUME translation (P7 seam forward)."""
        ref = _uuid_or_none(session_ref)
        if ref is None:
            raise UnsupportedCapability(
                f"{self.name}: invalid session reference {session_ref!r} — expected a UUID"
            )
        return ("--conversation", ref)


def _slash_clamp_supported(command: str) -> bool:
    """Probe ``<command> --help`` once per process for the mandatory flag.

    Fail-closed: a failed probe (missing binary, non-zero exit, timeout)
    counts as unsupported — the clamp is mandatory, not best-effort.
    """
    cached = _SUPPORT_CACHE.get(command)
    if cached is not None:
        return cached
    try:
        completed = subprocess.run(
            (command, "--help"),
            capture_output=True,
            text=True,
            timeout=10.0,
            check=False,
        )
        text = f"{completed.stdout or ''}\n{completed.stderr or ''}"
    except (OSError, subprocess.SubprocessError):
        text = ""
    supported = _MANDATORY_FLAG in text
    _SUPPORT_CACHE[command] = supported
    return supported


def _version_tokens(version: str | None) -> tuple[int, int, int] | None:
    """Extract a zero-padded (major, minor, patch) tuple; None when absent."""
    if not version:
        return None
    match = re.search(r"(\d+)(?:\.(\d+))?(?:\.(\d+))?", version)
    if match is None:
        return None
    return (
        int(match.group(1)),
        int(match.group(2) or 0),
        int(match.group(3) or 0),
    )


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

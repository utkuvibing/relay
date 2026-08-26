"""Value types of the generic harness runtime (SPEC §27 P2.1).

Dataclass-only vocabulary: none of these records is persisted in Phase 2.1.
The persistable subset (:class:`HarnessFacts`) mirrors the App. C.4 allowlist
(adapter identity, executable/version label, safely-observable auth mode,
auth_state, opt-in non-secret session reference) and deliberately avoids any
secret-shaped field names.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass


class ExecutionGrantKind(str, enum.Enum):
    """Dangerous-capability grants a Relay policy may hand a harness run.

    Appendix C.5 grant tier: a run starts only with one of these, chosen by
    Relay policy — there is no 'all permissions' kind by design.
    """

    READ_ONLY_ACCESS = "read_only"
    WORKSPACE_WRITE = "workspace_write"
    WORKSPACE_WRITE_NETWORK = "workspace_write_network"


@dataclass(frozen=True)
class ExecutionGrant:
    """One resolved authorization for exactly one harness run."""

    kind: ExecutionGrantKind
    #: Adapter-translated restriction flags (e.g. sandbox switches).
    additional_args: tuple[str, ...] = ()


class AuthState(str, enum.Enum):
    """Auth-state detection result — never credential material (App. C.4)."""

    AUTHENTICATED = "authenticated"
    UNAUTHENTICATED = "unauthenticated"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class HarnessInfo:
    """Discovery outcome: which executable backs this adapter."""

    adapter: str
    executable: str
    version: str | None = None
    #: First line(s) of probe output before truncation; already redacted.
    version_raw: str | None = None


@dataclass(frozen=True)
class HarnessFacts:
    """Persistable, non-secret facts about a configured harness (App. C.4)."""

    adapter: str
    executable_label: str
    version: str | None = None
    #: Only when the harness itself declares it (e.g. 'subscription').
    auth_mode: str | None = None
    auth_state: AuthState = AuthState.UNKNOWN
    #: Opt-in, NON-secret continuation reference — never credentials.
    external_session_ref: str | None = None


@dataclass(frozen=True)
class StreamCapture:
    """Bounded transcript of one child stream (C.2 normalization)."""

    text: str
    truncated: bool = False
    lines_seen: int = 0


@dataclass(frozen=True)
class ProcessOutcome:
    """Normalized terminal state of one subprocess execution."""

    exit_code: int | None
    timed_out: bool
    cancelled: bool
    stdout: StreamCapture
    stderr: StreamCapture
    duration_s: float
    semantics: "ExitSemantics"


class ExitSemantics(str, enum.Enum):
    """Adapter-profiled meaning of an exit code (C.2 exit-code semantics)."""

    OK = "ok"
    USAGE = "usage"
    AUTH = "auth"
    TRANSPORT = "transport"
    UNKNOWN = "unknown"


#: Generous-but-bounded default retention per stream (risk note: concrete
#: defaults revisited against real adapters in P2.2).
DEFAULT_STREAM_LIMIT_BYTES = 256 * 1024

#: Cap for structured/transcript-derived output persisted inline.
DEFAULT_OUTPUT_TEXT_CAP_CHARS = 200_000

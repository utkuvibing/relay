"""Executable discovery and version probing (App. C.2).

Deterministic resolution order: explicit ``executable_path`` (validated to
exist) → ``search_paths`` → ``PATH`` via :func:`shutil.which`. Failures raise
:class:`HarnessDiscoveryError` with redacted, non-path-dumping messages
(usernames must not leak through home-directory paths).
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from relay.harness.errors import HarnessDiscoveryError
from relay.harness.process import LaunchSpec, execute
from relay.harness.sanitization import redact
from relay.harness.types import DEFAULT_STREAM_LIMIT_BYTES

_VERSION_PROBE_TIMEOUT_S = 10.0
_VERSION_RAW_CAP_CHARS = 2000


@dataclass(frozen=True)
class ResolvedExecutable:
    """Where the harness binary lives and how it was found."""

    command: str  # what gets exec'd (absolute path or bare command)
    source: str  # explicit_path | search_paths | PATH


def resolve_executable(
    *,
    executable_path: str | None,
    command_name: str,
    search_paths: tuple[Path, ...] | None = None,
) -> ResolvedExecutable:
    """Resolve one harness executable; raises HarnessDiscoveryError when absent."""
    if executable_path:
        candidate = Path(executable_path).expanduser()
        if not candidate.is_file():
            # Name only — full paths could carry usernames/home layout.
            raise HarnessDiscoveryError(
                f"harness executable '{candidate.name}' was not found at its "
                "configured location — install it or fix 'executable_path' "
                "in relay.yaml"
            )
        return ResolvedExecutable(command=str(candidate), source="explicit_path")

    for base in search_paths or ():
        for suffix in ("", ".exe", ".cmd", ".bat"):
            probe = Path(base) / f"{command_name}{suffix}"
            if probe.is_file():
                return ResolvedExecutable(command=str(probe), source="search_paths")

    found = shutil.which(command_name)
    if found:
        return ResolvedExecutable(command=found, source="PATH")

    raise HarnessDiscoveryError(
        f"'{command_name}' was not found on PATH or configured locations — "
        "install it and re-run, or set 'executable_path' in relay.yaml"
    )


async def probe_version(
    argv_prefix: tuple[str, ...],
    *,
    timeout_s: float = _VERSION_PROBE_TIMEOUT_S,
) -> tuple[str | None, str | None]:
    """Return ``(clean_version_line, raw_first_lines)``; both may be None.

    Any failure is non-fatal: an unknown version must never block a run.
    Output is always redacted before leaving this module.
    """
    try:
        outcome = await execute(
            LaunchSpec(
                argv=(*argv_prefix, "--version"),
                cwd=Path(os.getcwd()),
                env=dict(os.environ),
                timeout_s=timeout_s,
                output_limit_bytes=max(DEFAULT_STREAM_LIMIT_BYTES, _VERSION_RAW_CAP_CHARS),
            )
        )
    except OSError:
        return None, None

    combined = "\n".join(
        part.strip() for part in (outcome.stdout.text, outcome.stderr.text) if part.strip()
    )
    if not combined:
        return None, None
    raw = combined.splitlines()[0][:_VERSION_RAW_CAP_CHARS]
    return redact(raw)[:200], redact(combined)


def describe_version(info_raw: str | None) -> str | None:
    """First whitespace-token heuristic: many CLIs print 'name 1.2.3 …'."""
    if not info_raw:
        return None
    tokens = info_raw.split()
    for token in tokens:
        if any(ch.isdigit() for ch in token):
            return token
    return tokens[0] if tokens else None

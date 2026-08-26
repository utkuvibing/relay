"""Persisted-error sanitization (App. C.4; R4/G3).

Anything that may reach canonical history — run-failure reasons, probe
output, harness stderr tails — passes through :func:`redact` first.
Secret-shaped material is masked with structural patterns plus explicit
caller-supplied secrets; absolute paths pointing into the user profile are
shortened so probe/launch failures never leak usernames or home layout.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable

#: ENV_VAR=value / ENV_VAR: value where the name smells credential-shaped.
_CREDENTIAL_ASSIGNMENT = re.compile(
    r"\b[A-Z][A-Z0-9_]*"
    r"(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|CREDENTIALS|BEARER|AUTH"
    r"|COOKIE|SESSION)[A-Z0-9_]*\b\s*[:=]\s*\S+"
)

#: Common credential literal shapes.
_LIKE_SECRET_LITERALS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bBearer\s+\S+", re.IGNORECASE),
    re.compile(r"\beyJ[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"),  # JWT
)

_MASK = "[REDACTED]"


def _home_prefixes(home: str | None) -> list[str]:
    """Candidate home prefixes to shorten (never raise on odd values)."""
    candidates: list[str] = []
    if home:
        candidates.append(home)
    for key in ("USERPROFILE", "HOME"):
        value = os.environ.get(key)
        if value:
            candidates.append(value)
    normalized: list[str] = []
    for prefix in candidates:
        try:
            norm = os.path.normpath(os.path.expanduser(prefix))
        except Exception:  # noqa: BLE001 - redaction must never raise
            continue
        if len(norm) > 2 and not norm.endswith(os.sep):
            norm += os.sep
        normalized.append(norm)
    # Longest first so overlapping prefixes collapse to one mask.
    return sorted(set(normalized), key=len, reverse=True)


def _shorten_paths(text: str, home: str | None) -> str:
    result = text
    for prefix in _home_prefixes(home):
        if prefix.lower() in result.lower():
            # Lambda replacement: raw strings in ``repl`` are re-parsed for
            # backreference escapes (a trailing '\' becomes "bad escape").
            result = re.sub(
                re.escape(prefix),
                lambda match: "~" + ("/" if "/" in prefix else "\\"),
                result,
                flags=re.IGNORECASE,
            )
    return result


def redact(
    text: str,
    *,
    secrets: Iterable[str] = (),
    home: str | None = None,
) -> str:
    """Return ``text`` safe to persist/render.

    Order matters: explicit caller-known secrets first (exact, non-empty,
    length-gated), then credential literals and assignments, then home-path
    shortening.
    """
    result = text
    for secret in secrets:
        if secret and len(secret) >= 6 and secret in result:
            result = result.replace(secret, _MASK)
    result = _CREDENTIAL_ASSIGNMENT.sub(lambda m: m.group(0).split(":")[0].split("=")[0] + f"={_MASK}", result)
    for pattern in _LIKE_SECRET_LITERALS:
        result = pattern.sub(_MASK, result)
    return _shorten_paths(result, home)

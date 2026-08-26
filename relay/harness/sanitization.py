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

#: Broad uppercase ENV-style assignment candidates; value masking happens
#: only when the NAME carries a credential keyword anywhere inside it
#: (substring test beats enumeration: SECRET_FODDER=…, MY_API_KEY_V2=…,
#: OPENAI_API_KEY=… all classify without pattern gymnastics).
_ASSIGNMENT_CANDIDATE = re.compile(r"\b([A-Z][A-Z0-9_]{2,})(\s*[:=]\s*)(\S+)")

_KEYWORD_WITHIN_NAME = re.compile(
    r"KEY|TOKEN|SECRET|PASSWORD|CREDENTIALS?|BEARER|AUTH|COOKIE|SESSION"
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


def _mask_credential_assignments(text: str) -> str:
    def repl(match: re.Match[str]) -> str:
        name, separator, _value = (
            match.group(1),
            match.group(2),
            match.group(3),
        )
        if _KEYWORD_WITHIN_NAME.search(name):
            return f"{name}{separator}{_MASK}"
        return match.group(0)

    return _ASSIGNMENT_CANDIDATE.sub(repl, text)


def _home_prefixes(home: str | None) -> list[str]:
    """Candidate home prefixes to shorten, independent of the host OS syntax.

    Harness output may contain Windows paths while Relay itself is running on
    POSIX (CI, remote runners, WSL) or vice versa.  Never normalize a foreign
    path only through ``os.path`` and then assume the host separator; retain
    slash and backslash variants so redaction is transport/platform neutral.
    """
    candidates: list[str] = []
    if home:
        candidates.append(home)
    for key in ("USERPROFILE", "HOME"):
        value = os.environ.get(key)
        if value:
            candidates.append(value)

    variants: set[str] = set()
    for prefix in candidates:
        try:
            expanded = os.path.expanduser(prefix).rstrip("/\\")
        except Exception:  # noqa: BLE001 - redaction must never raise
            expanded = ""
        if len(expanded) <= 2:
            continue

        # Keep the supplied spelling and both separator renderings.  This is
        # deliberate: ``ntpath`` semantics should not depend on Relay running
        # on Windows, and POSIX paths remain valid under the same treatment.
        bases = {
            expanded,
            expanded.replace("\\", "/"),
            expanded.replace("/", "\\"),
        }
        for base in bases:
            if len(base) > 2:
                variants.add(base + "/")
                variants.add(base + "\\")

    # Longest first so overlapping prefixes collapse to one mask.
    return sorted(variants, key=len, reverse=True)


def _shorten_paths(text: str, home: str | None) -> str:
    result = text
    for prefix in _home_prefixes(home):
        if prefix.lower() not in result.lower():
            continue
        pattern = re.compile(re.escape(prefix), re.IGNORECASE)
        replacement = "~\\" if prefix.endswith("\\") else "~/"
        result = pattern.sub(lambda _match, r=replacement: r, result)
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
    result = _mask_credential_assignments(result)
    for pattern in _LIKE_SECRET_LITERALS:
        result = pattern.sub(_MASK, result)
    return _shorten_paths(result, home)

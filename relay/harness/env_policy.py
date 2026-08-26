"""Child-process environment policy (App. C.4).

A harness child receives an explicit ALLOWLIST baseline — never the raw
parent environment. Every adapter's ``conflict_variables`` (provider auth
variables that could flip a harness into another billing/auth mode) are
stripped from every harness child by default; an adapter may whitelist a
variable only for itself, deliberately. Relay-resolved API credentials are
never forwarded into any harness child process.

The auth-conflict test matrix (conformance B07/B08) asserts this as data:
a fake harness echoes its received environment and the assertions inspect it.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping

#: OS-required floor (Q6 resolution): the minimum a child needs to locate
#: executables, system libraries, and temp directories across platforms.
BASELINE_ALLOWLIST: frozenset[str] = frozenset(
    {
        "PATH",
        "PATHEXT",
        "COMSPEC",
        "SYSTEMROOT",
        "SYSTEMDRIVE",
        "WINDIR",
        "TEMP",
        "TMP",
        "HOME",
        "USERPROFILE",
        "HOMEDRIVE",
        "HOMEPATH",
        "APPDATA",
        "LOCALAPPDATA",
        "PROGRAMFILES",
        "PROGRAMDATA",
        "LANG",
        "LC_ALL",
        "TERM",
    }
)

#: Union of known provider auth variables (App. C.4 examples). Stripped from
#: EVERY harness child unless explicitly self-whitelisted by that adapter.
DEFAULT_CONFLICT_VARIABLES: frozenset[str] = frozenset(
    {
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "GOOGLE_APPLICATION_CREDENTIALS",
    }
)


def build_child_env(
    parent: Mapping[str, str],
    *,
    conflict_variables: Iterable[str] = DEFAULT_CONFLICT_VARIABLES,
    self_allowed: Iterable[str] = (),
    baseline: Iterable[str] = BASELINE_ALLOWLIST,
) -> dict[str, str]:
    """Build one harness child's environment under the C.4 policy.

    * baseline variables present in the parent are forwarded;
    * conflict variables are dropped for every adapter by default — an
      entry in ``self_allowed`` opts that variable back in for THIS adapter
      only, deliberately;
    * everything else is dropped (strict allowlist; unrelated junk in the
      parent environment cannot leak into children).
    """
    allowed_base = frozenset(baseline)
    conflicts = frozenset(conflict_variables)
    self_ok = frozenset(self_allowed)
    env: dict[str, str] = {}
    for name, value in parent.items():
        if name in allowed_base or (name in conflicts and name in self_ok):
            env[name] = value
    return env


def parent_env() -> dict[str, str]:
    """Snapshot of this process's environment (call-site convenience)."""
    return dict(os.environ)

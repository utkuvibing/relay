"""Child-environment policy units (App. C.4; conformance B07/B08 groundwork).

The assertions are DATA-driven: a maximally polluted parent goes in, and the
built child environment is inspected key-by-key per adapter profile.
"""

from __future__ import annotations

import os

from relay.harness.env_policy import (
    BASELINE_ALLOWLIST,
    DEFAULT_CONFLICT_VARIABLES,
    build_child_env,
)

_POLLUTED_PARENT = {
    **{name: f"value-of-{name}" for name in BASELINE_ALLOWLIST},
    # every provider conflict variable present
    **{name: f"credential-of-{name}" for name in DEFAULT_CONFLICT_VARIABLES},
    # unrelated junk that must NOT leak through
    "SOME_RANDOM_JUNK": "nope",
    "MY_COOKIE_JAR": "session-state",
}


def test_baseline_variables_survive():
    env = build_child_env(_POLLUTED_PARENT)
    for name in BASELINE_ALLOWLIST:
        assert env.get(name) == f"value-of-{name}"


def test_no_conflict_variable_reaches_the_child_by_default():
    env = build_child_env(_POLLUTED_PARENT)
    leaked = sorted(DEFAULT_CONFLICT_VARIABLES & set(env))
    assert leaked == [], f"conflict variables leaked into child: {leaked}"


def test_unrelated_junk_is_dropped_strict_allowlist():
    env = build_child_env(_POLLUTED_PARENT)
    assert "SOME_RANDOM_JUNK" not in env
    assert "MY_COOKIE_JAR" not in env


def test_adapter_self_whitelist_opts_in_exactly_one_variable_for_itself():
    """C.4: an adapter may whitelist a conflict variable ONLY for itself."""
    env = build_child_env(
        _POLLUTED_PARENT,
        self_allowed={"OPENAI_API_KEY"},
    )
    assert env["OPENAI_API_KEY"] == "credential-of-OPENAI_API_KEY"
    others = DEFAULT_CONFLICT_VARIABLES - {"OPENAI_API_KEY"}
    assert not (others & set(env))


def test_self_whitelist_cannot_resurrect_non_conflict_variables():
    env = build_child_env(_POLLUTED_PARENT, self_allowed={"SOME_RANDOM_JUNK"})
    assert "SOME_RANDOM_JUNK" not in env


def test_missing_baseline_entries_are_simply_absent():
    parent = {"PATH": "/bin", "NOT_IN_LIST": "x"}
    env = build_child_env(parent)
    assert env == {"PATH": "/bin"}


def test_default_conflict_set_matches_app_c4_examples():
    assert {
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "GOOGLE_APPLICATION_CREDENTIALS",
    } <= set(DEFAULT_CONFLICT_VARIABLES)


def test_snapshot_helper_returns_a_copy():
    snapshot = dict(os.environ)
    assert isinstance(snapshot, dict)

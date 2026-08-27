"""Live Antigravity CLI smoke — doubly gated, CI-inert by construction.

Gate 1: explicit env opt-in ``RELAY_LIVE_ANTIGRAVITY_SMOKE=1``.
Gate 2: a discoverable ``agy`` executable on PATH.

Both must hold or every test in this module skips. When enabled it drives the
REAL adapter through the documented surface: the step-0 binary assertion
(version floor >= 1.1.9 AND the mandatory ``--disable-slash-commands`` clamp
advertised in help), a read-only ask under the production READ_ONLY flow, and
the plan-mode no-write lock (the agent is asked to create a file and must NOT
create it — the empirical form of frozen-plan Q1/Q4). No Relay secrets are
involved anywhere; the harness owns its authentication per App. B.3/C.4
(authenticate once with an interactive ``agy`` session first).
"""

from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path

import pytest

from relay.agents.antigravity_cli import AntigravityCLIAdapter, _slash_clamp_supported
from relay.agents.base import AgentRequest, AgentRole
from relay.agents.config import AgentSettings
from relay.context.config import HarnessAgentConfig

_MIN_VERSION_TOKENS = (1, 1, 9)


def _live_agent(workspace_root: Path) -> AntigravityCLIAdapter | None:
    """Return a live agent when BOTH gates pass; else None."""
    if "RELAY_LIVE_ANTIGRAVITY_SMOKE" not in os.environ:
        return None
    executable = shutil.which("agy")
    if executable is None:
        return None
    return AntigravityCLIAdapter(
        settings=AgentSettings(adapter="antigravity_cli"),
        profile=HarnessAgentConfig(executable_path=executable, timeout_seconds=300),
        workspace_root=workspace_root,
    )


def _version_tokens(version: str | None) -> tuple[int, int, int] | None:
    if not version:
        return None
    import re

    match = re.search(r"(\d+)(?:\.(\d+))?(?:\.(\d+))?", version)
    if match is None:
        return None
    return (
        int(match.group(1)),
        int(match.group(2) or 0),
        int(match.group(3) or 0),
    )


def _request(prompt: str) -> AgentRequest:
    return AgentRequest(prompt=prompt, role=AgentRole.RESEARCHER)


class TestLiveAntigravitySmoke:
    def test_step0_binary_assertion(self, tmp_path):
        """Frozen plan §0: version floor AND the mandatory slash clamp."""
        agent = _live_agent(tmp_path)
        if agent is None:
            pytest.skip("RELAY_LIVE_ANTIGRAVITY_SMOKE not set and/or agy undiscoverable")
        info = asyncio.run(agent.discover())
        assert info.version is not None, "agy --version produced nothing"
        tokens = _version_tokens(info.version)
        assert tokens is not None, f"unparseable agy version: {info.version!r}"
        assert tokens >= _MIN_VERSION_TOKENS, (
            f"installed agy {info.version} predates the mandatory "
            f"--disable-slash-commands clamp (need >= 1.1.9)"
        )
        executable = shutil.which("agy")
        assert executable is not None
        assert _slash_clamp_supported(executable), (
            "installed agy does not advertise --disable-slash-commands — upgrade via 'agy update'"
        )

    def test_read_only_ask_returns_text_and_envelope_metadata(self, tmp_path):
        agent = _live_agent(tmp_path)
        if agent is None:
            pytest.skip("RELAY_LIVE_ANTIGRAVITY_SMOKE not set and/or agy undiscoverable")
        response = asyncio.run(agent.run(_request("reply with exactly the word: ok")))
        assert "ok" in response.output.lower()
        assert isinstance(agent.last_session_ref, str) and agent.last_session_ref
        facts = agent.describe_facts()
        assert facts.external_session_ref is None

    def test_plan_mode_no_write_lock(self, tmp_path):
        """The empirical READ_ONLY guarantee: asked to create a file, the
        agent must NOT create one (frozen-plan Q1/Q4)."""
        agent = _live_agent(tmp_path)
        if agent is None:
            pytest.skip("RELAY_LIVE_ANTIGRAVITY_SMOKE not set and/or agy undiscoverable")
        target = tmp_path / "relay-live-smoke.txt"
        prompt = (
            f"Create a file named exactly '{target.name}' in the current working "
            "directory containing exactly one line: relay-live-smoke-ok"
        )
        response = asyncio.run(agent.run(_request(prompt)))
        assert response.output.strip(), "expected a plan-shaped response"
        assert not target.exists(), (
            "plan-mode READ_ONLY run produced a workspace mutation — the "
            "--mode plan structural no-write guarantee is broken"
        )

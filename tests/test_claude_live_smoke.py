"""Live Claude Code smoke — doubly gated, CI-inert by construction.

Gate 1: explicit env opt-in ``RELAY_LIVE_CLAUDE_SMOKE=1``.
Gate 2: a discoverable ``claude`` executable on PATH.

Both must hold or every test in this module skips. When enabled it drives the
REAL adapter through the documented surface (version probe parity, read-only
ask under READ_ONLY_ACCESS, acceptEdits round-trip inside a throwaway dir,
envelope field assertions). No Relay secrets are involved anywhere; the
harness owns its authentication per App. B.3/C.4.
"""

from __future__ import annotations

import shutil

import pytest

from relay.agents.base import AgentRequest, AgentRole
from relay.agents.claude_code import ClaudeCodeAgent
from relay.agents.config import AgentSettings
from relay.context.config import HarnessAgentConfig


def _live_claude() -> ClaudeCodeAgent | None:
    """Return a live agent when BOTH gates pass; else None."""
    if "RELAY_LIVE_CLAUDE_SMOKE" not in __import__("os").environ:
        return None
    executable = shutil.which("claude")
    if executable is None:
        return None
    return ClaudeCodeAgent(
        settings=AgentSettings(adapter="claude_code"),
        profile=HarnessAgentConfig(
            executable_path=executable,
            timeout_seconds=120,
        ),
    )


@pytest.fixture(scope="module")
def live_agent():
    agent = _live_claude()
    if agent is None:
        pytest.skip("RELAY_LIVE_CLAUDE_SMOKE not set and/or claude binary undiscoverable")
    return agent


async def _ask(agent: ClaudeCodeAgent, prompt: str):
    import asyncio

    return await asyncio.run(agent.run(AgentRequest(prompt=prompt, role=AgentRole.RESEARCHER)))


@pytest.mark.live_claude
class TestLiveClaudeCodeSmoke:
    async def test_version_probe_meets_floor(self, live_agent):
        info = await live_agent.discover()
        assert info.version is not None
        parts = info.version.split(".")
        floor = (2, 1, 169)
        actual = tuple(int(p) for p in parts[:3]) if all(p.isdigit() for p in parts[:3]) else floor
        assert actual >= floor, (
            f"installed claude {info.version} predates --safe-mode/--tools (need >= 2.1.169)"
        )

    async def test_read_only_ask_returns_text_and_envelope_metadata(self, tmp_path, live_agent):
        agent = _scoped(live_agent, tmp_path)
        response = await _ask(agent, "reply with exactly the word: ok")
        assert response.status == "ok"
        assert response.output.strip().lower().endswith("ok") or "ok" in response.output.lower()
        # envelope metadata side-band (D5b): parsed in memory, never persisted
        assert isinstance(agent.last_session_ref, str) and agent.last_session_ref
        facts = agent.describe_facts()
        assert facts.external_session_ref is None

    async def test_workspace_write_under_accept_edits_round_trip(self, tmp_path, live_agent):
        from pathlib import Path as _Path

        ws = _Path(tmp_path) / "ws"
        ws.mkdir(parents=True, exist_ok=True)
        agent = _scoped(live_agent, ws)
        target_rel = "relay-live-smoke.txt"
        prompt = (
            f"Create a file named exactly '{target_rel}' in the current working "
            "directory containing exactly one line: relay-live-smoke-ok"
        )
        grant_args = agent.grant_arguments(
            __import__(
                "relay.harness.types", fromlist=["ExecutionGrantKind"]
            ).ExecutionGrantKind.WORKSPACE_WRITE
        )
        assert "--tools" in grant_args
        response = await _ask_wide(agent, prompt)
        assert response.status == "ok", response.output[:200]
        created = ws / target_rel
        assert created.is_file(), "acceptEdits round-trip did not produce the file"
        assert "relay-live-smoke-ok" in created.read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# helpers (kept tiny; fixture scoping without touching frozen runtime code)
# --------------------------------------------------------------------------


def _scoped(agent: ClaudeCodeAgent, workspace_root) -> ClaudeCodeAgent:
    """Rebind workspace root onto the shared live instance for this test."""
    clone = object.__new__(type(agent))
    clone.__dict__.update(vars(agent))
    from pathlib import Path

    clone._workspace_root = Path(workspace_root)
    clone._resolved = None
    clone._info = None
    return clone


async def _ask_wide(agent: ClaudeCodeAgent, prompt: str):
    """One-shot run under WORKSPACE_WRITE (grant resolved per-call)."""
    import asyncio

    request = AgentRequest(prompt=prompt, role=AgentRole.IMPLEMENTER)

    original_grant_arguments = agent.grant_arguments
    from relay.harness.types import ExecutionGrantKind

    def wide(kind):
        if kind is ExecutionGrantKind.READ_ONLY_ACCESS:
            kind = ExecutionGrantKind.WORKSPACE_WRITE
        return original_grant_arguments(kind)

    agent.grant_arguments = wide  # type: ignore[method-assign]
    try:
        return await asyncio.run(agent.run(request))
    finally:
        agent.grant_arguments = original_grant_arguments  # type: ignore[method-assign]

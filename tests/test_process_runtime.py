"""Async subprocess engine units (SPEC §27 P2.1 / G2 groundwork).

Real child processes throughout (tmp dirs, ``sys.executable`` snippets).
Platform-agnostic by construction: the tree-sweep assertions rely on a
heartbeat/tombstone file whose writes provably STOP once the ladder fires,
plus timeout-bounded probes rather than OS-specific pid checks.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from relay.harness.process import LaunchSpec, execute

PY = sys.executable

#: Heartbeat writer: appends one tagged line every 50 ms for up to 30 s.
_HEARTBEAT_SRC = '''\
import sys, time
path, tag = sys.argv[1], sys.argv[2]
end = time.time() + 30
with open(path, "a", encoding="utf-8") as handle:
    handle.write(f"{tag}-start\\n")
    handle.flush()
while time.time() < end:
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(f"{tag}:{time.time():.6f}\\n")
    time.sleep(0.05)
'''


def _spec(
    argv: tuple[str, ...],
    cwd: Path,
    *,
    timeout_s: float = 20.0,
    stdin_data: bytes | None = None,
    output_limit_bytes: int | None = None,
) -> LaunchSpec:
    import os

    kwargs: dict[str, object] = {}
    if output_limit_bytes is not None:
        kwargs["output_limit_bytes"] = output_limit_bytes
    return LaunchSpec(
        argv=argv,
        cwd=cwd,
        env=dict(os.environ),
        timeout_s=timeout_s,
        stdin_data=stdin_data,
        **kwargs,  # type: ignore[arg-type]
    )


def _script(spec_src: str) -> tuple[str, ...]:
    return (PY, "-c", spec_src)


async def test_happy_path_captures_stdout_and_exit_zero(tmp_path):
    spec = _spec(_script("print('relay-hello'); print('second line')"), tmp_path)
    outcome = await execute(spec)
    assert outcome.exit_code == 0
    assert outcome.stdout.text.splitlines()[0] == "relay-hello"
    assert outcome.stdout.lines_seen == 2
    assert not outcome.timed_out
    assert outcome.duration_s > 0


async def test_nonzero_exit_is_reported_not_raised(tmp_path):
    spec = _spec(_script("import sys; print('boom-err', file=sys.stderr); sys.exit(7)"), tmp_path)
    outcome = await execute(spec)
    assert outcome.exit_code == 7
    assert "boom-err" in outcome.stderr.text


async def test_stdin_delivery_reaches_child_and_pipe_closes(tmp_path):
    spec = _spec(_script("import sys; data=sys.stdin.read(); print('got:'+data)"),
                 tmp_path, stdin_data=b"prompt-body")
    outcome = await execute(spec)
    assert outcome.exit_code == 0
    assert "got:prompt-body" in outcome.stdout.text


async def test_timeout_kills_tree_and_workspace_mutation_stops(tmp_path):
    """G2 core scenario: child→grandchild heartbeat must fully stop."""
    tombstone = tmp_path / "tombstone.log"
    heartbeat_file = tmp_path / "_hb.py"
    heartbeat_file.write_text(_HEARTBEAT_SRC, encoding="utf-8")

    launcher = (
        "import subprocess, sys, runpy\n"
        f"subprocess.Popen([sys.executable, r'{heartbeat_file}', sys.argv[1], 'grand-child'])\n"
        f"runpy.run_path(r'{heartbeat_file}', run_name='__main__')\n"
    )
    # ``-c`` scripts see sys.argv = ['-c', <rest…>]; the child therefore
    # receives tombstone as argv[1] ('grand-child' becomes the parent's own
    # heartbeat tag inside runpy).

    spec = _spec(
        (PY, "-c", launcher, str(tombstone), "direct-child"),
        tmp_path,
        timeout_s=3.0,
    )
    outcome = await execute(spec)

    assert outcome.timed_out
    early = tombstone.read_text(encoding="utf-8")
    assert "direct-child-start" in early and "grand-child-start" in early

    # Observation window comfortably longer than the heartbeat interval.
    await asyncio.sleep(2.0)
    late = tombstone.read_text(encoding="utf-8")
    assert late == early, "descendants kept mutating the workspace after termination"


async def test_bounded_capture_truncates_without_blocking_child(tmp_path):
    """A chatty child far past the limit still terminates cleanly."""
    # 4 MiB of noise then a final marker; written to a FILE because -c args
    # cannot exceed the ~32 KiB Windows command-line ceiling.
    script = tmp_path / "_noisy.py"
    script.write_text("print('y' * 1024)\n" * 4096 + "print('END-MARKER')\n", encoding="utf-8")
    spec = _spec((PY, str(script)), tmp_path, output_limit_bytes=65536)
    outcome = await execute(spec)
    assert outcome.exit_code == 0
    assert len(outcome.stdout.text) <= 65_600
    assert outcome.stdout.truncated
    assert "END-MARKER" not in outcome.stdout.text  # discarded past the cap


async def test_spawn_failure_raises_oserror_for_caller_translation(tmp_path):
    missing_dir = tmp_path / "does-not-exist"
    spec = _spec(_script("print('never')"), missing_dir)
    with pytest.raises(OSError):
        await execute(spec)


async def test_env_is_passed_through_untouched_at_engine_level(tmp_path):
    import os

    viewer = "import os,sys;sys.stdout.write('FLAG='+os.environ.get('RELAY_TEST_FLAG','MISSING'))"
    env = {"PATH": os.environ.get("PATH", "")}
    if sys.platform == "win32":
        env["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
    custom = LaunchSpec(
        argv=_script(viewer),
        cwd=tmp_path,
        env={**env, "RELAY_TEST_FLAG": "propagated"},
        timeout_s=20.0,
    )
    outcome = await execute(custom)
    assert "FLAG=propagated" in outcome.stdout.text

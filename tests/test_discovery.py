"""Executable discovery + version probing units (App. C.2)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from relay.harness.discovery import (
    describe_version,
    probe_version,
    resolve_executable,
)
from relay.harness.errors import HarnessDiscoveryError

PY = sys.executable


def _mk_exec(dir_path: Path, name: str) -> Path:
    target = dir_path / name
    target.write_text("# harness placeholder\n", encoding="utf-8")
    if sys.platform != "win32":
        target.chmod(0o755)
    return target


class TestResolveExecutable:
    def test_explicit_path_wins_when_it_exists(self, tmp_path):
        exe = _mk_exec(tmp_path, "fakeharness.exe")
        resolved = resolve_executable(
            executable_path=str(exe), command_name="fakeharness"
        )
        assert resolved.command == str(exe)
        assert resolved.source == "explicit_path"

    def test_explicit_path_missing_raises_redacted_discovery_error(self, tmp_path):
        ghost = tmp_path / "nowhere" / "ghost.exe"
        with pytest.raises(HarnessDiscoveryError) as excinfo:
            resolve_executable(executable_path=str(ghost), command_name="ghost")
        # The absolute path (and any username inside it) must NOT surface.
        assert "ghost" in str(excinfo.value)
        assert str(tmp_path) not in str(excinfo.value)

    def test_search_paths_resolution(self, tmp_path):
        exe = _mk_exec(tmp_path / "bin" if False else tmp_path, "fakeharness.cmd")
        tool_dir = tmp_path
        del exe
        resolved = resolve_executable(
            executable_path=None,
            command_name="fakeharness",
            search_paths=(tool_dir,),
        )
        assert resolved.source == "search_paths"
        assert Path(resolved.command).parent == tool_dir

    def test_not_found_anywhere_raises_with_command_name(self):
        with pytest.raises(HarnessDiscoveryError) as excinfo:
            resolve_executable(
                executable_path=None,
                command_name="definitely-not-a-real-harness-42",
            )
        assert "definitely-not-a-real-harness-42" in str(excinfo.value)


class TestProbeVersion:
    async def test_version_line_captured_and_redacted(self, tmp_path):
        clean, raw = await probe_version((PY, "-c", "print('fakeharness 1.2.3')"))
        assert clean is not None and clean.startswith("fakeharness")
        assert raw is not None and "fakeharness 1.2.3" in raw

    async def test_missing_executable_is_none_none_never_raises(self):
        clean, raw = await probe_version(("definitely-not-a-real-harness-42",))
        assert clean is None and raw is None

    async def test_stderr_fallback_still_yields_a_line(self, tmp_path):
        script = tmp_path / "_ver.py"
        script.write_text("import sys; print('only-stderr 9.9', file=sys.stderr)\n", encoding="utf-8")
        clean, raw = await probe_version((PY, str(script)))
        assert clean == "only-stderr 9.9"
        assert raw and "only-stderr" in raw


class TestDescribeVersion:
    def test_prefers_digit_bearing_token(self):
        assert describe_version("fakeharness 1.2.3 build") == "1.2.3"

    def test_falls_back_to_first_token(self):
        assert describe_version("some output") == "some"

    def test_none_safe(self):
        assert describe_version(None) is None

"""Executable discovery + version probing units (App. C.2)."""

from __future__ import annotations

import json
import sys

import pytest

from relay.harness.discovery import (
    probe_version,
    resolve_executable,
)
from relay.harness.errors import HarnessDiscoveryError

PY = sys.executable


class TestResolveExecutable:
    def test_explicit_path_missing_raises_redacted_discovery_error(self, tmp_path):
        ghost = tmp_path / "nowhere" / "ghost.exe"
        with pytest.raises(HarnessDiscoveryError) as excinfo:
            resolve_executable(executable_path=str(ghost), command_name="ghost")
        # The absolute path (and any username inside it) must NOT surface.
        assert "ghost" in str(excinfo.value)
        assert str(tmp_path) not in str(excinfo.value)


class TestProbeVersion:
    async def test_real_version_probe_redacts_child_output(self, tmp_path):
        # Failure modes: a credential leaks through the clean line, leaks
        # through the raw transcript, or the probe loses the version line.
        script = tmp_path / "version.py"
        script.write_text(
            "print('fakeharness 1.2.3 OPENAI_API_KEY=supersecret123')\n",
            encoding="utf-8",
        )

        clean, raw = await probe_version((PY, str(script)))
        assert clean == "fakeharness 1.2.3 OPENAI_API_KEY=[REDACTED]"
        assert raw == clean
        assert "supersecret123" not in clean + raw

        artifact = tmp_path / "version-probe.json"
        artifact.write_text(json.dumps({"clean": clean, "raw": raw}), encoding="utf-8")
        assert json.loads(artifact.read_text(encoding="utf-8")) == {
            "clean": clean,
            "raw": raw,
        }

    async def test_stderr_fallback_still_yields_a_line(self, tmp_path):
        script = tmp_path / "_ver.py"
        script.write_text(
            "import sys; print('only-stderr 9.9', file=sys.stderr)\n", encoding="utf-8"
        )
        clean, raw = await probe_version((PY, str(script)))
        assert clean == "only-stderr 9.9"
        assert raw and "only-stderr" in raw

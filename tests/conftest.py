"""Shared test fixtures: a git-repo workspace and an initialized Relay dir."""

from __future__ import annotations

import json
import subprocess
import sys

import pytest
from typer.testing import CliRunner

from relay.cli.main import app

runner = CliRunner()


@pytest.fixture()
def git_repo(tmp_path):
    """A real git repo with one committed file (build target workspace)."""
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.email", "test@relay.local"],
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.name", "Relay Tests"],
        capture_output=True,
        check=True,
    )
    (tmp_path / "README.md").write_text("# fixture repo\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], capture_output=True, check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "commit", "-m", "init"],
        cwd=tmp_path,
        capture_output=True,
        check=True,
    )
    return tmp_path


@pytest.fixture()
def build_workspace(git_repo, monkeypatch):
    """Initialized Relay workspace inside the git repo, configured harness agent."""
    monkeypatch.chdir(git_repo)
    # Pin the fake harness binary to this Python interpreter (json.dumps
    # escapes Windows path separators for valid YAML).
    executable = json.dumps(sys.executable)
    # Write relay.yaml directly (schema-stable) instead of relying on helpers.
    (git_repo / "relay.yaml").write_text(
        "agents:\n"
        "  impl:\n"
        "    backend: harness\n"
        "    adapter: fake_implementer_build\n"
        "    harness:\n"
        f"      executable_path: {executable}\n"
        "      grant: workspace_write\n"
        "      timeout_seconds: 60\n",
        encoding="utf-8",
    )
    runner.invoke(app, ["init"])
    return git_repo

"""relay build flow (P2.2b) — G2 closure tests.

Drives the full build path offline: transient-registered fake harness agent
writes a real file and emits codex-shaped JSONL (file_change / command
events), the orchestrator records observed tool events as ToolRun rows,
extracts a Relay-owned diff artifact, and writes IMPLEMENTATION_PRODUCED
evidence with run provenance — all inside a real git repo + SQLite store.

Hygiene audit (G2): decoy credential shapes in the parent environment must
never reach any persisted byte.
"""

from __future__ import annotations

import json
import subprocess

import pytest
from typer.testing import CliRunner

from relay.agents.base import ToolObservation
from relay.agents.registry import transient_adapters
from relay.cli.main import app
from relay.core.evidence import EvidenceKind
from relay.harness.capabilities import HarnessCapability
from relay.harness.errors import HarnessOutputError
from relay.harness.runtime import HarnessAgent
from relay.harness.sanitization import redact
from relay.storage.models import (
    ArtifactKind,
    EventType,
    Run,
    Task,
    ToolRun,
)
from relay.storage.store import SqliteRelayStore

runner = CliRunner()


# ---------------------------------------------------------------------------
# Fake implementer: a HarnessAgent bound to a local script that edits a file
# ---------------------------------------------------------------------------

_BUILD_SRC = r'''
import json, os, sys
data = sys.stdin.read()
if "--version" in sys.argv:
    print("build-fake 1.0.0"); sys.exit(0)
first_line = data.strip().splitlines()[0] if data.strip() else "empty"
# Simulate an implementation: write one tracked file.
with open("implemented.txt", "w", encoding="utf-8") as handle:
    handle.write("implemented by fake harness\n")
print(json.dumps({"type": "thread.started", "thread_id": "t-build"}))
print(json.dumps({"type": "item.started",
                  "item": {"id": "i1", "type": "command_execution", "command": "echo workspace-edit"}}))
print(json.dumps({"type": "item.completed",
                  "item": {"id": "i1", "type": "command_execution",
                           "command": "echo SECRET_CMD_KEY=leak-attempt"}}))
print(json.dumps({"type": "item.completed",
                  "item": {"id": "f1", "type": "file_change", "path": "implemented.txt"}}))
print(json.dumps({"type": "item.completed",
                  "item": {"id": "m", "type": "agent_message", "text": "done: wrote implemented.txt"}}))
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 42, "output_tokens": 7}}))
'''


class _FakeImplementer(HarnessAgent):
    name = "fake_implementer_build"
    capabilities = frozenset(
        {
            HarnessCapability.READ_ONLY_ACCESS,
            HarnessCapability.WORKSPACE_WRITE,
            HarnessCapability.SHELL_EXECUTION,
        }
    )

    def invocation_argv(self, resolved):
        return (resolved.command, "-c", _BUILD_SRC)

    def parse_output(self, stdout_text, stderr_text):
        """Normalize own JSONL → output + ToolObservations.

        Same neutral seam CodexCLIAdapter uses (blocker 1): core persists
        observations without knowing the fake's event vocabulary either.
        """
        self._last_observations = []
        finals: list[str] = []
        for line in stdout_text.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            item = payload.get("item")
            if not isinstance(item, dict) or not item.get("id"):
                continue
            item_type = str(item.get("type", "unknown"))
            if item_type == "agent_message":
                text = item.get("text")
                if isinstance(text, str) and text:
                    finals.append(text)
            else:
                command = item.get("command")
                self._last_observations.append(
                    ToolObservation(
                        kind="shell" if item_type == "command_execution" else item_type,
                        summary=str(item.get("id", ""))[:120],
                        command=redact(str(command or "")[:200]) or None,
                    )
                )
        if not finals:
            raise HarnessOutputError(f"{self.name}: no final agent message")
        return "\n".join(finals)

    def tool_observations(self):
        return list(getattr(self, "_last_observations", []) or [])


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
    import sys as _sys

    executable = json.dumps(_sys.executable)
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


class TestBuildFlowHappyPath:
    def test_end_to_end_produces_task_run_diff_toolruns_evidence(self, build_workspace):
        with transient_adapters({"fake_implementer_build": _FakeImplementer}):
            result = runner.invoke(app, ["build", "write implemented.txt"])
        assert result.exit_code == 0, result.output

        conn = __import__("relay.storage", fromlist=["connect"]).connect(
            build_workspace / ".relay" / "relay.sqlite3"
        )
        store = SqliteRelayStore(conn)

        # Task created and persisted.
        tasks = list(store.all_models(Task))
        assert len(tasks) == 1
        task = tasks[0]
        assert task.title.startswith("write implemented.txt")

        # Run succeeded — task linkage REQUIRED now (blocker 3).
        runs = list(store.all_models(Run))
        assert len(runs) == 1
        run = runs[0]
        assert run.status.value == "succeeded"
        assert run.task_id == task.id

        # DIFF artifact extracted Relay-owned from the dirty workspace.
        diffs = store.artifacts_for_run(run.id, kind=ArtifactKind.DIFF)
        assert len(diffs) == 1
        diff_text = diffs[0].content or ""
        assert "implemented.txt" in diff_text
        assert "+implemented by fake harness" in diff_text

        # RUN_OUTPUT artifact present.
        outputs = store.artifacts_for_run(run.id, kind=ArtifactKind.RUN_OUTPUT)
        assert len(outputs) == 1

        # Observed harness events recorded as ToolRuns — neutral kinds now
        # (blocker 1: no provider event vocabulary reaches core).
        tool_runs = list(store.all_models(ToolRun))
        types = {tr.tool for tr in tool_runs}
        assert "shell" in types
        assert "file_change" in types

        # IMPLEMENTATION_PRODUCED evidence with valid provenance.
        evidence_store = __import__(
            "relay.storage", fromlist=["SqliteEvidenceStore"]
        ).SqliteEvidenceStore(store)
        records = evidence_store.records_for_task(task.id)
        kinds = {r.kind for r in records}
        assert EvidenceKind.IMPLEMENTATION_PRODUCED in kinds
        impl = next(r for r in records if r.kind is EvidenceKind.IMPLEMENTATION_PRODUCED)
        assert impl.run_id == run.id
        assert impl.produced_by.startswith("agent:")

        conn.close()

    def test_event_log_records_task_and_tool_events(self, build_workspace):
        from relay.storage.events import EventLogWriter

        with transient_adapters({"fake_implementer_build": _FakeImplementer}):
            runner.invoke(app, ["build", "annotate"])
        conn = __import__("relay.storage", fromlist=["connect"]).connect(
            build_workspace / ".relay" / "relay.sqlite3"
        )
        writer = EventLogWriter(conn)
        types = [entry.type for entry in writer.all()]
        assert EventType.TASK_CREATED in types
        assert EventType.TOOL_COMPLETED in types
        assert EventType.ARTIFACT_CREATED in types
        assert EventType.EVIDENCE_RECORDED in types
        conn.close()


class TestBuildRefusals:
    def test_dirty_tracked_file_is_refused(self, build_workspace):
        # Modify a TRACKED file (untracked noise like .relay/ is fine).
        readme = build_workspace / "README.md"
        readme.write_text("# dirty\n", encoding="utf-8")
        result = runner.invoke(app, ["build", "anything"])
        assert result.exit_code == 1
        assert "commit or stash" in result.output

    def test_no_harness_agent_configured_refuses(self, tmp_path, monkeypatch):
        subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
        monkeypatch.chdir(tmp_path)
        (tmp_path / "README.md").write_text("# r\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(tmp_path), "add", "."], capture_output=True, check=True)
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "init"],
            cwd=tmp_path,
            capture_output=True,
            check=True,
        )
        runner.invoke(app, ["init"])  # default config: api-only
        result = runner.invoke(app, ["build", "whatever"])
        assert result.exit_code == 1
        assert "no harness-backed agent" in result.output


class TestDiffProvenance:
    def test_pre_existing_untracked_file_not_attributed_to_harness(self, build_workspace):
        """Blocker 2 regression: a file that existed BEFORE the build (the
        pre-run baseline) must NOT appear in the produced DIFF artifact —
        even though it is untracked and the harness touched nothing."""
        untracked = build_workspace / "pre_existing_notes.txt"
        untracked.write_text("existed before this build ran\n", encoding="utf-8")

        with transient_adapters({"fake_implementer_build": _FakeImplementer}):
            result = runner.invoke(app, ["build", "touch implemented.txt"])
        assert result.exit_code == 0, result.output

        conn = __import__("relay.storage", fromlist=["connect"]).connect(
            build_workspace / ".relay" / "relay.sqlite3"
        )
        store = SqliteRelayStore(conn)
        run = next(iter(store.all_models(Run)))
        diffs = store.artifacts_for_run(run.id, kind=ArtifactKind.DIFF)
        assert len(diffs) == 1
        diff_text = diffs[0].content or ""
        assert "implemented.txt" in diff_text  # harness-produced change present
        assert "pre_existing_notes.txt" not in diff_text  # baseline excluded
        conn.close()

    def test_noop_build_mints_no_diff_and_no_evidence(self, build_workspace):
        """Blocker 4: an empty/no-op build must not mint implementation evidence."""
        with transient_adapters({"fake_implementer_build": _NoopImplementer}):
            result = runner.invoke(app, ["build", "do nothing"])
        assert result.exit_code == 0, result.output

        conn = __import__("relay.storage", fromlist=["connect"]).connect(
            build_workspace / ".relay" / "relay.sqlite3"
        )
        store = SqliteRelayStore(conn)
        task = next(iter(store.all_models(Task)))
        run = next(iter(store.all_models(Run)))
        assert run.status.value == "succeeded"
        assert run.task_id == task.id
        # No workspace change → no DIFF artifact...
        assert store.artifacts_for_run(run.id, kind=ArtifactKind.DIFF) == []
        # ...and therefore NO implementation evidence was recorded.
        evidence_store = __import__(
            "relay.storage", fromlist=["SqliteEvidenceStore"]
        ).SqliteEvidenceStore(store)
        records = evidence_store.records_for_task(task.id)
        assert all(r.kind is not EvidenceKind.IMPLEMENTATION_PRODUCED for r in records)
        conn.close()

    def test_read_only_grant_refused_before_spawn(self, build_workspace):
        """Blocker 4: READ_ONLY_ACCESS build fails typed before any launch."""
        relay_yaml = build_workspace / "relay.yaml"
        text = relay_yaml.read_text(encoding="utf-8")
        relay_yaml.write_text(text.replace("grant: workspace_write", "grant: read_only"), encoding="utf-8")

        with transient_adapters({"fake_implementer_build": _FakeImplementer}):
            result = runner.invoke(app, ["build", "attempt read-only build"])
        assert result.exit_code == 1
        assert "workspace_write" in result.output

        # Refusal happened before spawn: no run rows persisted at all.
        conn = __import__("relay.storage", fromlist=["connect"]).connect(
            build_workspace / ".relay" / "relay.sqlite3"
        )
        store = SqliteRelayStore(conn)
        assert list(store.all_models(Run)) == []
        conn.close()


class _NoopImplementer(_FakeImplementer):
    """Same shape as the implementing fake but writes nothing."""

    name = "fake_noop_build"

    def invocation_argv(self, resolved):
        noop_script = (
            "import json\n"
            "print(json.dumps({'type':'item.completed','item':{'id':'m','type':'agent_message','text':'no-op done'}}))\n"
            "print(json.dumps({'type':'turn.completed'}))\n"
        )
        return (resolved.command, "-c", noop_script)


class TestG2HygieneAudit:
    def test_decoy_credentials_never_reach_any_persisted_byte(self, build_workspace, monkeypatch):
        """Decoy key shapes pollute parent env; none survive into the DB."""
        decoy_key = "sk-decoy-live-key-9f2c4e7a1b8d5f3e"
        monkeypatch.setenv("OPENAI_API_KEY", decoy_key)
        monkeypatch.setenv("CODEX_API_KEY", decoy_key)

        with transient_adapters({"fake_implementer_build": _FakeImplementer}):
            result = runner.invoke(app, ["build", "leak-probe"])
        assert result.exit_code == 0, result.output

        db_path = build_workspace / ".relay" / "relay.sqlite3"
        # WAL may hold recent pages too; audit both main db and wal sidecars.
        for sidecar in ("", "-wal", "-shm"):
            side = db_path.parent / (db_path.name + sidecar)
            if side.exists():
                assert decoy_key.encode() not in side.read_bytes(), f"key leaked into {sidecar or 'main'}"

        # The child echoed a credential-shaped command through ToolRun args;
        # the live key value must not survive anywhere in persisted JSON.
        conn = __import__("relay.storage", fromlist=["connect"]).connect(db_path)
        store = SqliteRelayStore(conn)
        for tr in store.all_models(ToolRun):
            assert decoy_key not in json.dumps(tr.arguments, default=str), (
                f"live decoy key leaked into tool_run {tr.id}"
            )
        conn.close()

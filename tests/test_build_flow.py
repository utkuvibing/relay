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

import asyncio
import json
import subprocess
import sys

import pytest
from typer.testing import CliRunner

from relay.agents.base import AgentRequest, AgentRole, ToolObservation
from relay.agents.config import AgentSettings
from relay.agents.registry import transient_adapters
from relay.cli.main import app
from relay.context.config import HarnessAgentConfig
from relay.core.evidence import EvidenceKind
from relay.core.orchestrator import BuildRefusal, run_build
from relay.core.state_machine import TaskState
from relay.harness.capabilities import HarnessCapability
from relay.harness.errors import HarnessOutputError
from relay.harness.runtime import HarnessAgent
from relay.harness.sanitization import redact
from relay.harness.types import ExecutionGrantKind
from relay.storage.events import EventLogWriter
from relay.storage.models import (
    Artifact,
    ArtifactKind,
    EventType,
    Run,
    Task,
    ToolRun,
)
from relay.storage.store import SqliteEvidenceStore, SqliteRelayStore

runner = CliRunner()


# ---------------------------------------------------------------------------
# Fake implementer: a HarnessAgent bound to a local script that edits a file
# ---------------------------------------------------------------------------

_BUILD_SRC = r"""
import json, os, sys
data = sys.stdin.read()
if "--version" in sys.argv:
    print("build-fake 1.0.0"); sys.exit(0)
if "You are the planner" in data:
    # P3.1: the planning leg — produce a plan, touch nothing.
    print(json.dumps({"type": "thread.started", "thread_id": "t-plan"}))
    print(json.dumps({"type": "item.completed",
                      "item": {"id": "m", "type": "agent_message",
                               "text": "# Plan\n\nGoal: implement the task\n"
                                       "Steps: write implemented.txt\n"
                                       "Files: implemented.txt\n"
                                       "Verification: file exists"}}))
    print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 5, "output_tokens": 3}}))
    sys.exit(0)
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
"""


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

        # Two runs: the READ_ONLY planner and the implementer (P3.1), both
        # task-linked.
        runs = list(store.all_models(Run))
        assert len(runs) == 2
        assert {r.role for r in runs} == {"planner", "implementer"}
        assert all(r.status.value == "succeeded" for r in runs)
        assert all(r.task_id == task.id for r in runs)
        impl_run = next(r for r in runs if r.role == "implementer")

        # DIFF artifact extracted Relay-owned from the dirty workspace.
        diffs = store.artifacts_for_run(impl_run.id, kind=ArtifactKind.DIFF)
        assert len(diffs) == 1
        diff_text = diffs[0].content or ""
        assert "implemented.txt" in diff_text
        assert "+implemented by fake harness" in diff_text

        # RUN_OUTPUT artifact present.
        outputs = store.artifacts_for_run(impl_run.id, kind=ArtifactKind.RUN_OUTPUT)
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
        assert impl.run_id == impl_run.id
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
        for args in (
            ["config", "user.email", "relay-test@local"],
            ["config", "user.name", "Relay Tests"],
        ):
            subprocess.run(["git", "-C", str(tmp_path), *args], capture_output=True, check=True)
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
        impl_run = next(r for r in store.all_models(Run) if r.role == "implementer")
        diffs = store.artifacts_for_run(impl_run.id, kind=ArtifactKind.DIFF)
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
        impl_run = next(r for r in store.all_models(Run) if r.role == "implementer")
        assert impl_run.status.value == "succeeded"
        assert impl_run.task_id == task.id
        # No workspace change → no DIFF artifact...
        assert store.artifacts_for_run(impl_run.id, kind=ArtifactKind.DIFF) == []
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
        relay_yaml.write_text(
            text.replace("grant: workspace_write", "grant: read_only"), encoding="utf-8"
        )

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

    def test_read_only_only_adapter_refuses_build_typed_and_named(self, build_workspace):
        """P2.4 K3 refusal parity, P3.1-updated: the antigravity adapter ships
        READ_ONLY-only (frozen plan Q4 - no per-invocation clamp flag exists
        upstream). Under the wired lifecycle the build fails at the PLANNING
        stage - the mandatory slash-clamp assertion fires against the pinned
        python stub before any write tier could even be resolved - so the
        failed run names the adapter, the task stays honestly blocked at
        CONTEXT_READY, and nothing implementation-shaped is recorded. G2
        machinery stays family-blind."""
        relay_yaml = build_workspace / "relay.yaml"
        text = relay_yaml.read_text(encoding="utf-8")
        relay_yaml.write_text(
            text.replace("adapter: fake_implementer_build", "adapter: antigravity_cli"),
            encoding="utf-8",
        )
        # antigravity_cli lives in the production registry - no transient layer.

        result = runner.invoke(app, ["build", "attempt antigravity build"])
        assert result.exit_code == 0, result.output  # run-level failure, not CLI error
        assert "antigravity_cli" in result.output
        assert "disable-slash-commands" in result.output

        conn = __import__("relay.storage", fromlist=["connect"]).connect(
            build_workspace / ".relay" / "relay.sqlite3"
        )
        store = SqliteRelayStore(conn)
        runs = list(store.all_models(Run))
        assert len(runs) == 1  # the planning run; implementation never started
        assert runs[0].status.value == "failed"
        assert runs[0].role == "planner"
        assert store.artifacts_for_run(runs[0].id, kind=ArtifactKind.DIFF) == []
        evidence_store = __import__(
            "relay.storage", fromlist=["SqliteEvidenceStore"]
        ).SqliteEvidenceStore(store)
        task = next(iter(store.all_models(Task)))
        assert task.state is TaskState.CONTEXT_READY  # blocked before PLAN_READY
        records = evidence_store.records_for_task(task.id)
        assert all(r.kind is not EvidenceKind.IMPLEMENTATION_PRODUCED for r in records)
        assert all(r.kind is not EvidenceKind.PLAN_PRODUCED for r in records)
        conn.close()


class TestTaskLifecycle:
    """P3.1: the deterministic machine owns the task from CREATED.

    Frozen plan (p3-task-state-machine-plan rev 2): context collection by
    relay:core, a READ_ONLY planning run minting the canonical PLAN artifact,
    the D.3 implicit freeze, then the implementation run — every edge
    validated against the EvidenceStore, every transition persisted.
    """

    def test_build_drives_task_through_the_machine(self, build_workspace):
        with transient_adapters({"fake_implementer_build": _FakeImplementer}):
            result = runner.invoke(app, ["build", "write implemented.txt"])
        assert result.exit_code == 0, result.output

        conn = __import__("relay.storage", fromlist=["connect"]).connect(
            build_workspace / ".relay" / "relay.sqlite3"
        )
        store = SqliteRelayStore(conn)
        evidence = SqliteEvidenceStore(store)

        runs = list(store.all_models(Run))
        plan_run = next(r for r in runs if r.role == "planner")
        impl_run = next(r for r in runs if r.role == "implementer")

        # The task ended the slice machine-persisted at IMPLEMENTED.
        task = next(iter(store.all_models(Task)))
        assert task.state is TaskState.IMPLEMENTED

        # The evidence ledger holds every kind the machine demanded, with
        # honest provenance: plan evidence points at the planning run,
        # implementation evidence at the implement run.
        records = evidence.records_for_task(task.id)
        kinds = {r.kind for r in records}
        assert {
            EvidenceKind.CONTEXT_COLLECTED,
            EvidenceKind.PLAN_PRODUCED,
            EvidenceKind.IMPLEMENTATION_PRODUCED,
        } <= kinds
        plan_record = next(r for r in records if r.kind is EvidenceKind.PLAN_PRODUCED)
        assert plan_record.run_id == plan_run.id
        assert plan_record.produced_by.startswith("agent:")
        plan_artifact = store.load_model(Artifact, plan_record.artifact_id)
        assert plan_artifact.kind is ArtifactKind.PLAN
        assert "Goal" in (plan_artifact.content or "")
        impl_record = next(r for r in records if r.kind is EvidenceKind.IMPLEMENTATION_PRODUCED)
        assert impl_record.run_id == impl_run.id

        # Every traversed edge is persisted as a STATE_TRANSITIONED event.
        writer = EventLogWriter(conn)
        contents = [e.content for e in writer.all() if e.type is EventType.STATE_TRANSITIONED]
        assert "task state: created -> context_ready" in contents
        assert "task state: context_ready -> plan_ready" in contents
        assert "task state: plan_ready -> implementing" in contents
        assert "task state: implementing -> implemented" in contents

        # The implement prompt was built from the frozen plan artifact (D.3):
        # downstream agents operate against the canonical plan.
        impl_input = store.artifacts_for_run(impl_run.id, kind=ArtifactKind.RUN_INPUT)
        assert "ACCEPTED PLAN" in (impl_input[0].content or "")
        conn.close()

    def test_noop_build_leaves_task_blocked_at_implementing(self, build_workspace):
        """SPEC §27 Phase 3 exit gate: a build producing no workspace change
        cannot reach IMPLEMENTED — the task stays honestly blocked, with the
        context and plan evidence on record but no implementation proof."""
        with transient_adapters({"fake_implementer_build": _NoopImplementer}):
            result = runner.invoke(app, ["build", "do nothing"])
        assert result.exit_code == 0, result.output

        conn = __import__("relay.storage", fromlist=["connect"]).connect(
            build_workspace / ".relay" / "relay.sqlite3"
        )
        store = SqliteRelayStore(conn)
        evidence = SqliteEvidenceStore(store)
        task = next(iter(store.all_models(Task)))
        assert task.state is TaskState.IMPLEMENTING
        kinds = {r.kind for r in evidence.records_for_task(task.id)}
        assert EvidenceKind.CONTEXT_COLLECTED in kinds
        assert EvidenceKind.PLAN_PRODUCED in kinds
        assert EvidenceKind.IMPLEMENTATION_PRODUCED not in kinds
        conn.close()

    def test_lifecycle_refuses_already_started_task(self, build_workspace):
        """No caller may re-drive a mid-flight lifecycle: guessing at
        in-progress state would transfer workflow authority back to callers."""
        conn = __import__("relay.storage", fromlist=["connect"]).connect(
            build_workspace / ".relay" / "relay.sqlite3"
        )
        store = SqliteRelayStore(conn)
        writer = EventLogWriter(conn)
        evidence = SqliteEvidenceStore(store)
        task = store.save_model(Task(title="in progress", state=TaskState.PLAN_READY))
        agent = _FakeImplementer(
            settings=AgentSettings(adapter="fake_implementer_build"),
            profile=HarnessAgentConfig(
                executable_path=sys.executable,
                grant=ExecutionGrantKind.WORKSPACE_WRITE,
            ),
            workspace_root=build_workspace,
        )
        request = AgentRequest(prompt="x", role=AgentRole.IMPLEMENTER, task_id=task.id)
        with pytest.raises(BuildRefusal, match="lifecycle already started"):
            asyncio.run(
                run_build(store, writer, evidence, agent, request, workspace_root=build_workspace)
            )
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
                assert decoy_key.encode() not in side.read_bytes(), (
                    f"key leaked into {sidecar or 'main'}"
                )

        # The child echoed a credential-shaped command through ToolRun args;
        # the live key value must not survive anywhere in persisted JSON.
        conn = __import__("relay.storage", fromlist=["connect"]).connect(db_path)
        store = SqliteRelayStore(conn)
        for tr in store.all_models(ToolRun):
            assert decoy_key not in json.dumps(tr.arguments, default=str), (
                f"live decoy key leaked into tool_run {tr.id}"
            )
        conn.close()

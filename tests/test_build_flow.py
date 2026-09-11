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
from pathlib import Path
from typing import ClassVar

import pytest
from typer.testing import CliRunner

from relay.agents.base import AgentRequest, AgentRole, ToolObservation
from relay.agents.config import AgentSettings
from relay.agents.registry import transient_adapters
from relay.cli.main import app
from relay.context.config import HarnessAgentConfig, VerificationConfig
from relay.core.evidence import EvidenceKind
from relay.core.orchestrator import (
    BuildRefusal,
    LoopStopReason,
    _capture_baseline,
    _diff_and_state_against_baseline,
    _open_approval_gate,
    _reviewer_for,
    _workspace_state_digest,
    advance_task,
    run_build,
)
from relay.core.permissions import PermissionGate
from relay.core.reviews import decode_review_record
from relay.core.state_machine import TaskState, TaskStateMachine
from relay.harness.capabilities import HarnessCapability
from relay.harness.errors import HarnessOutputError
from relay.harness.runtime import HarnessAgent
from relay.harness.sanitization import redact
from relay.harness.types import ExecutionGrantKind
from relay.storage.events import EventLogWriter
from relay.storage.models import (
    Approval,
    ApprovalStatus,
    Artifact,
    ArtifactKind,
    BuildLoopRecordPayload,
    EventType,
    EvidenceRecord,
    Run,
    RunStatus,
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
argv = sys.argv
if "--version" in argv:
    print("build-fake 1.0.0"); sys.exit(0)
verdict = "PASS"
if "--review-verdict" in argv:
    verdict = argv[argv.index("--review-verdict") + 1].upper()
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
if "You are the reviewer" in data:
    # P6.1: the review leg emits one strict provider-neutral JSON object.
    if "--review-crash" in argv:
        sys.exit(9)
    if verdict == "NONE":
        review_text = "looks fine but no structured report"
    elif verdict == "FINDINGS":
        review_text = json.dumps({
            "schema_version": "relay.review.v1",
            "verdict": "findings",
            "summary": "One issue blocks completion.",
            "findings": [{
                "id": "F1",
                "severity": "medium",
                "title": "Missing regression coverage",
                "description": "The implementation lacks a focused assertion.",
                "requested_change": "Add the focused assertion described by the plan.",
                "validation_expectation": "The configured verification command passes.",
                "location": {"path": "implemented.txt"}
            }]
        })
    else:
        review_text = json.dumps({
            "schema_version": "relay.review.v1",
            "verdict": "pass",
            "summary": "The changes match the plan.",
            "findings": []
        })
    print(json.dumps({"type": "thread.started", "thread_id": "t-review"}))
    print(json.dumps({"type": "item.completed",
                      "item": {"id": "m", "type": "agent_message", "text": review_text}}))
    print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 6, "output_tokens": 2}}))
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

        # The task ended blocked at VERIFYING: P3.1's workspace has no
        # verification config (P3.2 contract — absent config blocks honestly,
        # nothing implementation-shaped or test-shaped is minted past it).
        task = next(iter(store.all_models(Task)))
        assert task.state is TaskState.VERIFYING

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
        assert "task state: implemented -> verifying" in contents

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


def _with_verification(build_workspace, program: str, args: str) -> None:
    """Append a top-level verification block to the fixture relay.yaml."""
    relay_yaml = build_workspace / "relay.yaml"
    relay_yaml.write_text(
        relay_yaml.read_text(encoding="utf-8")
        + "verification:\n"
        + f"  program: {program}\n"
        + f"  args: {args}\n",
        encoding="utf-8",
    )


class TestVerification:
    """P3.2: Relay grades the exam — frozen plan Q-c/Q-f contract."""

    def test_pass_reaches_approval_required_with_provenance(self, build_workspace):
        _with_verification(
            build_workspace, json.dumps(sys.executable), '["-c", "print(\'tests ok\')"]'
        )
        with transient_adapters({"fake_implementer_build": _FakeImplementer}):
            result = runner.invoke(app, ["build", "write implemented.txt"])
        assert result.exit_code == 0, result.output

        conn = __import__("relay.storage", fromlist=["connect"]).connect(
            build_workspace / ".relay" / "relay.sqlite3"
        )
        store = SqliteRelayStore(conn)
        evidence = SqliteEvidenceStore(store)
        task = next(iter(store.all_models(Task)))
        # P3.3: the review leg chains after verification and lands the task
        # on the human's desk (gated default).
        assert task.state is TaskState.APPROVAL_REQUIRED

        records = evidence.records_for_task(task.id)
        passed = next(r for r in records if r.kind is EvidenceKind.TESTS_PASSED)
        assert passed.produced_by == "relay:verification"
        tool_run = store.load_model(ToolRun, passed.tool_run_id)
        assert tool_run is not None
        assert tool_run.tool == "verification"
        assert tool_run.parent_run_id is None  # Relay-owned (D5)
        assert tool_run.status.value == "succeeded"
        artifact = store.load_model(Artifact, passed.artifact_id)
        assert artifact.kind is ArtifactKind.TEST_RESULT
        assert "exit=0" in (artifact.content or "")

        writer = EventLogWriter(conn)
        contents = [e.content for e in writer.all() if e.type is EventType.STATE_TRANSITIONED]
        assert "task state: implemented -> verifying" in contents
        assert "task state: verifying -> reviewing" in contents
        # P3.3: review passed on top — provenance + the open human gate.
        review_record = next(r for r in records if r.kind is EvidenceKind.REVIEW_PASSED)
        assert review_record.produced_by.startswith("agent:")
        approvals = [a for a in store.all_models(Approval) if a.task_id == task.id]
        assert len(approvals) == 1 and approvals[0].status.value == "pending"
        conn.close()

    def test_failure_loops_back_to_implementing(self, build_workspace):
        _with_verification(
            build_workspace, json.dumps(sys.executable), '["-c", "import sys; sys.exit(3)"]'
        )
        with transient_adapters({"fake_implementer_build": _FakeImplementer}):
            result = runner.invoke(app, ["build", "write implemented.txt"])
        assert result.exit_code == 0, result.output  # run succeeded; lifecycle reworked

        conn = __import__("relay.storage", fromlist=["connect"]).connect(
            build_workspace / ".relay" / "relay.sqlite3"
        )
        store = SqliteRelayStore(conn)
        evidence = SqliteEvidenceStore(store)
        task = next(iter(store.all_models(Task)))
        assert task.state is TaskState.IMPLEMENTING  # honest rework loop

        kinds = {r.kind for r in evidence.records_for_task(task.id)}
        assert EvidenceKind.TESTS_PASSED not in kinds  # a failed exam mints nothing

        # The failed exam's output is reachable through the ToolRun's
        # result_ref (the artifact is Relay-owned: no run_id of its own).
        verify_rows = [t for t in store.all_models(ToolRun) if t.tool == "verification"]
        assert len(verify_rows) == 1 and verify_rows[0].status.value == "failed"
        result_artifact = store.load_model(Artifact, verify_rows[0].result_ref)
        assert result_artifact.kind is ArtifactKind.TEST_RESULT
        assert "exit=3" in (result_artifact.content or "")

        writer = EventLogWriter(conn)
        contents = [e.content for e in writer.all() if e.type is EventType.STATE_TRANSITIONED]
        assert "task state: verifying -> implementing" in contents
        conn.close()

    def test_unresolvable_program_blocks_at_verifying(self, build_workspace):
        _with_verification(build_workspace, "definitely-not-a-binary-xyz", "[]")
        with transient_adapters({"fake_implementer_build": _FakeImplementer}):
            result = runner.invoke(app, ["build", "write implemented.txt"])
        assert result.exit_code == 0, result.output

        conn = __import__("relay.storage", fromlist=["connect"]).connect(
            build_workspace / ".relay" / "relay.sqlite3"
        )
        store = SqliteRelayStore(conn)
        evidence = SqliteEvidenceStore(store)
        task = next(iter(store.all_models(Task)))
        assert task.state is TaskState.VERIFYING  # blocked, never "tests failed"

        rows = [t for t in store.all_models(ToolRun) if t.tool == "verification"]
        assert len(rows) == 1
        assert rows[0].status.value == "failed"
        assert rows[0].error is not None and "not found" in rows[0].error
        kinds = {r.kind for r in evidence.records_for_task(task.id)}
        assert EvidenceKind.TESTS_PASSED not in kinds
        conn.close()

    def test_missing_config_blocks_at_verifying(self, build_workspace):
        """No verification block at all: the §27 gap is visible, nothing minted."""
        with transient_adapters({"fake_implementer_build": _FakeImplementer}):
            result = runner.invoke(app, ["build", "write implemented.txt"])
        assert result.exit_code == 0, result.output

        conn = __import__("relay.storage", fromlist=["connect"]).connect(
            build_workspace / ".relay" / "relay.sqlite3"
        )
        store = SqliteRelayStore(conn)
        evidence = SqliteEvidenceStore(store)
        task = next(iter(store.all_models(Task)))
        assert task.state is TaskState.VERIFYING
        rows = [t for t in store.all_models(ToolRun) if t.tool == "verification"]
        assert rows == []
        kinds = {r.kind for r in evidence.records_for_task(task.id)}
        assert EvidenceKind.TESTS_PASSED not in kinds
        conn.close()

    def test_child_env_strips_parent_secrets(self, build_workspace, monkeypatch):
        """C.4 discipline extends to Relay's own executions: the verification
        child sees the baseline-stripped environment, not the parent's."""
        monkeypatch.setenv("DECOY_TEST_TOKEN", "sk-decoy-live-key-9f2c4e7a1b8d5f3e")
        probe = "import os; print(os.environ.get('DECOY_TEST_TOKEN', 'absent'))"
        _with_verification(
            build_workspace, json.dumps(sys.executable), f'["-c", {json.dumps(probe)}]'
        )
        with transient_adapters({"fake_implementer_build": _FakeImplementer}):
            result = runner.invoke(app, ["build", "write implemented.txt"])
        assert result.exit_code == 0, result.output

        conn = __import__("relay.storage", fromlist=["connect"]).connect(
            build_workspace / ".relay" / "relay.sqlite3"
        )
        store = SqliteRelayStore(conn)
        evidence = SqliteEvidenceStore(store)
        task = next(iter(store.all_models(Task)))
        passed = next(
            r for r in evidence.records_for_task(task.id) if r.kind is EvidenceKind.TESTS_PASSED
        )
        artifact = store.load_model(Artifact, passed.artifact_id)
        assert "absent" in (artifact.content or "")
        assert "sk-decoy" not in (artifact.content or "")
        conn.close()


class TestReviewApproval:
    """P3.3: review run + approval closure — frozen plan Q-d/Q-e contract."""

    def test_gated_flagship_reaches_approval_required_then_done(self, build_workspace):
        _with_verification(
            build_workspace, json.dumps(sys.executable), '["-c", "print(\'tests ok\')"]'
        )
        with transient_adapters({"fake_implementer_build": _FakeImplementer}):
            result = runner.invoke(app, ["build", "write implemented.txt"])
        assert result.exit_code == 0, result.output

        conn = __import__("relay.storage", fromlist=["connect"]).connect(
            build_workspace / ".relay" / "relay.sqlite3"
        )
        store = SqliteRelayStore(conn)
        evidence = SqliteEvidenceStore(store)
        task = next(iter(store.all_models(Task)))
        assert task.state is TaskState.APPROVAL_REQUIRED

        approvals = [a for a in store.all_models(Approval) if a.task_id == task.id]
        assert len(approvals) == 1
        assert approvals[0].status.value == "pending"
        assert approvals[0].action.value == "edit_files"

        review_run = next(r for r in store.all_models(Run) if r.role == "reviewer")
        review_record = next(
            r for r in evidence.records_for_task(task.id) if r.kind is EvidenceKind.REVIEW_PASSED
        )
        assert review_record.run_id == review_run.id
        assert review_record.produced_by.startswith("agent:")

        # The human closes it — the only producer the store allows.
        approved = runner.invoke(app, ["approve", task.id, "--by", "kaya"])
        assert approved.exit_code == 0, approved.output
        assert "DONE" in approved.output

        task = store.load_model(Task, task.id)
        assert task.state is TaskState.DONE
        decided = store.load_model(Approval, approvals[0].id)
        assert decided.status.value == "approved" and decided.decided_by == "kaya"
        kinds = {r.kind for r in evidence.records_for_task(task.id)}
        assert EvidenceKind.APPROVAL_GRANTED in kinds
        granted = next(
            r for r in evidence.records_for_task(task.id) if r.kind is EvidenceKind.APPROVAL_GRANTED
        )
        assert granted.produced_by == "human:kaya"

        writer = EventLogWriter(conn)
        contents = [e.content for e in writer.all() if e.type is EventType.STATE_TRANSITIONED]
        assert "task state: approval_required -> done" in contents
        conn.close()

    def test_findings_rework_leaves_no_review_evidence(self, build_workspace):
        _with_verification(
            build_workspace, json.dumps(sys.executable), '["-c", "print(\'tests ok\')"]'
        )

        class FindingsImplementer(_FakeImplementer):
            def invocation_argv(self, resolved):
                return (resolved.command, "-c", _BUILD_SRC, "--review-verdict", "findings")

        with transient_adapters({"fake_implementer_build": FindingsImplementer}):
            result = runner.invoke(app, ["build", "write implemented.txt"])
        assert result.exit_code == 0, result.output

        conn = __import__("relay.storage", fromlist=["connect"]).connect(
            build_workspace / ".relay" / "relay.sqlite3"
        )
        store = SqliteRelayStore(conn)
        evidence = SqliteEvidenceStore(store)
        task = next(iter(store.all_models(Task)))
        assert task.state is TaskState.IMPLEMENTING  # honest rework loop

        kinds = {r.kind for r in evidence.records_for_task(task.id)}
        assert EvidenceKind.REVIEW_PASSED not in kinds
        assert EvidenceKind.APPROVAL_GRANTED not in kinds
        review_artifacts = [
            a for a in store.all_models(Artifact) if a.kind is ArtifactKind.REVIEW_FINDING
        ]
        packets = [
            a for a in store.all_models(Artifact) if a.kind is ArtifactKind.FIX_PACKET
        ]
        assert len(review_artifacts) == 1
        assert len(packets) == 1
        packet = json.loads(packets[0].content or "{}")
        assert packet["schema_version"] == "relay.fix_packet.v1"
        assert packet["review_artifact_id"] == review_artifacts[0].id
        assert len(packet["findings"]) == 1
        conn.close()

    def test_missing_verdict_fails_closed(self, build_workspace):
        _with_verification(
            build_workspace, json.dumps(sys.executable), '["-c", "print(\'tests ok\')"]'
        )

        class NoVerdictImplementer(_FakeImplementer):
            def invocation_argv(self, resolved):
                return (resolved.command, "-c", _BUILD_SRC, "--review-verdict", "none")

        with transient_adapters({"fake_implementer_build": NoVerdictImplementer}):
            result = runner.invoke(app, ["build", "write implemented.txt"])
        assert result.exit_code == 0, result.output

        conn = __import__("relay.storage", fromlist=["connect"]).connect(
            build_workspace / ".relay" / "relay.sqlite3"
        )
        store = SqliteRelayStore(conn)
        evidence = SqliteEvidenceStore(store)
        task = next(iter(store.all_models(Task)))
        assert task.state is TaskState.REVIEWING  # prose can never mint a verdict
        kinds = {r.kind for r in evidence.records_for_task(task.id)}
        assert EvidenceKind.REVIEW_PASSED not in kinds
        reports = [a for a in store.all_models(Artifact) if a.kind is ArtifactKind.REPORT]
        assert reports and "relay.review.invalid.v1" in (reports[-1].content or "")
        assert [a for a in store.all_models(Artifact) if a.kind is ArtifactKind.REVIEW_FINDING] == []
        assert [a for a in store.all_models(Artifact) if a.kind is ArtifactKind.FIX_PACKET] == []
        conn.close()

    def test_direct_mode_reaches_done_without_human(self, build_workspace):
        relay_yaml = build_workspace / "relay.yaml"
        relay_yaml.write_text(
            relay_yaml.read_text(encoding="utf-8") + "approval:\n  mode: direct\n",
            encoding="utf-8",
        )
        _with_verification(
            build_workspace, json.dumps(sys.executable), '["-c", "print(\'tests ok\')"]'
        )
        with transient_adapters({"fake_implementer_build": _FakeImplementer}):
            result = runner.invoke(app, ["build", "write implemented.txt"])
        assert result.exit_code == 0, result.output

        conn = __import__("relay.storage", fromlist=["connect"]).connect(
            build_workspace / ".relay" / "relay.sqlite3"
        )
        store = SqliteRelayStore(conn)
        evidence = SqliteEvidenceStore(store)
        task = next(iter(store.all_models(Task)))
        assert task.state is TaskState.DONE  # A.3 direct path

        records = evidence.records_for_task(task.id)
        kinds = {r.kind for r in records}
        assert {
            EvidenceKind.TESTS_PASSED,
            EvidenceKind.REVIEW_PASSED,
            EvidenceKind.NO_PENDING_APPROVALS,
            EvidenceKind.IMPLEMENTATION_PRODUCED,
        } <= kinds
        no_pending = next(r for r in records if r.kind is EvidenceKind.NO_PENDING_APPROVALS)
        assert no_pending.produced_by == "relay:review"
        assert [a for a in store.all_models(Approval) if a.task_id == task.id] == []
        conn.close()

    def test_dedicated_reviewer_config_is_used(self, build_workspace):
        relay_yaml = build_workspace / "relay.yaml"
        exe = json.dumps(sys.executable)
        text = relay_yaml.read_text(encoding="utf-8")
        # Inject the reviewer as a SIBLING of impl inside the agents mapping
        # (a second top-level 'agents:' key would REPLACE the mapping), then
        # reference it from the top-level reviewer key.
        text = text.replace(
            "      timeout_seconds: 60\n",
            "      timeout_seconds: 60\n"
            "  fake_reviewer:\n"
            "    backend: harness\n"
            "    adapter: fake_implementer_build\n"
            "    model: reviewer-model-1\n"
            f"    harness: {{executable_path: {exe}, grant: workspace_write}}\n",
        )
        relay_yaml.write_text(text + "reviewer: fake_reviewer\n", encoding="utf-8")
        _with_verification(
            build_workspace, json.dumps(sys.executable), '["-c", "print(\'tests ok\')"]'
        )
        with transient_adapters({"fake_implementer_build": _FakeImplementer}):
            result = runner.invoke(app, ["build", "write implemented.txt", "--agent", "impl"])
        assert result.exit_code == 0, result.output

        conn = __import__("relay.storage", fromlist=["connect"]).connect(
            build_workspace / ".relay" / "relay.sqlite3"
        )
        store = SqliteRelayStore(conn)
        evidence = SqliteEvidenceStore(store)
        task = next(iter(store.all_models(Task)))
        assert task.state is TaskState.APPROVAL_REQUIRED
        review_run = next(r for r in store.all_models(Run) if r.role == "reviewer")
        assert review_run.agent == "fake_reviewer"  # Q-d: dedicated reviewer used
        assert review_run.model == "reviewer-model-1"
        review_record = next(
            r for r in evidence.records_for_task(task.id) if r.kind is EvidenceKind.REVIEW_PASSED
        )
        assert review_record.produced_by == "agent:fake_reviewer"
        conn.close()

    def test_harness_reviewer_is_always_bound_read_only(self, build_workspace):
        settings = AgentSettings(adapter="fake_implementer_build")
        configured = _FakeImplementer(
            settings=settings,
            profile=HarnessAgentConfig(
                executable_path=sys.executable,
                grant=ExecutionGrantKind.WORKSPACE_WRITE,
            ),
            workspace_root=build_workspace,
        )
        review_agent = _reviewer_for(configured)
        assert isinstance(review_agent, HarnessAgent)
        assert review_agent.profile is not None
        assert review_agent.profile.grant is ExecutionGrantKind.READ_ONLY_ACCESS

        no_profile = _FakeImplementer(
            settings=settings,
            profile=None,
            workspace_root=build_workspace,
        )
        review_agent = _reviewer_for(no_profile)
        assert review_agent.profile is not None
        assert review_agent.profile.grant is ExecutionGrantKind.READ_ONLY_ACCESS

    def test_review_run_failure_blocks_at_reviewing(self, build_workspace):
        _with_verification(
            build_workspace, json.dumps(sys.executable), '["-c", "print(\'tests ok\')"]'
        )

        class CrashingReviewer(_FakeImplementer):
            def invocation_argv(self, resolved):
                return (resolved.command, "-c", _BUILD_SRC, "--review-crash")

        with transient_adapters({"fake_implementer_build": CrashingReviewer}):
            result = runner.invoke(app, ["build", "write implemented.txt"])
        assert result.exit_code == 0, result.output

        conn = __import__("relay.storage", fromlist=["connect"]).connect(
            build_workspace / ".relay" / "relay.sqlite3"
        )
        store = SqliteRelayStore(conn)
        evidence = SqliteEvidenceStore(store)
        task = next(iter(store.all_models(Task)))
        assert task.state is TaskState.REVIEWING  # blocked: no verdict invented (D8)
        kinds = {r.kind for r in evidence.records_for_task(task.id)}
        assert EvidenceKind.REVIEW_PASSED not in kinds
        conn.close()

    def test_approve_refuses_wrong_state_and_missing_identity(self, build_workspace):
        with transient_adapters({"fake_implementer_build": _FakeImplementer}):
            result = runner.invoke(app, ["build", "write implemented.txt"])
        assert result.exit_code == 0, result.output
        task = next(iter(_all_tasks(build_workspace)))

        # --by is required (explicit provenance).
        missing_by = runner.invoke(app, ["approve", task.id])
        assert missing_by.exit_code != 0

        # A task not at APPROVAL_REQUIRED cannot be approved.
        conn = __import__("relay.storage", fromlist=["connect"]).connect(
            build_workspace / ".relay" / "relay.sqlite3"
        )
        store = SqliteRelayStore(conn)
        fresh = store.save_model(Task(title="fresh", state=TaskState.CREATED))
        conn.close()
        wrong_state = runner.invoke(app, ["approve", fresh.id, "--by", "kaya"])
        assert wrong_state.exit_code == 1
        # rich wraps long lines: assert on wrap-resistant fragments.
        assert "'created'" in wrong_state.output
        assert "can be approved" in wrong_state.output


def _all_tasks(root):
    conn = __import__("relay.storage", fromlist=["connect"]).connect(
        root / ".relay" / "relay.sqlite3"
    )
    try:
        return list(SqliteRelayStore(conn).all_models(Task))
    finally:
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


class TestTaskObservability:
    """P3.4: status/inspect/build endings make machine position visible.

    Everything shown is derived from stored records — the same contract the
    taskview unit tests pin, exercised here through the real build flow.
    """

    def _flagship(self, build_workspace):
        _with_verification(
            build_workspace, json.dumps(sys.executable), '["-c", "print(\'tests ok\')"]'
        )
        with transient_adapters({"fake_implementer_build": _FakeImplementer}):
            result = runner.invoke(app, ["build", "write implemented.txt"])
        assert result.exit_code == 0, result.output
        return result

    def test_build_ending_names_state_and_approve_hint(self, build_workspace):
        result = self._flagship(build_workspace)
        task = next(iter(_all_tasks(build_workspace)))

        # D6: the gated ending is explicit — state + the unblocking command.
        assert "state: approval_required" in result.output
        assert f"relay approve {task.id}" in result.output
        assert "blocked:" not in result.output

    def test_status_shows_position_gap_and_hint(self, build_workspace):
        self._flagship(build_workspace)
        task = next(iter(_all_tasks(build_workspace)))

        status_result = runner.invoke(app, ["status"])
        assert status_result.exit_code == 0, status_result.output
        assert "approval_required" in status_result.output  # table + panel
        assert "missing approval_granted" in status_result.output
        assert f"relay approve {task.id}" in status_result.output
        assert "reviewing -> approval_required" in status_result.output

    def test_status_after_approval_shows_terminal_no_active_panel(self, build_workspace):
        self._flagship(build_workspace)
        task = next(iter(_all_tasks(build_workspace)))
        approved = runner.invoke(app, ["approve", task.id, "--by", "kaya"])
        assert approved.exit_code == 0, approved.output

        status_result = runner.invoke(app, ["status"])
        assert status_result.exit_code == 0, status_result.output
        assert "done" in status_result.output  # table state column
        assert "Active task" not in status_result.output  # nothing non-terminal

    def test_status_shows_verifying_gap_when_config_missing(self, build_workspace):
        """No verification block: the §27 gap is visible, never papered over."""
        with transient_adapters({"fake_implementer_build": _FakeImplementer}):
            result = runner.invoke(app, ["build", "write implemented.txt"])
        assert result.exit_code == 0, result.output

        status_result = runner.invoke(app, ["status"])
        assert status_result.exit_code == 0, status_result.output
        assert "verifying" in status_result.output
        assert "missing tests_passed" in status_result.output

    def test_noop_build_ending_names_the_implementation_gap(self, build_workspace):
        with transient_adapters({"fake_implementer_build": _NoopImplementer}):
            result = runner.invoke(app, ["build", "do nothing"])
        assert result.exit_code == 0, result.output
        assert "state: implementing" in result.output
        assert "blocked: missing implementation_produced" in result.output

    def test_inspect_ledger_renders_records_with_provenance(self, build_workspace):
        self._flagship(build_workspace)
        task = next(iter(_all_tasks(build_workspace)))

        detail = runner.invoke(app, ["inspect", task.id])
        assert detail.exit_code == 0, detail.output
        assert "Transitions" in detail.output
        assert "created -> context_ready" in detail.output
        assert "reviewing -> approval_required" in detail.output
        assert "context_collected by relay:core" in detail.output
        assert "tests_passed by relay:verification" in detail.output
        assert "review_passed by agent:impl" in detail.output
        assert "pending edit_files" in detail.output

    def test_inspect_json_is_a_complete_machine_ledger(self, build_workspace):
        self._flagship(build_workspace)
        task = next(iter(_all_tasks(build_workspace)))

        result = runner.invoke(app, ["inspect", task.id, "--json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["task"]["state"] == "approval_required"
        assert payload["edges"] == [{"to": "done", "missing": ["approval_granted"]}]
        assert payload["transitions"][-1]["to"] == "approval_required"
        kinds = {record["kind"] for record in payload["evidence"]}
        assert {"context_collected", "plan_produced", "tests_passed", "review_passed"} <= kinds
        tests = next(r for r in payload["evidence"] if r["kind"] == "tests_passed")
        assert tests["produced_by"] == "relay:verification"
        assert tests["tool_run_id"] is not None
        assert payload["approvals"][0]["status"] == "pending"

    def test_inspect_resolves_unique_prefix_and_refuses_the_rest(self, build_workspace):
        self._flagship(build_workspace)
        task = next(iter(_all_tasks(build_workspace)))

        # Unique prefix resolves (status shows 8 chars).
        short = runner.invoke(app, ["inspect", task.id[:8]])
        assert short.exit_code == 0, short.output

        # A crafted twin shares the first 4 chars -> ambiguous prefix.
        conn = __import__("relay.storage", fromlist=["connect"]).connect(
            build_workspace / ".relay" / "relay.sqlite3"
        )
        store = SqliteRelayStore(conn)
        store.save_model(Task(id=task.id[:4] + "ffff" + task.id[8:], title="twin"))
        conn.close()
        ambiguous = runner.invoke(app, ["inspect", task.id[:4]])
        assert ambiguous.exit_code == 1
        assert "ambiguous" in ambiguous.output

        unknown = runner.invoke(app, ["inspect", "nope"])
        assert unknown.exit_code == 1
        assert "does not exist" in unknown.output


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


# ---------------------------------------------------------------------------
# P3 hardening: atomic completion boundaries (fault-injection rollback tests)
# ---------------------------------------------------------------------------


class _FaultInjected(Exception):
    """Simulated crash: raised at a chosen write inside an open transaction."""


def _writer_failing_on(content_marker: str):
    """EventLogWriter factory that raises when the marked event is recorded.

    The fault fires mid-transaction — after every earlier write in the same
    ``BEGIN IMMEDIATE`` block — so a clean post-mortem (nothing committed)
    proves the boundary is all-or-nothing: any surviving write would show up
    in the reopened database.
    """

    class _FaultyWriter(EventLogWriter):
        def record(self, entry):
            if entry.type is EventType.STATE_TRANSITIONED and content_marker in entry.content:
                raise _FaultInjected(f"injected fault at: {entry.content}")
            return super().record(entry)

    return _FaultyWriter


class TestAdvanceTaskAtomic:
    """The shared persistence helper itself, at the store level.

    ``advance_task`` is the single transition-persistence path; these tests
    pin its contract directly — companion records commit atomically with the
    transition, and a mid-transaction fault rolls the whole boundary back.
    """

    @staticmethod
    def _reviewing_task(tmp_path, name: str):
        conn = __import__("relay.storage", fromlist=["connect"]).connect(tmp_path / name)
        __import__("relay.storage", fromlist=["migrate"]).migrate(conn)
        store = SqliteRelayStore(conn)
        evidence = SqliteEvidenceStore(store)
        writer = EventLogWriter(conn)
        task = store.save_model(Task(title="gate", state=TaskState.REVIEWING))
        evidence.record(
            EvidenceRecord(
                kind=EvidenceKind.TESTS_PASSED,
                task_id=task.id,
                produced_by="relay:verification",
                tool_run_id="tr-1",
            )
        )
        evidence.record(
            EvidenceRecord(
                kind=EvidenceKind.REVIEW_PASSED,
                task_id=task.id,
                produced_by="agent:impl",
                run_id="run-1",
            )
        )
        return conn, store, evidence, writer, task

    def test_gate_records_commit_atomically_with_the_transition(self, tmp_path):
        conn, store, evidence, writer, task = self._reviewing_task(tmp_path, "atomic-ok.sqlite3")

        approval, event = _open_approval_gate(task)
        machine = TaskStateMachine(task_id=task.id, store=evidence, state=task.state)
        updated = advance_task(
            machine,
            store,
            writer,
            task,
            TaskState.APPROVAL_REQUIRED,
            created_approval=approval,
            events=(event,),
        )
        assert updated.state is TaskState.APPROVAL_REQUIRED
        assert store.load_model(Task, task.id).state is TaskState.APPROVAL_REQUIRED
        rows = [a for a in store.all_models(Approval) if a.task_id == task.id]
        assert len(rows) == 1
        assert rows[0].status is ApprovalStatus.PENDING
        types = [e.type for e in writer.all() if f"task:{task.id}" in e.references]
        assert EventType.APPROVAL_REQUESTED in types
        conn.close()

    def test_fault_rolls_back_the_whole_boundary(self, tmp_path):
        conn, store, evidence, writer, task = self._reviewing_task(tmp_path, "atomic-fault.sqlite3")

        approval, event = _open_approval_gate(task)
        machine = TaskStateMachine(task_id=task.id, store=evidence, state=task.state)
        with pytest.raises(_FaultInjected):
            advance_task(
                machine,
                store,
                _writer_failing_on("reviewing -> approval_required")(conn),
                task,
                TaskState.APPROVAL_REQUIRED,
                created_approval=approval,
                events=(event,),
            )
        # Post-mortem: the boundary never happened — nothing from it committed.
        assert store.load_model(Task, task.id).state is TaskState.REVIEWING
        assert [a for a in store.all_models(Approval) if a.task_id == task.id] == []
        assert EventType.APPROVAL_REQUESTED not in [
            e.type for e in writer.all() if f"task:{task.id}" in e.references
        ]
        # The transaction was released: the store accepts new work.
        assert store.in_transaction is False
        conn.close()


class TestAtomicClosure:
    """Completion/promotion boundaries are all-or-nothing (P3 + P6.1).

    Faults can land on the canonical review artifact, PASS evidence, pending
    approval or direct attestation, or the final transition event. Earlier
    transactions may survive; the promotion's own writes never do.
    """

    @staticmethod
    def _promotion_counts(store, task_id: str):
        kinds = {r.kind for r in SqliteEvidenceStore(store).records_for_task(task_id)}
        reviews = [
            a for a in store.all_models(Artifact) if a.kind is ArtifactKind.REVIEW_FINDING
        ]
        approvals = [a for a in store.all_models(Approval) if a.task_id == task_id]
        return kinds, reviews, approvals

    def test_review_artifact_fault_rolls_back_pass_promotion(self, build_workspace, monkeypatch):
        _with_verification(
            build_workspace, json.dumps(sys.executable), '["-c", "print(\'tests ok\')"]'
        )
        original = SqliteRelayStore.save_model

        def fail_review_artifact(store, record):
            if isinstance(record, Artifact) and record.kind is ArtifactKind.REVIEW_FINDING:
                raise _FaultInjected("review artifact")
            return original(store, record)

        monkeypatch.setattr(SqliteRelayStore, "save_model", fail_review_artifact)
        with transient_adapters({"fake_implementer_build": _FakeImplementer}):
            result = runner.invoke(app, ["build", "write implemented.txt"])
        assert isinstance(result.exception, _FaultInjected)

        conn = __import__("relay.storage", fromlist=["connect"]).connect(
            build_workspace / ".relay" / "relay.sqlite3"
        )
        store = SqliteRelayStore(conn)
        task = next(iter(store.all_models(Task)))
        kinds, reviews, approvals = self._promotion_counts(store, task.id)
        assert task.state is TaskState.REVIEWING
        assert EvidenceKind.REVIEW_PASSED not in kinds
        assert reviews == []
        assert approvals == []
        conn.close()

    def test_review_evidence_fault_rolls_back_pass_promotion(self, build_workspace, monkeypatch):
        _with_verification(
            build_workspace, json.dumps(sys.executable), '["-c", "print(\'tests ok\')"]'
        )
        original = SqliteEvidenceStore.record

        def fail_review_evidence(store, record):
            if record.kind is EvidenceKind.REVIEW_PASSED:
                raise _FaultInjected("review evidence")
            return original(store, record)

        monkeypatch.setattr(SqliteEvidenceStore, "record", fail_review_evidence)
        with transient_adapters({"fake_implementer_build": _FakeImplementer}):
            result = runner.invoke(app, ["build", "write implemented.txt"])
        assert isinstance(result.exception, _FaultInjected)

        conn = __import__("relay.storage", fromlist=["connect"]).connect(
            build_workspace / ".relay" / "relay.sqlite3"
        )
        store = SqliteRelayStore(conn)
        task = next(iter(store.all_models(Task)))
        kinds, reviews, approvals = self._promotion_counts(store, task.id)
        assert task.state is TaskState.REVIEWING
        assert EvidenceKind.REVIEW_PASSED not in kinds
        assert reviews == []
        assert approvals == []
        conn.close()

    def test_approval_fault_rolls_back_pass_promotion(self, build_workspace, monkeypatch):
        _with_verification(
            build_workspace, json.dumps(sys.executable), '["-c", "print(\'tests ok\')"]'
        )
        original = SqliteRelayStore.save_model

        def fail_approval(store, record):
            if isinstance(record, Approval):
                raise _FaultInjected("approval")
            return original(store, record)

        monkeypatch.setattr(SqliteRelayStore, "save_model", fail_approval)
        with transient_adapters({"fake_implementer_build": _FakeImplementer}):
            result = runner.invoke(app, ["build", "write implemented.txt"])
        assert isinstance(result.exception, _FaultInjected)

        conn = __import__("relay.storage", fromlist=["connect"]).connect(
            build_workspace / ".relay" / "relay.sqlite3"
        )
        store = SqliteRelayStore(conn)
        task = next(iter(store.all_models(Task)))
        kinds, reviews, approvals = self._promotion_counts(store, task.id)
        assert task.state is TaskState.REVIEWING
        assert EvidenceKind.REVIEW_PASSED not in kinds
        assert reviews == []
        assert approvals == []
        conn.close()

    def test_direct_attestation_fault_rolls_back_pass_promotion(
        self, build_workspace, monkeypatch
    ):
        relay_yaml = build_workspace / "relay.yaml"
        relay_yaml.write_text(
            relay_yaml.read_text(encoding="utf-8") + "approval:\n  mode: direct\n",
            encoding="utf-8",
        )
        _with_verification(
            build_workspace, json.dumps(sys.executable), '["-c", "print(\'tests ok\')"]'
        )
        original = SqliteEvidenceStore.record

        def fail_attestation(store, record):
            if record.kind is EvidenceKind.NO_PENDING_APPROVALS:
                raise _FaultInjected("direct attestation")
            return original(store, record)

        monkeypatch.setattr(SqliteEvidenceStore, "record", fail_attestation)
        with transient_adapters({"fake_implementer_build": _FakeImplementer}):
            result = runner.invoke(app, ["build", "write implemented.txt"])
        assert isinstance(result.exception, _FaultInjected)

        conn = __import__("relay.storage", fromlist=["connect"]).connect(
            build_workspace / ".relay" / "relay.sqlite3"
        )
        store = SqliteRelayStore(conn)
        task = next(iter(store.all_models(Task)))
        kinds, reviews, approvals = self._promotion_counts(store, task.id)
        assert task.state is TaskState.REVIEWING
        assert EvidenceKind.REVIEW_PASSED not in kinds
        assert EvidenceKind.NO_PENDING_APPROVALS not in kinds
        assert reviews == []
        assert approvals == []
        conn.close()

    def test_fix_packet_fault_rolls_back_findings_promotion(self, build_workspace, monkeypatch):
        _with_verification(
            build_workspace, json.dumps(sys.executable), '["-c", "print(\'tests ok\')"]'
        )

        class FindingsImplementer(_FakeImplementer):
            def invocation_argv(self, resolved):
                return (resolved.command, "-c", _BUILD_SRC, "--review-verdict", "findings")

        original = SqliteRelayStore.save_model

        def fail_packet(store, record):
            if isinstance(record, Artifact) and record.kind is ArtifactKind.FIX_PACKET:
                raise _FaultInjected("fix packet")
            return original(store, record)

        monkeypatch.setattr(SqliteRelayStore, "save_model", fail_packet)
        with transient_adapters({"fake_implementer_build": FindingsImplementer}):
            result = runner.invoke(app, ["build", "write implemented.txt"])
        assert isinstance(result.exception, _FaultInjected)

        conn = __import__("relay.storage", fromlist=["connect"]).connect(
            build_workspace / ".relay" / "relay.sqlite3"
        )
        store = SqliteRelayStore(conn)
        task = next(iter(store.all_models(Task)))
        assert task.state is TaskState.REVIEWING
        kinds = {a.kind for a in store.all_models(Artifact)}
        assert ArtifactKind.REVIEW_FINDING not in kinds
        assert ArtifactKind.FIX_PACKET not in kinds
        conn.close()

    def test_findings_transition_rolls_back_review_and_packet(self, build_workspace, monkeypatch):
        _with_verification(
            build_workspace, json.dumps(sys.executable), '["-c", "print(\'tests ok\')"]'
        )

        class FindingsImplementer(_FakeImplementer):
            def invocation_argv(self, resolved):
                return (resolved.command, "-c", _BUILD_SRC, "--review-verdict", "findings")

        monkeypatch.setattr(
            "relay.cli.main.EventLogWriter",
            _writer_failing_on("reviewing -> implementing"),
        )
        with transient_adapters({"fake_implementer_build": FindingsImplementer}):
            result = runner.invoke(app, ["build", "write implemented.txt"])
        assert isinstance(result.exception, _FaultInjected)

        conn = __import__("relay.storage", fromlist=["connect"]).connect(
            build_workspace / ".relay" / "relay.sqlite3"
        )
        store = SqliteRelayStore(conn)
        task = next(iter(store.all_models(Task)))
        assert task.state is TaskState.REVIEWING
        kinds = {a.kind for a in store.all_models(Artifact)}
        assert ArtifactKind.REVIEW_FINDING not in kinds
        assert ArtifactKind.FIX_PACKET not in kinds
        conn.close()

    def test_gate_transition_rolls_back_approval_request(self, build_workspace, monkeypatch):
        """Crash in REVIEWING -> APPROVAL_REQUIRED: no gate, no approval row."""
        _with_verification(
            build_workspace, json.dumps(sys.executable), '["-c", "print(\'tests ok\')"]'
        )
        monkeypatch.setattr(
            "relay.cli.main.EventLogWriter",
            _writer_failing_on("reviewing -> approval_required"),
        )
        with transient_adapters({"fake_implementer_build": _FakeImplementer}):
            result = runner.invoke(app, ["build", "write implemented.txt"])
        assert isinstance(result.exception, _FaultInjected)

        conn = __import__("relay.storage", fromlist=["connect"]).connect(
            build_workspace / ".relay" / "relay.sqlite3"
        )
        store = SqliteRelayStore(conn)
        evidence = SqliteEvidenceStore(store)
        writer = EventLogWriter(conn)
        task = next(iter(store.all_models(Task)))
        assert task.state is TaskState.REVIEWING  # old side of the edge
        assert [a for a in store.all_models(Approval) if a.task_id == task.id] == []
        types = [e.type for e in writer.all() if f"task:{task.id}" in e.references]
        assert EventType.APPROVAL_REQUESTED not in types
        # The whole PASS promotion rolled back — including its canonical review.
        kinds = {r.kind for r in evidence.records_for_task(task.id)}
        assert EvidenceKind.REVIEW_PASSED not in kinds
        assert [a for a in store.all_models(Artifact) if a.kind is ArtifactKind.REVIEW_FINDING] == []
        conn.close()

    def test_approve_rolls_back_decision_evidence_and_transition(
        self, build_workspace, monkeypatch
    ):
        """Crash in APPROVAL_REQUIRED -> DONE: the human decision, the
        APPROVAL_GRANTED evidence, and their events roll back together."""
        _with_verification(
            build_workspace, json.dumps(sys.executable), '["-c", "print(\'tests ok\')"]'
        )
        with transient_adapters({"fake_implementer_build": _FakeImplementer}):
            built = runner.invoke(app, ["build", "write implemented.txt"])
        assert built.exit_code == 0, built.output
        task = next(iter(_all_tasks(build_workspace)))
        assert task.state is TaskState.APPROVAL_REQUIRED

        monkeypatch.setattr(
            "relay.cli.main.EventLogWriter",
            _writer_failing_on("approval_required -> done"),
        )
        result = runner.invoke(app, ["approve", task.id, "--by", "kaya"])
        assert isinstance(result.exception, _FaultInjected)

        conn = __import__("relay.storage", fromlist=["connect"]).connect(
            build_workspace / ".relay" / "relay.sqlite3"
        )
        store = SqliteRelayStore(conn)
        evidence = SqliteEvidenceStore(store)
        writer = EventLogWriter(conn)
        assert store.load_model(Task, task.id).state is TaskState.APPROVAL_REQUIRED
        pending = [a for a in store.all_models(Approval) if a.task_id == task.id]
        assert len(pending) == 1
        assert pending[0].status is ApprovalStatus.PENDING
        assert pending[0].decided_by is None  # the decision rolled back
        kinds = {r.kind for r in evidence.records_for_task(task.id)}
        assert EvidenceKind.APPROVAL_GRANTED not in kinds
        types = [e.type for e in writer.all() if f"task:{task.id}" in e.references]
        assert EventType.APPROVAL_GRANTED not in types
        conn.close()

    def test_direct_mode_rolls_back_attestation_and_transition(self, build_workspace, monkeypatch):
        """Crash in REVIEWING -> DONE (A.3 direct path): the relay-attested
        empty queue and its event roll back with the transition."""
        relay_yaml = build_workspace / "relay.yaml"
        relay_yaml.write_text(
            relay_yaml.read_text(encoding="utf-8") + "approval:\n  mode: direct\n",
            encoding="utf-8",
        )
        _with_verification(
            build_workspace, json.dumps(sys.executable), '["-c", "print(\'tests ok\')"]'
        )
        monkeypatch.setattr(
            "relay.cli.main.EventLogWriter",
            _writer_failing_on("reviewing -> done"),
        )
        with transient_adapters({"fake_implementer_build": _FakeImplementer}):
            result = runner.invoke(app, ["build", "write implemented.txt"])
        assert isinstance(result.exception, _FaultInjected)

        conn = __import__("relay.storage", fromlist=["connect"]).connect(
            build_workspace / ".relay" / "relay.sqlite3"
        )
        store = SqliteRelayStore(conn)
        evidence = SqliteEvidenceStore(store)
        writer = EventLogWriter(conn)
        task = next(iter(store.all_models(Task)))
        assert task.state is TaskState.REVIEWING
        kinds = {r.kind for r in evidence.records_for_task(task.id)}
        assert EvidenceKind.NO_PENDING_APPROVALS not in kinds  # rolled back
        assert EvidenceKind.REVIEW_PASSED not in kinds  # same promotion transaction
        assert [a for a in store.all_models(Artifact) if a.kind is ArtifactKind.REVIEW_FINDING] == []
        contents = [e.content for e in writer.all() if f"task:{task.id}" in e.references]
        assert "task state: reviewing -> done" not in contents
        conn.close()


class TestBaselineIsolation:
    """Regression: build provenance baselines are execution-local.

    The pre-fix implementation handed baselines off through module-global
    ``_workdir_state``, so a second build's capture overwrote the first
    build's baseline and its diff attributed/removed the wrong files.
    """

    def test_independent_baselines_do_not_contaminate(self, tmp_path):
        from relay.core import orchestrator

        # The global handoff must not exist at all.
        assert not hasattr(orchestrator, "_workdir_state")

        gate = PermissionGate()
        ws_a = tmp_path / "ws-a"
        ws_b = tmp_path / "ws-b"
        ws_a.mkdir()
        ws_b.mkdir()
        (ws_a / "keep.txt").write_text("a-original\n", encoding="utf-8")
        (ws_b / "bfile.txt").write_text("b-original\n", encoding="utf-8")

        # Interleaved captures: build B captures AFTER build A, before A diffs.
        baseline_a = _capture_baseline(ws_a)
        baseline_b = _capture_baseline(ws_b)

        (ws_a / "keep.txt").write_text("a-changed\n", encoding="utf-8")
        (ws_a / "new.txt").write_text("a-new\n", encoding="utf-8")

        diff_a, _ = _diff_and_state_against_baseline(gate, ws_a, "task-a", baseline_a)
        diff_b, _ = _diff_and_state_against_baseline(gate, ws_b, "task-b", baseline_b)

        # A's diff reflects only A's own delta — under the global handoff,
        # B's baseline would have made "keep.txt" look new and "bfile.txt"
        # look deleted.
        assert "modified: keep.txt" in diff_a
        assert "new file: new.txt" in diff_a
        assert "bfile.txt" not in diff_a
        assert diff_b.strip() == ""

    def test_tracked_file_growing_past_cap_is_not_a_phantom_deletion(
        self, tmp_path, monkeypatch
    ):
        """Case A: a baseline-tracked file crossing the cap stays tracked.

        Under the per-scan cap the grown file would drop out of the current
        snapshot while remaining in the baseline — a phantom ``deleted
        file:`` line. Frozen membership keeps it tracked; the rendered diff
        uses a bounded marker and the raw-byte digest still sees the change.
        """
        from relay.core import orchestrator

        monkeypatch.setattr(orchestrator, "_BASELINE_FILE_CAP_BYTES", 64)
        gate = PermissionGate()
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "big.dat").write_bytes(b"a" * 60)  # under cap → tracked member

        baseline = _capture_baseline(ws)
        (ws / "big.dat").write_bytes(b"b" * 100)  # grown past cap

        diff, digest = _diff_and_state_against_baseline(gate, ws, "task", baseline)
        assert "deleted file" not in diff
        assert "big.dat" in diff  # honest bounded representation
        assert digest != _workspace_state_digest(baseline.files)

        # The grown file is read into the tracked set deterministically —
        # an identical rescan produces the identical digest.
        diff2, digest2 = _diff_and_state_against_baseline(gate, ws, "task", baseline)
        assert digest2 == digest
        assert diff2 == diff

    def test_baseline_oversized_file_shrinking_is_not_a_phantom_creation(
        self, tmp_path, monkeypatch
    ):
        """Case B: a baseline-oversized file stays untracked after shrinking.

        Under the per-scan cap the shrunken file would enter the current
        snapshot while absent from the baseline — a phantom ``new file:``
        line. Frozen membership keeps it deliberately untracked, and the
        digest is unchanged by invisible bytes.
        """
        from relay.core import orchestrator

        monkeypatch.setattr(orchestrator, "_BASELINE_FILE_CAP_BYTES", 64)
        gate = PermissionGate()
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "big.dat").write_bytes(b"a" * 100)  # over cap → untracked member

        baseline = _capture_baseline(ws)
        (ws / "big.dat").write_bytes(b"b" * 10)  # shrunk under cap

        diff, digest = _diff_and_state_against_baseline(gate, ws, "task", baseline)
        assert "new file" not in diff
        assert "big.dat" not in diff
        assert diff.strip() == ""
        # An untracked file contributes nothing — digest equals empty state.
        assert digest == _workspace_state_digest({})

    def test_new_file_created_past_cap_stays_bounded_deterministically(
        self, tmp_path, monkeypatch
    ):
        """Post-baseline files keep the size bound — deliberately untracked.

        A new oversized file never enters the tracked set (bounded scanning),
        and the rule is deterministic: every scan reaches the same verdict.
        """
        from relay.core import orchestrator

        monkeypatch.setattr(orchestrator, "_BASELINE_FILE_CAP_BYTES", 64)
        gate = PermissionGate()
        ws = tmp_path / "ws"
        ws.mkdir()

        baseline = _capture_baseline(ws)
        (ws / "huge.dat").write_bytes(b"x" * 200)  # new file, over cap

        diff, digest = _diff_and_state_against_baseline(gate, ws, "task", baseline)
        assert diff.strip() == ""
        assert digest == _workspace_state_digest({})
        _, digest2 = _diff_and_state_against_baseline(gate, ws, "task", baseline)
        assert digest2 == digest

    def test_oversized_tracked_member_is_streamed_not_read_wholesale(
        self, tmp_path, monkeypatch
    ):
        """Bounded I/O: an oversized contract member never hits read_bytes.

        Membership stays tracked, the rendered DIFF carries the bounded
        oversized marker, and the digest sees the real byte change — all
        without the file's contents ever entering memory.
        """
        from relay.core import orchestrator

        monkeypatch.setattr(orchestrator, "_BASELINE_FILE_CAP_BYTES", 64)
        gate = PermissionGate()
        ws = tmp_path / "ws"
        ws.mkdir()
        big = ws / "big.dat"
        big.write_bytes(b"a" * 60)  # under cap → tracked member

        baseline = _capture_baseline(ws)
        big.write_bytes(b"b" * 100)  # grown past cap

        read_calls: list[Path] = []
        real_read_bytes = Path.read_bytes

        def _spy(self: Path) -> bytes:
            read_calls.append(self)
            return real_read_bytes(self)

        monkeypatch.setattr(Path, "read_bytes", _spy)

        diff, digest = _diff_and_state_against_baseline(gate, ws, "task", baseline)
        assert "oversized file big.dat differs" in diff
        assert "deleted file" not in diff
        assert digest != _workspace_state_digest(baseline.files)
        # The whole point of the fix: the oversized member was streamed,
        # never loaded wholesale through read_bytes.
        assert big not in read_calls

    def test_oversized_tracked_member_digest_is_deterministic_and_exact(
        self, tmp_path, monkeypatch
    ):
        """Streamed identity is stable: same oversized state → same digest,
        different oversized bytes → different digest (no-progress exactness).
        """
        from relay.core import orchestrator

        monkeypatch.setattr(orchestrator, "_BASELINE_FILE_CAP_BYTES", 64)
        gate = PermissionGate()
        ws = tmp_path / "ws"
        ws.mkdir()
        big = ws / "big.dat"
        big.write_bytes(b"a" * 60)

        baseline = _capture_baseline(ws)
        big.write_bytes(b"b" * 100)  # oversized member, state 1

        _, digest1 = _diff_and_state_against_baseline(gate, ws, "task", baseline)
        _, digest2 = _diff_and_state_against_baseline(gate, ws, "task", baseline)
        assert digest1 == digest2  # unchanged oversized state → no-progress

        big.write_bytes(b"c" * 100)  # same size, different bytes, still over cap
        diff3, digest3 = _diff_and_state_against_baseline(gate, ws, "task", baseline)
        assert digest3 != digest1  # streamed SHA-256 sees the change
        assert "oversized file big.dat differs" in diff3


# ---------------------------------------------------------------------------
# P6.2 — bounded fix loop (SPEC §27 Phase 6)
# ---------------------------------------------------------------------------

_LOOP_SRC = r"""
import json, sys
data = sys.stdin.read()
argv = sys.argv
if "--version" in argv:
    print("build-fake 1.0.0"); sys.exit(0)
review_counter = __REVIEW_COUNTER__
fix_counter = __FIX_COUNTER__

def _bump(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            n = int(fh.read().strip() or "0")
    except OSError:
        n = 0
    n += 1
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(str(n))
    return n

if "You are the planner" in data:
    if "--plan-crash" in argv:
        sys.exit(9)
    print(json.dumps({"type": "thread.started", "thread_id": "t-plan"}))
    print(json.dumps({"type": "item.completed",
                      "item": {"id": "m", "type": "agent_message",
                               "text": "# Plan\n\nGoal: implement the task\n"
                                       "Steps: write implemented.txt\n"
                                       "Files: implemented.txt\n"
                                       "Verification: file exists"}}))
    print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 5, "output_tokens": 3}}))
    sys.exit(0)
if "You are the reviewer" in data:
    n = _bump(review_counter)
    crash_at = 0
    if "--review-crash-at" in argv:
        crash_at = int(argv[argv.index("--review-crash-at") + 1])
    if crash_at and n == crash_at:
        sys.exit(9)
    seq = ["pass"]
    if "--review-verdicts" in argv:
        seq = [v.strip().upper() for v in argv[argv.index("--review-verdicts") + 1].split(",")]
    verdict = seq[n - 1] if n - 1 < len(seq) else "PASS"
    if verdict == "NONE":
        review_text = "looks fine but no structured report"
    elif verdict == "FINDINGS":
        review_text = json.dumps({
            "schema_version": "relay.review.v1",
            "verdict": "findings",
            "summary": "One issue blocks completion.",
            "findings": [{
                "id": "F1",
                "severity": "medium",
                "title": "Missing regression coverage",
                "description": "The implementation lacks a focused assertion.",
                "requested_change": "Add the focused assertion described by the plan.",
                "validation_expectation": "The configured verification command passes.",
                "location": {"path": "implemented.txt"}
            }]
        })
    else:
        review_text = json.dumps({
            "schema_version": "relay.review.v1",
            "verdict": "pass",
            "summary": "The changes match the plan.",
            "findings": []
        })
    print(json.dumps({"type": "thread.started", "thread_id": "t-review"}))
    print(json.dumps({"type": "item.completed",
                      "item": {"id": "m", "type": "agent_message", "text": review_text}}))
    print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 6, "output_tokens": 2}}))
    sys.exit(0)
# implementation / fix leg — flags shape the workspace write:
#   --fix-crash      fix runs exit nonzero (durable run failure, no retry)
#   --noop           write nothing at all (no-op run → no diff, no evidence)
#   --binary         distinct BINARY bytes per dispatch — every attempt
#                    renders the same 'Binary files differ' text, so only
#                    the raw state digest can tell real progress
#   --identical-fix  fix runs rewrite identical bytes (true no-progress)
if "fix attempt" in data and "--fix-crash" in argv:
    sys.exit(9)
if "--noop" in argv:
    pass
elif "--binary" in argv:
    n = _bump(fix_counter)
    with open("implemented.bin", "wb") as handle:
        handle.write(b"\x00\x01 binary-state-%d \xff\xfe\x00" % n)
elif "fix attempt" in data and "--identical-fix" not in argv:
    n = _bump(fix_counter)
    with open("implemented.txt", "w", encoding="utf-8") as handle:
        handle.write("implemented by fake harness - fix pass %d\n" % n)
else:
    with open("implemented.txt", "w", encoding="utf-8") as handle:
        handle.write("implemented by fake harness\n")
print(json.dumps({"type": "thread.started", "thread_id": "t-build"}))
print(json.dumps({"type": "item.completed",
                  "item": {"id": "m", "type": "agent_message", "text": "done: wrote implemented.txt"}}))
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 42, "output_tokens": 7}}))
"""


def _loop_implementer(tmp_path, *argv_flags):
    """A sequenced fake harness for the fix loop.

    ``--review-verdicts a,b,c`` sequences per-review verdicts (missing →
    pass); ``--review-crash-at N`` exits nonzero on the Nth review;
    ``--identical-fix`` makes fix runs rewrite identical content
    (no-progress); ``--plan-crash`` fails the planning leg. Sequencing
    state lives in counter files under ``.relay/`` — excluded from both
    the baseline and diff extraction, so they never pollute a DIFF.
    """
    src = _LOOP_SRC.replace(
        "__REVIEW_COUNTER__", json.dumps(str(tmp_path / ".relay" / "review-count.txt"))
    ).replace(
        "__FIX_COUNTER__", json.dumps(str(tmp_path / ".relay" / "fix-count.txt"))
    )

    class _Loop(_FakeImplementer):
        # Every AgentRequest this class serves (impl, fix, planner, and
        # reviewer instances alike — _planner_for/_reviewer_for rebind via
        # type(agent)) lands here for assertions on prompt/context_refs.
        seen_requests: ClassVar[list[AgentRequest]] = []

        async def run(self, request):
            type(self).seen_requests.append(request)
            return await super().run(request)

        def invocation_argv(self, resolved):
            return (resolved.command, "-c", src, *argv_flags)

    return _Loop


def _loop_agent(cls, workspace):
    """A workspace-write instance of a sequenced fake for direct run_build calls."""
    return cls(
        settings=AgentSettings(adapter="fake_implementer_build"),
        profile=HarnessAgentConfig(
            executable_path=sys.executable,
            grant=ExecutionGrantKind.WORKSPACE_WRITE,
        ),
        workspace_root=workspace,
    )


def _loop_records(store, task_id: str) -> list:
    """Persisted relay.build.loop.v1 REPORT artifacts for one task."""
    return [
        a
        for a in store.all_models(Artifact)
        if a.kind is ArtifactKind.REPORT
        and a.task_id == task_id
        and "relay.build.loop.v1" in (a.content or "")
    ]


def _decode_loop_record(artifact) -> BuildLoopRecordPayload:
    return BuildLoopRecordPayload.model_validate(json.loads(artifact.content or "{}"))


def _open_store(root):
    conn = __import__("relay.storage", fromlist=["connect"]).connect(
        root / ".relay" / "relay.sqlite3"
    )
    return conn, SqliteRelayStore(conn)


class TestFixLoop:
    """P6.2: findings / failed-verification rework through a bounded loop."""

    def test_findings_then_pass_promotes_with_attempt_scoped_pinning(self, build_workspace):
        """Flagship: review 1 findings → fix run → review 2 pass → gated."""
        _with_verification(
            build_workspace, json.dumps(sys.executable), '["-c", "print(\'tests ok\')"]'
        )
        loop_impl = _loop_implementer(
            build_workspace, "--review-verdicts", "findings,pass"
        )
        with transient_adapters({"fake_implementer_build": loop_impl}):
            result = runner.invoke(app, ["build", "write implemented.txt"])
        assert result.exit_code == 0, result.output
        assert "attempts 2" in result.output
        assert "pass_promoted" in result.output

        conn, store = _open_store(build_workspace)
        task = next(iter(store.all_models(Task)))
        assert task.state is TaskState.APPROVAL_REQUIRED

        # Post-plan run sequence: impl → reviewer(findings) → fix → reviewer(pass).
        runs = list(store.all_models(Run))
        assert [r.role for r in runs] == [
            "planner",
            "implementer",
            "reviewer",
            "implementer",
            "reviewer",
        ]
        fix_run = runs[3]

        # The fix run consumed the persisted packet bytes verbatim.
        packets = [
            a for a in store.all_models(Artifact) if a.kind is ArtifactKind.FIX_PACKET
        ]
        assert len(packets) == 1
        fix_input = store.artifacts_for_run(fix_run.id, kind=ArtifactKind.RUN_INPUT)[0]
        fix_prompt = fix_input.content or ""
        assert "fix attempt 2" in fix_prompt
        assert "FIX PACKET (relay.fix_packet.v1)" in fix_prompt
        assert (packets[0].content or "") in fix_prompt

        # The second review binds the FIX attempt's run/diff/verification.
        reviews = [
            a for a in store.all_models(Artifact) if a.kind is ArtifactKind.REVIEW_FINDING
        ]
        assert len(reviews) == 2
        pass_record = decode_review_record(reviews[-1].content or "")
        subject = pass_record.sources.subject
        fix_diff = store.artifacts_for_run(fix_run.id, kind=ArtifactKind.DIFF)[0]
        verify_rows = [t for t in store.all_models(ToolRun) if t.tool == "verification"]
        assert len(verify_rows) == 2
        assert subject.implementation_run_id == fix_run.id
        assert subject.diff_artifact_id == fix_diff.id
        assert subject.verification_tool_run_id == verify_rows[1].id

        writer = EventLogWriter(conn)
        contents = [e.content for e in writer.all() if e.type is EventType.STATE_TRANSITIONED]
        assert "task state: reviewing -> implementing" in contents
        assert contents.count("task state: implementing -> implemented") == 2
        conn.close()

    def test_budget_exhaustion_parks_and_records_loop_stop(self, build_workspace):
        relay_yaml = build_workspace / "relay.yaml"
        relay_yaml.write_text(
            relay_yaml.read_text(encoding="utf-8") + "budget:\n  max_fix_loops: 1\n",
            encoding="utf-8",
        )
        _with_verification(
            build_workspace, json.dumps(sys.executable), '["-c", "print(\'tests ok\')"]'
        )
        loop_impl = _loop_implementer(
            build_workspace, "--review-verdicts", "findings,findings"
        )
        with transient_adapters({"fake_implementer_build": loop_impl}):
            result = runner.invoke(app, ["build", "write implemented.txt"])
        assert result.exit_code == 0, result.output
        assert "attempts 2" in result.output
        assert "budget_exhausted" in result.output

        conn, store = _open_store(build_workspace)
        task = next(iter(store.all_models(Task)))
        assert task.state is TaskState.IMPLEMENTING
        runs = list(store.all_models(Run))
        assert [r.role for r in runs] == [
            "planner",
            "implementer",
            "reviewer",
            "implementer",
            "reviewer",
        ]
        records = _loop_records(store, task.id)
        assert len(records) == 1
        payload = _decode_loop_record(records[0])
        assert payload.reason == "budget_exhausted"
        assert payload.fix_runs_used == 1
        assert payload.last_review_artifact_id is not None
        assert payload.last_fix_packet_artifact_id is not None
        assert payload.last_diff_artifact_id is not None
        assert store.load_model(Artifact, payload.last_fix_packet_artifact_id).kind is (
            ArtifactKind.FIX_PACKET
        )
        assert store.load_model(Artifact, payload.last_diff_artifact_id).kind is (
            ArtifactKind.DIFF
        )
        conn.close()

    def test_default_bound_allows_at_most_four_attempts(self, build_workspace):
        """max_fix_loops=3 (default) ⇒ ≤1 impl + 3 fix dispatches, 4 reviews."""
        _with_verification(
            build_workspace, json.dumps(sys.executable), '["-c", "print(\'tests ok\')"]'
        )
        loop_impl = _loop_implementer(
            build_workspace,
            "--review-verdicts",
            "findings,findings,findings,findings",
        )
        with transient_adapters({"fake_implementer_build": loop_impl}):
            result = runner.invoke(app, ["build", "write implemented.txt"])
        assert result.exit_code == 0, result.output
        assert "attempts 4" in result.output
        assert "budget_exhausted" in result.output

        conn, store = _open_store(build_workspace)
        task = next(iter(store.all_models(Task)))
        assert task.state is TaskState.IMPLEMENTING
        runs = list(store.all_models(Run))
        assert [r.role for r in runs].count("implementer") == 4
        assert [r.role for r in runs].count("reviewer") == 4
        diffs = [a for a in store.all_models(Artifact) if a.kind is ArtifactKind.DIFF]
        assert len(diffs) == 4
        payload = _decode_loop_record(_loop_records(store, task.id)[0])
        assert payload.reason == "budget_exhausted"
        assert payload.fix_runs_used == 3
        conn.close()

    def test_zero_budget_preserves_p61_one_pass(self, build_workspace):
        relay_yaml = build_workspace / "relay.yaml"
        relay_yaml.write_text(
            relay_yaml.read_text(encoding="utf-8") + "budget:\n  max_fix_loops: 0\n",
            encoding="utf-8",
        )
        _with_verification(
            build_workspace, json.dumps(sys.executable), '["-c", "print(\'tests ok\')"]'
        )
        loop_impl = _loop_implementer(build_workspace, "--review-verdicts", "findings")
        with transient_adapters({"fake_implementer_build": loop_impl}):
            result = runner.invoke(app, ["build", "write implemented.txt"])
        assert result.exit_code == 0, result.output
        assert "attempts 1" in result.output
        assert "loop_disabled" in result.output

        conn, store = _open_store(build_workspace)
        task = next(iter(store.all_models(Task)))
        assert task.state is TaskState.IMPLEMENTING
        runs = list(store.all_models(Run))
        assert [r.role for r in runs] == ["planner", "implementer", "reviewer"]
        assert (
            len(
                [
                    a
                    for a in store.all_models(Artifact)
                    if a.kind is ArtifactKind.FIX_PACKET
                ]
            )
            == 1
        )
        assert _loop_records(store, task.id) == []
        conn.close()

    def test_failed_verification_reworks_then_passes(self, build_workspace):
        """A red exam feeds the fixer its persisted TEST_RESULT, then green."""
        # Under .relay/ so the retry counter never enters the diff.
        counter = build_workspace / ".relay" / "verify-count.txt"
        script = (
            "import sys, pathlib\n"
            f"p = pathlib.Path({json.dumps(str(counter))})\n"
            "n = int(p.read_text() or '0') if p.exists() else 0\n"
            "p.write_text(str(n + 1))\n"
            "sys.exit(3 if n == 0 else 0)\n"
        )
        _with_verification(
            build_workspace, json.dumps(sys.executable), f'["-c", {json.dumps(script)}]'
        )
        loop_impl = _loop_implementer(build_workspace)
        with transient_adapters({"fake_implementer_build": loop_impl}):
            result = runner.invoke(app, ["build", "write implemented.txt"])
        assert result.exit_code == 0, result.output
        assert "attempts 2" in result.output
        assert "pass_promoted" in result.output

        conn, store = _open_store(build_workspace)
        task = next(iter(store.all_models(Task)))
        assert task.state is TaskState.APPROVAL_REQUIRED
        fix_run = [r for r in store.all_models(Run) if r.role == "implementer"][1]
        fix_input = store.artifacts_for_run(fix_run.id, kind=ArtifactKind.RUN_INPUT)[0]
        fix_prompt = fix_input.content or ""
        assert "fix attempt 2" in fix_prompt
        assert "FAILED VERIFICATION OUTPUT" in fix_prompt
        assert "exit=3" in fix_prompt
        verify_rows = [t for t in store.all_models(ToolRun) if t.tool == "verification"]
        assert [t.status.value for t in verify_rows] == ["failed", "succeeded"]
        # The passing review pins the POST-FIX verification records.
        review = next(
            a for a in store.all_models(Artifact) if a.kind is ArtifactKind.REVIEW_FINDING
        )
        subject = decode_review_record(review.content or "").sources.subject
        assert subject.implementation_run_id == fix_run.id
        assert subject.verification_tool_run_id == verify_rows[1].id
        conn.close()

    def test_identical_fix_stops_without_new_evidence(self, build_workspace):
        """A fix run producing a byte-identical workspace = §24 no-progress."""
        _with_verification(
            build_workspace, json.dumps(sys.executable), '["-c", "print(\'tests ok\')"]'
        )
        loop_impl = _loop_implementer(
            build_workspace, "--review-verdicts", "findings", "--identical-fix"
        )
        with transient_adapters({"fake_implementer_build": loop_impl}):
            result = runner.invoke(app, ["build", "write implemented.txt"])
        assert result.exit_code == 0, result.output
        assert "attempts 2" in result.output
        assert "no_workspace_change" in result.output

        conn, store = _open_store(build_workspace)
        evidence = SqliteEvidenceStore(store)
        task = next(iter(store.all_models(Task)))
        assert task.state is TaskState.IMPLEMENTING
        runs = list(store.all_models(Run))
        # The fix run was dispatched but minted nothing new: 1 DIFF, 1
        # IMPLEMENTATION_PRODUCED, 1 reviewer (no second review ran).
        assert [r.role for r in runs].count("implementer") == 2
        assert [r.role for r in runs].count("reviewer") == 1
        diffs = [a for a in store.all_models(Artifact) if a.kind is ArtifactKind.DIFF]
        assert len(diffs) == 1
        impl_evidence = [
            r
            for r in evidence.records_for_task(task.id)
            if r.kind is EvidenceKind.IMPLEMENTATION_PRODUCED
        ]
        assert len(impl_evidence) == 1
        payload = _decode_loop_record(_loop_records(store, task.id)[0])
        assert payload.reason == "no_workspace_change"
        assert payload.fix_runs_used == 1
        # The parked state's diff is still reachable through the record.
        assert payload.last_diff_artifact_id == diffs[0].id
        conn.close()

    def test_reviewer_crash_mid_loop_parks_without_retry(self, build_workspace):
        """P6.1 contract inside the loop: a failed review leg parks at
        REVIEWING — the loop never retries it."""
        _with_verification(
            build_workspace, json.dumps(sys.executable), '["-c", "print(\'tests ok\')"]'
        )
        loop_impl = _loop_implementer(
            build_workspace,
            "--review-verdicts",
            "findings,pass",
            "--review-crash-at",
            "2",
        )
        with transient_adapters({"fake_implementer_build": loop_impl}):
            result = runner.invoke(app, ["build", "write implemented.txt"])
        assert result.exit_code == 0, result.output
        assert "attempts 2" in result.output
        assert "review_blocked" in result.output

        conn, store = _open_store(build_workspace)
        evidence = SqliteEvidenceStore(store)
        task = next(iter(store.all_models(Task)))
        assert task.state is TaskState.REVIEWING
        runs = list(store.all_models(Run))
        reviewer_runs = [r for r in runs if r.role == "reviewer"]
        assert len(reviewer_runs) == 2  # exactly one failed — never retried
        assert reviewer_runs[1].status is RunStatus.FAILED
        kinds = {r.kind for r in evidence.records_for_task(task.id)}
        assert EvidenceKind.REVIEW_PASSED not in kinds
        assert _loop_records(store, task.id) == []
        conn.close()

    def test_planning_failure_counts_zero_attempts(self, build_workspace):
        """Frozen semantics: the planner never counts as an attempt."""
        conn, store = _open_store(build_workspace)
        writer = EventLogWriter(conn)
        evidence = SqliteEvidenceStore(store)
        task = store.save_model(Task(title="planning failure"))
        loop_impl = _loop_implementer(build_workspace, "--plan-crash")
        agent = loop_impl(
            settings=AgentSettings(adapter="fake_implementer_build"),
            profile=HarnessAgentConfig(
                executable_path=sys.executable,
                grant=ExecutionGrantKind.WORKSPACE_WRITE,
            ),
            workspace_root=build_workspace,
        )
        request = AgentRequest(prompt="x", role=AgentRole.IMPLEMENTER, task_id=task.id)
        outcome = asyncio.run(
            run_build(store, writer, evidence, agent, request, workspace_root=build_workspace)
        )
        assert outcome.attempts == 0
        assert outcome.ask is None
        assert outcome.planner is not None
        assert outcome.planner.run.status is RunStatus.FAILED
        assert store.load_model(Task, task.id).state is TaskState.CONTEXT_READY
        conn.close()

    def test_second_findings_promotion_rolls_back_atomically(
        self, build_workspace, monkeypatch
    ):
        """Mid-loop rework edges stay atomic: a crash on the SECOND
        findings → implementing promotion leaves the first committed and
        the second entirely absent."""
        _with_verification(
            build_workspace, json.dumps(sys.executable), '["-c", "print(\'tests ok\')"]'
        )
        loop_impl = _loop_implementer(
            build_workspace, "--review-verdicts", "findings,findings,pass"
        )
        monkeypatch.setattr(
            "relay.cli.main.EventLogWriter",
            _writer_failing_nth("reviewing -> implementing", 2),
        )
        with transient_adapters({"fake_implementer_build": loop_impl}):
            result = runner.invoke(app, ["build", "write implemented.txt"])
        assert isinstance(result.exception, _FaultInjected)

        conn, store = _open_store(build_workspace)
        task = next(iter(store.all_models(Task)))
        assert task.state is TaskState.REVIEWING  # old side of the failed edge
        reviews = [
            a for a in store.all_models(Artifact) if a.kind is ArtifactKind.REVIEW_FINDING
        ]
        packets = [
            a for a in store.all_models(Artifact) if a.kind is ArtifactKind.FIX_PACKET
        ]
        assert len(reviews) == 1  # first promotion committed
        assert len(packets) == 1
        diffs = [a for a in store.all_models(Artifact) if a.kind is ArtifactKind.DIFF]
        assert len(diffs) == 2  # both attempts' work survives honestly
        conn.close()

    def test_pass_promotion_after_fix_loop_rolls_back_atomically(
        self, build_workspace, monkeypatch
    ):
        """A crash on the post-loop PASS promotion leaves the earlier
        findings promotion durable and the gate entirely uncommitted."""
        _with_verification(
            build_workspace, json.dumps(sys.executable), '["-c", "print(\'tests ok\')"]'
        )
        loop_impl = _loop_implementer(
            build_workspace, "--review-verdicts", "findings,pass"
        )
        monkeypatch.setattr(
            "relay.cli.main.EventLogWriter",
            _writer_failing_on("reviewing -> approval_required"),
        )
        with transient_adapters({"fake_implementer_build": loop_impl}):
            result = runner.invoke(app, ["build", "write implemented.txt"])
        assert isinstance(result.exception, _FaultInjected)

        conn, store = _open_store(build_workspace)
        evidence = SqliteEvidenceStore(store)
        task = next(iter(store.all_models(Task)))
        assert task.state is TaskState.REVIEWING
        reviews = [
            a for a in store.all_models(Artifact) if a.kind is ArtifactKind.REVIEW_FINDING
        ]
        assert len(reviews) == 1  # findings review survived; pass review rolled back
        assert (
            decode_review_record(reviews[0].content or "").report.verdict.value == "findings"
        )
        packets = [
            a for a in store.all_models(Artifact) if a.kind is ArtifactKind.FIX_PACKET
        ]
        assert len(packets) == 1
        assert [a for a in store.all_models(Approval) if a.task_id == task.id] == []
        kinds = {r.kind for r in evidence.records_for_task(task.id)}
        assert EvidenceKind.REVIEW_PASSED not in kinds
        conn.close()

    def test_fix_run_failure_returns_only_latest_attempt_facts(self, build_workspace):
        """PR-review: a crashed fixer must not leak attempt-1 records.

        findings → fix dispatched → fixer process fails: every
        latest-attempt field belongs to attempt 2 alone.
        """
        conn, store = _open_store(build_workspace)
        writer = EventLogWriter(conn)
        evidence = SqliteEvidenceStore(store)
        task = store.save_model(Task(title="fixer crash"))
        loop_impl = _loop_implementer(
            build_workspace, "--review-verdicts", "findings", "--fix-crash"
        )
        agent = _loop_agent(loop_impl, build_workspace)
        outcome = asyncio.run(
            run_build(
                store,
                writer,
                evidence,
                agent,
                AgentRequest(
                    prompt="x", role=AgentRole.IMPLEMENTER, task_id=task.id
                ),
                workspace_root=build_workspace,
                verification=VerificationConfig(
                    program=sys.executable, args=["-c", "print('ok')"]
                ),
            )
        )
        assert outcome.attempts == 2
        assert outcome.stop is LoopStopReason.RUN_FAILED
        assert outcome.ask is not None
        assert outcome.ask.response is None
        impl_runs = [r for r in store.all_models(Run) if r.role == "implementer"]
        assert len(impl_runs) == 2
        assert outcome.ask.run.id == impl_runs[1].id
        assert outcome.ask.run.status is RunStatus.FAILED
        # Attempt-1 records must not leak into latest-attempt fields.
        assert outcome.diff_artifact_id is None
        assert outcome.tool_run_ids == ()
        assert outcome.verification is None
        assert outcome.review is None
        assert store.load_model(Task, task.id).state is TaskState.IMPLEMENTING
        # The earlier attempt's ledger stays durable for inspection.
        assert (
            len(
                [
                    a
                    for a in store.all_models(Artifact)
                    if a.kind is ArtifactKind.DIFF
                ]
            )
            == 1
        )
        assert (
            len(
                [
                    a
                    for a in store.all_models(Artifact)
                    if a.kind is ArtifactKind.FIX_PACKET
                ]
            )
            == 1
        )
        conn.close()

    def test_verification_blocked_after_fix_leaks_no_prior_review(
        self, build_workspace
    ):
        """PR-review: a later attempt blocked in VERIFYING returns its own
        verification result and NO review — attempt 1's findings review
        must not leak into outcome.review."""
        counter = build_workspace / ".relay" / "verify-count.txt"
        script = (
            "import sys, pathlib, time\n"
            f"p = pathlib.Path({json.dumps(str(counter))})\n"
            "n = int(p.read_text() or '0') if p.exists() else 0\n"
            "p.write_text(str(n + 1))\n"
            "if n == 0: sys.exit(0)\n"
            "time.sleep(30)\n"
        )
        conn, store = _open_store(build_workspace)
        writer = EventLogWriter(conn)
        evidence = SqliteEvidenceStore(store)
        task = store.save_model(Task(title="blocked verify"))
        loop_impl = _loop_implementer(build_workspace, "--review-verdicts", "findings")
        agent = _loop_agent(loop_impl, build_workspace)
        outcome = asyncio.run(
            run_build(
                store,
                writer,
                evidence,
                agent,
                AgentRequest(
                    prompt="x", role=AgentRole.IMPLEMENTER, task_id=task.id
                ),
                workspace_root=build_workspace,
                verification=VerificationConfig(
                    program=sys.executable,
                    args=["-c", script],
                    timeout_seconds=1,
                ),
            )
        )
        assert outcome.attempts == 2
        assert outcome.stop is LoopStopReason.VERIFICATION_BLOCKED
        assert outcome.ask is not None
        fix_run = outcome.ask.run
        assert fix_run.status is RunStatus.SUCCEEDED
        # Latest-attempt fields: the fix run minted a fresh cumulative DIFF
        # and reached verification — which timed out and parked VERIFYING.
        fix_diffs = store.artifacts_for_run(fix_run.id, kind=ArtifactKind.DIFF)
        assert len(fix_diffs) == 1
        assert outcome.diff_artifact_id == fix_diffs[0].id
        assert outcome.verification is not None
        assert outcome.verification.tool_run is not None
        assert outcome.verification.tool_run.status is RunStatus.FAILED
        assert outcome.review is None  # the attempt-1 findings must not leak
        assert store.load_model(Task, task.id).state is TaskState.VERIFYING
        conn.close()

    def test_binary_change_between_attempts_is_progress(self, build_workspace):
        """PR-review: every binary edit renders identically ('Binary files
        differ') — only the raw state digest can tell real progress."""
        _with_verification(
            build_workspace, json.dumps(sys.executable), '["-c", "print(\'tests ok\')"]'
        )
        loop_impl = _loop_implementer(
            build_workspace, "--review-verdicts", "findings,pass", "--binary"
        )
        with transient_adapters({"fake_implementer_build": loop_impl}):
            result = runner.invoke(app, ["build", "write implemented.bin"])
        assert result.exit_code == 0, result.output
        assert "attempts 2" in result.output
        assert "pass_promoted" in result.output

        conn, store = _open_store(build_workspace)
        diffs = [
            a for a in store.all_models(Artifact) if a.kind is ArtifactKind.DIFF
        ]
        # Two attempts → two DIFFs whose rendered text aliases; the loop
        # correctly saw progress anyway (under rendered-text equality the
        # fix run would have been misjudged no-progress).
        assert len(diffs) == 2
        assert all(
            "Binary files implemented.bin differ" in (a.content or "") for a in diffs
        )
        conn.close()

    def test_preexisting_ignored_trees_mint_no_diff_or_evidence(self, build_workspace):
        """PR-review: baseline and snapshot share ONE exclusion policy — a
        pre-existing node_modules/__pycache__ can never manufacture a fake
        'deleted file' DIFF or IMPLEMENTATION_PRODUCED."""
        (build_workspace / "node_modules" / "pkg").mkdir(parents=True)
        (build_workspace / "node_modules" / "pkg" / "index.js").write_text(
            "x", encoding="utf-8"
        )
        (build_workspace / "__pycache__").mkdir()
        (build_workspace / "__pycache__" / "m.pyc").write_bytes(b"\x00\x01")
        loop_impl = _loop_implementer(build_workspace, "--noop")
        with transient_adapters({"fake_implementer_build": loop_impl}):
            result = runner.invoke(app, ["build", "no-op task"])
        assert result.exit_code == 0, result.output
        assert "attempts 1" in result.output
        assert "no_blocking_input" in result.output

        conn, store = _open_store(build_workspace)
        evidence = SqliteEvidenceStore(store)
        task = next(iter(store.all_models(Task)))
        assert task.state is TaskState.IMPLEMENTING
        assert [
            a for a in store.all_models(Artifact) if a.kind is ArtifactKind.DIFF
        ] == []
        assert [
            r
            for r in evidence.records_for_task(task.id)
            if r.kind is EvidenceKind.IMPLEMENTATION_PRODUCED
        ] == []
        conn.close()

    def test_fix_attempt_context_refs_carry_canonical_inputs(self, build_workspace):
        """PR-review: a fix run keeps the caller's refs and names every
        pinned canonical input the packet certifies — never just the
        blocking artifact."""
        conn, store = _open_store(build_workspace)
        writer = EventLogWriter(conn)
        evidence = SqliteEvidenceStore(store)
        task = store.save_model(Task(title="ctx refs"))
        loop_impl = _loop_implementer(
            build_workspace, "--review-verdicts", "findings,pass"
        )
        agent = _loop_agent(loop_impl, build_workspace)
        outcome = asyncio.run(
            run_build(
                store,
                writer,
                evidence,
                agent,
                AgentRequest(
                    prompt="x",
                    role=AgentRole.IMPLEMENTER,
                    task_id=task.id,
                    context_refs=["room:r9", "artifact:custom"],
                ),
                workspace_root=build_workspace,
                verification=VerificationConfig(
                    program=sys.executable, args=["-c", "print('ok')"]
                ),
            )
        )
        assert outcome.stop is LoopStopReason.PASS_PROMOTED
        impl_requests = [
            r for r in loop_impl.seen_requests if r.role is AgentRole.IMPLEMENTER
        ]
        assert len(impl_requests) == 2
        refs = impl_requests[1].context_refs
        assert refs[:2] == ["room:r9", "artifact:custom"]  # originals preserved first
        assert len(refs) == len(set(refs))  # de-duplicated
        packet = next(
            a for a in store.all_models(Artifact) if a.kind is ArtifactKind.FIX_PACKET
        )
        review = next(
            a for a in store.all_models(Artifact) if a.kind is ArtifactKind.REVIEW_FINDING
        )
        plan = next(a for a in store.all_models(Artifact) if a.kind is ArtifactKind.PLAN)
        impl_run_1 = next(r for r in store.all_models(Run) if r.role == "implementer")
        review_run = next(r for r in store.all_models(Run) if r.role == "reviewer")
        assert f"task:{task.id}" in refs
        assert f"artifact:{plan.id}" in refs
        assert f"artifact:{packet.id}" in refs
        assert f"artifact:{review.id}" in refs  # packet's pinned review artifact
        assert f"run:{impl_run_1.id}" in refs  # pinned implementation run
        assert f"run:{review_run.id}" in refs  # pinned reviewer run
        assert any(r.startswith("evidence:") for r in refs)
        assert any(r.startswith("tool_run:") for r in refs)
        conn.close()

    def test_failed_verification_fix_refs_include_plan_and_result(
        self, build_workspace
    ):
        """The TEST_RESULT blocking input names the failed exam artifact
        and the canonical plan alongside the task."""
        counter = build_workspace / ".relay" / "verify-count.txt"
        script = (
            "import sys, pathlib\n"
            f"p = pathlib.Path({json.dumps(str(counter))})\n"
            "n = int(p.read_text() or '0') if p.exists() else 0\n"
            "p.write_text(str(n + 1))\n"
            "sys.exit(3 if n == 0 else 0)\n"
        )
        conn, store = _open_store(build_workspace)
        writer = EventLogWriter(conn)
        evidence = SqliteEvidenceStore(store)
        task = store.save_model(Task(title="ctx refs test-result"))
        loop_impl = _loop_implementer(build_workspace)
        agent = _loop_agent(loop_impl, build_workspace)
        outcome = asyncio.run(
            run_build(
                store,
                writer,
                evidence,
                agent,
                AgentRequest(
                    prompt="x", role=AgentRole.IMPLEMENTER, task_id=task.id
                ),
                workspace_root=build_workspace,
                verification=VerificationConfig(
                    program=sys.executable, args=["-c", script]
                ),
            )
        )
        assert outcome.stop is LoopStopReason.PASS_PROMOTED
        impl_requests = [
            r for r in loop_impl.seen_requests if r.role is AgentRole.IMPLEMENTER
        ]
        assert len(impl_requests) == 2
        refs = impl_requests[1].context_refs
        test_result = next(
            a for a in store.all_models(Artifact) if a.kind is ArtifactKind.TEST_RESULT
        )
        plan = next(a for a in store.all_models(Artifact) if a.kind is ArtifactKind.PLAN)
        assert f"task:{task.id}" in refs
        assert f"artifact:{plan.id}" in refs
        assert f"artifact:{test_result.id}" in refs
        conn.close()


def _writer_failing_nth(content_marker: str, n: int):
    """Like ``_writer_failing_on`` but fires only on the Nth match."""

    class _FaultyWriter(EventLogWriter):
        def record(self, entry):
            if entry.type is EventType.STATE_TRANSITIONED and content_marker in entry.content:
                self._seen = getattr(self, "_seen", 0) + 1
                if self._seen == n:
                    raise _FaultInjected(f"injected fault at: {entry.content}")
            return super().record(entry)

    return _FaultyWriter

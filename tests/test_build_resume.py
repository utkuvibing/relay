"""P6.3 — resume parked builds via `relay continue` (SPEC §20, §23).

The shared ledger-driven driver serves `relay build` and `relay continue`
identically: the ledger is the cursor, provenance markers bind every
build-dispatched run, budgets gate only NEW implementer dispatches, and
the durable baseline is verified before any resume writes.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import sys

import pytest
from test_build_flow import (
    _FakeImplementer,
    _loop_agent,
    _loop_implementer,
    _open_store,
    _with_verification,
)
from typer.testing import CliRunner

from relay.agents.base import Agent, AgentRequest, AgentRole
from relay.agents.config import AgentSettings
from relay.agents.registry import transient_adapters
from relay.cli.main import app
from relay.context.config import HarnessAgentConfig
from relay.core import baseline as _baseline
from relay.core.baseline import (
    BaselineIntegrityError,
    baseline_dir,
    capture_baseline,
    load_baseline,
    persist_baseline,
)
from relay.core.build_ledger import BUILD_SENDER, ContinueRefusal, derive_position
from relay.core.evidence import EvidenceKind
from relay.core.orchestrator import continue_build, run_ask
from relay.core.state_machine import TaskState
from relay.harness.types import ExecutionGrantKind
from relay.storage.events import EventLogWriter
from relay.storage.models import (
    Artifact,
    ArtifactKind,
    EventLogEntry,
    EventType,
    Message,
    MessageType,
    Run,
    RunStatus,
    Task,
    ToolRun,
)
from relay.storage.store import SqliteEvidenceStore

runner = CliRunner()


def _counts(conn) -> dict[str, int]:
    """Row counts per table — the zero-delta assertion vocabulary."""
    rows = {}
    for table in (
        "runs",
        "artifacts",
        "event_log",
        "evidence_records",
        "tool_runs",
        "tasks",
    ):
        rows[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    return rows


def _markers(conn, task_id: str) -> list[EventLogEntry]:
    writer = EventLogWriter(conn)
    return [
        e
        for e in writer.all()
        if e.type is EventType.BUILD_RUN_DISPATCHED and e.task_id == task_id
    ]


def _stage_of(marker: EventLogEntry) -> str:
    return next(r[12:] for r in marker.references if r.startswith("build_stage:"))


def _run_of(marker: EventLogEntry) -> str:
    return next(r[4:] for r in marker.references if r.startswith("run:"))


def _budget_parked_build(build_workspace, monkeypatch=None, *, verdicts="findings,findings", loops=1):
    """Park a build at IMPLEMENTING with the fix budget exhausted."""
    relay_yaml = build_workspace / "relay.yaml"
    relay_yaml.write_text(
        relay_yaml.read_text(encoding="utf-8") + f"budget:\n  max_fix_loops: {loops}\n",
        encoding="utf-8",
    )
    _with_verification(
        build_workspace, json.dumps(sys.executable), '["-c", "print(\'tests ok\')"]'
    )
    loop_impl = _loop_implementer(build_workspace, "--review-verdicts", verdicts)
    with transient_adapters({"fake_implementer_build": loop_impl}):
        result = runner.invoke(app, ["build", "write implemented.txt"])
    assert result.exit_code == 0, result.output
    assert "budget_exhausted" in result.output
    return loop_impl


def _raise_budget(build_workspace, loops: int) -> None:
    relay_yaml = build_workspace / "relay.yaml"
    text = relay_yaml.read_text(encoding="utf-8")
    relay_yaml.write_text(
        text.replace("max_fix_loops: 1", f"max_fix_loops: {loops}"), encoding="utf-8"
    )


def _delivery_run(store, writer, task: Task, *, status: RunStatus = RunStatus.SUCCEEDED):
    """A task-scoped IMPLEMENTER run bound by P4 MESSAGE_DELIVERED provenance.

    Exercises the exact contaminating shape the plan calls out: same
    ``task_id``, same ``role=implementer``, zero build provenance.
    """
    message = Message(
        sender="human:tester",
        recipient="impl",
        task_id=task.id,
        type=MessageType.NOTE,
        content="please take a look",
    )
    store.save_model(message)

    def bind(run, _artifact):
        return [
            EventLogEntry(
                type=EventType.MESSAGE_DELIVERED,
                task_id=task.id,
                sender="relay:delivery",
                recipient="impl",
                content=f"note bound to run {run.id}",
                references=[f"message:{message.id}", f"run:{run.id}", f"task:{task.id}"],
            )
        ]

    class _Noop(Agent):
        name = "impl"

        async def run(self, request):  # pragma: no cover - shape only
            raise AssertionError("unused")

    # run_ask settles status itself; drive a real (failing) dispatch so the
    # row + marker commit through the genuine Tx1 seam.
    async def _go():
        return await run_ask(
            store,
            writer,
            _Noop(),
            AgentRequest(prompt="delivery", role=AgentRole.IMPLEMENTER, task_id=task.id),
            agent_name="impl",
            pre_provider=bind,
        )

    outcome = asyncio.run(_go())
    run = outcome.run
    if status is RunStatus.RUNNING:
        # A crashed delivery: Tx1 committed, final settle never ran.
        conn_run = store.load_model(Run, run.id)
        return conn_run
    return run


class TestResumeEndToEnd:
    def test_continue_completes_parked_build(self, build_workspace):
        """Flagship: budget-parked build resumes; the persisted packet feeds
        the next fix; the review pins the NEW attempt's records."""
        loop_impl = _budget_parked_build(build_workspace)
        conn, store = _open_store(build_workspace)
        task = next(iter(store.all_models(Task)))
        assert task.state is TaskState.IMPLEMENTING
        impl_runs_before = len(
            [r for r in store.all_models(Run) if r.role == "implementer"]
        )
        conn.close()

        _raise_budget(build_workspace, 3)
        with transient_adapters({"fake_implementer_build": loop_impl}):
            result = runner.invoke(app, ["continue"])
        assert result.exit_code == 0, result.output
        assert "resumed from implementing" in result.output
        assert "pass_promoted" in result.output
        assert "(2 prior)" in result.output

        conn, store = _open_store(build_workspace)
        task = next(iter(store.all_models(Task)))
        assert task.state is TaskState.APPROVAL_REQUIRED
        runs = list(store.all_models(Run))
        assert [r.role for r in runs] == [
            "planner",
            "implementer",
            "reviewer",
            "implementer",
            "reviewer",
            "implementer",  # the resumed fix dispatch
            "reviewer",
        ]
        assert len([r for r in runs if r.role == "implementer"]) == impl_runs_before + 1

        # Exactly one well-formed marker per build-dispatched run.
        markers = _markers(conn, task.id)
        assert len(markers) == len(runs)
        assert sorted(_stage_of(m) for m in markers) == [
            "fix",
            "fix",
            "implement",
            "plan",
            "review",
            "review",
            "review",
        ]
        impl_markers = [m for m in markers if _stage_of(m) in ("implement", "fix")]
        attempts = sorted(
            int(next(r[14:] for r in m.references if r.startswith("build_attempt:")))
            for m in impl_markers
        )
        assert attempts == [1, 2, 3]

        # The final review binds the resumed attempt's run/diff/verification.
        reviews = [
            a for a in store.all_models(Artifact) if a.kind is ArtifactKind.REVIEW_FINDING
        ]
        from relay.core.reviews import decode_review_record

        record = decode_review_record(reviews[-1].content or "")
        fix_run = runs[5]
        fix_diff = store.artifacts_for_run(fix_run.id, kind=ArtifactKind.DIFF)[0]
        verify_rows = [t for t in store.all_models(ToolRun) if t.tool == "verification"]
        assert record.sources.subject.implementation_run_id == fix_run.id
        assert record.sources.subject.diff_artifact_id == fix_diff.id
        assert record.sources.subject.verification_tool_run_id == verify_rows[-1].id
        conn.close()

    def test_continue_replans_a_plan_crashed_build(self, build_workspace):
        """Parked at CONTEXT_READY (planning failed): continue re-dispatches
        the planner — no duplicate context, no duplicate plan artifacts."""
        loop_impl = _loop_implementer(build_workspace, "--plan-crash")
        with transient_adapters({"fake_implementer_build": loop_impl}):
            result = runner.invoke(app, ["build", "write implemented.txt"])
        assert result.exit_code == 0, result.output

        conn, store = _open_store(build_workspace)
        task = next(iter(store.all_models(Task)))
        assert task.state is TaskState.CONTEXT_READY
        conn.close()

        loop_impl2 = _loop_implementer(build_workspace)
        with transient_adapters({"fake_implementer_build": loop_impl2}):
            result = runner.invoke(app, ["continue"])
        assert result.exit_code == 0, result.output
        assert "resumed from context_ready" in result.output
        # No verification configured → the resumed build parks honestly at
        # VERIFYING; the point stands: re-plan minted exactly one artifact.
        assert "verification_blocked" in result.output

        conn, store = _open_store(build_workspace)
        task = next(iter(store.all_models(Task)))
        assert task.state is TaskState.VERIFYING
        plans = [
            a for a in store.all_models(Artifact) if a.kind is ArtifactKind.PLAN
        ]
        assert len(plans) == 1  # the re-plan minted exactly one canonical plan
        planner_runs = [r for r in store.all_models(Run) if r.role == "planner"]
        assert len(planner_runs) == 2  # crashed attempt + resumed attempt
        diffs = [
            a for a in store.all_models(Artifact) if a.kind is ArtifactKind.DIFF
        ]
        assert len(diffs) == 1  # the resumed implement minted the DIFF
        conn.close()


class TestBudgetSemantics:
    def test_exhausted_budget_verifies_existing_diff_without_dispatch(
        self, build_workspace
    ):
        """A1: budget=max_fix_loops:0 (any new dispatch refuses) but the
        next action is VERIFY of an already-persisted DIFF — resumes fully."""
        _with_verification(
            build_workspace,
            '"definitely-not-on-path-relay-verify"',
            "[]",
        )
        loop_impl = _loop_implementer(build_workspace)
        with transient_adapters({"fake_implementer_build": loop_impl}):
            result = runner.invoke(app, ["build", "write implemented.txt"])
        assert result.exit_code == 0, result.output
        assert "verification_blocked" in result.output

        conn, store = _open_store(build_workspace)
        task = next(iter(store.all_models(Task)))
        assert task.state is TaskState.VERIFYING
        impl_before = len(list(store.all_models(Run)))
        diffs = [
            a for a in store.all_models(Artifact) if a.kind is ArtifactKind.DIFF
        ]
        assert len(diffs) == 1  # the persisted DIFF awaiting verification
        conn.close()

        # Fix the verification command AND exhaust the fix budget entirely.
        relay_yaml = build_workspace / "relay.yaml"
        text = relay_yaml.read_text(encoding="utf-8")
        text = text.replace(
            '"definitely-not-on-path-relay-verify"', json.dumps(sys.executable)
        ).replace("[]", '["-c", "print(\'tests ok\')"]')
        relay_yaml.write_text(
            text + "budget:\n  max_fix_loops: 0\n", encoding="utf-8"
        )

        with transient_adapters({"fake_implementer_build": loop_impl}):
            result = runner.invoke(app, ["continue"])
        assert result.exit_code == 0, result.output
        assert "pass_promoted" in result.output

        conn, store = _open_store(build_workspace)
        task = next(iter(store.all_models(Task)))
        assert task.state is TaskState.APPROVAL_REQUIRED
        # Zero new implementer runs: only a review dispatch happened.
        runs = list(store.all_models(Run))
        assert len(runs) == impl_before + 1
        assert runs[-1].role == "reviewer"
        diffs = [
            a for a in store.all_models(Artifact) if a.kind is ArtifactKind.DIFF
        ]
        assert len(diffs) == 1  # never re-minted
        conn.close()

    def test_exhausted_budget_reruns_review_without_dispatch(self, build_workspace):
        """A1 variant: parked at REVIEWING (reviewer run failed) + budget
        exhausted — the review re-runs, PASS promotes, zero impl runs."""
        _with_verification(
            build_workspace, json.dumps(sys.executable), '["-c", "print(\'tests ok\')"]'
        )
        relay_yaml = build_workspace / "relay.yaml"
        relay_yaml.write_text(
            relay_yaml.read_text(encoding="utf-8") + "budget:\n  max_fix_loops: 0\n",
            encoding="utf-8",
        )
        loop_impl = _loop_implementer(build_workspace, "--review-crash-at", "1")
        with transient_adapters({"fake_implementer_build": loop_impl}):
            result = runner.invoke(app, ["build", "write implemented.txt"])
        assert result.exit_code == 0, result.output
        assert "review_blocked" in result.output

        conn, store = _open_store(build_workspace)
        task = next(iter(store.all_models(Task)))
        assert task.state is TaskState.REVIEWING
        conn.close()

        with transient_adapters({"fake_implementer_build": loop_impl}):
            result = runner.invoke(app, ["continue"])
        assert result.exit_code == 0, result.output
        assert "pass_promoted" in result.output

        conn, store = _open_store(build_workspace)
        task = next(iter(store.all_models(Task)))
        assert task.state is TaskState.APPROVAL_REQUIRED
        runs = list(store.all_models(Run))
        assert [r.role for r in runs] == [
            "planner",
            "implementer",
            "reviewer",  # crashed
            "reviewer",  # resumed review — no new impl/fix dispatch
        ]
        conn.close()

    def test_exhausted_budget_new_fix_refuses_then_budget_raise_resumes(
        self, build_workspace
    ):
        """A2: a real new fix IS required and the budget is spent — refuse
        pre-execution with zero delta; raising the budget resumes."""
        loop_impl = _budget_parked_build(build_workspace)
        conn, store = _open_store(build_workspace)
        before = _counts(conn)
        task = next(iter(store.all_models(Task)))
        conn.close()

        with transient_adapters({"fake_implementer_build": loop_impl}):
            result = runner.invoke(app, ["continue", task.id])
        assert result.exit_code == 1
        assert "budget" in result.output

        conn, store = _open_store(build_workspace)
        assert _counts(conn) == before  # zero store delta
        conn.close()

        _raise_budget(build_workspace, 3)
        with transient_adapters({"fake_implementer_build": loop_impl}):
            result = runner.invoke(app, ["continue", task.id])
        assert result.exit_code == 0, result.output
        conn, store = _open_store(build_workspace)
        task = next(iter(store.all_models(Task)))
        assert task.state is TaskState.APPROVAL_REQUIRED
        impl_runs = [r for r in store.all_models(Run) if r.role == "implementer"]
        assert len(impl_runs) == 3
        conn.close()


class TestProvenance:
    def test_p4_implementer_run_never_counts_toward_attempts(self, build_workspace):
        """B1: a delivery-bound task-scoped IMPLEMENTER run changes neither
        the attempt count nor the budget arithmetic."""
        loop_impl = _budget_parked_build(build_workspace)
        conn, store = _open_store(build_workspace)
        writer = EventLogWriter(conn)
        task = next(iter(store.all_models(Task)))
        _delivery_run(store, writer, task)
        conn.commit() if hasattr(conn, "commit") else None
        conn.close()

        _raise_budget(build_workspace, 3)
        with transient_adapters({"fake_implementer_build": loop_impl}):
            result = runner.invoke(app, ["continue"])
        assert result.exit_code == 0, result.output

        conn, store = _open_store(build_workspace)
        task = next(iter(store.all_models(Task)))
        markers = _markers(conn, task.id)
        impl_markers = [m for m in markers if _stage_of(m) in ("implement", "fix")]
        # The delivery run carries NO build marker; the new fix is attempt 3.
        attempts = sorted(
            int(next(r[14:] for r in m.references if r.startswith("build_attempt:")))
            for m in impl_markers
        )
        assert attempts == [1, 2, 3]
        assert task.state is TaskState.APPROVAL_REQUIRED
        conn.close()

    def test_unattributed_task_scoped_run_refuses(self, build_workspace):
        """B3: a task-scoped run bound by NEITHER build nor delivery
        provenance is never guessed into the build — typed refusal, zero
        delta."""
        _budget_parked_build(build_workspace)
        conn, store = _open_store(build_workspace)
        writer = EventLogWriter(conn)
        task = next(iter(store.all_models(Task)))
        # A bare task-scoped run with no marker at all (pre-P6.3 shape).
        asyncio.run(
            run_ask(
                store,
                writer,
                _FakeImplementer(
                    settings=AgentSettings(adapter="fake_implementer_build"),
                    profile=HarnessAgentConfig(
                        executable_path=sys.executable,
                        grant=ExecutionGrantKind.WORKSPACE_WRITE,
                    ),
                    workspace_root=build_workspace,
                ),
                AgentRequest(
                    prompt="rogue", role=AgentRole.IMPLEMENTER, task_id=task.id
                ),
            )
        )
        before = _counts(conn)
        conn.close()

        evidence = SqliteEvidenceStore(store)
        conn, store = _open_store(build_workspace)
        evidence = SqliteEvidenceStore(store)
        writer = EventLogWriter(conn)
        with pytest.raises(ContinueRefusal) as excinfo:
            asyncio.run(
                continue_build(
                    store,
                    writer,
                    evidence,
                    _loop_agent(_loop_implementer(build_workspace), build_workspace),
                    task.id,
                    workspace_root=build_workspace,
                    agent_name="impl",
                )
            )
        assert excinfo.value.code == "unattributed_runs"
        conn.close()

        conn, store = _open_store(build_workspace)
        assert _counts(conn) == before
        conn.close()

    def test_settle_interrupted_leaves_p4_runs_untouched(self, build_workspace):
        """B2/D6: --settle-interrupted cancels only marker-bound build runs;
        a RUNNING P4 delivery is never touched."""
        loop_impl = _budget_parked_build(build_workspace)
        conn, store = _open_store(build_workspace)
        writer = EventLogWriter(conn)
        task = next(iter(store.all_models(Task)))

        # A crashed build dispatch: Tx1 committed the run + its marker, the
        # process died before settle. attempt 3 is consistent with 2 prior.
        crashed = Run(
            agent="impl",
            role="implementer",
            task_id=task.id,
            status=RunStatus.RUNNING,
        )
        store.save_model(crashed)
        writer.record(
            EventLogEntry(
                type=EventType.BUILD_RUN_DISPATCHED,
                task_id=task.id,
                sender=BUILD_SENDER,
                content="build stage 'fix' dispatched",
                references=[
                    f"task:{task.id}",
                    f"run:{crashed.id}",
                    "build_stage:fix",
                    "build_attempt:3",
                ],
            )
        )
        # A crashed P4 delivery still RUNNING on the same task.
        p4_run = Run(
            agent="impl",
            role="implementer",
            task_id=task.id,
            status=RunStatus.RUNNING,
        )
        store.save_model(p4_run)
        writer.record(
            EventLogEntry(
                type=EventType.MESSAGE_DELIVERED,
                task_id=task.id,
                sender="relay:delivery",
                recipient="impl",
                content="delivery bound to run",
                references=["message:m1", f"run:{p4_run.id}", f"task:{task.id}"],
            )
        )
        conn.close()

        # Without the flag: run_in_flight refusal.
        with transient_adapters({"fake_implementer_build": loop_impl}):
            result = runner.invoke(app, ["continue", task.id])
        assert result.exit_code == 1
        assert "settle-interrupted" in result.output

        _raise_budget(build_workspace, 5)
        with transient_adapters({"fake_implementer_build": loop_impl}):
            result = runner.invoke(app, ["continue", task.id, "--settle-interrupted"])
        assert result.exit_code == 0, result.output

        conn, store = _open_store(build_workspace)
        task = next(iter(store.all_models(Task)))
        assert task.state is TaskState.APPROVAL_REQUIRED
        settled = store.load_model(Run, crashed.id)
        assert settled.status is RunStatus.CANCELLED
        untouched = store.load_model(Run, p4_run.id)
        assert untouched.status is RunStatus.RUNNING  # never settled
        # The settled crashed run still counts as attempt 3; the resumed
        # fix is attempt 4.
        markers = _markers(conn, task.id)
        attempts = sorted(
            int(next(r[14:] for r in m.references if r.startswith("build_attempt:")))
            for m in markers
            if _stage_of(m) in ("implement", "fix")
        )
        assert attempts == [1, 2, 3, 4]
        conn.close()

    def test_interrupted_verification_toolrun_settles(self, build_workspace):
        """A verification ToolRun stuck RUNNING blocks resume; settling it
        re-runs the exam under current config."""
        _with_verification(
            build_workspace, json.dumps(sys.executable), '["-c", "print(\'tests ok\')"]'
        )
        loop_impl = _loop_implementer(build_workspace)
        with transient_adapters({"fake_implementer_build": loop_impl}):
            result = runner.invoke(app, ["build", "write implemented.txt"])
        assert result.exit_code == 0, result.output

        conn, store = _open_store(build_workspace)
        writer = EventLogWriter(conn)
        task = next(iter(store.all_models(Task)))
        assert task.state is TaskState.APPROVAL_REQUIRED  # sanity

        # Rewind the task to VERIFYING and plant an in-flight tool run —
        # the "killed mid-exam" shape.
        rewound = task.model_copy(update={"state": TaskState.VERIFYING})
        store.update_model(rewound)
        stuck = ToolRun(
            tool="verification",
            arguments={"program": "x", "args": []},
            status=RunStatus.RUNNING,
        )
        store.save_model(stuck)
        writer.record(
            EventLogEntry(
                type=EventType.TOOL_REQUESTED,
                content="verification requested",
                references=[f"task:{task.id}", f"tool_run:{stuck.id}"],
            )
        )
        conn.close()

        # The task is VERIFYING — but approvals exist? No: APPROVAL_REQUIRED
        # was rewritten; derive on VERIFYING needs a SUCCEEDED earlier tr —
        # the first exam's records are still there. The newest tr is the
        # stuck one → action "verify" after settling.
        with transient_adapters({"fake_implementer_build": loop_impl}):
            result = runner.invoke(app, ["continue", task.id])
        assert result.exit_code == 1
        assert "run" in result.output

        with transient_adapters({"fake_implementer_build": loop_impl}):
            result = runner.invoke(
                app, ["continue", task.id, "--settle-interrupted"]
            )
        assert result.exit_code == 0, result.output

        conn, store = _open_store(build_workspace)
        settled = store.load_model(ToolRun, stuck.id)
        assert settled.status is RunStatus.CANCELLED
        task = next(iter(store.all_models(Task)))
        assert task.state is TaskState.APPROVAL_REQUIRED
        conn.close()


class TestBaselineIntegrity:
    def _parked(self, build_workspace):
        _budget_parked_build(build_workspace)
        conn, store = _open_store(build_workspace)
        task = next(iter(store.all_models(Task)))
        conn.close()
        return task

    def _continue(self, build_workspace, task_id, loop_impl):
        with transient_adapters({"fake_implementer_build": loop_impl}):
            return runner.invoke(app, ["continue", task_id])

    def test_missing_manifest_fails_closed(self, build_workspace):
        loop_impl = _budget_parked_build(build_workspace)
        conn, store = _open_store(build_workspace)
        task = next(iter(store.all_models(Task)))
        conn.close()
        (baseline_dir(build_workspace, task.id) / "manifest.json").unlink()

        before_conn, _store = _open_store(build_workspace)
        before = _counts(before_conn)
        before_conn.close()
        result = self._continue(build_workspace, task.id, loop_impl)
        assert result.exit_code == 1
        assert "baseline" in result.output
        conn, store = _open_store(build_workspace)
        assert _counts(conn) == before
        task = next(iter(store.all_models(Task)))
        assert task.state is TaskState.IMPLEMENTING  # unmoved
        conn.close()

    def test_corrupt_blob_fails_closed(self, build_workspace):
        loop_impl = _budget_parked_build(build_workspace)
        conn, store = _open_store(build_workspace)
        task = next(iter(store.all_models(Task)))
        conn.close()
        blobs = baseline_dir(build_workspace, task.id) / "blobs"
        victim = next(iter(blobs.iterdir()))
        victim.write_bytes(b"tampered")

        result = self._continue(build_workspace, task.id, loop_impl)
        assert result.exit_code == 1
        assert "baseline" in result.output

    def test_tampered_manifest_fails_closed(self, build_workspace):
        loop_impl = _budget_parked_build(build_workspace)
        conn, store = _open_store(build_workspace)
        task = next(iter(store.all_models(Task)))
        conn.close()
        manifest = baseline_dir(build_workspace, task.id) / "manifest.json"
        manifest.write_text(manifest.read_text() + " ", encoding="utf-8")

        result = self._continue(build_workspace, task.id, loop_impl)
        assert result.exit_code == 1
        assert "baseline" in result.output

    def test_reviewing_park_with_corrupt_baseline_fails_closed(self, build_workspace):
        """Baseline verification precedes EVERY resume stage — even a
        REVIEWING park that would never touch the workspace refuses."""
        _with_verification(
            build_workspace, json.dumps(sys.executable), '["-c", "print(\'tests ok\')"]'
        )
        loop_impl = _loop_implementer(build_workspace, "--review-crash-at", "1")
        with transient_adapters({"fake_implementer_build": loop_impl}):
            result = runner.invoke(app, ["build", "write implemented.txt"])
        assert result.exit_code == 0, result.output

        conn, store = _open_store(build_workspace)
        task = next(iter(store.all_models(Task)))
        assert task.state is TaskState.REVIEWING
        conn.close()

        baseline = baseline_dir(build_workspace, task.id)
        shutil.rmtree(baseline)  # snapshot gone entirely
        result = self._continue(build_workspace, task.id, loop_impl)
        assert result.exit_code == 1
        assert "baseline" in result.output

    def test_pin_less_snapshot_with_attempts_refuses(self, build_workspace):
        """attempts >= 1 + no ledger pin → no_baseline: the snapshot is
        never silently trusted or rebuilt."""
        _budget_parked_build(build_workspace)
        conn, store = _open_store(build_workspace)
        task = next(iter(store.all_models(Task)))
        pin = next(
            a
            for a in store.all_models(Artifact)
            if a.kind is ArtifactKind.REPORT
            and "relay.build.baseline.v1" in (a.content or "")
        )
        conn.execute("DELETE FROM artifacts WHERE id = ?", [pin.id])
        conn.commit()
        conn.close()

        conn, store = _open_store(build_workspace)
        evidence = SqliteEvidenceStore(store)
        writer = EventLogWriter(conn)
        with pytest.raises(ContinueRefusal) as excinfo:
            asyncio.run(
                continue_build(
                    store,
                    writer,
                    evidence,
                    _loop_agent(_loop_implementer(build_workspace), build_workspace),
                    task.id,
                    workspace_root=build_workspace,
                    agent_name="impl",
                )
            )
        assert excinfo.value.code == "no_baseline"
        conn.close()

    def test_load_baseline_unit_contracts(self, tmp_path):
        """count drift, foreign task, and non-canonical manifests all fail
        closed at the load_baseline boundary itself."""
        root = tmp_path / "ws"
        root.mkdir()
        (root / "a.txt").write_text("hello", encoding="utf-8")
        snapshot = capture_baseline(root)
        pin = persist_baseline(root, "t1", snapshot)
        loaded = load_baseline(root, "t1", pin)
        assert set(loaded.files) == {"a.txt"}

        wrong_count = pin.model_copy(update={"file_count": pin.file_count + 1})
        with pytest.raises(BaselineIntegrityError) as excinfo:
            load_baseline(root, "t1", wrong_count)
        assert excinfo.value.code == "count_mismatch"

        with pytest.raises(BaselineIntegrityError) as excinfo:
            load_baseline(root, "other-task", pin)
        assert excinfo.value.code == "manifest_missing"

        # A manifest that decodes but isn't canonical fails the pin digest.
        manifest = baseline_dir(root, "t1") / "manifest.json"
        raw = json.loads(manifest.read_text(encoding="utf-8"))
        manifest.write_text(json.dumps(raw, indent=2), encoding="utf-8")
        with pytest.raises(BaselineIntegrityError) as excinfo:
            load_baseline(root, "t1", pin)
        assert excinfo.value.code == "manifest_digest_mismatch"

    def test_publish_ordering_fsync_then_rename_then_parent_fsync(
        self, tmp_path, monkeypatch
    ):
        """The acceptance ordering: staging fsync → atomic rename → parent
        dir fsync — and the pin only exists after publish returns."""
        calls: list[str] = []
        real_fsync = _baseline._fsync_dir
        real_publish = _baseline._publish_dir

        def spy_fsync(path):
            calls.append(f"fsync:{path.name}")
            return real_fsync(path)

        def spy_publish(staging, final):
            calls.append("rename")
            return real_publish(staging, final)

        monkeypatch.setattr(_baseline, "_fsync_dir", spy_fsync)
        monkeypatch.setattr(_baseline, "_publish_dir", spy_publish)

        root = tmp_path / "ws"
        root.mkdir()
        (root / "a.txt").write_text("hello", encoding="utf-8")
        pin = persist_baseline(root, "t1", capture_baseline(root))

        staging_name = "t1.staging"
        assert calls == [f"fsync:{staging_name}", "rename", "fsync:baselines"]
        manifest = baseline_dir(root, "t1") / "manifest.json"
        assert (
            hashlib.sha256(manifest.read_bytes()).hexdigest() == pin.manifest_digest
        )


class TestCrashSafety:
    def test_run_ask_settles_cancelled_run(self, build_workspace):
        """CancelledError settles the run row CANCELLED — never RUNNING."""
        conn, store = _open_store(build_workspace)
        writer = EventLogWriter(conn)

        class _Canceller(Agent):
            name = "canceller"

            async def run(self, request):
                raise asyncio.CancelledError()

        with pytest.raises(asyncio.CancelledError):
            asyncio.run(
                run_ask(
                    store,
                    writer,
                    _Canceller(),
                    AgentRequest(prompt="x", role=AgentRole.RESEARCHER),
                )
            )
        run = next(iter(store.all_models(Run)))
        assert run.status is RunStatus.CANCELLED
        conn.close()

    def test_atomic_diff_evidence_mint_rolls_back(self, build_workspace, monkeypatch):
        """Fault inside the DIFF+evidence transaction leaves neither behind;
        continue re-dispatches from the ledger — no half-minted boundary."""
        _with_verification(
            build_workspace, json.dumps(sys.executable), '["-c", "print(\'tests ok\')"]'
        )
        loop_impl = _loop_implementer(build_workspace)

        real_record = SqliteEvidenceStore.record
        fired = {"n": 0}

        def flaky_record(self, record):
            if record.kind is EvidenceKind.IMPLEMENTATION_PRODUCED and not fired["n"]:
                fired["n"] = 1
                raise RuntimeError("injected mint fault")
            return real_record(self, record)

        monkeypatch.setattr(SqliteEvidenceStore, "record", flaky_record)
        with transient_adapters({"fake_implementer_build": loop_impl}):
            result = runner.invoke(app, ["build", "write implemented.txt"])
        assert result.exit_code != 0

        conn, store = _open_store(build_workspace)
        task = next(iter(store.all_models(Task)))
        assert task.state is TaskState.IMPLEMENTING
        assert not [
            a for a in store.all_models(Artifact) if a.kind is ArtifactKind.DIFF
        ]
        evidence = SqliteEvidenceStore(store)
        assert not evidence.records_for_task(
            task.id, EvidenceKind.IMPLEMENTATION_PRODUCED
        )
        conn.close()

        monkeypatch.setattr(SqliteEvidenceStore, "record", real_record)
        # Continue 1: re-dispatch writes the SAME bytes the mint-faulted run
        # already left on disk → raw state digest identical → honest
        # no_workspace_change park (never a phantom mint).
        with transient_adapters({"fake_implementer_build": loop_impl}):
            result = runner.invoke(app, ["continue", task.id])
        assert result.exit_code == 0, result.output
        assert "no_workspace_change" in result.output

        conn, store = _open_store(build_workspace)
        task = next(iter(store.all_models(Task)))
        assert task.state is TaskState.IMPLEMENTING
        conn.close()

        # Continue 2: with the orphaned write removed, the re-dispatch
        # produces genuinely new state → mint → verify → review → gated.
        (build_workspace / "implemented.txt").unlink()
        with transient_adapters({"fake_implementer_build": loop_impl}):
            result = runner.invoke(app, ["continue", task.id])
        assert result.exit_code == 0, result.output
        conn, store = _open_store(build_workspace)
        task = next(iter(store.all_models(Task)))
        assert task.state is TaskState.APPROVAL_REQUIRED
        impl_runs = [r for r in store.all_models(Run) if r.role == "implementer"]
        assert len(impl_runs) == 3  # faulted + no-progress + completing
        conn.close()


class TestContinueRefusals:
    def test_no_request_refuses(self, build_workspace):
        """A task that was never built has no durable request record."""
        conn, store = _open_store(build_workspace)
        store.save_model(Task(title="orphan"))
        task = next(iter(store.all_models(Task)))
        conn.close()
        result = runner.invoke(app, ["continue", task.id])
        assert result.exit_code == 1
        assert "no durable build request" in result.output

    def test_done_task_is_terminal(self, build_workspace):
        _with_verification(
            build_workspace, json.dumps(sys.executable), '["-c", "print(\'tests ok\')"]'
        )
        relay_yaml = build_workspace / "relay.yaml"
        relay_yaml.write_text(
            relay_yaml.read_text(encoding="utf-8") + "approval:\n  mode: direct\n",
            encoding="utf-8",
        )
        loop_impl = _loop_implementer(build_workspace)
        with transient_adapters({"fake_implementer_build": loop_impl}):
            result = runner.invoke(app, ["build", "write implemented.txt"])
        assert result.exit_code == 0, result.output

        conn, store = _open_store(build_workspace)
        task = next(iter(store.all_models(Task)))
        assert task.state is TaskState.DONE
        conn.close()
        result = runner.invoke(app, ["continue", task.id])
        assert result.exit_code == 1
        assert "already done" in result.output

    def test_approval_required_guides_to_approve(self, build_workspace):
        _with_verification(
            build_workspace, json.dumps(sys.executable), '["-c", "print(\'tests ok\')"]'
        )
        loop_impl = _loop_implementer(build_workspace)
        with transient_adapters({"fake_implementer_build": loop_impl}):
            result = runner.invoke(app, ["build", "write implemented.txt"])
        assert result.exit_code == 0, result.output

        conn, store = _open_store(build_workspace)
        task = next(iter(store.all_models(Task)))
        assert task.state is TaskState.APPROVAL_REQUIRED
        conn.close()
        result = runner.invoke(app, ["continue", task.id])
        assert result.exit_code == 1
        assert "awaits human approval" in result.output

    def test_implementer_drift_refuses(self, build_workspace):
        """continue binds the PINNED implementer — a different resolved
        agent/model refuses rather than silently re-binding."""
        _budget_parked_build(build_workspace)
        conn, store = _open_store(build_workspace)
        evidence = SqliteEvidenceStore(store)
        writer = EventLogWriter(conn)
        task = next(iter(store.all_models(Task)))
        with pytest.raises(ContinueRefusal) as excinfo:
            asyncio.run(
                continue_build(
                    store,
                    writer,
                    evidence,
                    _loop_agent(_loop_implementer(build_workspace), build_workspace),
                    task.id,
                    workspace_root=build_workspace,
                    agent_name="someone-else",
                )
            )
        assert excinfo.value.code == "implementer_mismatch"
        conn.close()


class TestDerivePosition:
    def test_derive_position_is_pure_and_complete(self, build_workspace):
        """derive_position exposes the parked position without writing."""
        _budget_parked_build(build_workspace)
        conn, store = _open_store(build_workspace)
        evidence = SqliteEvidenceStore(store)
        task = next(iter(store.all_models(Task)))
        before = _counts(conn)
        position = derive_position(store, evidence, task.id)
        assert position.attempts == 2
        assert position.fix_runs_used == 1
        assert position.next_action == "dispatch"
        assert position.pending_input is not None
        assert position.pending_input.kind is ArtifactKind.FIX_PACKET
        assert position.baseline_pin is not None
        assert position.request.prompt == "write implemented.txt"
        assert position.request.implementer == "impl"
        assert _counts(conn) == before  # read-only
        conn.close()

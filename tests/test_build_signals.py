"""P6.4 — bounded stage-signal micro-interactions (SPEC §27 Phase 6; App. D.4–D.9, D.11-P6).

A build implement/fix/review run may answer with exactly one strict
``relay.stage_signal.v1`` object as its whole output instead of its normal
output. Relay persists it as a task-scoped bus ``Message`` (run authorship),
resolves blocking signals through ``deliver_and_reply``, promotes planner
decisions (and superseding plan revisions), and re-dispatches the SAME
attempt as a continuation carrying causal marker refs
(``build_continuation:``/``signal_reply:``). Every blocked path persists a
durable ``relay.build.escalation.v1`` record and parks; ``relay continue``
re-derives and retries against the CURRENT configuration.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import ClassVar

import pytest
from pydantic import ValidationError
from test_build_flow import _FakeImplementer, _open_store
from test_build_resume import _budget_parked_build, _markers, _run_of, _stage_of
from typer.testing import CliRunner

from relay.agents.base import AgentRequest
from relay.agents.registry import transient_adapters
from relay.cli.main import app
from relay.core.build_ledger import ContinueRefusal, derive_position
from relay.core.evidence import EvidenceKind
from relay.core.stage_signals import (
    OpenSignal,
    SignalContractError,
    SignalServices,
    check_signal_legal,
    parse_stage_signal,
    resolve_open_signal,
)
from relay.core.state_machine import TaskState
from relay.storage.events import EventLogWriter
from relay.storage.models import (
    Artifact,
    ArtifactKind,
    BuildEscalationPayload,
    Decision,
    DecisionStatus,
    EventLogEntry,
    EventType,
    EvidenceRecord,
    Message,
    MessageType,
    PlannerDecisionPayload,
    PlanRevisionPayload,
    Run,
    RunStatus,
    SignalInvalidPayload,
    Task,
    utcnow,
)
from relay.storage.store import SqliteEvidenceStore

runner = CliRunner()


# ---------------------------------------------------------------------------
# Signal-capable fake harness — one script serves every leg:
#   * bus delivery (D15 envelope) -> --answer-file / --answer-dir / crash
#   * planner leg                 -> fixed plan text
#   * reviewer leg                -> <dir>/<n>.txt signal override, else verdicts
#   * impl/fix leg                -> <dir>/<n>.txt signal (no write) or
#                                  note (write + note output), else file write
# Run ordinals are per-leg counters under .relay/ (baseline/diff-excluded).
# ---------------------------------------------------------------------------

_SIGNAL_SRC = r"""
import json, os, sys
data = sys.stdin.read()
argv = sys.argv
if "--version" in argv:
    print("build-fake 1.0.0"); sys.exit(0)
review_counter = __REVIEW_COUNTER__
impl_counter = __IMPL_COUNTER__
answer_counter = __ANSWER_COUNTER__

def _emit(text):
    print(json.dumps({"type": "thread.started", "thread_id": "t"}))
    print(json.dumps({"type": "item.completed",
                      "item": {"id": "m", "type": "agent_message", "text": text}}))
    print(json.dumps({"type": "turn.completed",
                      "usage": {"input_tokens": 5, "output_tokens": 3}}))
    sys.exit(0)

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

def _flag(name):
    return argv[argv.index(name) + 1] if name in argv else None

def _slotted(directory, n):
    if directory is None:
        return None
    path = os.path.join(directory, "%d.txt" % n)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        return fh.read()

# Bus-delivery leg — the frozen D15 envelope; the run's whole output becomes
# the reply content verbatim.
if "You received a message via the Relay conversation bus." in data:
    n = _bump(answer_counter)
    if _flag("--answer-crash-at") == str(n):
        sys.exit(9)
    answer = _slotted(_flag("--answer-dir"), n)
    if answer is None:
        path = _flag("--answer-file")
        if path is not None:
            with open(path, encoding="utf-8") as fh:
                answer = fh.read()
    _emit(answer if answer is not None else "acknowledged")

if "You are the planner" in data:
    _emit("# Plan\n\nGoal: implement the task\nSteps: write implemented.txt\n"
          "Files: implemented.txt\nVerification: file exists")

if "You are the reviewer" in data:
    n = _bump(review_counter)
    text = _slotted(_flag("--review-signal-dir"), n)
    if text is not None:
        _emit(text)
    seq = ["pass"]
    if "--review-verdicts" in argv:
        seq = [v.strip().upper() for v in _flag("--review-verdicts").split(",")]
    verdict = seq[n - 1] if n - 1 < len(seq) else "PASS"
    if verdict == "FINDINGS":
        _emit(json.dumps({
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
        }))
    _emit(json.dumps({
        "schema_version": "relay.review.v1",
        "verdict": "pass",
        "summary": "The changes match the plan.",
        "findings": []
    }))

# implementation / fix leg — the Nth run's whole output comes from a slot:
#   --impl-signal-dir   signal as whole output, NO workspace write
#   --impl-note-dir     workspace write, THEN the note as whole output
n = _bump(impl_counter)
text = _slotted(_flag("--impl-signal-dir"), n)
if text is not None:
    _emit(text)
text = _slotted(_flag("--impl-note-dir"), n)
if text is not None:
    with open("implemented.txt", "w", encoding="utf-8") as fh:
        fh.write("implemented by fake harness\n")
    _emit(text)
if "fix attempt" in data:
    with open("implemented.txt", "w", encoding="utf-8") as fh:
        fh.write("implemented by fake harness - fix pass %d\n" % n)
else:
    with open("implemented.txt", "w", encoding="utf-8") as fh:
        fh.write("implemented by fake harness\n")
_emit("done: wrote implemented.txt")
"""


def _signal_fake(tmp_path, *argv_flags):
    """The impl/review/plan-leg fake — also answers deliveries addressed to it."""
    src = (
        _SIGNAL_SRC.replace(
            "__REVIEW_COUNTER__", json.dumps(str(tmp_path / ".relay" / "sig-review.txt"))
        )
        .replace("__IMPL_COUNTER__", json.dumps(str(tmp_path / ".relay" / "sig-impl.txt")))
        .replace("__ANSWER_COUNTER__", json.dumps(str(tmp_path / ".relay" / "sig-answer.txt")))
    )

    class _Signal(_FakeImplementer):
        seen_requests: ClassVar[list[AgentRequest]] = []

        async def run(self, request):
            type(self).seen_requests.append(request)
            return await super().run(request)

        def invocation_argv(self, resolved):
            return (resolved.command, "-c", src, *argv_flags)

    return _Signal


def _signal_answerer(tmp_path, *argv_flags):
    """The resolved recipient agent — only ever sees the delivery envelope."""
    src = (
        _SIGNAL_SRC.replace(
            "__REVIEW_COUNTER__", json.dumps(str(tmp_path / ".relay" / "ans-review.txt"))
        )
        .replace("__IMPL_COUNTER__", json.dumps(str(tmp_path / ".relay" / "ans-impl.txt")))
        .replace("__ANSWER_COUNTER__", json.dumps(str(tmp_path / ".relay" / "ans-count.txt")))
    )

    class _Answerer(_FakeImplementer):
        seen_requests: ClassVar[list[AgentRequest]] = []

        async def run(self, request):
            type(self).seen_requests.append(request)
            return await super().run(request)

        def invocation_argv(self, resolved):
            return (resolved.command, "-c", src, *argv_flags)

    return _Answerer


def _signal_workspace(
    build_workspace,
    *,
    roles: dict[str, str] | None = None,
    communication: str = "",
    verification: bool = True,
):
    """Rewrite relay.yaml for signal tests.

    Agents: ``impl`` (workspace_write implementer), ``planner_bot``
    (read_only answerer), ``rev`` (read_only reviewer via ``reviewer:``).
    ``roles`` maps AgentRole -> agent name; ``{}`` writes no roles block.
    ``communication``/``verification`` append raw top-level YAML.
    """
    executable = json.dumps(sys.executable)
    if roles is None:
        roles = {"planner": "planner_bot", "implementer": "impl", "reviewer": "rev"}
    roles_yaml = (
        "roles:\n" + "".join(f"  {role}: {agent}\n" for role, agent in roles.items())
        if roles
        else ""
    )
    verification_yaml = (
        "verification:\n"
        f"  program: {executable}\n"
        "  args: [\"-c\", \"print('tests ok')\"]\n"
        if verification
        else ""
    )
    (build_workspace / "relay.yaml").write_text(
        "agents:\n"
        "  impl:\n"
        "    backend: harness\n"
        "    adapter: fake_implementer_build\n"
        "    harness:\n"
        f"      executable_path: {executable}\n"
        "      grant: workspace_write\n"
        "      timeout_seconds: 60\n"
        "  planner_bot:\n"
        "    backend: harness\n"
        "    adapter: fake_answerer\n"
        "    harness:\n"
        f"      executable_path: {executable}\n"
        "      grant: read_only\n"
        "      timeout_seconds: 60\n"
        "  rev:\n"
        "    backend: harness\n"
        "    adapter: fake_implementer_build\n"
        "    harness:\n"
        f"      executable_path: {executable}\n"
        "      grant: read_only\n"
        "      timeout_seconds: 60\n"
        "reviewer: rev\n"
        + roles_yaml
        + communication
        + verification_yaml,
        encoding="utf-8",
    )


def _signal_json(kind: str, to_role: str, body: str = "signal body", references=()) -> str:
    return json.dumps(
        {
            "schema_version": "relay.stage_signal.v1",
            "kind": kind,
            "to_role": to_role,
            "body": body,
            "references": list(references),
        }
    )


def _decision_json(
    outcome: str,
    plan_effect: str,
    statement: str,
    *,
    rationale: str | None = None,
    revised_plan: str | None = None,
) -> str:
    payload: dict[str, object] = {
        "schema_version": "relay.planner_decision.v1",
        "outcome": outcome,
        "plan_effect": plan_effect,
        "statement": statement,
    }
    if rationale is not None:
        payload["rationale"] = rationale
    if revised_plan is not None:
        payload["revised_plan"] = revised_plan
    return json.dumps(payload)


def _write_slot(directory: Path, n: int, text: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{n}.txt"
    path.write_text(text, encoding="utf-8")
    return path


def _write_file(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _refs(marker: EventLogEntry, prefix: str) -> list[str]:
    return [r[len(prefix) :] for r in marker.references if r.startswith(prefix)]


def _task(store) -> Task:
    return next(iter(store.all_models(Task)))


def _messages(store, task_id: str) -> list[Message]:
    return list(
        store.all_models(
            Message, "WHERE task_id = ?", [task_id], order_by="rowid ASC"
        )
    )


def _escalations(store, task_id: str) -> list[BuildEscalationPayload]:
    return [
        BuildEscalationPayload.model_validate_json(a.content or "")
        for a in store.all_models(Artifact)
        if a.kind is ArtifactKind.REPORT
        and a.task_id == task_id
        and '"relay.build.escalation.v1"' in (a.content or "")
    ]


def _diagnostics(store, task_id: str) -> list[SignalInvalidPayload]:
    return [
        SignalInvalidPayload.model_validate_json(a.content or "")
        for a in store.all_models(Artifact)
        if a.kind is ArtifactKind.REPORT
        and a.task_id == task_id
        and '"relay.build.signal.invalid.v1"' in (a.content or "")
    ]


def _run_input(store, run_id: str) -> str:
    inputs = store.artifacts_for_run(run_id, kind=ArtifactKind.RUN_INPUT)
    assert len(inputs) == 1
    return inputs[0].content or ""


def _forge_run(store, task: Task, *, agent: str = "impl", role: str = "implementer",
               status: RunStatus = RunStatus.SUCCEEDED) -> Run:
    run = Run(
        agent=agent,
        role=role,
        task_id=task.id,
        status=status,
        ended_at=utcnow(),
    )
    store.save_model(run)
    return run


def _derive_refusal(store, task_id: str) -> ContinueRefusal:
    """derive_position must fail closed — return the typed refusal."""
    with pytest.raises(ContinueRefusal) as err:
        derive_position(store, SqliteEvidenceStore(store), task_id)
    return err.value


def _forge_marker(
    writer: EventLogWriter,
    task: Task,
    run: Run,
    stage: str,
    *,
    attempt: int | None = None,
    continuation: tuple[str, str] | None = None,
) -> None:
    references = [f"task:{task.id}", f"run:{run.id}", f"build_stage:{stage}"]
    if attempt is not None:
        references.append(f"build_attempt:{attempt}")
    if continuation is not None:
        references.append(f"build_continuation:{continuation[0]}")
        references.append(f"signal_reply:{continuation[1]}")
    writer.record(
        EventLogEntry(
            type=EventType.BUILD_RUN_DISPATCHED,
            task_id=task.id,
            sender="relay:build",
            content="forged dispatch marker",
            references=references,
        )
    )


# ---------------------------------------------------------------------------
# Unit: strict signal parsing (intended-vs-ordinary discrimination)
# ---------------------------------------------------------------------------


class TestSignalParsing:
    def test_ordinary_prose_is_not_a_signal(self):
        assert parse_stage_signal("done: wrote implemented.txt") is None

    def test_json_object_without_signal_schema_is_ordinary(self):
        assert parse_stage_signal('{"diff": "abc"}') is None
        assert (
            parse_stage_signal(
                '{"schema_version": "relay.review.v1", "verdict": "pass", "findings": []}'
            )
            is None
        )

    def test_valid_signal_parses(self):
        signal = parse_stage_signal(
            _signal_json("clarification_request", "planner", "which file?", ["artifact:x"])
        )
        assert signal is not None
        assert signal.kind == "clarification_request"
        assert signal.to_role == "planner"
        assert signal.body == "which file?"
        assert list(signal.references) == ["artifact:x"]

    def test_wrong_signal_version_fails_closed(self):
        with pytest.raises(SignalContractError) as err:
            parse_stage_signal(
                '{"schema_version": "relay.stage_signal.v9", "kind": "note",'
                ' "to_role": "planner", "body": "x"}'
            )
        assert err.value.code == "bad_schema_version"

    def test_malformed_json_carrying_marker_fails_closed(self):
        with pytest.raises(SignalContractError) as err:
            parse_stage_signal('{"schema_version": "relay.stage_signal.v1", "kind":')
        assert err.value.code == "malformed"

    def test_non_object_carrying_marker_fails_closed(self):
        with pytest.raises(SignalContractError) as err:
            parse_stage_signal('"relay.stage_signal.v1"')
        assert err.value.code == "malformed"

    def test_unknown_kind_is_invalid(self):
        with pytest.raises(SignalContractError) as err:
            parse_stage_signal(_signal_json("teleport", "planner"))
        assert err.value.code == "invalid_signal"

    def test_extra_field_is_invalid(self):
        with pytest.raises(SignalContractError) as err:
            parse_stage_signal(
                '{"schema_version": "relay.stage_signal.v1", "kind": "note",'
                ' "to_role": "planner", "body": "x", "extra": 1}'
            )
        assert err.value.code == "invalid_signal"

    def test_duplicate_key_is_invalid(self):
        with pytest.raises(SignalContractError) as err:
            parse_stage_signal(
                '{"schema_version": "relay.stage_signal.v1", "kind": "note",'
                ' "kind": "note", "to_role": "planner", "body": "x"}'
            )
        assert err.value.code == "duplicate_key"

    def test_oversized_output_with_marker_fails_closed(self):
        with pytest.raises(SignalContractError) as err:
            parse_stage_signal('relay.stage_signal.v1' + "x" * 21_000)
        assert err.value.code == "oversized"

    def test_oversized_ordinary_output_is_not_a_signal(self):
        assert parse_stage_signal("x" * 25_000) is None


# ---------------------------------------------------------------------------
# Unit: stage/kind/role legality
# ---------------------------------------------------------------------------


class TestSignalLegality:
    @staticmethod
    def _legal(kind: str, to_role: str, run_role: str) -> str | None:
        try:
            check_signal_legal(
                parse_stage_signal(_signal_json(kind, to_role)),  # type: ignore[arg-type]
                run_role,
            )
        except SignalContractError as exc:
            return exc.code
        return None

    def test_implementer_legality(self):
        assert self._legal("clarification_request", "planner", "implementer") is None
        assert self._legal("proposal", "planner", "implementer") is None
        assert self._legal("note", "planner", "implementer") is None
        assert self._legal("note", "reviewer", "implementer") is None
        assert self._legal("clarification_request", "reviewer", "implementer") == (
            "role_not_permitted"
        )
        assert self._legal("clarification_request", "implementer", "implementer") == (
            "role_not_permitted"
        )
        assert self._legal("challenge", "planner", "implementer") == "kind_not_permitted"
        assert self._legal("note", "participant", "implementer") == "role_not_permitted"

    def test_reviewer_legality(self):
        assert self._legal("clarification_request", "implementer", "reviewer") is None
        assert self._legal("challenge", "planner", "reviewer") is None
        # Reviewer note is deliberately excluded from this slice.
        assert self._legal("note", "planner", "reviewer") == "kind_not_permitted"
        assert self._legal("note", "implementer", "reviewer") == "kind_not_permitted"
        assert self._legal("proposal", "planner", "reviewer") == "kind_not_permitted"
        assert self._legal("clarification_request", "planner", "reviewer") == (
            "role_not_permitted"
        )

    def test_other_roles_have_no_signal_vocabulary(self):
        assert self._legal("note", "planner", "planner") == "kind_not_permitted"
        assert self._legal("clarification_request", "planner", "participant") == (
            "kind_not_permitted"
        )

    def test_unknown_target_role_is_bad_role(self):
        assert self._legal("clarification_request", "nonexistent", "implementer") == (
            "bad_role"
        )


# ---------------------------------------------------------------------------
# Unit: relay.planner_decision.v1 validation matrix
# ---------------------------------------------------------------------------


class TestPlannerDecisionContract:
    def test_accept_unchanged(self):
        payload = PlannerDecisionPayload.model_validate_json(
            _decision_json("accept", "unchanged", "plan stands")
        )
        assert payload.outcome == "accept"
        assert payload.plan_effect == "unchanged"
        assert payload.revised_plan is None

    def test_accept_supersede_requires_revised_plan(self):
        with pytest.raises(ValidationError):
            PlannerDecisionPayload.model_validate_json(
                _decision_json("accept", "supersede", "take the new approach")
            )
        with pytest.raises(ValidationError):
            PlannerDecisionPayload.model_validate_json(
                _decision_json("accept", "supersede", "x", revised_plan="   ")
            )

    def test_unchanged_forbids_revised_plan(self):
        with pytest.raises(ValidationError):
            PlannerDecisionPayload.model_validate_json(
                _decision_json("accept", "unchanged", "x", revised_plan="# Plan v2")
            )

    def test_reject_must_be_unchanged(self):
        payload = PlannerDecisionPayload.model_validate_json(
            _decision_json("reject", "unchanged", "not this way", rationale="too risky")
        )
        assert payload.outcome == "reject"
        with pytest.raises(ValidationError):
            PlannerDecisionPayload.model_validate_json(
                _decision_json("reject", "supersede", "x", revised_plan="# Plan v2")
            )
        with pytest.raises(ValidationError):
            PlannerDecisionPayload.model_validate_json(
                _decision_json("reject", "unchanged", "x", revised_plan="# Plan v2")
            )

    def test_statement_is_required_nonblank(self):
        with pytest.raises(ValidationError):
            PlannerDecisionPayload.model_validate_json(
                _decision_json("accept", "unchanged", "   ")
            )


# ---------------------------------------------------------------------------
# Integration: implementer <-> planner clarification (flagship)
# ---------------------------------------------------------------------------


class TestImplPlannerClarification:
    def _build_with_signal(self, build_workspace, *, extra_yaml: str = ""):
        _signal_workspace(build_workspace, communication=extra_yaml)
        signals = build_workspace / ".relay" / "impl-signals"
        _write_slot(
            signals,
            1,
            _signal_json("clarification_request", "planner", "which file should I write?"),
        )
        planner_answer = _write_file(
            build_workspace / ".relay" / "planner-answer.txt", "write implemented.txt"
        )
        impl = _signal_fake(
            build_workspace,
            "--impl-signal-dir",
            str(signals),
            "--answer-file",
            str(planner_answer),
        )
        answerer = _signal_answerer(
            build_workspace, "--answer-file", str(planner_answer)
        )
        with transient_adapters(
            {"fake_implementer_build": impl, "fake_answerer": answerer}
        ):
            result = runner.invoke(
                app, ["build", "write implemented.txt", "--agent", "impl"]
            )
        return result, impl

    def test_full_exchange_and_same_attempt_continuation(self, build_workspace):
        result, _impl = self._build_with_signal(build_workspace)
        assert result.exit_code == 0, result.output
        assert "pass_promoted" in result.output
        # Continuations never consume attempts — this build ran ONE attempt.
        assert "attempts 1" in result.output

        conn, store = _open_store(build_workspace)
        task = _task(store)
        assert task.state is TaskState.APPROVAL_REQUIRED

        runs = list(store.all_models(Run))
        # plan, impl signal run, planner delivery, impl continuation, review.
        assert [(r.agent, r.role) for r in runs] == [
            ("impl", "planner"),
            ("impl", "implementer"),
            ("planner_bot", "planner"),
            ("impl", "implementer"),
            ("rev", "reviewer"),
        ]
        signal_run, delivery_run, continuation_run = runs[1], runs[2], runs[3]

        # The signal run minted no DIFF; the continuation minted the only one.
        assert store.artifacts_for_run(signal_run.id, kind=ArtifactKind.DIFF) == []
        assert len(store.artifacts_for_run(continuation_run.id, kind=ArtifactKind.DIFF)) == 1

        # The exchange: blocking request impl -> planner, answered back.
        messages = _messages(store, task.id)
        assert len(messages) == 2
        signal_msg, reply = messages
        assert signal_msg.type is MessageType.CLARIFICATION_REQUEST
        assert signal_msg.blocking is True
        assert signal_msg.sender == "impl"
        assert signal_msg.recipient == "planner_bot"
        assert signal_msg.recipient_role == "planner"
        assert signal_msg.run_id == signal_run.id
        assert "which file" in signal_msg.content
        assert reply.type is MessageType.CLARIFICATION_RESPONSE
        assert reply.reply_to_id == signal_msg.id
        assert reply.sender == "planner_bot"
        assert reply.recipient == "impl"
        assert reply.run_id == delivery_run.id
        assert "write implemented.txt" in reply.content

        # Marker provenance: both impl runs share attempt 1; the continuation
        # names the signal message AND its canonical reply.
        markers = _markers(conn, task.id)
        impl_markers = [m for m in markers if _stage_of(m) == "implement"]
        assert len(impl_markers) == 2
        assert [_refs(m, "build_attempt:") for m in impl_markers] == [["1"], ["1"]]
        assert _refs(impl_markers[0], "build_continuation:") == []
        assert _refs(impl_markers[0], "signal_reply:") == []
        assert _refs(impl_markers[1], "build_continuation:") == [signal_msg.id]
        assert _refs(impl_markers[1], "signal_reply:") == [reply.id]

        # The delivery run binds by MESSAGE_DELIVERED, not a build marker.
        assert not any(_run_of(m) == delivery_run.id for m in markers)

        # The continuation prompt carries the answered exchange.
        prompt = _run_input(store, continuation_run.id)
        assert "STAGE EXCHANGE HISTORY" in prompt
        assert "which file should I write?" in prompt
        assert "write implemented.txt" in prompt

        # Ledger derivation agrees: one attempt, no fix budget consumed.
        evidence = SqliteEvidenceStore(store)
        position = derive_position(store, evidence, task.id)
        assert position.attempts == 1
        assert position.fix_runs_used == 0
        assert position.impl_attempts == (1, 1)
        conn.close()

    def test_continuation_never_consults_fix_budget(self, build_workspace):
        """max_fix_loops=0 disables fresh fix dispatches entirely — an
        answered clarification still continues the same attempt."""
        result, _impl = self._build_with_signal(
            build_workspace, extra_yaml="budget:\n  max_fix_loops: 0\n"
        )
        assert result.exit_code == 0, result.output
        assert "pass_promoted" in result.output

        conn, store = _open_store(build_workspace)
        evidence = SqliteEvidenceStore(store)
        position = derive_position(store, evidence, _task(store).id)
        assert position.attempts == 1
        assert len(position.impl_runs) == 2
        conn.close()

    def test_signals_advertised_only_when_deliverable(self, build_workspace):
        # No roles configured -> no signal can resolve -> prompt is identical
        # to the pre-P6.4 shape (no COMMUNICATION SIGNALS block).
        _signal_workspace(build_workspace, roles={})
        signals = build_workspace / ".relay" / "impl-signals"
        _write_slot(
            signals, 1, _signal_json("clarification_request", "planner", "which file?")
        )
        answer = _write_file(build_workspace / ".relay" / "a.txt", "x")
        impl = _signal_fake(
            build_workspace,
            "--impl-signal-dir",
            str(signals),
            "--answer-file",
            str(answer),
        )
        answerer = _signal_answerer(build_workspace, "--answer-file", str(answer))
        with transient_adapters(
            {"fake_implementer_build": impl, "fake_answerer": answerer}
        ):
            result = runner.invoke(
                app, ["build", "write implemented.txt", "--agent", "impl"]
            )
        assert result.exit_code == 0, result.output
        # The signal could not resolve -> durable escalation, parked stage.
        assert "communication_blocked" in result.output

        conn, store = _open_store(build_workspace)
        task = _task(store)
        signal_run = next(r for r in store.all_models(Run) if r.role == "implementer")
        assert "COMMUNICATION SIGNALS" not in _run_input(store, signal_run.id)
        escalations = _escalations(store, task.id)
        assert [e.reason for e in escalations] == ["unresolved_role"]
        assert escalations[0].run_id == signal_run.id
        assert escalations[0].signal_message_id is None
        conn.close()

    def test_appendix_advertises_deliverable_signals(self, build_workspace):
        _signal_workspace(build_workspace, verification=False)
        signals = build_workspace / ".relay" / "impl-signals"
        _write_slot(
            signals, 1, _signal_json("clarification_request", "planner", "which file?")
        )
        answer = _write_file(build_workspace / ".relay" / "a.txt", "implemented.txt")
        impl = _signal_fake(
            build_workspace,
            "--impl-signal-dir",
            str(signals),
            "--answer-file",
            str(answer),
        )
        answerer = _signal_answerer(build_workspace, "--answer-file", str(answer))
        with transient_adapters(
            {"fake_implementer_build": impl, "fake_answerer": answerer}
        ):
            runner.invoke(app, ["build", "write implemented.txt", "--agent", "impl"])

        conn, store = _open_store(build_workspace)
        signal_run = next(r for r in store.all_models(Run) if r.role == "implementer")
        prompt = _run_input(store, signal_run.id)
        assert "COMMUNICATION SIGNALS" in prompt
        assert '"clarification_request" to "planner"' in prompt
        assert '"proposal" to "planner"' in prompt
        assert '"note" to "planner"' in prompt
        assert '"note" to "reviewer"' in prompt
        # The reviewer leg (not run here) would get its own vocabulary —
        # the implementer is never offered reviewer-only shapes.
        assert '"challenge"' not in prompt
        conn.close()


# ---------------------------------------------------------------------------
# Integration: reviewer <-> implementer Q&A and reviewer -> planner challenge
# ---------------------------------------------------------------------------


class TestReviewerExchanges:
    def test_reviewer_to_implementer_clarification(self, build_workspace):
        _signal_workspace(build_workspace)
        signals = build_workspace / ".relay" / "review-signals"
        _write_slot(
            signals,
            1,
            _signal_json(
                "clarification_request",
                "implementer",
                "why does implemented.txt contain this line?",
            ),
        )
        impl_answer = _write_file(
            build_workspace / ".relay" / "impl-answer.txt", "the plan required it"
        )
        planner_answer = _write_file(build_workspace / ".relay" / "p.txt", "x")
        impl = _signal_fake(
            build_workspace,
            "--review-signal-dir",
            str(signals),
            "--answer-file",
            str(impl_answer),
        )
        answerer = _signal_answerer(
            build_workspace, "--answer-file", str(planner_answer)
        )
        with transient_adapters(
            {"fake_implementer_build": impl, "fake_answerer": answerer}
        ):
            result = runner.invoke(
                app, ["build", "write implemented.txt", "--agent", "impl"]
            )
        assert result.exit_code == 0, result.output
        assert "pass_promoted" in result.output

        conn, store = _open_store(build_workspace)
        task = _task(store)
        assert task.state is TaskState.APPROVAL_REQUIRED

        runs = list(store.all_models(Run))
        # plan, impl, review signal, impl delivery answer, review continuation.
        assert [(r.agent, r.role) for r in runs] == [
            ("impl", "planner"),
            ("impl", "implementer"),
            ("rev", "reviewer"),
            ("impl", "implementer"),
            ("rev", "reviewer"),
        ]
        review_signal_run, answer_run, review_cont_run = runs[2], runs[3], runs[4]

        messages = _messages(store, task.id)
        assert len(messages) == 2
        signal_msg, reply = messages
        assert signal_msg.type is MessageType.CLARIFICATION_REQUEST
        assert signal_msg.sender == "rev"
        assert signal_msg.recipient == "impl"
        assert signal_msg.recipient_role == "implementer"
        assert signal_msg.run_id == review_signal_run.id
        assert reply.type is MessageType.CLARIFICATION_RESPONSE
        assert reply.sender == "impl"
        assert reply.recipient == "rev"
        assert reply.run_id == answer_run.id

        markers = _markers(conn, task.id)
        review_markers = [m for m in markers if _stage_of(m) == "review"]
        assert len(review_markers) == 2
        # Review runs carry no attempt number; the continuation carries refs.
        assert _refs(review_markers[0], "build_attempt:") == []
        assert _refs(review_markers[0], "build_continuation:") == []
        assert _refs(review_markers[1], "build_continuation:") == [signal_msg.id]
        assert _refs(review_markers[1], "signal_reply:") == [reply.id]

        prompt = _run_input(store, review_cont_run.id)
        assert "STAGE EXCHANGE HISTORY" in prompt
        assert "why does implemented.txt contain this line?" in prompt
        assert "the plan required it" in prompt
        conn.close()

    def test_reviewer_challenge_promotes_decision(self, build_workspace):
        _signal_workspace(build_workspace)
        signals = build_workspace / ".relay" / "review-signals"
        _write_slot(
            signals,
            1,
            _signal_json("challenge", "planner", "the plan misses error handling"),
        )
        decision = _write_file(
            build_workspace / ".relay" / "planner-decision.txt",
            _decision_json(
                "accept",
                "unchanged",
                "error handling stays out of scope",
                rationale="the task statement does not require it",
            ),
        )
        impl = _signal_fake(
            build_workspace,
            "--review-signal-dir",
            str(signals),
            "--answer-file",
            str(decision),
        )
        answerer = _signal_answerer(build_workspace, "--answer-file", str(decision))
        with transient_adapters(
            {"fake_implementer_build": impl, "fake_answerer": answerer}
        ):
            result = runner.invoke(
                app, ["build", "write implemented.txt", "--agent", "impl"]
            )
        assert result.exit_code == 0, result.output
        assert "pass_promoted" in result.output

        conn, store = _open_store(build_workspace)
        task = _task(store)
        assert task.state is TaskState.APPROVAL_REQUIRED

        messages = _messages(store, task.id)
        assert len(messages) == 2
        challenge, reply = messages
        assert challenge.type is MessageType.CHALLENGE
        assert challenge.sender == "rev"
        assert challenge.recipient == "planner_bot"
        assert challenge.recipient_role == "planner"
        assert "relay.planner_decision.v1" in challenge.content  # reply contract
        assert reply.type is MessageType.FINAL_POSITION
        assert reply.sender == "planner_bot"

        decisions = list(store.all_models(Decision))
        assert len(decisions) == 1
        decision_row = decisions[0]
        assert decision_row.status is DecisionStatus.ACCEPTED
        assert decision_row.statement == "error handling stays out of scope"
        assert decision_row.proposed_by == "rev"
        assert decision_row.accepted_by == "planner_bot"
        assert decision_row.task_id == task.id

        writer = EventLogWriter(conn)
        types = [e.type for e in writer.all()]
        assert EventType.DECISION_PROPOSED in types
        assert EventType.DECISION_ACCEPTED in types

        # plan_effect unchanged -> no plan revision, single PLAN artifact.
        plans = [
            a
            for a in store.all_models(Artifact)
            if a.kind is ArtifactKind.PLAN and a.task_id == task.id
        ]
        assert len(plans) == 1
        assert not [
            a
            for a in store.all_models(Artifact)
            if "relay.plan_revision.v1" in (a.content or "")
        ]
        conn.close()

    def test_reviewer_note_is_a_signal_violation(self, build_workspace):
        _signal_workspace(build_workspace)
        signals = build_workspace / ".relay" / "review-signals"
        _write_slot(signals, 1, _signal_json("note", "planner", "fyi"))
        answer = _write_file(build_workspace / ".relay" / "a.txt", "x")
        impl = _signal_fake(
            build_workspace,
            "--review-signal-dir",
            str(signals),
            "--answer-file",
            str(answer),
        )
        answerer = _signal_answerer(build_workspace, "--answer-file", str(answer))
        with transient_adapters(
            {"fake_implementer_build": impl, "fake_answerer": answerer}
        ):
            result = runner.invoke(
                app, ["build", "write implemented.txt", "--agent", "impl"]
            )
        assert result.exit_code == 0, result.output
        assert "communication_blocked" in result.output

        conn, store = _open_store(build_workspace)
        task = _task(store)
        assert task.state is TaskState.REVIEWING
        diagnostics = _diagnostics(store, task.id)
        assert [d.code for d in diagnostics] == ["kind_not_permitted"]
        assert diagnostics[0].stage == "review"
        # No review artifact and no message were minted for the illegal kind.
        assert not [
            a
            for a in store.all_models(Artifact)
            if a.kind is ArtifactKind.REVIEW_FINDING and a.task_id == task.id
        ]
        assert _messages(store, task.id) == []

        # `relay continue` re-runs the review — a legit retry needs no
        # continuation refs because the signal run authored no messages.
        with transient_adapters(
            {"fake_implementer_build": impl, "fake_answerer": answerer}
        ):
            result = runner.invoke(app, ["continue"])
        assert result.exit_code == 0, result.output
        assert "pass_promoted" in result.output
        task = _task(store)
        assert task.state is TaskState.APPROVAL_REQUIRED
        conn.close()


# ---------------------------------------------------------------------------
# Integration: fixer -> planner proposal + plan supersession
# ---------------------------------------------------------------------------


class TestPlanRevision:
    def test_proposal_supersedes_plan_atomically(self, build_workspace):
        _signal_workspace(build_workspace)
        signals = build_workspace / ".relay" / "impl-signals"
        _write_slot(
            signals,
            2,  # the FIX run (impl run ordinal 2) emits the proposal
            _signal_json("proposal", "planner", "adopt the simpler approach"),
        )
        decision = _write_file(
            build_workspace / ".relay" / "planner-decision.txt",
            _decision_json(
                "accept",
                "supersede",
                "plan v2 is canonical",
                rationale="the proposal is strictly better",
                revised_plan="# Plan v2\n\nGoal: implement the task\n"
                "Steps: write implemented.txt the simple way\n"
                "Files: implemented.txt\nVerification: file exists",
            ),
        )
        impl_answer = _write_file(build_workspace / ".relay" / "a.txt", "x")
        impl = _signal_fake(
            build_workspace,
            "--impl-signal-dir",
            str(signals),
            "--review-verdicts",
            "findings,pass",
            "--answer-file",
            str(impl_answer),
        )
        answerer = _signal_answerer(build_workspace, "--answer-file", str(decision))
        with transient_adapters(
            {"fake_implementer_build": impl, "fake_answerer": answerer}
        ):
            result = runner.invoke(
                app, ["build", "write implemented.txt", "--agent", "impl"]
            )
        assert result.exit_code == 0, result.output
        assert "pass_promoted" in result.output
        assert "attempts 2" in result.output

        conn, store = _open_store(build_workspace)
        task = _task(store)
        assert task.state is TaskState.APPROVAL_REQUIRED

        # The plan chain: root -> superseding tip, fully provenance-bound.
        plans = [
            a
            for a in store.all_models(Artifact)
            if a.kind is ArtifactKind.PLAN and a.task_id == task.id
        ]
        assert len(plans) == 2
        revisions = [
            PlanRevisionPayload.model_validate_json(a.content or "")
            for a in store.all_models(Artifact)
            if '"relay.plan_revision.v1"' in (a.content or "")
        ]
        assert len(revisions) == 1
        revision = revisions[0]

        messages = _messages(store, task.id)
        proposal, reply = messages
        assert proposal.type is MessageType.PROPOSAL
        assert proposal.sender == "impl"
        assert proposal.recipient_role == "planner"
        assert reply.type is MessageType.FINAL_POSITION
        assert reply.reply_to_id == proposal.id

        old_plan, new_plan = sorted(plans, key=lambda a: a.created_at)
        if revision.plan_artifact_id != new_plan.id:
            old_plan, new_plan = new_plan, old_plan
        assert revision.supersedes_plan_artifact_id == old_plan.id
        assert revision.plan_artifact_id == new_plan.id
        assert revision.signal_message_id == proposal.id
        assert revision.reply_message_id == reply.id
        assert revision.author_run_id == reply.run_id
        assert new_plan.run_id == reply.run_id
        assert "Plan v2" in (new_plan.content or "")

        # PLAN_PRODUCED exists for both links — the tip is provable.
        evidence = SqliteEvidenceStore(store)
        produced = [
            r
            for r in evidence.records_for_task(task.id)
            if r.kind is EvidenceKind.PLAN_PRODUCED
        ]
        assert {r.artifact_id for r in produced} == {old_plan.id, new_plan.id}

        decisions = list(store.all_models(Decision))
        assert len(decisions) == 1
        assert decisions[0].status is DecisionStatus.ACCEPTED
        assert decisions[0].accepted_by == "planner_bot"

        # Marker provenance: the fix signal run and its continuation share
        # attempt 2; the continuation names the exchange.
        markers = _markers(conn, task.id)
        fix_markers = [m for m in markers if _stage_of(m) == "fix"]
        assert len(fix_markers) == 2
        assert [_refs(m, "build_attempt:") for m in fix_markers] == [["2"], ["2"]]
        assert _refs(fix_markers[1], "build_continuation:") == [proposal.id]
        assert _refs(fix_markers[1], "signal_reply:") == [reply.id]

        # The continuation prompt consumed the SUPERSEDED plan tip and the
        # still-pending fix packet.
        runs = list(store.all_models(Run))
        continuation_run = runs[-2]  # before the final review
        assert continuation_run.role == "implementer"
        prompt = _run_input(store, continuation_run.id)
        assert "Plan v2" in prompt
        assert "FIX PACKET" in prompt
        assert "STAGE EXCHANGE HISTORY" in prompt

        position = derive_position(store, evidence, task.id)
        assert position.plan_artifact is not None
        assert position.plan_artifact.id == new_plan.id
        assert position.attempts == 2
        conn.close()


# ---------------------------------------------------------------------------
# Integration: non-blocking notes
# ---------------------------------------------------------------------------


class TestNotes:
    def test_impl_note_surfaces_in_review_prompt(self, build_workspace):
        _signal_workspace(build_workspace)
        notes = build_workspace / ".relay" / "impl-notes"
        _write_slot(
            notes,
            1,
            _signal_json("note", "reviewer", "watch the file encoding on this one"),
        )
        answer = _write_file(build_workspace / ".relay" / "a.txt", "x")
        impl = _signal_fake(
            build_workspace,
            "--impl-note-dir",
            str(notes),
            "--answer-file",
            str(answer),
        )
        answerer = _signal_answerer(build_workspace, "--answer-file", str(answer))
        with transient_adapters(
            {"fake_implementer_build": impl, "fake_answerer": answerer}
        ):
            result = runner.invoke(
                app, ["build", "write implemented.txt", "--agent", "impl"]
            )
        assert result.exit_code == 0, result.output
        assert "pass_promoted" in result.output

        conn, store = _open_store(build_workspace)
        task = _task(store)
        assert task.state is TaskState.APPROVAL_REQUIRED

        # The note is a real non-blocking bus message — and the run still
        # minted its normal DIFF (notes compose with ordinary output).
        messages = _messages(store, task.id)
        assert len(messages) == 1
        note = messages[0]
        assert note.type is MessageType.NOTE
        assert note.blocking is False
        assert note.sender == "impl"
        assert note.recipient == "rev"
        assert note.recipient_role == "reviewer"

        runs = list(store.all_models(Run))
        impl_run, review_run = runs[1], runs[2]
        assert len(store.artifacts_for_run(impl_run.id, kind=ArtifactKind.DIFF)) == 1
        # No continuation markers anywhere — a note never blocks.
        for marker in _markers(conn, task.id):
            assert _refs(marker, "build_continuation:") == []

        prompt = _run_input(store, review_run.id)
        assert "NOTES ADDRESSED TO YOUR ROLE" in prompt
        assert "watch the file encoding" in prompt
        conn.close()

    def test_note_to_unresolved_role_escalates_without_parking(self, build_workspace):
        _signal_workspace(build_workspace, roles={"planner": "planner_bot"})
        notes = build_workspace / ".relay" / "impl-notes"
        _write_slot(notes, 1, _signal_json("note", "reviewer", "unbound role"))
        answer = _write_file(build_workspace / ".relay" / "a.txt", "x")
        impl = _signal_fake(
            build_workspace,
            "--impl-note-dir",
            str(notes),
            "--answer-file",
            str(answer),
        )
        answerer = _signal_answerer(build_workspace, "--answer-file", str(answer))
        with transient_adapters(
            {"fake_implementer_build": impl, "fake_answerer": answerer}
        ):
            result = runner.invoke(
                app, ["build", "write implemented.txt", "--agent", "impl"]
            )
        # A dropped note never halts the build — but the drop is durable.
        assert result.exit_code == 0, result.output
        assert "pass_promoted" in result.output

        conn, store = _open_store(build_workspace)
        task = _task(store)
        assert task.state is TaskState.APPROVAL_REQUIRED
        assert _messages(store, task.id) == []
        escalations = _escalations(store, task.id)
        assert [e.reason for e in escalations] == ["unresolved_role"]
        conn.close()


# ---------------------------------------------------------------------------
# Integration: fail-closed invalid signals
# ---------------------------------------------------------------------------


class TestInvalidSignals:
    def test_invalid_signal_parks_then_continue_runs_fresh_attempt(
        self, build_workspace
    ):
        _signal_workspace(build_workspace)
        signals = build_workspace / ".relay" / "impl-signals"
        _write_slot(signals, 1, _signal_json("teleport", "planner", "x"))
        answer = _write_file(build_workspace / ".relay" / "a.txt", "x")
        impl = _signal_fake(
            build_workspace,
            "--impl-signal-dir",
            str(signals),
            "--answer-file",
            str(answer),
        )
        answerer = _signal_answerer(build_workspace, "--answer-file", str(answer))
        with transient_adapters(
            {"fake_implementer_build": impl, "fake_answerer": answerer}
        ):
            result = runner.invoke(
                app, ["build", "write implemented.txt", "--agent", "impl"]
            )
        assert result.exit_code == 0, result.output
        assert "communication_blocked" in result.output

        conn, store = _open_store(build_workspace)
        task = _task(store)
        assert task.state is TaskState.IMPLEMENTING
        diagnostics = _diagnostics(store, task.id)
        assert [d.code for d in diagnostics] == ["invalid_signal"]
        assert diagnostics[0].stage == "implement"
        # The signal run minted no DIFF and no message.
        signal_run = next(r for r in store.all_models(Run) if r.role == "implementer")
        assert store.artifacts_for_run(signal_run.id, kind=ArtifactKind.DIFF) == []
        assert _messages(store, task.id) == []

        # The invalid run consumed attempt 1 — continue dispatches a FRESH
        # attempt 2 (model misbehavior costs an attempt, like any no-op).
        with transient_adapters(
            {"fake_implementer_build": impl, "fake_answerer": answerer}
        ):
            result = runner.invoke(app, ["continue"])
        assert result.exit_code == 0, result.output
        assert "pass_promoted" in result.output
        assert "attempts 2" in result.output
        impl_markers = [
            m for m in _markers(conn, task.id) if _stage_of(m) == "implement"
        ]
        assert [_refs(m, "build_attempt:") for m in impl_markers] == [["1"], ["2"]]
        assert _refs(impl_markers[1], "build_continuation:") == []
        conn.close()

    def test_malformed_intended_signal_parks_without_diff(self, build_workspace):
        _signal_workspace(build_workspace)
        signals = build_workspace / ".relay" / "impl-signals"
        _write_slot(
            signals, 1, '{"schema_version": "relay.stage_signal.v1", "kind":'
        )
        answer = _write_file(build_workspace / ".relay" / "a.txt", "x")
        impl = _signal_fake(
            build_workspace,
            "--impl-signal-dir",
            str(signals),
            "--answer-file",
            str(answer),
        )
        answerer = _signal_answerer(build_workspace, "--answer-file", str(answer))
        with transient_adapters(
            {"fake_implementer_build": impl, "fake_answerer": answerer}
        ):
            result = runner.invoke(
                app, ["build", "write implemented.txt", "--agent", "impl"]
            )
        assert result.exit_code == 0, result.output
        assert "communication_blocked" in result.output

        conn, store = _open_store(build_workspace)
        task = _task(store)
        diagnostics = _diagnostics(store, task.id)
        assert [d.code for d in diagnostics] == ["malformed"]
        signal_run = next(r for r in store.all_models(Run) if r.role == "implementer")
        assert store.artifacts_for_run(signal_run.id, kind=ArtifactKind.DIFF) == []
        conn.close()


# ---------------------------------------------------------------------------
# Integration: escalate-and-park on every blocked path, then resume
# ---------------------------------------------------------------------------


class TestEscalationAndResume:
    def _parked_signal_build(
        self,
        build_workspace,
        *,
        roles=None,
        communication: str = "",
        answerer_flags: tuple[str, ...] = (),
        signal_body: str = "which file should I write?",
    ):
        _signal_workspace(build_workspace, roles=roles, communication=communication)
        signals = build_workspace / ".relay" / "impl-signals"
        _write_slot(
            signals,
            1,
            _signal_json("clarification_request", "planner", signal_body),
        )
        impl_answer = _write_file(
            build_workspace / ".relay" / "impl-answer.txt", "write implemented.txt"
        )
        planner_answer = _write_file(
            build_workspace / ".relay" / "planner-answer.txt", "write implemented.txt"
        )
        impl = _signal_fake(
            build_workspace,
            "--impl-signal-dir",
            str(signals),
            "--answer-file",
            str(impl_answer),
        )
        answerer = _signal_answerer(
            build_workspace,
            *(answerer_flags or ("--answer-file", str(planner_answer))),
        )
        with transient_adapters(
            {"fake_implementer_build": impl, "fake_answerer": answerer}
        ):
            result = runner.invoke(
                app, ["build", "write implemented.txt", "--agent", "impl"]
            )
        return result, impl, answerer

    def test_unresolved_role_parks_then_continue_resolves(self, build_workspace):
        result, impl, answerer = self._parked_signal_build(
            build_workspace, roles={"implementer": "impl"}  # planner unbound
        )
        assert "communication_blocked" in result.output

        conn, store = _open_store(build_workspace)
        task = _task(store)
        assert task.state is TaskState.IMPLEMENTING
        assert [e.reason for e in _escalations(store, task.id)] == ["unresolved_role"]
        conn.close()

        _signal_workspace(build_workspace)  # restore full roles
        with transient_adapters(
            {"fake_implementer_build": impl, "fake_answerer": answerer}
        ):
            result = runner.invoke(app, ["continue"])
        assert result.exit_code == 0, result.output
        assert "pass_promoted" in result.output

        conn, store = _open_store(build_workspace)
        assert _task(store).state is TaskState.APPROVAL_REQUIRED
        # One escalation record total — deduped, not repeated per attempt.
        assert len(_escalations(store, _task(store).id)) == 1
        conn.close()

    def test_self_send_parks_then_continue_resolves(self, build_workspace):
        result, impl, answerer = self._parked_signal_build(
            build_workspace,
            roles={
                "planner": "impl",  # resolves back onto the emitter
                "implementer": "impl",
            },
        )
        assert "communication_blocked" in result.output

        conn, store = _open_store(build_workspace)
        assert [e.reason for e in _escalations(store, _task(store).id)] == ["self_send"]
        conn.close()

        _signal_workspace(build_workspace)
        with transient_adapters(
            {"fake_implementer_build": impl, "fake_answerer": answerer}
        ):
            result = runner.invoke(app, ["continue"])
        assert result.exit_code == 0, result.output
        assert "pass_promoted" in result.output
        conn, store = _open_store(build_workspace)
        assert _task(store).state is TaskState.APPROVAL_REQUIRED
        conn.close()

    def test_policy_refused_parks_then_continue_resolves(self, build_workspace):
        # Explicit edges that omit implementer -> planner.
        edges = (
            "communication:\n"
            "  edges:\n"
            "    - {from: reviewer, to: implementer,"
            " types: [clarification_request], blocking: true}\n"
        )
        result, impl, answerer = self._parked_signal_build(
            build_workspace, communication=edges
        )
        assert "communication_blocked" in result.output

        conn, store = _open_store(build_workspace)
        assert [e.reason for e in _escalations(store, _task(store).id)] == [
            "policy_refused"
        ]
        conn.close()

        full_edges = (
            "communication:\n"
            "  edges:\n"
            "    - {from: implementer, to: planner,"
            " types: [clarification_request], blocking: true}\n"
            "    - {from: planner, to: implementer,"
            " types: [clarification_response], blocking: false}\n"
            "    - {from: reviewer, to: implementer,"
            " types: [clarification_request], blocking: true}\n"
        )
        _signal_workspace(build_workspace, communication=full_edges)
        with transient_adapters(
            {"fake_implementer_build": impl, "fake_answerer": answerer}
        ):
            result = runner.invoke(app, ["continue"])
        assert result.exit_code == 0, result.output
        assert "pass_promoted" in result.output
        conn, store = _open_store(build_workspace)
        assert _task(store).state is TaskState.APPROVAL_REQUIRED
        conn.close()

    def test_blocking_budget_exhausted_parks_then_continue_resolves(
        self, build_workspace
    ):
        budgets = (
            "communication:\n"
            "  budgets:\n"
            "    max_agent_turns: 16\n"
            "    max_blocking_messages: 0\n"
        )
        result, impl, answerer = self._parked_signal_build(
            build_workspace, communication=budgets
        )
        assert "communication_blocked" in result.output

        conn, store = _open_store(build_workspace)
        assert [e.reason for e in _escalations(store, _task(store).id)] == [
            "budget_exhausted"
        ]
        # The refused send persisted no message.
        assert _messages(store, _task(store).id) == []
        conn.close()

        _signal_workspace(build_workspace)  # default budgets
        with transient_adapters(
            {"fake_implementer_build": impl, "fake_answerer": answerer}
        ):
            result = runner.invoke(app, ["continue"])
        assert result.exit_code == 0, result.output
        assert "pass_promoted" in result.output
        conn.close()

    def test_turn_budget_exhausted_mid_exchange(self, build_workspace):
        """The SECOND delivery hits the turn cap — the first exchange is
        durable, the parked signal resumes after the budget is raised."""
        budgets = (
            "communication:\n"
            "  budgets:\n"
            "    max_agent_turns: 1\n"
            "    max_blocking_messages: 3\n"
        )
        _signal_workspace(build_workspace, communication=budgets)
        signals = build_workspace / ".relay" / "impl-signals"
        _write_slot(signals, 1, _signal_json("clarification_request", "planner", "q1"))
        _write_slot(signals, 2, _signal_json("clarification_request", "planner", "q2"))
        planner_answer = _write_file(
            build_workspace / ".relay" / "planner-answer.txt", "answer"
        )
        impl = _signal_fake(
            build_workspace,
            "--impl-signal-dir",
            str(signals),
            "--answer-file",
            str(planner_answer),
        )
        answerer = _signal_answerer(
            build_workspace, "--answer-file", str(planner_answer)
        )
        with transient_adapters(
            {"fake_implementer_build": impl, "fake_answerer": answerer}
        ):
            result = runner.invoke(
                app, ["build", "write implemented.txt", "--agent", "impl"]
            )
        assert result.exit_code == 0, result.output
        assert "communication_blocked" in result.output

        conn, store = _open_store(build_workspace)
        task = _task(store)
        # Exchange 1 fully resolved; signal 2's send persisted but its
        # delivery was refused by the turn budget.
        messages = _messages(store, task.id)
        assert len(messages) == 3  # signal 1 + reply 1 + signal 2
        escalations = _escalations(store, task.id)
        assert [e.reason for e in escalations] == ["turn_budget_exhausted"]
        assert escalations[0].signal_message_id == messages[2].id

        _signal_workspace(build_workspace)  # default budgets
        with transient_adapters(
            {"fake_implementer_build": impl, "fake_answerer": answerer}
        ):
            result = runner.invoke(app, ["continue"])
        assert result.exit_code == 0, result.output
        assert "pass_promoted" in result.output

        position = derive_position(store, SqliteEvidenceStore(store), task.id)
        assert position.attempts == 1
        assert position.impl_attempts == (1, 1, 1)
        # The final continuation prompt embeds BOTH answered exchanges.
        continuation_run = position.impl_runs[-1]
        prompt = _run_input(store, continuation_run.id)
        assert prompt.count("--- exchange") == 2
        conn.close()

    def test_failed_delivery_retries_once_with_superseding_send(
        self, build_workspace
    ):
        """A crashed delivery run ends FAILED with no reply: park, then
        `relay continue` retries the exchange with a superseding message."""
        signals = build_workspace / ".relay" / "impl-signals"
        _signal_workspace(build_workspace)
        _write_slot(signals, 1, _signal_json("clarification_request", "planner", "q"))
        planner_answer = _write_file(
            build_workspace / ".relay" / "planner-answer.txt", "answer"
        )
        impl = _signal_fake(
            build_workspace,
            "--impl-signal-dir",
            str(signals),
            "--answer-file",
            str(planner_answer),
        )
        answerer = _signal_answerer(
            build_workspace,
            "--answer-file",
            str(planner_answer),
            "--answer-crash-at",
            "1",
        )
        with transient_adapters(
            {"fake_implementer_build": impl, "fake_answerer": answerer}
        ):
            result = runner.invoke(
                app, ["build", "write implemented.txt", "--agent", "impl"]
            )
        assert result.exit_code == 0, result.output
        assert "communication_blocked" in result.output

        conn, store = _open_store(build_workspace)
        task = _task(store)
        messages = _messages(store, task.id)
        assert len(messages) == 1
        assert [e.reason for e in _escalations(store, task.id)] == ["delivery_failed"]
        failed_delivery = next(
            r
            for r in store.all_models(Run)
            if r.agent == "planner_bot" and r.status is RunStatus.FAILED
        )
        assert failed_delivery is not None
        conn.close()

        with transient_adapters(
            {"fake_implementer_build": impl, "fake_answerer": answerer}
        ):
            result = runner.invoke(app, ["continue"])
        assert result.exit_code == 0, result.output
        assert "pass_promoted" in result.output

        conn, store = _open_store(build_workspace)
        messages = _messages(store, task.id)
        assert len(messages) == 3  # msg1 + retry msg2 + reply on msg2
        retry, reply = messages[1], messages[2]
        assert retry.type is MessageType.CLARIFICATION_REQUEST
        assert retry.sender == "impl"
        assert f"message:{messages[0].id}" in retry.references  # supersedes msg1
        assert reply.reply_to_id == retry.id
        assert reply.sender == "planner_bot"

        # The continuation binds to the RETRIED message, not the failed one.
        impl_markers = [
            m for m in _markers(conn, task.id) if _stage_of(m) == "implement"
        ]
        assert _refs(impl_markers[-1], "build_continuation:") == [retry.id]
        assert _refs(impl_markers[-1], "signal_reply:") == [reply.id]
        # Escalation dedupe: exactly one record per stalled message.
        assert len(_escalations(store, task.id)) == 1
        conn.close()


# ---------------------------------------------------------------------------
# Integration: delivery_pending escalation + interrupted-delivery settlement
# ---------------------------------------------------------------------------


class TestInterruptedDelivery:
    def _policy_refused_park(self, build_workspace):
        """Park with a persisted open signal message but no delivery marker:
        send edge admitted, reply edge missing."""
        edges = (
            "communication:\n"
            "  edges:\n"
            "    - {from: implementer, to: planner,"
            " types: [clarification_request], blocking: true}\n"
        )
        _signal_workspace(build_workspace, communication=edges)
        signals = build_workspace / ".relay" / "impl-signals"
        _write_slot(signals, 1, _signal_json("clarification_request", "planner", "q"))
        planner_answer = _write_file(
            build_workspace / ".relay" / "planner-answer.txt", "answer"
        )
        impl = _signal_fake(
            build_workspace,
            "--impl-signal-dir",
            str(signals),
            "--answer-file",
            str(planner_answer),
        )
        answerer = _signal_answerer(
            build_workspace, "--answer-file", str(planner_answer)
        )
        with transient_adapters(
            {"fake_implementer_build": impl, "fake_answerer": answerer}
        ):
            result = runner.invoke(
                app, ["build", "write implemented.txt", "--agent", "impl"]
            )
        assert result.exit_code == 0, result.output
        assert "communication_blocked" in result.output
        return impl, answerer

    def test_settle_interrupted_settles_signal_delivery_only(
        self, build_workspace
    ):
        impl, answerer = self._policy_refused_park(build_workspace)
        conn, store = _open_store(build_workspace)
        writer = EventLogWriter(conn)
        task = _task(store)
        signal_msg = _messages(store, task.id)[0]

        # A crashed delivery run bound to the OPEN signal message.
        zombie = Run(
            agent="planner_bot", role="planner", task_id=task.id, status=RunStatus.RUNNING
        )
        store.save_model(zombie)
        writer.record(
            EventLogEntry(
                type=EventType.MESSAGE_DELIVERED,
                task_id=task.id,
                sender="relay:delivery",
                recipient="planner_bot",
                content="forged zombie delivery",
                references=[
                    f"message:{signal_msg.id}",
                    f"run:{zombie.id}",
                    f"task:{task.id}",
                ],
            )
        )

        # An unrelated P4 delivery zombie — must never be touched.
        p4_message = Message(
            sender="human:tester",
            recipient="impl",
            task_id=task.id,
            type=MessageType.NOTE,
            content="unrelated",
        )
        store.save_model(p4_message)
        p4_zombie = Run(
            agent="impl", role="implementer", task_id=task.id, status=RunStatus.RUNNING
        )
        store.save_model(p4_zombie)
        writer.record(
            EventLogEntry(
                type=EventType.MESSAGE_DELIVERED,
                task_id=task.id,
                sender="relay:delivery",
                recipient="impl",
                content="unrelated p4 delivery",
                references=[
                    f"message:{p4_message.id}",
                    f"run:{p4_zombie.id}",
                    f"task:{task.id}",
                ],
            )
        )
        conn.close()

        # Unsettled in-flight delivery refuses resume.
        with transient_adapters(
            {"fake_implementer_build": impl, "fake_answerer": answerer}
        ):
            result = runner.invoke(app, ["continue"])
        assert result.exit_code == 1
        assert "interrupted" in result.output
        assert "--settle-interrupted" in result.output

        # Settle: the signal's zombie is cancelled; the P4 zombie survives.
        with transient_adapters(
            {"fake_implementer_build": impl, "fake_answerer": answerer}
        ):
            result = runner.invoke(app, ["continue", "--settle-interrupted"])
        assert result.exit_code == 0, result.output
        assert "communication_blocked" in result.output  # reply edge still refused

        conn, store = _open_store(build_workspace)
        assert store.load_model(Run, zombie.id).status is RunStatus.CANCELLED
        assert store.load_model(Run, p4_zombie.id).status is RunStatus.RUNNING

        # The settled delivery retried the exchange with a superseding send.
        signal_messages = [
            m
            for m in _messages(store, task.id)
            if m.type is MessageType.CLARIFICATION_REQUEST
        ]
        assert len(signal_messages) == 2
        retry_msg = signal_messages[1]
        assert f"message:{signal_msg.id}" in retry_msg.references
        escalations = _escalations(store, task.id)
        assert [e.reason for e in escalations] == ["policy_refused"] * 2
        assert {e.signal_message_id for e in escalations} == {
            m.id for m in signal_messages
        }
        conn.close()

        # Admit the reply edge -> the retry resolves and the build completes.
        full_edges = (
            "communication:\n"
            "  edges:\n"
            "    - {from: implementer, to: planner,"
            " types: [clarification_request], blocking: true}\n"
            "    - {from: planner, to: implementer,"
            " types: [clarification_response], blocking: false}\n"
        )
        _signal_workspace(build_workspace, communication=full_edges)
        with transient_adapters(
            {"fake_implementer_build": impl, "fake_answerer": answerer}
        ):
            result = runner.invoke(app, ["continue"])
        assert result.exit_code == 0, result.output
        assert "pass_promoted" in result.output
        conn, store = _open_store(build_workspace)
        assert _task(store).state is TaskState.APPROVAL_REQUIRED
        conn.close()

    def test_delivery_pending_escalation_is_persisted(self, build_workspace):
        """resolve_open_signal observing a RUNNING delivery writes the
        delivery_pending record before parking (unit-level driver call)."""
        from relay.context.config import load_config
        from relay.core.bus import ConversationBus
        from relay.core.delivery import MessageDelivery
        from relay.core.policy import (
            SqliteCommunicationPolicyGate,
            policy_from_config,
        )
        from relay.core.resolver import ConfigRoleResolver

        _signal_workspace(build_workspace)
        conn, store = _open_store(build_workspace)
        writer = EventLogWriter(conn)
        evidence = SqliteEvidenceStore(store)
        task = Task(title="t")
        store.save_model(task)
        signal_run = _forge_run(store, task)
        signal = parse_stage_signal(
            _signal_json("clarification_request", "planner", "q")
        )
        assert signal is not None
        message = Message(
            sender="impl",
            run_id=signal_run.id,
            task_id=task.id,
            recipient="planner_bot",
            recipient_role="planner",
            type=MessageType.CLARIFICATION_REQUEST,
            blocking=True,
            content="q",
        )
        store.save_model(message)
        zombie = Run(
            agent="planner_bot", role="planner", task_id=task.id, status=RunStatus.RUNNING
        )
        store.save_model(zombie)
        writer.record(
            EventLogEntry(
                type=EventType.MESSAGE_DELIVERED,
                task_id=task.id,
                sender="relay:delivery",
                recipient="planner_bot",
                content="in-flight",
                references=[
                    f"message:{message.id}",
                    f"run:{zombie.id}",
                    f"task:{task.id}",
                ],
            )
        )

        config = load_config(build_workspace)
        resolver = ConfigRoleResolver(dict(config.roles), set(config.agents))
        gate = SqliteCommunicationPolicyGate(store, policy_from_config(config))

        class _NullFactory:
            def build(self, name):  # pragma: no cover - never reached
                raise AssertionError("pending delivery must refuse before build")

            def model_of(self, name):
                return None

        bus = ConversationBus(store, writer, resolver, gate)
        delivery = MessageDelivery(store, writer, _NullFactory(), bus, gate)
        services = SignalServices(bus=bus, delivery=delivery, resolver=resolver)
        open_signal = OpenSignal(
            run=signal_run,
            signal=signal,
            stage="implement",
            attempt=1,
            message=message,
            reply=None,
        )
        resolution = asyncio.run(
            resolve_open_signal(
                store,
                writer,
                evidence,
                services,
                task,
                open_signal,
                signal_context=None,
            )
        )
        assert resolution.status == "escalated"
        escalations = _escalations(store, task.id)
        assert [e.reason for e in escalations] == ["delivery_pending"]
        assert escalations[0].signal_message_id == message.id
        conn.close()


# ---------------------------------------------------------------------------
# Fail-closed ledger provenance (forged markers and plan chains)
# ---------------------------------------------------------------------------


class TestLedgerProvenanceRefusals:
    def test_repeat_attempt_without_continuation_refs_refuses(self, build_workspace):
        _budget_parked_build(build_workspace)  # attempts 1, 2 parked
        conn, store = _open_store(build_workspace)
        writer = EventLogWriter(conn)
        task = _task(store)
        forged = _forge_run(store, task)
        _forge_marker(writer, task, forged, "fix", attempt=2)  # repeat, no refs
        assert _derive_refusal(store, task.id).code == "ledger_inconsistent"
        conn.close()

    def test_fresh_attempt_carrying_continuation_refs_refuses(self, build_workspace):
        _budget_parked_build(build_workspace)
        conn, store = _open_store(build_workspace)
        writer = EventLogWriter(conn)
        task = _task(store)
        forged = _forge_run(store, task)
        _forge_marker(
            writer,
            task,
            forged,
            "fix",
            attempt=3,
            continuation=("m-deadbeef", "r-deadbeef"),
        )
        assert _derive_refusal(store, task.id).code == "ledger_inconsistent"
        conn.close()

    def test_continuation_refs_to_foreign_message_refuse(self, build_workspace):
        _budget_parked_build(build_workspace)
        conn, store = _open_store(build_workspace)
        writer = EventLogWriter(conn)
        task = _task(store)
        forged = _forge_run(store, task)
        # A real repeated attempt with refs that resolve to nothing.
        _forge_marker(
            writer,
            task,
            forged,
            "fix",
            attempt=2,
            continuation=("nonexistent-message", "nonexistent-reply"),
        )
        assert _derive_refusal(store, task.id).code == "ledger_inconsistent"
        conn.close()

    def test_continuation_refs_must_name_predecessors_signal(
        self, build_workspace
    ):
        """Refs naming a message authored by a DIFFERENT run are refused —
        the causal chain must bind the immediately preceding run."""
        result, _impl = TestImplPlannerClarification()._build_with_signal(
            build_workspace
        )
        assert result.exit_code == 0
        conn, store = _open_store(build_workspace)
        writer = EventLogWriter(conn)
        task = _task(store)
        signal_msg, reply = _messages(store, task.id)
        forged = _forge_run(store, task)
        # attempt 1 repeats — but the immediate predecessor (the real
        # continuation run) authored no blocking messages.
        _forge_marker(
            writer,
            task,
            forged,
            "implement",
            attempt=1,
            continuation=(signal_msg.id, reply.id),
        )
        assert _derive_refusal(store, task.id).code == "ledger_inconsistent"
        conn.close()

    def test_review_repeat_without_refs_after_blocking_signal_refuses(
        self, build_workspace
    ):
        """A parked open reviewer signal makes any subsequent review run
        without continuation refs a ledger inconsistency."""
        # Park at REVIEWING with an open reviewer signal: send edge admitted,
        # reply edge denied so the message persists undelivered.
        edges = (
            "communication:\n"
            "  edges:\n"
            "    - {from: reviewer, to: implementer,"
            " types: [clarification_request], blocking: true}\n"
        )
        _signal_workspace(build_workspace, communication=edges)
        signals = build_workspace / ".relay" / "review-signals"
        _write_slot(
            signals,
            1,
            _signal_json("clarification_request", "implementer", "why?"),
        )
        answer = _write_file(build_workspace / ".relay" / "a.txt", "x")
        impl_cls = _signal_fake(
            build_workspace,
            "--review-signal-dir",
            str(signals),
            "--answer-file",
            str(answer),
        )
        answerer = _signal_answerer(build_workspace, "--answer-file", str(answer))
        with transient_adapters(
            {"fake_implementer_build": impl_cls, "fake_answerer": answerer}
        ):
            result = runner.invoke(
                app, ["build", "write implemented.txt", "--agent", "impl"]
            )
        assert result.exit_code == 0, result.output
        assert "communication_blocked" in result.output

        conn, store = _open_store(build_workspace)
        writer = EventLogWriter(conn)
        task = _task(store)
        assert task.state is TaskState.REVIEWING
        assert len(_messages(store, task.id)) == 1
        forged = _forge_run(store, task, agent="rev", role="reviewer")
        _forge_marker(writer, task, forged, "review")  # repeat, no refs
        assert _derive_refusal(store, task.id).code == "ledger_inconsistent"
        conn.close()

    def test_orphan_plan_artifact_refuses(self, build_workspace):
        """A second PLAN artifact outside the revision chain is corruption."""
        _budget_parked_build(build_workspace)
        conn, store = _open_store(build_workspace)
        task = _task(store)
        store.save_model(
            Artifact(
                kind=ArtifactKind.PLAN,
                task_id=task.id,
                run_id=next(
                    r.id for r in store.all_models(Run) if r.role == "planner"
                ),
                content="# rogue plan",
            )
        )
        assert _derive_refusal(store, task.id).code == "ledger_inconsistent"
        conn.close()

    def test_plan_revision_with_bogus_provenance_refuses(self, build_workspace):
        """A revision record naming an unresolvable signal/reply is refused."""
        _budget_parked_build(build_workspace)
        conn, store = _open_store(build_workspace)
        task = _task(store)
        plans = [
            a
            for a in store.all_models(Artifact)
            if a.kind is ArtifactKind.PLAN and a.task_id == task.id
        ]
        assert len(plans) == 1
        rogue_plan = store.save_model(
            Artifact(
                kind=ArtifactKind.PLAN,
                task_id=task.id,
                run_id=plans[0].run_id,
                content="# rogue tip",
            )
        )
        store.save_model(
            Artifact(
                kind=ArtifactKind.REPORT,
                task_id=task.id,
                content=json.dumps(
                    {
                        "schema_version": "relay.plan_revision.v1",
                        "task_id": task.id,
                        "plan_artifact_id": rogue_plan.id,
                        "supersedes_plan_artifact_id": plans[0].id,
                        "decision_id": "no-such-decision",
                        "signal_message_id": "no-such-message",
                        "reply_message_id": "no-such-reply",
                        "author_run_id": "no-such-run",
                    }
                ),
            )
        )
        assert _derive_refusal(store, task.id).code == "ledger_inconsistent"
        conn.close()


# ---------------------------------------------------------------------------
# Crash-gap recovery: intended-invalid signals never fall through to normal
# handling — including when the RUN_OUTPUT commit outlived the diagnostic.
# ---------------------------------------------------------------------------


def _delete_diagnostic(conn, store, task_id: str) -> str:
    """Drop the persisted signal diagnostic — the crash-gap state where the
    RUN_OUTPUT commit survived but the diagnostic's did not. The orphaned
    ARTIFACT_CREATED event stays (the log is append-only); nothing resolves
    diagnostic provenance through event refs."""
    diagnostics = [
        a
        for a in store.all_models(Artifact)
        if a.kind is ArtifactKind.REPORT
        and a.task_id == task_id
        and '"relay.build.signal.invalid.v1"' in (a.content or "")
    ]
    assert len(diagnostics) == 1
    artifact_id = diagnostics[0].id
    conn.execute("DELETE FROM artifacts WHERE id = ?", [artifact_id])
    conn.commit()
    return artifact_id


class TestInvalidSignalCrashRecovery:
    def test_impl_crash_gap_recovers_parks_then_resumes(self, build_workspace):
        """RUN_OUTPUT committed, diagnostic absent: continue must re-derive
        the invalid intent, persist the diagnostic, and park — never mint
        a fresh attempt from that run."""
        _signal_workspace(build_workspace)
        signals = build_workspace / ".relay" / "impl-signals"
        _write_slot(signals, 1, _signal_json("teleport", "planner", "x"))
        impl = _signal_fake(build_workspace, "--impl-signal-dir", str(signals))
        answerer = _signal_answerer(build_workspace)
        with transient_adapters(
            {"fake_implementer_build": impl, "fake_answerer": answerer}
        ):
            result = runner.invoke(
                app, ["build", "write implemented.txt", "--agent", "impl"]
            )
        assert result.exit_code == 0, result.output
        assert "communication_blocked" in result.output

        conn, store = _open_store(build_workspace)
        task = _task(store)
        assert [d.code for d in _diagnostics(store, task.id)] == ["invalid_signal"]
        impl_markers = [m for m in _markers(conn, task.id) if _stage_of(m) == "implement"]
        assert len(impl_markers) == 1

        # The crash gap: drop the diagnostic commit, keep the RUN_OUTPUT.
        _delete_diagnostic(conn, store, task.id)
        assert _diagnostics(store, task.id) == []

        # Resume: the ledger recovers the invalid intent — diagnostic is
        # re-persisted and the build re-parks. NO new attempt, no DIFF.
        with transient_adapters(
            {"fake_implementer_build": impl, "fake_answerer": answerer}
        ):
            result = runner.invoke(app, ["continue"])
        assert result.exit_code == 0, result.output
        assert "communication_blocked" in result.output
        diagnostics = _diagnostics(store, task.id)
        assert [d.code for d in diagnostics] == ["invalid_signal"]
        signal_run = next(r for r in store.all_models(Run) if r.role == "implementer")
        assert diagnostics[0].run_id == signal_run.id
        impl_markers = [m for m in _markers(conn, task.id) if _stage_of(m) == "implement"]
        assert len(impl_markers) == 1  # no fresh attempt was dispatched
        assert store.artifacts_for_run(signal_run.id, kind=ArtifactKind.DIFF) == []

        # Idempotent: losing the diagnostic AGAIN recovers the same way —
        # still exactly one diagnostic row, still parked, still no attempt.
        _delete_diagnostic(conn, store, task.id)
        with transient_adapters(
            {"fake_implementer_build": impl, "fake_answerer": answerer}
        ):
            result = runner.invoke(app, ["continue"])
        assert result.exit_code == 0, result.output
        assert "communication_blocked" in result.output
        assert len(_diagnostics(store, task.id)) == 1
        impl_markers = [m for m in _markers(conn, task.id) if _stage_of(m) == "implement"]
        assert len(impl_markers) == 1

        # With the diagnostic durable, the run is a consumed no-op —
        # continue dispatches a genuine fresh attempt.
        with transient_adapters(
            {"fake_implementer_build": impl, "fake_answerer": answerer}
        ):
            result = runner.invoke(app, ["continue"])
        assert result.exit_code == 0, result.output
        assert "pass_promoted" in result.output
        impl_markers = [m for m in _markers(conn, task.id) if _stage_of(m) == "implement"]
        assert [_refs(m, "build_attempt:") for m in impl_markers] == [["1"], ["2"]]
        conn.close()

    def test_review_crash_gap_recovers_parks_then_resumes(self, build_workspace):
        """Reviewer invalid-signal output obeys the same fail-closed rule:
        the missing diagnostic is recovered and the build re-parks."""
        _signal_workspace(build_workspace)
        signals = build_workspace / ".relay" / "review-signals"
        _write_slot(signals, 1, _signal_json("note", "planner", "fyi"))
        impl = _signal_fake(build_workspace, "--review-signal-dir", str(signals))
        answerer = _signal_answerer(build_workspace)
        with transient_adapters(
            {"fake_implementer_build": impl, "fake_answerer": answerer}
        ):
            result = runner.invoke(
                app, ["build", "write implemented.txt", "--agent", "impl"]
            )
        assert result.exit_code == 0, result.output
        assert "communication_blocked" in result.output

        conn, store = _open_store(build_workspace)
        task = _task(store)
        assert task.state is TaskState.REVIEWING
        assert [d.code for d in _diagnostics(store, task.id)] == ["kind_not_permitted"]
        review_markers = [m for m in _markers(conn, task.id) if _stage_of(m) == "review"]
        assert len(review_markers) == 1

        _delete_diagnostic(conn, store, task.id)

        with transient_adapters(
            {"fake_implementer_build": impl, "fake_answerer": answerer}
        ):
            result = runner.invoke(app, ["continue"])
        assert result.exit_code == 0, result.output
        assert "communication_blocked" in result.output
        assert [d.code for d in _diagnostics(store, task.id)] == ["kind_not_permitted"]
        review_markers = [m for m in _markers(conn, task.id) if _stage_of(m) == "review"]
        assert len(review_markers) == 1  # no new review run was dispatched
        assert not [
            a
            for a in store.all_models(Artifact)
            if a.kind is ArtifactKind.REVIEW_FINDING and a.task_id == task.id
        ]

        # Diagnostic durable → the review run is consumed → a legitimate
        # review retry proceeds.
        with transient_adapters(
            {"fake_implementer_build": impl, "fake_answerer": answerer}
        ):
            result = runner.invoke(app, ["continue"])
        assert result.exit_code == 0, result.output
        assert "pass_promoted" in result.output
        task = _task(store)
        assert task.state is TaskState.APPROVAL_REQUIRED
        conn.close()

    def test_buried_invalid_run_without_diagnostic_refuses(self, build_workspace):
        """An intended-invalid run that is NOT the last bound run and lacks
        a diagnostic is unforgeable-by-crash — the ledger refuses it."""
        _signal_workspace(build_workspace)
        signals = build_workspace / ".relay" / "impl-signals"
        _write_slot(signals, 1, _signal_json("teleport", "planner", "x"))
        impl = _signal_fake(build_workspace, "--impl-signal-dir", str(signals))
        answerer = _signal_answerer(build_workspace)
        with transient_adapters(
            {"fake_implementer_build": impl, "fake_answerer": answerer}
        ):
            result = runner.invoke(
                app, ["build", "write implemented.txt", "--agent", "impl"]
            )
        assert result.exit_code == 0, result.output

        conn, store = _open_store(build_workspace)
        task = _task(store)
        _delete_diagnostic(conn, store, task.id)
        # Bury the invalid run: a later impl run binds after it.
        later = _forge_run(store, task)
        writer = EventLogWriter(conn)
        _forge_marker(writer, task, later, "implement", attempt=2)
        conn.commit()
        assert _derive_refusal(store, task.id).code == "ledger_inconsistent"
        conn.close()


# ---------------------------------------------------------------------------
# Plan revision provenance: every revision edge must bind the full causal
# chain (signal -> delivery run -> canonical reply -> ACCEPTED decision ->
# new PLAN -> PLAN_PRODUCED evidence) or the ledger refuses.
# ---------------------------------------------------------------------------


def _superseded_plan_build(build_workspace):
    """Drive the fixer->planner proposal->supersede e2e. Returns the open
    (conn, store, task, answerer) — callers close the connection."""
    _signal_workspace(build_workspace)
    signals = build_workspace / ".relay" / "impl-signals"
    _write_slot(
        signals,
        2,  # the FIX run (impl ordinal 2) emits the proposal
        _signal_json("proposal", "planner", "adopt the simpler approach"),
    )
    decision = _write_file(
        build_workspace / ".relay" / "planner-decision.txt",
        _decision_json(
            "accept",
            "supersede",
            "plan v2 is canonical",
            rationale="the proposal is strictly better",
            revised_plan="# Plan v2\n\nGoal: implement the task\n"
            "Steps: write implemented.txt the simple way\n"
            "Files: implemented.txt\nVerification: file exists",
        ),
    )
    impl_answer = _write_file(build_workspace / ".relay" / "a.txt", "x")
    impl = _signal_fake(
        build_workspace,
        "--impl-signal-dir",
        str(signals),
        "--review-verdicts",
        "findings,pass",
        "--answer-file",
        str(impl_answer),
    )
    answerer = _signal_answerer(build_workspace, "--answer-file", str(decision))
    with transient_adapters(
        {"fake_implementer_build": impl, "fake_answerer": answerer}
    ):
        result = runner.invoke(
            app, ["build", "write implemented.txt", "--agent", "impl"]
        )
    assert result.exit_code == 0, result.output
    assert "pass_promoted" in result.output
    conn, store = _open_store(build_workspace)
    return conn, store, _task(store), answerer


def _revision_parts(store, task):
    """(report artifact, payload, decision, signal, reply, root, tip)."""
    reports = [
        a
        for a in store.all_models(Artifact)
        if a.task_id == task.id and '"relay.plan_revision.v1"' in (a.content or "")
    ]
    assert len(reports) == 1
    revision = PlanRevisionPayload.model_validate_json(reports[0].content or "")
    return (
        reports[0],
        revision,
        store.load_model(Decision, revision.decision_id),
        store.load_model(Message, revision.signal_message_id),
        store.load_model(Message, revision.reply_message_id),
        store.load_model(Artifact, revision.supersedes_plan_artifact_id),
        store.load_model(Artifact, revision.plan_artifact_id),
    )


def _replace_revision(store, task: Task, **overrides) -> PlanRevisionPayload:
    """Swap the real revision report for a forged variant (``artifacts`` is
    mutable; ``messages``/``evidence_records`` are trigger-protected)."""
    report, revision, *_ = _revision_parts(store, task)
    store.delete_model(report)
    forged = revision.model_copy(update=overrides)
    store.save_model(
        Artifact(
            kind=ArtifactKind.REPORT,
            task_id=task.id,
            content=forged.model_dump_json(),
        )
    )
    return forged


class TestPlanRevisionProvenance:
    def test_rejected_decision_refuses(self, build_workspace):
        conn, store, task, _a = _superseded_plan_build(build_workspace)
        _report, _rev, decision, _s, _r, _root, _tip = _revision_parts(store, task)
        conn.execute(
            "UPDATE decisions SET status = ? WHERE id = ?",
            [DecisionStatus.REJECTED.value, decision.id],
        )
        conn.commit()
        assert _derive_refusal(store, task.id).code == "ledger_inconsistent"
        conn.close()

    def test_decision_accepted_by_mismatch_refuses(self, build_workspace):
        conn, store, task, _a = _superseded_plan_build(build_workspace)
        _report, _rev, decision, _s, _r, _root, _tip = _revision_parts(store, task)
        conn.execute(
            "UPDATE decisions SET accepted_by = ? WHERE id = ?",
            ["mallory", decision.id],
        )
        conn.commit()
        assert _derive_refusal(store, task.id).code == "ledger_inconsistent"
        conn.close()

    def test_decision_proposed_by_mismatch_refuses(self, build_workspace):
        conn, store, task, _a = _superseded_plan_build(build_workspace)
        _report, _rev, decision, _s, _r, _root, _tip = _revision_parts(store, task)
        conn.execute(
            "UPDATE decisions SET proposed_by = ? WHERE id = ?",
            ["mallory", decision.id],
        )
        conn.commit()
        assert _derive_refusal(store, task.id).code == "ledger_inconsistent"
        conn.close()

    def test_clarification_signal_type_refuses(self, build_workspace):
        """A revision edge bound to a clarification_request (not a
        challenge/proposal) fails closed."""
        conn, store, task, _a = _superseded_plan_build(build_workspace)
        _report, _rev, _d, signal, _r, _root, _tip = _revision_parts(store, task)
        forged_signal = store.save_model(
            Message(
                sender=signal.sender,
                recipient=signal.recipient,
                recipient_role=signal.recipient_role,
                task_id=task.id,
                type=MessageType.CLARIFICATION_REQUEST,
                blocking=True,
                run_id=signal.run_id,
                content="forged clarification signal",
            )
        )
        _replace_revision(store, task, signal_message_id=forged_signal.id)
        assert _derive_refusal(store, task.id).code == "ledger_inconsistent"
        conn.close()

    def test_missing_plan_produced_evidence_refuses(self, build_workspace):
        """A revision edge whose tip plan has no PLAN_PRODUCED evidence."""
        conn, store, task, _a = _superseded_plan_build(build_workspace)
        _report, _rev, _d, _s, reply, _root, _tip = _revision_parts(store, task)
        forged_tip = store.save_model(
            Artifact(
                kind=ArtifactKind.PLAN,
                task_id=task.id,
                run_id=reply.run_id,
                content="# forged tip",
            )
        )
        _replace_revision(store, task, plan_artifact_id=forged_tip.id)
        assert _derive_refusal(store, task.id).code == "ledger_inconsistent"
        conn.close()

    def test_plan_produced_wrong_run_refuses(self, build_workspace):
        """PLAN_PRODUCED exists for the tip but names the wrong run."""
        conn, store, task, _a = _superseded_plan_build(build_workspace)
        _report, _rev, _d, _s, reply, _root, _tip = _revision_parts(store, task)
        forged_tip = store.save_model(
            Artifact(
                kind=ArtifactKind.PLAN,
                task_id=task.id,
                run_id=reply.run_id,
                content="# forged tip",
            )
        )
        SqliteEvidenceStore(store).record(
            EvidenceRecord(
                kind=EvidenceKind.PLAN_PRODUCED,
                task_id=task.id,
                run_id="no-such-run",
                artifact_id=forged_tip.id,
                produced_by="agent:forged",
            )
        )
        _replace_revision(store, task, plan_artifact_id=forged_tip.id)
        assert _derive_refusal(store, task.id).code == "ledger_inconsistent"
        conn.close()

    def test_plan_produced_wrong_artifact_refuses(self, build_workspace):
        """PLAN_PRODUCED exists for the reply run but names the wrong plan."""
        conn, store, task, _a = _superseded_plan_build(build_workspace)
        _report, _rev, _d, _s, reply, _root, _tip = _revision_parts(store, task)
        forged_tip = store.save_model(
            Artifact(
                kind=ArtifactKind.PLAN,
                task_id=task.id,
                run_id=reply.run_id,
                content="# forged tip",
            )
        )
        SqliteEvidenceStore(store).record(
            EvidenceRecord(
                kind=EvidenceKind.PLAN_PRODUCED,
                task_id=task.id,
                run_id=reply.run_id,
                artifact_id="no-such-artifact",
                produced_by="agent:forged",
            )
        )
        _replace_revision(store, task, plan_artifact_id=forged_tip.id)
        assert _derive_refusal(store, task.id).code == "ledger_inconsistent"
        conn.close()

    def test_orphan_revision_record_refuses(self, build_workspace):
        """A revision record superseding nothing reachable is corruption."""
        conn, store, task, _a = _superseded_plan_build(build_workspace)
        _report, revision, _d, _s, _r, _root, _tip = _revision_parts(store, task)
        store.save_model(
            Artifact(
                kind=ArtifactKind.REPORT,
                task_id=task.id,
                content=revision.model_copy(
                    update={"supersedes_plan_artifact_id": "no-such-plan"}
                ).model_dump_json(),
            )
        )
        assert _derive_refusal(store, task.id).code == "ledger_inconsistent"
        conn.close()

    def test_double_supersession_refuses(self, build_workspace):
        """A second revision record superseding the SAME plan is a fork."""
        conn, store, task, _a = _superseded_plan_build(build_workspace)
        _report, revision, _d, _s, _r, _root, _tip = _revision_parts(store, task)
        store.save_model(
            Artifact(
                kind=ArtifactKind.REPORT,
                task_id=task.id,
                content=revision.model_dump_json(),
            )
        )
        assert _derive_refusal(store, task.id).code == "ledger_inconsistent"
        conn.close()

    def test_reply_not_from_delivery_run_refuses(self, build_workspace):
        """The reply must be authored by the run that delivered the signal:
        a forged exchange with a foreign reply run fails closed."""
        conn, store, task, _a = _superseded_plan_build(build_workspace)
        _report, _rev, _d, signal, reply, _root, _tip = _revision_parts(
            store, task
        )
        forged_signal = store.save_model(
            Message(
                sender=signal.sender,
                recipient=signal.recipient,
                recipient_role=signal.recipient_role,
                task_id=task.id,
                type=MessageType.PROPOSAL,
                blocking=True,
                run_id=signal.run_id,
                content="forged proposal signal",
            )
        )
        forged_reply = store.save_model(
            Message(
                sender=signal.recipient,
                recipient=signal.sender,
                task_id=task.id,
                type=MessageType.FINAL_POSITION,
                reply_to_id=forged_signal.id,
                run_id="no-such-run",
                content="forged reply",
            )
        )
        forged_tip = store.save_model(
            Artifact(
                kind=ArtifactKind.PLAN,
                task_id=task.id,
                run_id=reply.run_id,
                content="# forged tip",
            )
        )
        _replace_revision(
            store,
            task,
            signal_message_id=forged_signal.id,
            reply_message_id=forged_reply.id,
            plan_artifact_id=forged_tip.id,
            author_run_id=forged_reply.run_id,
        )
        assert _derive_refusal(store, task.id).code == "ledger_inconsistent"
        conn.close()


# ---------------------------------------------------------------------------
# Bounded planner context: challenge/proposal deliveries carry the current
# plan + original request inside the frozen envelope.
# ---------------------------------------------------------------------------


class TestSignalDeliveryContext:
    def test_proposal_delivery_carries_plan_request_and_blocker(
        self, build_workspace
    ):
        """The planner answers a superseding proposal with the canonical
        plan in view — the context rides the message content and refs."""
        conn, store, task, answerer = _superseded_plan_build(build_workspace)
        _report, _rev, _d, signal, _r, root, _tip = _revision_parts(
            store, task
        )
        packets = [
            a
            for a in store.all_models(Artifact)
            if a.kind is ArtifactKind.FIX_PACKET and a.task_id == task.id
        ]
        assert len(packets) == 1

        # The proposal message embeds the bounded context block BEFORE the
        # reply contract — all inside the frozen D15 envelope.
        assert signal.type is MessageType.PROPOSAL
        content = signal.content
        assert "ORIGINAL BUILD REQUEST:" in content
        assert "write implemented.txt" in content
        assert f"CURRENT CANONICAL PLAN (artifact:{root.id}):" in content
        assert (root.content or "")[:200] in content
        assert f"STAGE BLOCKER: artifact:{packets[0].id}" in content
        assert "relay.planner_decision.v1" in content
        assert content.index("CURRENT CANONICAL PLAN") < content.index(
            "relay.planner_decision.v1"
        )

        # Provenance refs ride message.references -> context_refs.
        assert f"artifact:{root.id}" in signal.references
        assert f"artifact:{packets[0].id}" in signal.references
        delivery_requests = [
            r for r in answerer.seen_requests if "adopt the simpler approach" in r.prompt
        ]
        assert len(delivery_requests) == 1
        request = delivery_requests[0]
        assert f"artifact:{root.id}" in request.context_refs
        assert f"artifact:{packets[0].id}" in request.context_refs

        # The envelope itself is untouched: verbatim D15 prefix.
        assert request.prompt.startswith(
            "You received a message via the Relay conversation bus.\n"
            "FROM: impl\nTYPE: proposal\nBLOCKING: true\n\nMESSAGE:\n"
        )
        conn.close()

    def test_clarification_carries_no_decision_context(self, build_workspace):
        """Only challenge/proposal get the context block — clarification
        messages keep the pre-context shape."""
        _signal_workspace(build_workspace)
        signals = build_workspace / ".relay" / "impl-signals"
        _write_slot(
            signals, 1, _signal_json("clarification_request", "planner", "which file?")
        )
        answer = _write_file(build_workspace / ".relay" / "a.txt", "the file")
        impl = _signal_fake(
            build_workspace, "--impl-signal-dir", str(signals)
        )
        answerer = _signal_answerer(build_workspace, "--answer-file", str(answer))
        with transient_adapters(
            {"fake_implementer_build": impl, "fake_answerer": answerer}
        ):
            result = runner.invoke(
                app, ["build", "write implemented.txt", "--agent", "impl"]
            )
        assert result.exit_code == 0, result.output
        conn, store = _open_store(build_workspace)
        task = _task(store)
        message = _messages(store, task.id)[0]
        assert message.type is MessageType.CLARIFICATION_REQUEST
        assert "CURRENT CANONICAL PLAN" not in message.content
        assert "ORIGINAL BUILD REQUEST" not in message.content
        assert not [r for r in message.references if r.startswith("artifact:")]
        conn.close()

"""P6.3 — build-run provenance and ledger-derived build position.

``relay build`` and ``relay continue`` share one idempotent stage driver
(``orchestrator._drive_build``); this module is its read model. Every fact
comes from durable ledger records — ``BUILD_RUN_DISPATCHED`` binding markers
committed in each dispatched run's Tx1, run rows, artifacts, evidence, and
event-log sequence order — never from timestamps, roles, or guesses.

Provenance rules:

* a run is build-owned iff exactly one well-formed ``BUILD_RUN_DISPATCHED``
  marker names it (``sender="relay:build"``, refs ``run:``, ``task:``,
  ``build_stage:<plan|implement|fix|review>``; implement/fix additionally
  carry ``build_attempt:<n>``);
* P6.4 continuation runs REPEAT their attempt number and must carry the
  full causal chain — ``build_continuation:<message_id>`` +
  ``signal_reply:<reply_id>`` resolving to the previous run's blocking
  signal message and its canonical answering reply — else the ledger
  refuses closed;
* P4 deliveries are bound by ``MESSAGE_DELIVERED`` and ignored everywhere;
* a task-scoped run bound by NEITHER marker refuses ``unattributed_runs`` —
  attempt counting can never silently miscount a foreign run.

``derive_position`` is pure reads: it never writes, so callers can consult
it as often as the driver iterates.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Literal, NoReturn

import pydantic

from relay.core.evidence import EvidenceKind, EvidenceStore
from relay.core.reviews import ReviewContractError, decode_fix_packet
from relay.core.stage_signals import (
    OpenSignal,
    SignalContractError,
    blocking_messages_authored_by,
    canonical_replies_for,
    check_signal_legal,
    signal_for_run_output,
    signal_is_blocking,
    signal_message_type,
)
from relay.core.state_machine import TaskState
from relay.storage.events import EventLogWriter
from relay.storage.models import (
    Artifact,
    ArtifactKind,
    BuildBaselineRecordPayload,
    BuildRequestRecordPayload,
    Decision,
    EventLogEntry,
    EventType,
    EvidenceRecord,
    Message,
    MessageType,
    PlanRevisionPayload,
    Run,
    RunStatus,
    Task,
    ToolRun,
    utcnow,
)
from relay.storage.store import SqliteRelayStore

#: Marker sender identity — the same contract family as P4.2's
#: ``relay:delivery`` MESSAGE_DELIVERED provenance.
BUILD_SENDER = "relay:build"

BuildStage = Literal["plan", "implement", "fix", "review"]
_BUILD_STAGES = frozenset({"plan", "implement", "fix", "review"})
_IMPL_STAGES = frozenset({"implement", "fix"})

#: Blocking signal message types that admit a canonical reply (P6.4).
_REPLY_PARENT_TYPES = frozenset(
    {
        MessageType.CLARIFICATION_REQUEST,
        MessageType.CHALLENGE,
        MessageType.PROPOSAL,
    }
)

NextAction = Literal[
    "collect_context",
    "plan",
    "advance",
    "dispatch",
    "resolve_signal",
    "verify",
    "review",
    "stop",
]


class BuildRefusal(Exception):
    """Typed refusal: the requested implementer cannot do a build safely."""


class ContinueRefusal(BuildRefusal):
    """Typed pre-execution refusal for ``relay continue`` — zero store delta.

    ``code`` is a stable machine-readable reason the CLI surfaces verbatim.
    """

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        super().__init__(message or code)


def _refuse(code: str, message: str) -> NoReturn:
    raise ContinueRefusal(code, message)


def build_dispatch_hook(
    task_id: str,
    stage: BuildStage,
    attempt: int | None = None,
    continuation: tuple[str, str] | None = None,
) -> Callable[[Run, Artifact], Iterable[EventLogEntry]]:
    """``pre_provider`` hook committing this run's build binding in Tx1.

    The marker asserts a binding, never a success — failed and cancelled
    runs retain it, which is exactly what resume needs to attribute them.

    ``continuation`` carries ``(signal_message_id, reply_message_id)`` for
    P6.4 same-attempt continuation dispatches — the persisted blocking
    exchange this run resumes from. The ledger refuses a repeated attempt
    number without it.
    """

    def bind(run: Run, _input_artifact: Artifact) -> Iterable[EventLogEntry]:
        references = [f"task:{task_id}", f"run:{run.id}", f"build_stage:{stage}"]
        if stage in _IMPL_STAGES and attempt is not None:
            references.append(f"build_attempt:{attempt}")
        if continuation is not None:
            references.append(f"build_continuation:{continuation[0]}")
            references.append(f"signal_reply:{continuation[1]}")
        return [
            EventLogEntry(
                type=EventType.BUILD_RUN_DISPATCHED,
                task_id=task_id,
                sender=BUILD_SENDER,
                content=f"build stage '{stage}' dispatched as run {run.id}",
                references=references,
            )
        ]

    return bind


@dataclass(frozen=True)
class AttemptRecords:
    """Ledger records bound to one build-owned implement/fix run."""

    run: Run
    diff_artifact: Artifact | None
    implementation_produced: EvidenceRecord | None
    #: Latest task-bound verification ToolRun in this attempt's window —
    #: after this run's dispatch marker, before the next impl marker.
    verification_tool_run: ToolRun | None
    test_result_artifact: Artifact | None
    tests_passed: EvidenceRecord | None
    #: The FIX_PACKET whose subject pins this run, when review produced one.
    fix_packet: Artifact | None


@dataclass(frozen=True)
class BuildPosition:
    """Read-only ledger-derived position of one task's build (P6.3/P6.4)."""

    task: Task
    request: BuildRequestRecordPayload
    baseline_pin: BuildBaselineRecordPayload | None
    plan_artifact: Artifact | None
    plan_run: Run | None
    impl_runs: tuple[Run, ...]
    #: Attempt number of each impl run, parallel to ``impl_runs``.
    #: Continuation runs repeat the previous number.
    impl_attempts: tuple[int, ...]
    latest_records: AttemptRecords | None
    pending_input: Artifact | None
    #: The latest no-diff impl/review run's blocking signal state, when it
    #: has one — answered signals drive continuation dispatch, unanswered
    #: ones drive ``resolve_signal``.
    pending_signal: OpenSignal | None
    #: Signal exchanges of the current attempt (or review streak), paired
    #: ``(signal message, canonical reply|None)`` — the continuation
    #: prompt's exchange history.
    exchanges: tuple[tuple[Message, Message | None], ...]
    in_flight_runs: tuple[Run, ...]
    #: RUNNING delivery runs for this task's signal messages — zombie
    #: deliveries ``--settle-interrupted`` cancels.
    in_flight_delivery_runs: tuple[Run, ...]
    in_flight_tool_runs: tuple[ToolRun, ...]
    next_action: NextAction
    advance_target: TaskState | None
    needs_baseline_capture: bool
    last_diff_artifact_id: str | None
    last_review_artifact_id: str | None
    last_fix_packet_artifact_id: str | None

    @property
    def attempts(self) -> int:
        """Build attempts across ALL invocations — continuation runs do
        not increment this (P6.4)."""
        return self.impl_attempts[-1] if self.impl_attempts else 0

    @property
    def fix_runs_used(self) -> int:
        """Cumulative fix attempts — the ``budget.max_fix_loops`` currency."""
        return max(0, self.attempts - 1)


def _single_report(
    store: SqliteRelayStore,
    task_id: str,
    schema: str,
    payload: type[BuildRequestRecordPayload | BuildBaselineRecordPayload],
) -> BuildRequestRecordPayload | BuildBaselineRecordPayload | None:
    """Decode the task's single REPORT artifact of ``schema`` (or refuse)."""
    artifacts = [
        a
        for a in store.all_models(
            Artifact, "WHERE task_id = ? AND kind = ?", [task_id, ArtifactKind.REPORT.value]
        )
        if a.content is not None and schema in a.content
    ]
    if len(artifacts) > 1:
        _refuse("ledger_inconsistent", f"task '{task_id}' has multiple '{schema}' records")
    if not artifacts:
        return None
    try:
        decoded = payload.model_validate_json(artifacts[0].content or "")
    except pydantic.ValidationError as exc:
        _refuse(
            "ledger_inconsistent",
            f"task '{task_id}' has an undecodable '{schema}' record: {type(exc).__name__}",
        )
    if decoded.task_id != task_id:
        _refuse(
            "ledger_inconsistent",
            f"task '{task_id}' has a '{schema}' record naming a different task",
        )
    return decoded


def _pending_input(records: AttemptRecords | None) -> Artifact | None:
    """The blocking artifact the NEXT fix dispatch must consume, if any.

    Mirrors the P6.2 in-memory ``blocking`` variable exactly: a failed
    verification's persisted TEST_RESULT, else the latest FIX_PACKET
    pinning this run.
    """
    if records is None:
        return None
    tool_run = records.verification_tool_run
    if (
        tool_run is not None
        and tool_run.status is RunStatus.FAILED
        and tool_run.result_ref is not None
        and records.test_result_artifact is not None
    ):
        return records.test_result_artifact
    if records.fix_packet is not None:
        return records.fix_packet
    return None


def _validate_continuation(
    store: SqliteRelayStore,
    task_id: str,
    continuation: tuple[str, str] | None,
    predecessor_run_id: str,
) -> None:
    """Fail-closed check that a repeated dispatch is a P6.4 continuation.

    The marker must name (a) the LATEST blocking signal message authored by
    the immediately preceding run, and (b) that message's canonical
    answering reply. Anything less means an attempt number was repeated
    without provenance — ledger corruption, refuse.
    """
    if continuation is None:
        _refuse(
            "ledger_inconsistent",
            f"run '{predecessor_run_id}' was followed by a same-attempt run "
            "without continuation provenance",
        )
    message_id, reply_id = continuation
    message = store.load_model(Message, message_id)
    authored = blocking_messages_authored_by(store, task_id, predecessor_run_id)
    if (
        message is None
        or message.task_id != task_id
        or not message.blocking
        or message.type not in _REPLY_PARENT_TYPES
        or message.run_id != predecessor_run_id
        or not authored
        or authored[-1].id != message.id
    ):
        _refuse(
            "ledger_inconsistent",
            f"continuation ref 'message:{message_id}' does not name the "
            f"latest blocking signal authored by run '{predecessor_run_id}'",
        )
    reply = store.load_model(Message, reply_id)
    if reply is None or reply.id not in {
        r.id for r in canonical_replies_for(store, message)
    }:
        _refuse(
            "ledger_inconsistent",
            f"continuation ref 'message:{reply_id}' is not a canonical "
            f"answer to signal 'message:{message_id}'",
        )


def _validate_plan_revision(
    store: SqliteRelayStore,
    task_id: str,
    revision: PlanRevisionPayload,
    tip: Artifact,
) -> None:
    """Fail-closed check that a plan revision link is fully provenance-bound:
    the reply must be the canonical answer to the named signal, and the new
    plan artifact must be authored by that reply's delivery run."""

    signal = store.load_model(Message, revision.signal_message_id)
    reply = store.load_model(Message, revision.reply_message_id)
    decision = store.load_model(Decision, revision.decision_id)
    if (
        signal is None
        or signal.task_id != task_id
        or reply is None
        or reply.id not in {r.id for r in canonical_replies_for(store, signal)}
        or decision is None
        or decision.task_id != task_id
        or revision.author_run_id != reply.run_id
        or tip.run_id != reply.run_id
        or reply.run_id is None
    ):
        _refuse(
            "ledger_inconsistent",
            f"plan revision '{revision.plan_artifact_id}' is not bound to "
            "its decision/exchange provenance",
        )


def _open_signal_for_run(
    store: SqliteRelayStore,
    task: Task,
    run: Run,
    *,
    stage: str,
    attempt: int | None,
) -> OpenSignal | None:
    """Ledger-derived signal state for one run — the valid, legal, blocking
    signal it emitted plus its latest message/reply, or None."""

    signal = signal_for_run_output(store, run)
    if signal is None or not signal_is_blocking(signal):
        return None
    try:
        check_signal_legal(signal, run.role)
    except SignalContractError:
        return None
    authored = blocking_messages_authored_by(store, task.id, run.id)
    message = authored[-1] if authored else None
    reply: Message | None = None
    if message is not None:
        replies = canonical_replies_for(store, message)
        if len(replies) > 1:
            _refuse(
                "ledger_inconsistent",
                f"signal message '{message.id}' has multiple canonical replies",
            )
        reply = replies[0] if replies else None
    return OpenSignal(
        run=run, signal=signal, stage=stage, attempt=attempt, message=message, reply=reply
    )


def _exchanges_for_runs(
    store: SqliteRelayStore, task_id: str, run_ids: list[str]
) -> tuple[tuple[Message, Message | None], ...]:
    """All blocking signal messages authored by ``run_ids`` paired with
    their canonical reply — the exchange history a continuation prompt
    embeds."""
    pairs: list[tuple[Message, Message | None]] = []
    for run_id in run_ids:
        for message in blocking_messages_authored_by(store, task_id, run_id):
            replies = canonical_replies_for(store, message)
            if len(replies) > 1:
                _refuse(
                    "ledger_inconsistent",
                    f"signal message '{message.id}' has multiple canonical replies",
                )
            pairs.append((message, replies[0] if replies else None))
    return tuple(pairs)


def derive_position(
    store: SqliteRelayStore, evidence: EvidenceStore, task_id: str
) -> BuildPosition:
    """Derive the task's build position from durable ledger records only."""
    task = store.load_model(Task, task_id)
    if task is None:
        _refuse("no_task", f"task '{task_id}' does not exist")

    # --- build-run provenance: markers bind runs to build stages ---------
    # run_id -> (stage, seq, attempt, continuation (message_id, reply_id))
    bound: dict[str, tuple[str, int, int | None, tuple[str, str] | None]] = {}
    for marker in store.all_models(
        EventLogEntry,
        "WHERE type = ? AND task_id = ?",
        [EventType.BUILD_RUN_DISPATCHED.value, task.id],
        order_by="sequence ASC",
    ):
        run_refs = [r[4:] for r in marker.references if r.startswith("run:")]
        stage_refs = [r[12:] for r in marker.references if r.startswith("build_stage:")]
        attempt_refs = [r[14:] for r in marker.references if r.startswith("build_attempt:")]
        cont_refs = [
            r[19:] for r in marker.references if r.startswith("build_continuation:")
        ]
        reply_refs = [r[13:] for r in marker.references if r.startswith("signal_reply:")]
        if (
            len(run_refs) != 1
            or len(stage_refs) != 1
            or stage_refs[0] not in _BUILD_STAGES
            or len(attempt_refs) > 1
            or len(cont_refs) > 1
            or len(reply_refs) > 1
            or (len(cont_refs) == 0) != (len(reply_refs) == 0)
            or marker.sender != BUILD_SENDER
        ):
            _refuse(
                "ledger_inconsistent",
                f"build dispatch marker seq={marker.sequence} is malformed",
            )
        attempt: int | None = None
        if attempt_refs:
            if not attempt_refs[0].isdigit():
                _refuse(
                    "ledger_inconsistent",
                    f"build dispatch marker seq={marker.sequence} has a bad attempt ref",
                )
            attempt = int(attempt_refs[0])
        if (attempt is None) != (stage_refs[0] not in _IMPL_STAGES):
            _refuse(
                "ledger_inconsistent",
                f"build dispatch marker seq={marker.sequence} mismatches its stage",
            )
        continuation: tuple[str, str] | None = None
        if cont_refs:
            if stage_refs[0] == "plan":
                _refuse(
                    "ledger_inconsistent",
                    f"build dispatch marker seq={marker.sequence} carries "
                    "continuation refs on a plan stage",
                )
            continuation = (cont_refs[0], reply_refs[0])
        if run_refs[0] in bound:
            _refuse(
                "ledger_inconsistent",
                f"run '{run_refs[0]}' carries two build dispatch markers",
            )
        bound[run_refs[0]] = (
            stage_refs[0],
            marker.sequence or 0,
            attempt,
            continuation,
        )

    delivered: set[str] = set()
    delivery_runs_for_message: dict[str, list[str]] = {}
    for entry in store.all_models(
        EventLogEntry,
        "WHERE type = ? AND task_id = ?",
        [EventType.MESSAGE_DELIVERED.value, task.id],
        order_by="sequence ASC",
    ):
        run_ids = [r[4:] for r in entry.references if r.startswith("run:")]
        delivered.update(run_ids)
        for ref in entry.references:
            if ref.startswith("message:"):
                delivery_runs_for_message.setdefault(ref[8:], []).extend(run_ids)

    task_runs = list(
        store.all_models(Run, "WHERE task_id = ?", [task.id], order_by="rowid ASC")
    )
    unattributed = [
        run.id for run in task_runs if run.id not in bound and run.id not in delivered
    ]
    if unattributed:
        _refuse(
            "unattributed_runs",
            f"task '{task.id}' has task-scoped runs bound to neither build "
            f"nor delivery provenance: {', '.join(unattributed)}",
        )

    bound_runs: dict[str, Run] = {}
    for run_id in bound:
        run = store.load_model(Run, run_id)
        if run is None or run.task_id != task.id:
            _refuse(
                "ledger_inconsistent",
                f"build dispatch marker names run '{run_id}' outside this task",
            )
        bound_runs[run_id] = run

    # --- durable build records ------------------------------------------
    request = _single_report(store, task.id, "relay.build.request.v1", BuildRequestRecordPayload)
    if request is None:
        _refuse(
            "no_request",
            f"task '{task.id}' has no durable build request (relay.build.request.v1)",
        )
    assert isinstance(request, BuildRequestRecordPayload)
    pin = _single_report(store, task.id, "relay.build.baseline.v1", BuildBaselineRecordPayload)
    assert pin is None or isinstance(pin, BuildBaselineRecordPayload)

    plans = list(
        store.all_models(
            Artifact, "WHERE task_id = ? AND kind = ?", [task.id, ArtifactKind.PLAN.value]
        )
    )

    # --- plan revision chain (P6.4): supersession is append-only ---------
    revisions: list[PlanRevisionPayload] = []
    for artifact in store.all_models(
        Artifact,
        "WHERE task_id = ? AND kind = ?",
        [task.id, ArtifactKind.REPORT.value],
    ):
        content = artifact.content or ""
        if '"relay.plan_revision.v1"' not in content:
            continue
        try:
            revision = PlanRevisionPayload.model_validate_json(content)
        except pydantic.ValidationError:
            _refuse(
                "ledger_inconsistent",
                f"plan revision record '{artifact.id}' is undecodable",
            )
        if revision.task_id != task.id:
            _refuse(
                "ledger_inconsistent",
                f"plan revision record '{artifact.id}' names a different task",
            )
        revisions.append(revision)

    plan_artifact: Artifact | None = None
    plan_run: Run | None = None
    if plans:
        # Exactly one ROOT plan — authored by a bound plan-stage run.
        roots = [
            p
            for p in plans
            if p.run_id is not None and bound.get(p.run_id, ("", 0, None, None))[0] == "plan"
        ]
        if len(roots) != 1:
            _refuse(
                "ledger_inconsistent",
                f"task '{task.id}' has {len(roots)} root plan artifacts",
            )
        current = roots[0]
        seen = {current.id}
        while True:
            successors = [
                r for r in revisions if r.supersedes_plan_artifact_id == current.id
            ]
            if len(successors) > 1:
                _refuse(
                    "ledger_inconsistent",
                    f"plan artifact '{current.id}' is superseded twice",
                )
            if not successors:
                break
            revision = successors[0]
            tip = next((p for p in plans if p.id == revision.plan_artifact_id), None)
            if tip is None or tip.id in seen:
                _refuse(
                    "ledger_inconsistent",
                    f"plan revision '{revision.plan_artifact_id}' is missing or cyclic",
                )
            _validate_plan_revision(store, task.id, revision, tip)
            current = tip
            seen.add(current.id)
        leftover = [p.id for p in plans if p.id not in seen]
        if leftover:
            _refuse(
                "ledger_inconsistent",
                f"task '{task.id}' has plan artifacts outside the revision "
                f"chain: {', '.join(leftover)}",
            )
        plan_artifact = current
        run = store.load_model(Run, plan_artifact.run_id or "")
        if run is None:
            _refuse(
                "ledger_inconsistent",
                f"plan artifact '{plan_artifact.id}' has no resolvable author run",
            )
        plan_run = run

    impl_markers = sorted(
        (
            (seq, run_id, attempt, continuation)
            for run_id, (stage, seq, attempt, continuation) in bound.items()
            if stage in _IMPL_STAGES
        ),
        key=lambda entry: entry[0],
    )
    impl_attempts_list: list[int] = []
    for index, (_seq, run_id, attempt, continuation) in enumerate(impl_markers):
        assert attempt is not None  # marker validation guarantees impl attempts
        impl_attempts_list.append(attempt)
        if index == 0:
            if attempt != 1 or continuation is not None:
                _refuse(
                    "ledger_inconsistent",
                    f"first build attempt marker for run '{run_id}' is "
                    f"attempt {attempt} (expected 1, no continuation refs)",
                )
            continue
        prev_attempt = impl_markers[index - 1][2]
        assert prev_attempt is not None
        if attempt == prev_attempt + 1:
            if continuation is not None:
                _refuse(
                    "ledger_inconsistent",
                    f"fresh attempt marker for run '{run_id}' carries "
                    "continuation refs",
                )
        elif attempt == prev_attempt:
            # P6.4 same-attempt continuation — the marker must name the
            # previous run's blocking signal AND its canonical reply.
            _validate_continuation(
                store, task.id, continuation, impl_markers[index - 1][1]
            )
        else:
            _refuse(
                "ledger_inconsistent",
                f"build attempt marker for run '{run_id}' is out of order "
                f"(expected {prev_attempt} or {prev_attempt + 1}, found {attempt})",
            )
    impl_runs = tuple(bound_runs[run_id] for _seq, run_id, _a, _c in impl_markers)
    impl_attempts = tuple(impl_attempts_list)
    impl_marker_seqs = [seq for seq, _run_id, _a, _c in impl_markers]
    # Index into impl_markers of the last run of each attempt group.
    impl_group_ends = [
        index
        for index, (_s, _r, attempt, _c) in enumerate(impl_markers)
        if index + 1 == len(impl_markers) or impl_markers[index + 1][2] != attempt
    ]

    # --- review-run streaks: repeats need continuation provenance --------
    review_streak: list[tuple[str, tuple[str, str] | None]] = []
    review_streaks: list[list[str]] = []
    for run_id, (stage, _seq, _a, continuation) in sorted(
        bound.items(), key=lambda pair: pair[1][1]
    ):
        if stage in _IMPL_STAGES:
            if review_streak:
                review_streaks.append([r for r, _c in review_streak])
            review_streak = []
            continue
        if stage != "review":
            continue
        if review_streak:
            # A repeated review run must continue from the previous review
            # run's blocking signal — unless that run authored none, which
            # makes it a legit retry of a failed/invalid review.
            prev_run_id = review_streak[-1][0]
            if continuation is None:
                if blocking_messages_authored_by(store, task.id, prev_run_id):
                    _refuse(
                        "ledger_inconsistent",
                        f"review run '{run_id}' repeats without continuation "
                        "provenance although its predecessor authored a "
                        "blocking signal",
                    )
            else:
                _validate_continuation(store, task.id, continuation, prev_run_id)
        elif continuation is not None:
            _refuse(
                "ledger_inconsistent",
                f"first review run '{run_id}' carries continuation refs",
            )
        review_streak.append((run_id, continuation))
    if review_streak:
        review_streaks.append([r for r, _c in review_streak])

    # --- signal/message consistency: authored messages imply signals -----
    signal_runs = [
        bound_runs[run_id]
        for run_id, (stage, _s, _a, _c) in bound.items()
        if stage in _IMPL_STAGES or stage == "review"
    ]
    for run in signal_runs:
        authored = blocking_messages_authored_by(store, task.id, run.id)
        if not authored:
            continue
        # A blocking message can only exist where a valid, legal, blocking
        # signal output was persisted — anything else is corruption.
        signal = signal_for_run_output(store, run)
        if signal is None:
            _refuse(
                "ledger_inconsistent",
                f"run '{run.id}' authored blocking messages without a "
                "valid blocking stage signal",
            )
        try:
            check_signal_legal(signal, run.role)
        except SignalContractError as exc:
            _refuse(
                "ledger_inconsistent",
                f"run '{run.id}' authored blocking messages but its signal "
                f"is illegal: {exc.code}",
            )
        expected_type = signal_message_type(signal)
        expected_role = signal.to_role
        for index, message in enumerate(authored):
            if (
                message.type is not expected_type
                or message.recipient_role != expected_role
            ):
                _refuse(
                    "ledger_inconsistent",
                    f"message '{message.id}' does not match its run's "
                    f"signal output",
                )
            if index > 0 and f"message:{authored[index - 1].id}" not in (
                message.references or []
            ):
                _refuse(
                    "ledger_inconsistent",
                    f"signal retry '{message.id}' does not reference the "
                    "message it supersedes",
                )
        if not signal_is_blocking(signal):
            _refuse(
                "ledger_inconsistent",
                f"run '{run.id}' authored blocking messages from a "
                "non-blocking signal",
            )
        if store.artifacts_for_run(run.id, kind=ArtifactKind.DIFF):
            _refuse(
                "ledger_inconsistent",
                f"signal run '{run.id}' also minted a DIFF artifact",
            )

    # P6.4: zombie delivery runs blocking an OPEN signal — only delivery
    # runs serving an unanswered blocking signal authored by a bound run
    # are build-relevant; any other in-flight delivery stays untouched.
    open_signal_message_ids = [
        message.id
        for run in signal_runs
        for message in blocking_messages_authored_by(store, task.id, run.id)
        if not canonical_replies_for(store, message)
    ]
    in_flight_delivery_runs = tuple(
        run
        for run in task_runs
        if run.status is RunStatus.RUNNING
        and run.id in {
            run_id
            for message_id in open_signal_message_ids
            for run_id in delivery_runs_for_message.get(message_id, [])
        }
    )

    if pin is None and impl_runs:
        _refuse(
            "no_baseline",
            f"task '{task.id}' has {len(impl_runs)} build attempt(s) but no "
            "baseline pin — refusing to guess the pre-implementation tree",
        )
    if task.state in (TaskState.CREATED, TaskState.CONTEXT_READY, TaskState.PLAN_READY) and impl_runs:
        _refuse(
            "ledger_inconsistent",
            f"task '{task.id}' is {task.state.value} but already has build attempts",
        )

    # --- task-bound verification tool runs (window keys) -----------------
    requested: dict[str, int] = {}  # tool_run_id -> TOOL_REQUESTED sequence
    for entry in store.all_models(
        EventLogEntry,
        "WHERE type = ?",
        [EventType.TOOL_REQUESTED.value],
        order_by="sequence ASC",
    ):
        if f"task:{task.id}" not in entry.references:
            continue
        for ref in entry.references:
            if ref.startswith("tool_run:"):
                tr_id = ref[9:]
                if tr_id in requested:
                    _refuse(
                        "ledger_inconsistent",
                        f"tool run '{tr_id}' was requested twice for task '{task.id}'",
                    )
                requested[tr_id] = entry.sequence or 0
    tool_runs: dict[str, ToolRun] = {}
    for tr_id in requested:
        row = store.load_model(ToolRun, tr_id)
        if row is None:
            _refuse("ledger_inconsistent", f"requested tool run '{tr_id}' is missing")
        tool_runs[tr_id] = row
    verifications = [
        (seq, tr) for tr_id, seq in requested.items() if (tr := tool_runs[tr_id]).tool == "verification"
    ]

    in_flight_runs = tuple(
        run for run in task_runs if run.id in bound and run.status is RunStatus.RUNNING
    )
    in_flight_tool_runs = tuple(
        tr for _seq, tr in verifications if tr.status is RunStatus.RUNNING
    )

    # --- fix packets pin the attempt they were minted against ------------
    packet_for: dict[str, Artifact] = {}
    for artifact in store.all_models(
        Artifact,
        "WHERE task_id = ? AND kind = ?",
        [task.id, ArtifactKind.FIX_PACKET.value],
    ):
        try:
            packet = decode_fix_packet(artifact.content or "")
        except ReviewContractError as exc:
            _refuse(
                "ledger_inconsistent",
                f"fix packet '{artifact.id}' is undecodable: {exc.code}",
            )
        subject_run = packet.sources.subject.implementation_run_id
        if subject_run not in bound or bound[subject_run][0] not in _IMPL_STAGES:
            _refuse(
                "ledger_inconsistent",
                f"fix packet '{artifact.id}' pins a run outside this build",
            )
        if subject_run in packet_for:
            _refuse(
                "ledger_inconsistent",
                f"two fix packets pin run '{subject_run}'",
            )
        packet_for[subject_run] = artifact

    produced = {
        rec.run_id: rec
        for rec in evidence.records_for_task(task.id, EvidenceKind.IMPLEMENTATION_PRODUCED)
        if rec.run_id is not None
    }
    passed = {
        rec.tool_run_id: rec
        for rec in evidence.records_for_task(task.id, EvidenceKind.TESTS_PASSED)
        if rec.tool_run_id is not None
    }

    def attempt_records(index: int) -> AttemptRecords:
        run = impl_runs[index]
        lower = impl_marker_seqs[index]
        upper = impl_marker_seqs[index + 1] if index + 1 < len(impl_runs) else None

        diffs = store.artifacts_for_run(run.id, kind=ArtifactKind.DIFF)
        if len(diffs) > 1:
            _refuse("ledger_inconsistent", f"run '{run.id}' minted multiple DIFF artifacts")
        diff = diffs[0] if diffs else None
        produced_rec = produced.get(run.id)
        if (diff is None) != (produced_rec is None):
            # The mint is atomic (D7): one without the other is corruption.
            _refuse(
                "ledger_inconsistent",
                f"run '{run.id}' has a DIFF artifact without matching "
                "IMPLEMENTATION_PRODUCED evidence (or vice versa)",
            )

        window = [
            (seq, tr) for seq, tr in verifications if lower < seq and (upper is None or seq < upper)
        ]
        latest_tr = max(window, key=lambda pair: pair[0])[1] if window else None
        test_result: Artifact | None = None
        if latest_tr is not None and latest_tr.result_ref is not None:
            result = store.load_model(Artifact, latest_tr.result_ref)
            if result is None or result.kind is not ArtifactKind.TEST_RESULT:
                _refuse(
                    "ledger_inconsistent",
                    f"verification tool run '{latest_tr.id}' references a "
                    "missing or non-TEST_RESULT artifact",
                )
            test_result = result
        tests_passed_rec = passed.get(latest_tr.id) if latest_tr is not None else None
        if (
            latest_tr is not None
            and latest_tr.status is RunStatus.SUCCEEDED
            and tests_passed_rec is None
        ):
            _refuse(
                "ledger_inconsistent",
                f"verification tool run '{latest_tr.id}' succeeded without TESTS_PASSED",
            )
        return AttemptRecords(
            run=run,
            diff_artifact=diff,
            implementation_produced=produced_rec,
            verification_tool_run=latest_tr,
            test_result_artifact=test_result,
            tests_passed=tests_passed_rec,
            fix_packet=packet_for.get(run.id),
        )

    latest = attempt_records(impl_group_ends[-1]) if impl_group_ends else None
    previous = (
        attempt_records(impl_group_ends[-2]) if len(impl_group_ends) >= 2 else None
    )

    # --- next action -----------------------------------------------------
    next_action: NextAction
    advance_target: TaskState | None = None
    pending: Artifact | None = None
    pending_signal: OpenSignal | None = None
    exchanges: tuple[tuple[Message, Message | None], ...] = ()
    state = task.state

    if state is TaskState.CREATED:
        if evidence.records_for_task(task.id, EvidenceKind.CONTEXT_COLLECTED):
            next_action, advance_target = "advance", TaskState.CONTEXT_READY
        else:
            next_action = "collect_context"
    elif state is TaskState.CONTEXT_READY:
        if plan_artifact is not None and any(
            rec.run_id == plan_artifact.run_id
            for rec in evidence.records_for_task(task.id, EvidenceKind.PLAN_PRODUCED)
        ):
            next_action, advance_target = "advance", TaskState.PLAN_READY
        elif plan_artifact is not None:
            _refuse(
                "ledger_inconsistent",
                f"plan artifact '{plan_artifact.id}' exists without PLAN_PRODUCED",
            )
        else:
            next_action = "plan"
    elif state is TaskState.PLAN_READY:
        next_action, advance_target = "advance", TaskState.IMPLEMENTING
    elif state is TaskState.IMPLEMENTING:
        if plan_artifact is None:
            _refuse(
                "ledger_inconsistent",
                f"task '{task.id}' is implementing without a plan artifact",
            )
        if not impl_runs:
            next_action = "dispatch"
        elif latest is not None and latest.diff_artifact is not None:
            pending = _pending_input(latest)
            if pending is not None:
                next_action = "dispatch"
            else:
                next_action, advance_target = "advance", TaskState.IMPLEMENTED
        else:
            # The latest dispatch minted nothing. P6.4: it may carry an
            # open blocking signal — resolve it, or (once answered)
            # re-dispatch the SAME attempt with the exchange. A run with
            # no valid signal is a consumed no-op: the next dispatch is a
            # fresh attempt fed the previous attempt's blocker.
            latest_run = impl_runs[-1]
            pending = _pending_input(previous)
            pending_signal = _open_signal_for_run(
                store,
                task,
                latest_run,
                stage=bound[latest_run.id][0],
                attempt=impl_attempts[-1],
            )
            if pending_signal is not None:
                group_start = (
                    impl_group_ends[-2] + 1 if len(impl_group_ends) >= 2 else 0
                )
                exchanges = _exchanges_for_runs(
                    store,
                    task.id,
                    [
                        rid
                        for _s, rid, _a, _c in impl_markers[
                            group_start : impl_group_ends[-1] + 1
                        ]
                    ],
                )
                if pending_signal.reply is None:
                    next_action = "resolve_signal"
                else:
                    next_action = "dispatch"
            else:
                next_action = "dispatch"
    elif state is TaskState.IMPLEMENTED:
        if latest is None or latest.diff_artifact is None or latest.implementation_produced is None:
            _refuse(
                "ledger_inconsistent",
                f"task '{task.id}' is IMPLEMENTED without persisted DIFF evidence",
            )
        next_action, advance_target = "advance", TaskState.VERIFYING
    elif state is TaskState.VERIFYING:
        if latest is None or latest.diff_artifact is None:
            _refuse(
                "ledger_inconsistent",
                f"task '{task.id}' is VERIFYING without a persisted DIFF",
            )
        tool_run = latest.verification_tool_run
        if tool_run is None or tool_run.status in (
            RunStatus.CANCELLED,
            RunStatus.RUNNING,
        ) or (tool_run.status is RunStatus.FAILED and tool_run.result_ref is None):
            # Never ran, could-not-execute, settled, or in-flight: (re)run.
            next_action = "verify"
        elif tool_run.status is RunStatus.SUCCEEDED:
            if latest.tests_passed is None or latest.test_result_artifact is None:
                _refuse(
                    "ledger_inconsistent",
                    f"task '{task.id}' has a succeeded verification without "
                    "its evidence pair",
                )
            next_action, advance_target = "advance", TaskState.REVIEWING
        else:  # FAILED with a persisted TEST_RESULT — the exam failed.
            next_action, advance_target = "advance", TaskState.IMPLEMENTING
    elif state is TaskState.REVIEWING:
        if (
            latest is None
            or latest.diff_artifact is None
            or latest.implementation_produced is None
            or latest.tests_passed is None
            or latest.test_result_artifact is None
            or latest.verification_tool_run is None
            or latest.verification_tool_run.status is not RunStatus.SUCCEEDED
            or plan_artifact is None
            or plan_run is None
        ):
            _refuse(
                "ledger_inconsistent",
                f"task '{task.id}' is REVIEWING without its full pinned input set",
            )
        # P6.4: the latest review run may carry an open blocking signal.
        streak = review_streaks[-1] if review_streaks else []
        latest_review_id = streak[-1] if streak else None
        if latest_review_id is not None:
            pending_signal = _open_signal_for_run(
                store,
                task,
                bound_runs[latest_review_id],
                stage="review",
                attempt=None,
            )
            if pending_signal is not None:
                exchanges = _exchanges_for_runs(store, task.id, streak)
        if pending_signal is not None and pending_signal.reply is None:
            next_action = "resolve_signal"
        else:
            next_action = "review"
    else:  # APPROVAL_REQUIRED / DONE
        next_action = "stop"

    needs_baseline_capture = (
        next_action == "dispatch" and not impl_runs and pin is None
    )

    diffs = [
        a
        for a in store.all_models(
            Artifact, "WHERE task_id = ? AND kind = ?", [task.id, ArtifactKind.DIFF.value]
        )
    ]
    reviews = [
        a
        for a in store.all_models(
            Artifact,
            "WHERE task_id = ? AND kind = ?",
            [task.id, ArtifactKind.REVIEW_FINDING.value],
        )
    ]
    packets = [
        a
        for a in store.all_models(
            Artifact,
            "WHERE task_id = ? AND kind = ?",
            [task.id, ArtifactKind.FIX_PACKET.value],
        )
    ]

    return BuildPosition(
        task=task,
        request=request,
        baseline_pin=pin,
        plan_artifact=plan_artifact,
        plan_run=plan_run,
        impl_runs=impl_runs,
        impl_attempts=impl_attempts,
        latest_records=latest,
        pending_input=pending,
        pending_signal=pending_signal,
        exchanges=exchanges,
        in_flight_runs=in_flight_runs,
        in_flight_delivery_runs=in_flight_delivery_runs,
        in_flight_tool_runs=in_flight_tool_runs,
        next_action=next_action,
        advance_target=advance_target,
        needs_baseline_capture=needs_baseline_capture,
        last_diff_artifact_id=diffs[-1].id if diffs else None,
        last_review_artifact_id=reviews[-1].id if reviews else None,
        last_fix_packet_artifact_id=packets[-1].id if packets else None,
    )


def settle_interrupted_runs(
    store: SqliteRelayStore, writer: EventLogWriter, position: BuildPosition
) -> None:
    """Settle build-owned in-flight rows as CANCELLED in one transaction.

    Marker-bound runs, signal delivery runs (P6.4), and task-bound
    verification tool runs are touched — unbound rows are never settled.
    """
    task = position.task
    with store.transaction():
        for run in (*position.in_flight_runs, *position.in_flight_delivery_runs):
            store.update_model(
                run.model_copy(
                    update={"status": RunStatus.CANCELLED, "ended_at": utcnow()}
                )
            )
            writer.record(
                EventLogEntry(
                    type=EventType.AGENT_RUN_FINISHED,
                    content=(
                        f"agent '{run.agent}' interrupted; settled as cancelled "
                        "by relay continue"
                    ),
                    references=[f"run:{run.id}", f"task:{task.id}"],
                )
            )
        for tool_run in position.in_flight_tool_runs:
            store.update_model(
                tool_run.model_copy(
                    update={
                        "status": RunStatus.CANCELLED,
                        "ended_at": utcnow(),
                        "error": "interrupted; settled as cancelled by relay continue",
                    }
                )
            )
            writer.record(
                EventLogEntry(
                    type=EventType.TOOL_COMPLETED,
                    content="verification interrupted; settled as cancelled by relay continue",
                    references=[f"task:{task.id}", f"tool_run:{tool_run.id}"],
                )
            )

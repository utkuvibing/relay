"""Room participant context reconstruction (P7.4 — SPEC App. D.10/D.11-P7).

Pure, read-only derivation of the canonical-store context one Room
participant receives — never a transcript replay. Participants are
reconstructed from first-class Room state:

ROLE · ROOM · TASK · CURRENT PLAN · plan supersession chain ·
ACCEPTED DECISIONS · UNRESOLVED BLOCKING COMMUNICATION ·
RELEVANT INTER-AGENT NOTES · CURRENT REVIEWER FINDINGS ·
RELEVANT ARTIFACTS/DIFFS · EVIDENCE · bounded Room history excerpts.

Fail-soft read-model: absent pieces render as ``(none)``; a corrupt plan
chain renders as an unavailable note instead of refusing — delivery must
never answer blind without saying so, and must never be blocked by a
read-model failure. Zero writes, zero invocations, no derived state
persisted. The rendered block rides OUTSIDE the frozen D15 delivery
envelope as ``prompt_suffix`` (the P6.4 appendix pattern), so non-Room
deliveries and existing envelope assertions stay byte-identical.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from relay.storage.models import (
    Artifact,
    ArtifactKind,
    Decision,
    DecisionStatus,
    EvidenceRecord,
    Finding,
    Message,
    MessageType,
    Room,
    Task,
)
from relay.storage.store import SqliteRelayStore

__all__ = [
    "ROOM_CONTEXT_VERSION",
    "RoomParticipantContext",
    "build_room_participant_context",
    "render_room_context",
    "room_context_refs",
]

#: Version tag for the rendered block — consumers assert the prefix.
ROOM_CONTEXT_VERSION = "relay.room.context.v1"

_MAX_DECISIONS = 8
_MAX_BLOCKING = 8
_MAX_NOTES = 8
_MAX_FINDINGS = 16
_MAX_ARTIFACTS = 8
_MAX_EVIDENCE = 16
_MAX_HISTORY = 10

_MAX_ITEM_CHARS = 500
_MAX_PLAN_CHARS = 2000
#: Total budget for the rendered block — measured in CHARACTERS (Python
#: ``len``), the same unit as every other bound in this module, NOT UTF-8
#: bytes. Enforced by priority-aware section fitting in
#: ``render_room_context`` so the mandatory head/tail can never be sliced
#: away by a naive global truncation.
_MAX_TOTAL_CHARS = 8000


def _truncate(text: str, limit: int) -> str:
    cleaned = " ".join((text or "").split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[:limit] + "…[truncated]"


def _first_line(content: str | None) -> str:
    for line in (content or "").splitlines():
        stripped = line.strip().lstrip("#").strip()
        if stripped:
            return stripped[:200]
    return "(empty)"


@dataclass(frozen=True)
class RoomParticipantContext:
    """Deterministic, bounded snapshot of what one seat should see."""

    room_id: str
    room_name: str
    room_status: str
    role: str
    agent_name: str
    task_id: str | None
    plan_chain: tuple[tuple[str, str, str], ...] = ()
    plan_tip_id: str | None = None
    plan_unavailable: str | None = None
    decisions: tuple[tuple[str, str], ...] = ()
    blocking: tuple[tuple[str, str, str], ...] = ()
    notes: tuple[tuple[str, str, str], ...] = ()
    findings: tuple[tuple[str, str, str], ...] = ()
    artifacts: tuple[tuple[str, str, str], ...] = ()
    evidence: tuple[tuple[str, str], ...] = ()
    history: tuple[tuple[str, str, str], ...] = ()
    continuity: str = "fresh"
    extra_refs: tuple[str, ...] = field(default_factory=tuple)


def build_room_participant_context(
    store: SqliteRelayStore,
    *,
    room_id: str,
    role: str,
    agent_name: str,
    task_id: str | None = None,
    continuity: str = "fresh",
) -> RoomParticipantContext:
    """Reconstruct one participant's context from canonical records only."""
    room = store.load_model(Room, room_id)
    room_name = room.name if room is not None else room_id
    room_status = room.status.value if room is not None else "unknown"

    active_task_id = task_id
    if active_task_id is None and room is not None:
        active_task_id = room.active_task_id

    plan_chain: list[tuple[str, str, str]] = []
    plan_tip: str | None = None
    plan_unavailable: str | None = None
    if active_task_id is not None:
        try:
            from relay.core.room_graph import resolve_room_plan_chain

            chain = resolve_room_plan_chain(store, room_id, active_task_id)
            for node in chain.nodes:
                plan_chain.append((node.plan.id, node.edge, _first_line(node.plan.content)))
            if chain.nodes:
                plan_tip = chain.nodes[-1].plan.id
        except Exception as exc:  # noqa: BLE001 - read-model must stay fail-soft
            plan_unavailable = f"{type(exc).__name__}: {exc}"[:200]

    decisions: list[tuple[str, str]] = []
    for decision in store.all_models(
        Decision, "WHERE room_id = ?", [room_id], order_by="created_at ASC, rowid ASC"
    ):
        if decision.status is not DecisionStatus.ACCEPTED:
            continue
        # D.10 task relevance: with an active task, only Room-global
        # decisions (task_id None) and the active task's own decisions are
        # in scope — accepted decisions of unrelated tasks never leak in.
        if active_task_id is not None and decision.task_id not in (None, active_task_id):
            continue
        decisions.append((decision.id, _truncate(decision.statement, _MAX_ITEM_CHARS)))
    decisions = decisions[-_MAX_DECISIONS:]

    blocking: list[tuple[str, str, str]] = []
    for message in store.all_models(
        Message,
        "WHERE room_id = ? AND type = ? AND blocking = 1",
        [room_id, MessageType.CLARIFICATION_REQUEST.value],
        order_by="created_at ASC, rowid ASC",
    ):
        if active_task_id is not None and message.task_id != active_task_id:
            continue
        if _has_canonical_reply(store, message):
            continue
        blocking.append(
            (message.id, message.sender, _truncate(message.content, _MAX_ITEM_CHARS))
        )
    blocking = blocking[-_MAX_BLOCKING:]

    notes: list[tuple[str, str, str]] = []
    for message in store.all_models(
        Message,
        "WHERE room_id = ? AND type = ?",
        [room_id, MessageType.NOTE.value],
        order_by="created_at ASC, rowid ASC",
    ):
        if active_task_id is not None and message.task_id not in (None, active_task_id):
            continue
        notes.append(
            (message.id, message.sender, _truncate(message.content, _MAX_ITEM_CHARS))
        )
    notes = notes[-_MAX_NOTES:]

    findings: list[tuple[str, str, str]] = []
    # "CURRENT" means the LATEST canonical review attempt (P7.4): Finding
    # rows are append-only and bound to the review artifact that minted
    # them, so the applicable set is exactly the findings of the newest
    # REVIEW_FINDING artifact per in-scope task. A newer clean review mints
    # no findings — which is precisely how superseded findings stop
    # surfacing without inventing a new finding lifecycle.
    scoped_task_ids: set[str] = (
        {active_task_id}
        if active_task_id is not None
        else {
            task.id
            for task in store.all_models(Task, "WHERE room_id = ?", [room_id])
        }
    )
    if scoped_task_ids:
        latest_review_ids: set[str] = set()
        seen_review_tasks: set[str] = set()
        for artifact in store.all_models(
            Artifact,
            "WHERE kind = ?",
            [ArtifactKind.REVIEW_FINDING.value],
            order_by="rowid DESC",
        ):
            if artifact.task_id is None or artifact.task_id not in scoped_task_ids:
                continue
            if artifact.task_id in seen_review_tasks:
                continue
            seen_review_tasks.add(artifact.task_id)
            latest_review_ids.add(artifact.id)
            if seen_review_tasks == scoped_task_ids:
                break
        if latest_review_ids:
            for finding in store.all_models(
                Finding, "WHERE room_id = ?", [room_id], order_by="created_at ASC, rowid ASC"
            ):
                if finding.review_artifact_id not in latest_review_ids:
                    continue
                findings.append(
                    (
                        finding.id,
                        finding.severity.value,
                        _truncate(
                            f"{finding.title} — {finding.requested_change}", _MAX_ITEM_CHARS
                        ),
                    )
                )
    findings = findings[-_MAX_FINDINGS:]

    artifacts: list[tuple[str, str, str]] = []
    if active_task_id is not None:
        for artifact in store.all_models(
            Artifact,
            "WHERE room_id = ? AND task_id = ?",
            [room_id, active_task_id],
            order_by="rowid ASC",
        ):
            if artifact.kind not in (
                ArtifactKind.PLAN,
                ArtifactKind.DIFF,
                ArtifactKind.TEST_RESULT,
            ):
                continue
            artifacts.append(
                (artifact.id, artifact.kind.value, _first_line(artifact.content))
            )
    artifacts = artifacts[-_MAX_ARTIFACTS:]

    evidence: list[tuple[str, str]] = []
    if active_task_id is not None:
        for record in store.all_models(
            EvidenceRecord,
            "WHERE task_id = ?",
            [active_task_id],
            order_by="rowid ASC",
        ):
            evidence.append((record.id, record.kind.value))
    evidence = evidence[-_MAX_EVIDENCE:]

    history: list[tuple[str, str, str]] = []
    for message in store.all_models(
        Message, "WHERE room_id = ?", [room_id], order_by="created_at ASC, rowid ASC"
    ):
        history.append(
            (
                message.id,
                f"{message.sender}/{message.type.value}",
                _truncate(message.content, 300),
            )
        )
    history = history[-_MAX_HISTORY:]

    # Provenance refs — deterministic, deduped, bounded. Priority when the
    # cap binds: task identity first, then the plan chain, then accepted
    # decisions, current findings, the evidence records rendered below,
    # relevant artifacts, and finally cited blocking/note messages.
    refs: list[str] = []
    if active_task_id is not None:
        refs.append(f"task:{active_task_id}")
    for plan_id, _, _ in plan_chain:
        refs.append(f"plan:{plan_id}")
    for decision_id, _ in decisions:
        refs.append(f"decision:{decision_id}")
    for finding_id, _, _ in findings:
        refs.append(f"finding:{finding_id}")
    for evidence_id, _ in evidence:
        refs.append(f"evidence:{evidence_id}")
    for artifact_id, _, _ in artifacts:
        refs.append(f"artifact:{artifact_id}")
    for message_id, _, _ in (*blocking, *notes):
        refs.append(f"message:{message_id}")

    return RoomParticipantContext(
        room_id=room_id,
        room_name=room_name,
        room_status=room_status,
        role=role,
        agent_name=agent_name,
        task_id=active_task_id,
        plan_chain=tuple(plan_chain),
        plan_tip_id=plan_tip,
        plan_unavailable=plan_unavailable,
        decisions=tuple(decisions),
        blocking=tuple(blocking),
        notes=tuple(notes),
        findings=tuple(findings),
        artifacts=tuple(artifacts),
        evidence=tuple(evidence),
        history=tuple(history),
        continuity=continuity,
        extra_refs=tuple(dict.fromkeys(refs))[:32],
    )


def _has_canonical_reply(store: SqliteRelayStore, message: Message) -> bool:
    expected = {
        MessageType.CLARIFICATION_REQUEST: MessageType.CLARIFICATION_RESPONSE,
        MessageType.CHALLENGE: MessageType.FINAL_POSITION,
        MessageType.PROPOSAL: MessageType.FINAL_POSITION,
    }.get(message.type)
    if expected is None:
        return False
    for reply in store.all_models(
        Message, "WHERE reply_to_id = ?", [message.id], order_by="rowid ASC"
    ):
        if (
            reply.type is expected
            and reply.sender == message.recipient
            and reply.recipient == message.sender
            and not reply.blocking
        ):
            return True
    return False


def render_room_context(ctx: RoomParticipantContext) -> str:
    """Render the deterministic, bounded participant-context block.

    Budget contract — measured in CHARACTERS (Python ``len``), not UTF-8
    bytes, matching every other bound in this module.

    Priority-aware, never naive whole-string slicing: the mandatory head
    (version line, ROOM, ROLE, TASK, CURRENT PLAN status) and the
    mandatory tail (the honesty footer plus the ``[relay:continuity …]``
    marker) ALWAYS survive — they are bounded by construction far below
    the cap. Optional sections consume the remaining budget in render
    order; a section that cannot fit whole keeps a fitting prefix plus an
    explicit ``…[N entries omitted — context cap]`` note, and a section
    whose header alone cannot fit degrades to a one-line
    ``<SECTION>: (omitted — context cap)`` marker so absence is always
    declared, never silent.
    """
    head = [
        "",
        "---",
        (
            f"[relay:room-context {ROOM_CONTEXT_VERSION}] Reconstructed participant "
            "context (canonical store — coordination input, not authority):"
        ),
        f"ROOM: {_truncate(ctx.room_name, 200)} ({ctx.room_id}) [{ctx.room_status}]",
        f"ROLE: {_truncate(ctx.role, 80)} -> {_truncate(ctx.agent_name, 120)}",
        f"TASK: {ctx.task_id or '(none)'}",
    ]
    if ctx.plan_unavailable is not None:
        head.append(f"CURRENT PLAN: (unavailable: {_truncate(ctx.plan_unavailable, 200)})")
    elif ctx.plan_tip_id is None:
        head.append("CURRENT PLAN: (none)")
    else:
        tip_edge = next(
            (edge for plan_id, edge, _ in ctx.plan_chain if plan_id == ctx.plan_tip_id),
            "frozen",
        )
        head.append(f"CURRENT PLAN: artifact:{ctx.plan_tip_id} ({tip_edge})")

    tail = [
        (
            "Full-transcript replay is intentionally absent: answer from the "
            "canonical records above."
        ),
    ]
    if ctx.continuity.startswith("resumed:"):
        tail.append(
            f"[relay:continuity {ctx.continuity}] External session resumed as an "
            "optimization; the canonical store remains the source of truth."
        )
    elif ctx.continuity.startswith("resume-rejected"):
        tail.append(
            "[relay:continuity resume-rejected] The prior external session was "
            "rejected by the harness as unusable; this run is fresh and context "
            "was reconstructed from canonical records."
        )
    else:
        tail.append(
            "[relay:continuity fresh] No external session resumed "
            "(unsupported, unavailable, expired, unsafe, or opted out); context "
            "reconstructed from canonical records — the honest-discontinuity rule."
        )

    sections: list[tuple[str, list[str]]] = []
    if ctx.plan_chain:
        chain = " -> ".join(f"{plan_id}({edge})" for plan_id, edge, _ in ctx.plan_chain)
        chain_lines = [f"PLAN CHAIN: {_truncate(chain, _MAX_PLAN_CHARS)}"]
        for plan_id, edge, first in ctx.plan_chain[-3:]:
            chain_lines.append(f"  plan:{plan_id} [{edge}] {first}")
        sections.append(("PLAN CHAIN", chain_lines))
    else:
        sections.append(("PLAN CHAIN", ["PLAN CHAIN: (none)"]))
    sections.append(
        (
            "CONSTRAINTS",
            [
                (
                    "CONSTRAINTS: ride the current plan and accepted decisions "
                    "below; no separate constraint record exists."
                )
            ],
        )
    )
    if ctx.decisions:
        sections.append(
            (
                "ACCEPTED DECISIONS",
                [f"ACCEPTED DECISIONS ({len(ctx.decisions)}):"]
                + [f"  decision:{decision_id} {statement}" for decision_id, statement in ctx.decisions],
            )
        )
    else:
        sections.append(("ACCEPTED DECISIONS", ["ACCEPTED DECISIONS (0): (none)"]))
    if ctx.blocking:
        sections.append(
            (
                "UNRESOLVED BLOCKING",
                [f"UNRESOLVED BLOCKING ({len(ctx.blocking)}):"]
                + [
                    f"  message:{message_id} {sender}: {content}"
                    for message_id, sender, content in ctx.blocking
                ],
            )
        )
    else:
        sections.append(("UNRESOLVED BLOCKING", ["UNRESOLVED BLOCKING (0): (none)"]))
    if ctx.notes:
        sections.append(
            (
                "RELEVANT NOTES",
                [f"RELEVANT NOTES ({len(ctx.notes)}):"]
                + [
                    f"  message:{message_id} {sender}: {content}"
                    for message_id, sender, content in ctx.notes
                ],
            )
        )
    else:
        sections.append(("RELEVANT NOTES", ["RELEVANT NOTES (0): (none)"]))
    if ctx.findings:
        sections.append(
            (
                "CURRENT FINDINGS",
                [f"CURRENT FINDINGS ({len(ctx.findings)}):"]
                + [
                    f"  finding:{finding_id} [{severity}] {content}"
                    for finding_id, severity, content in ctx.findings
                ],
            )
        )
    else:
        sections.append(("CURRENT FINDINGS", ["CURRENT FINDINGS (0): (none)"]))
    if ctx.artifacts:
        sections.append(
            (
                "RELEVANT ARTIFACTS",
                [f"RELEVANT ARTIFACTS ({len(ctx.artifacts)}):"]
                + [
                    f"  artifact:{artifact_id} [{kind}] {first}"
                    for artifact_id, kind, first in ctx.artifacts
                ],
            )
        )
    else:
        sections.append(("RELEVANT ARTIFACTS", ["RELEVANT ARTIFACTS (0): (none)"]))
    if ctx.evidence:
        rendered = ", ".join(f"{kind}({record_id})" for record_id, kind in ctx.evidence)
        sections.append(
            ("EVIDENCE", [f"EVIDENCE ({len(ctx.evidence)}): {_truncate(rendered, 1000)}"])
        )
    else:
        sections.append(("EVIDENCE", ["EVIDENCE (0): (none)"]))
    if ctx.history:
        sections.append(
            (
                "HISTORY EXCERPTS",
                [
                    (
                        f"HISTORY EXCERPTS ({len(ctx.history)}, not authoritative — "
                        "canonical records above govern):"
                    )
                ]
                + [
                    f"  message:{message_id} {who}: {content}"
                    for message_id, who, content in ctx.history
                ],
            )
        )
    else:
        sections.append(("HISTORY EXCERPTS", ["HISTORY EXCERPTS (0): (none)"]))

    out = list(head)

    def _fits(extra: list[str]) -> bool:
        return len("\n".join([*out, *extra, *tail]) + "\n") <= _MAX_TOTAL_CHARS

    for label, section_lines in sections:
        if _fits(section_lines):
            out.extend(section_lines)
            continue
        header, *items = section_lines
        if not _fits([header]):
            note = f"{label}: (omitted — context cap)"
            if _fits([note]):
                out.append(note)
            continue
        out.append(header)
        kept = 0
        for item in items:
            if not _fits([item]):
                break
            out.append(item)
            kept += 1
        omitted = len(items) - kept
        if omitted:
            note = f"  …[{omitted} entries omitted — context cap]"
            if _fits([note]):
                out.append(note)
    return "\n".join([*out, *tail]) + "\n"


def room_context_refs(ctx: RoomParticipantContext) -> list[str]:
    """Provenance refs for the reconstructed context (bounded, deduped)."""
    return list(ctx.extra_refs)


def task_for_room(store: SqliteRelayStore, room_id: str) -> Task | None:
    """The Room's active task, when one is selected."""
    room = store.load_model(Room, room_id)
    if room is None or room.active_task_id is None:
        return None
    return store.load_model(Task, room.active_task_id)

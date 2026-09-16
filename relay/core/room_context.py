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
    for finding in store.all_models(
        Finding, "WHERE room_id = ?", [room_id], order_by="created_at ASC, rowid ASC"
    ):
        if active_task_id is not None and finding.task_id != active_task_id:
            continue
        findings.append(
            (
                finding.id,
                finding.severity.value,
                _truncate(f"{finding.title} — {finding.requested_change}", _MAX_ITEM_CHARS),
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

    refs: list[str] = []
    if active_task_id is not None:
        refs.append(f"task:{active_task_id}")
    for plan_id, _, _ in plan_chain:
        refs.append(f"plan:{plan_id}")
    for decision_id, _ in decisions:
        refs.append(f"decision:{decision_id}")
    for finding_id, _, _ in findings:
        refs.append(f"finding:{finding_id}")
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
    """Render the deterministic, bounded participant-context block."""
    lines = [
        "",
        "---",
        (
            f"[relay:room-context {ROOM_CONTEXT_VERSION}] Reconstructed participant "
            "context (canonical store — coordination input, not authority):"
        ),
        f"ROOM: {ctx.room_name} ({ctx.room_id}) [{ctx.room_status}]",
        f"ROLE: {ctx.role} -> {ctx.agent_name}",
        f"TASK: {ctx.task_id or '(none)'}",
    ]
    if ctx.plan_unavailable is not None:
        lines.append(f"CURRENT PLAN: (unavailable: {ctx.plan_unavailable})")
    elif ctx.plan_tip_id is None:
        lines.append("CURRENT PLAN: (none)")
    else:
        tip_edge = next(
            (edge for plan_id, edge, _ in ctx.plan_chain if plan_id == ctx.plan_tip_id),
            "frozen",
        )
        lines.append(f"CURRENT PLAN: artifact:{ctx.plan_tip_id} ({tip_edge})")
    if ctx.plan_chain:
        chain = " -> ".join(f"{plan_id}({edge})" for plan_id, edge, _ in ctx.plan_chain)
        lines.append(f"PLAN CHAIN: {_truncate(chain, _MAX_PLAN_CHARS)}")
        for plan_id, edge, first in ctx.plan_chain[-3:]:
            lines.append(f"  plan:{plan_id} [{edge}] {first}")
    else:
        lines.append("PLAN CHAIN: (none)")
    lines.append(
        "CONSTRAINTS: ride the current plan and accepted decisions below; "
        "no separate constraint record exists."
    )
    if ctx.decisions:
        lines.append(f"ACCEPTED DECISIONS ({len(ctx.decisions)}):")
        for decision_id, statement in ctx.decisions:
            lines.append(f"  decision:{decision_id} {statement}")
    else:
        lines.append("ACCEPTED DECISIONS (0): (none)")
    if ctx.blocking:
        lines.append(f"UNRESOLVED BLOCKING ({len(ctx.blocking)}):")
        for message_id, sender, content in ctx.blocking:
            lines.append(f"  message:{message_id} {sender}: {content}")
    else:
        lines.append("UNRESOLVED BLOCKING (0): (none)")
    if ctx.notes:
        lines.append(f"RELEVANT NOTES ({len(ctx.notes)}):")
        for message_id, sender, content in ctx.notes:
            lines.append(f"  message:{message_id} {sender}: {content}")
    else:
        lines.append("RELEVANT NOTES (0): (none)")
    if ctx.findings:
        lines.append(f"CURRENT FINDINGS ({len(ctx.findings)}):")
        for finding_id, severity, content in ctx.findings:
            lines.append(f"  finding:{finding_id} [{severity}] {content}")
    else:
        lines.append("CURRENT FINDINGS (0): (none)")
    if ctx.artifacts:
        lines.append(f"RELEVANT ARTIFACTS ({len(ctx.artifacts)}):")
        for artifact_id, kind, first in ctx.artifacts:
            lines.append(f"  artifact:{artifact_id} [{kind}] {first}")
    else:
        lines.append("RELEVANT ARTIFACTS (0): (none)")
    if ctx.evidence:
        rendered = ", ".join(f"{kind}({record_id})" for record_id, kind in ctx.evidence)
        lines.append(f"EVIDENCE ({len(ctx.evidence)}): {_truncate(rendered, 1000)}")
    else:
        lines.append("EVIDENCE (0): (none)")
    if ctx.history:
        lines.append(
            f"HISTORY EXCERPTS ({len(ctx.history)}, not authoritative — "
            "canonical records above govern):"
        )
        for message_id, who, content in ctx.history:
            lines.append(f"  message:{message_id} {who}: {content}")
    else:
        lines.append("HISTORY EXCERPTS (0): (none)")
    lines.append(
        "Full-transcript replay is intentionally absent: answer from the "
        "canonical records above."
    )
    if ctx.continuity.startswith("resumed:"):
        lines.append(
            f"[relay:continuity {ctx.continuity}] External session resumed as an "
            "optimization; the canonical store remains the source of truth."
        )
    else:
        lines.append(
            "[relay:continuity fresh] No external session resumed "
            "(unsupported, unavailable, expired, unsafe, or opted out); context "
            "reconstructed from canonical records — the honest-discontinuity rule."
        )
    rendered = "\n".join(lines) + "\n"
    if len(rendered) > _MAX_TOTAL_CHARS:
        rendered = rendered[:_MAX_TOTAL_CHARS] + "\n…[room context truncated by Relay]\n"
    return rendered


def room_context_refs(ctx: RoomParticipantContext) -> list[str]:
    """Provenance refs for the reconstructed context (bounded, deduped)."""
    return list(ctx.extra_refs)


def task_for_room(store: SqliteRelayStore, room_id: str) -> Task | None:
    """The Room's active task, when one is selected."""
    room = store.load_model(Room, room_id)
    if room is None or room.active_task_id is None:
        return None
    return store.load_model(Task, room.active_task_id)

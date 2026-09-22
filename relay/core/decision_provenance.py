"""Generic, read-only decision provenance from canonical ledger records."""

from __future__ import annotations

from typing import Any

from relay.core.decision_references import DecisionReferenceError, validate_decision_references
from relay.storage.models import (
    Artifact,
    Decision,
    DecisionStatus,
    EventLogEntry,
    EventType,
    EvidenceRecord,
    Finding,
    Message,
    MessageType,
    Run,
    RunStatus,
)
from relay.storage.store import SqliteRelayStore


class DecisionProvenanceError(ValueError):
    """Canonical decision records contradict their supposed provenance."""


def _citation_node(store: SqliteRelayStore, reference: str) -> dict[str, str]:
    kind, record_id = reference.split(":", 1)
    if kind == "message":
        item = store.load_model(Message, record_id)
        assert item is not None
        return {"reference": reference, "kind": item.type.value, "by": item.sender, "summary": item.content[:500]}
    if kind == "finding":
        item = store.load_model(Finding, record_id)
        assert item is not None
        return {"reference": reference, "kind": "finding", "by": item.review_run_id, "summary": f"{item.title}: {item.description}"[:500]}
    if kind in ("plan", "artifact"):
        item = store.load_model(Artifact, record_id)
        assert item is not None
        return {"reference": reference, "kind": item.kind.value, "by": item.run_id or "relay", "summary": (item.content or item.content_ref or "")[:500]}
    if kind == "evidence":
        item = store.load_model(EvidenceRecord, record_id)
        assert item is not None
        return {"reference": reference, "kind": item.kind.value, "by": item.produced_by, "summary": f"task:{item.task_id}"}
    item = store.load_model(Decision, record_id)
    assert item is not None
    return {"reference": reference, "kind": f"decision:{item.status.value}", "by": item.accepted_by or item.proposed_by or "unknown", "summary": item.statement[:500]}


def _single(entries: list[EventLogEntry], label: str) -> EventLogEntry | None:
    if len(entries) > 1:
        raise DecisionProvenanceError(f"multiple {label} events")
    return entries[0] if entries else None


_GRAPH_CLASSES = ("proposal", "objection", "evidence", "rebuttal", "decision")


def _decision_graph(
    store: SqliteRelayStore,
    decision: Decision,
    parent: Message | None,
    citations: list[str],
    gaps: list[str],
) -> dict[str, Any]:
    """Classify only typed, verified records; never classify prose content."""
    nodes: dict[str, list[dict[str, str | None]]] = {kind: [] for kind in _GRAPH_CLASSES}

    def add(kind: str, reference: str, summary: str, source: str) -> None:
        if any(node["provenance_ref"] == reference for node in nodes[kind]):
            return
        nodes[kind].append(
            {"provenance_ref": reference, "summary": summary[:500], "source": source}
        )

    if parent is not None:
        kind = "proposal" if parent.type is MessageType.PROPOSAL else "objection"
        add(kind, f"message:{parent.id}", parent.content, "promotion_exchange")
    for reference in citations:
        kind, record_id = reference.split(":", 1)
        if kind == "message":
            message = store.load_model(Message, record_id)
            assert message is not None  # shared validator resolved it
            graph_kind = {
                MessageType.PROPOSAL: "proposal",
                MessageType.CHALLENGE: "objection",
                MessageType.REBUTTAL: "rebuttal",
            }.get(message.type)
            if graph_kind is not None:
                add(graph_kind, reference, message.content, "explicit_citation")
        elif kind in ("finding", "evidence"):
            cited = _citation_node(store, reference)
            add("evidence", reference, cited["summary"], "explicit_citation")
        elif kind == "decision":
            cited = store.load_model(Decision, record_id)
            assert cited is not None
            add("decision", reference, cited.statement, "explicit_citation")
    decision_ref = f"decision:{decision.id}"
    add("decision", decision_ref, decision.statement, "canonical_decision")
    for kind in _GRAPH_CLASSES:
        nodes[kind].sort(key=lambda node: str(node["provenance_ref"]))
    edges = [
        {"from": str(node["provenance_ref"]), "to": decision_ref, "relation": node["source"]}
        for kind in _GRAPH_CLASSES
        for node in nodes[kind]
        if node["provenance_ref"] != decision_ref
    ]
    edges.sort(key=lambda edge: (edge["from"], edge["relation"], edge["to"]))
    graph_gaps = [*gaps, *(f"unrecorded_{kind}" for kind in _GRAPH_CLASSES if not nodes[kind])]
    return {"nodes": nodes, "edges": edges, "gaps": graph_gaps}


def build_decision_provenance(store: SqliteRelayStore, decision: Decision) -> dict[str, Any]:
    """Project one decision without writes, agent calls, or transcript inference."""
    try:
        validate_decision_references(store, decision)
    except DecisionReferenceError as exc:
        raise DecisionProvenanceError(str(exc)) from exc
    token = f"decision:{decision.id}"
    all_events = list(store.all_models(EventLogEntry, order_by="sequence ASC"))
    events = [event for event in all_events if token in event.references]
    proposed = _single([e for e in events if e.type is EventType.DECISION_PROPOSED], "proposal")
    final = _single([e for e in events if e.type in (EventType.DECISION_ACCEPTED, EventType.DECISION_REJECTED)], "outcome")
    gaps: list[str] = []
    if proposed is None:
        gaps.append("proposal_event")
    if final is None:
        gaps.append("outcome_event")
    elif (final.type is EventType.DECISION_REJECTED and decision.status is not DecisionStatus.REJECTED) or (
        final.type is EventType.DECISION_ACCEPTED and decision.status not in (DecisionStatus.ACCEPTED, DecisionStatus.SUPERSEDED)
    ):
        raise DecisionProvenanceError("decision status contradicts outcome event")
    for event in (proposed, final):
        if event is not None and (event.room_id != decision.room_id or event.task_id != decision.task_id):
            raise DecisionProvenanceError("decision event has foreign scope")

    reply_id = decision.source_reply_id
    if reply_id is None and proposed is not None:
        message_ids = [ref.removeprefix("message:") for ref in proposed.references if ref.startswith("message:")]
        candidates = [store.load_model(Message, item) for item in message_ids]
        replies = [item for item in candidates if item is not None and item.reply_to_id in message_ids]
        if len(replies) == 1:
            reply_id = replies[0].id
        elif replies:
            raise DecisionProvenanceError("legacy promotion has ambiguous replies")
    reply = store.load_model(Message, reply_id) if reply_id else None
    if reply_id is not None and reply is None:
        raise DecisionProvenanceError("promotion reply is missing")
    if reply is None:
        gaps.append("source_reply")
    parent = store.load_model(Message, reply.reply_to_id) if reply and reply.reply_to_id else None
    if reply is not None:
        if parent is None or parent.id == reply.id:
            raise DecisionProvenanceError("promotion reply has no valid parent")
        if reply.room_id != decision.room_id or reply.task_id != decision.task_id:
            raise DecisionProvenanceError("promotion reply has foreign scope")
        if parent.room_id != decision.room_id or parent.task_id != decision.task_id:
            raise DecisionProvenanceError("proposal has foreign scope")
        if parent.type not in (MessageType.PROPOSAL, MessageType.CHALLENGE) or reply.type is not MessageType.FINAL_POSITION:
            raise DecisionProvenanceError("promotion exchange has invalid message types")
        for event in (proposed, final):
            if event is not None and (
                f"message:{parent.id}" not in event.references
                or f"message:{reply.id}" not in event.references
            ):
                raise DecisionProvenanceError("promotion event contradicts exchange")
    runs: dict[str, dict[str, str | None] | None] = {}
    for name, message in (("proposal", parent), ("reply", reply)):
        if message is None or message.run_id is None:
            runs[name] = None
            if message is not None and not message.sender.startswith(("human:", "relay:")):
                gaps.append(f"{name}_run")
            continue
        run = store.load_model(Run, message.run_id)
        if run is None or run.agent != message.sender or run.status is not RunStatus.SUCCEEDED:
            raise DecisionProvenanceError(f"{name} authoring run is missing or foreign")
        runs[name] = {"id": run.id, "agent": run.agent, "backend": run.backend, "adapter_version": run.adapter_version}
    delivery_marker = None
    if parent is not None and reply is not None and reply.run_id is not None:
        bindings = [
            event for event in all_events
            if event.type in (EventType.MESSAGE_DELIVERED, EventType.MESSAGE_DELIVERY_FALLBACK)
            and event.sender == "relay:delivery"
            and event.room_id == parent.room_id
            and f"message:{parent.id}" in event.references
            and f"run:{reply.run_id}" in event.references
        ]
        if len(bindings) > 1:
            raise DecisionProvenanceError("multiple delivery bindings for promotion reply")
        delivery_marker = bindings[0] if bindings else None
        if delivery_marker is None:
            gaps.append("delivery_binding")
    successors = list(store.all_models(Decision, "WHERE supersedes_decision_id = ?", [decision.id]))
    if len(successors) > 1:
        raise DecisionProvenanceError("decision has multiple supersession successors")
    successor = successors[0] if successors else None
    if successor is not None and (successor.room_id != decision.room_id or successor.task_id != decision.task_id):
        raise DecisionProvenanceError("supersession successor has foreign scope")
    if decision.status is DecisionStatus.SUPERSEDED and successor is None:
        raise DecisionProvenanceError("superseded decision has no successor")
    if successor is not None and decision.status is not DecisionStatus.SUPERSEDED:
        raise DecisionProvenanceError("supersession successor contradicts status")
    if successor is not None and decision.room_id is not None:
        markers = [
            event for event in all_events
            if event.type is EventType.DECISION_SUPERSEDED
            and f"decision:{successor.id}" in event.references
            and f"supersedes_decision:{decision.id}" in event.references
        ]
        if len(markers) != 1 or markers[0].room_id != decision.room_id:
            raise DecisionProvenanceError("Room supersession marker is missing or foreign")
    if decision.supersedes_decision_id is not None:
        predecessor = store.load_model(Decision, decision.supersedes_decision_id)
        if predecessor is None or predecessor.room_id != decision.room_id or predecessor.task_id != decision.task_id:
            raise DecisionProvenanceError("supersession predecessor is missing or foreign")
    citations = list(decision.references)
    graph = _decision_graph(store, decision, parent, citations, gaps)
    return {
        "version": "relay.decision.provenance.v1",
        "decision": {"id": decision.id, "statement": decision.statement, "rationale": decision.rationale,
                     "status": decision.status.value, "room_id": decision.room_id, "task_id": decision.task_id,
                     "proposed_by": decision.proposed_by, "accepted_by": decision.accepted_by},
        "exchange": {"proposal_message_id": parent.id if parent else None, "reply_message_id": reply.id if reply else None,
                     "proposal_type": parent.type.value if parent else None,
                     "proposal_text": parent.content[:1000] if parent else None,
                     "reply_text": reply.content[:1000] if reply else None, "runs": runs},
        "events": {"proposed": proposed.sequence if proposed else None, "outcome": final.sequence if final else None,
                   "delivery_binding": delivery_marker.sequence if delivery_marker else None,
                   "delivery_binding_type": delivery_marker.type.value if delivery_marker else None},
        "references": citations,
        "graph": graph,
        "canonical_fields": {
            "source": "decision_row",
            "supported_by": list(decision.supported_by),
            "challenged_by": list(decision.challenged_by),
            "verified_by": decision.verified_by,
            "alternatives_considered": list(decision.alternatives_considered),
            "primary_objection": decision.primary_objection,
        },
        "citation_nodes": [_citation_node(store, ref) for ref in citations],
        "evidence": [ref for ref in citations if ref.startswith("evidence:")],
        "support": [ref for ref in citations if ref.startswith("message:") and (message := store.load_model(Message, ref.split(":", 1)[1])) is not None and message.type in (MessageType.OPINION, MessageType.REBUTTAL)],
        "objections": [ref for ref in citations if ref.startswith("message:") and (message := store.load_model(Message, ref.split(":", 1)[1])) is not None and message.type is MessageType.CHALLENGE],
        "alternatives": [ref for ref in citations if ref.startswith("decision:") and (alternative := store.load_model(Decision, ref.split(":", 1)[1])) is not None and alternative.status is DecisionStatus.REJECTED],
        "supersession": {"predecessor_id": decision.supersedes_decision_id, "successor_id": successor.id if successor else None},
        "gaps": gaps,
    }

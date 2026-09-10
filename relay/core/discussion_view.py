"""Provider-free reconstruction of discussion progress from canonical records."""

from typing import Any

from relay.agents.base import AgentRole
from relay.core.protocol_encoding import decode_definition, request_id
from relay.core.protocol_outcomes import ProtocolOutcome, ledger_revision, outcome_events
from relay.core.protocols import (
    EvaluationStatus,
    StageContext,
    StageEvaluation,
    evaluate_protocol,
    evaluate_stage,
    protocol_schedule,
)
from relay.core.stage_facts import collect_stage_facts, snapshot
from relay.storage.models import Message, ProtocolExecution
from relay.storage.store import SqliteRelayStore


class DiscussionLookupError(ValueError):
    """Safe user-facing lookup diagnostic."""


def discussion_envelope(execution_id: str | None = None) -> dict[str, Any]:
    return {
        "version": "relay.discussion.v1",
        "execution_id": execution_id,
        "room_id": None,
        "task_id": None,
        "topic": None,
        "protocol": None,
        "progress": None,
        "outputs": [],
        "last_observation": None,
        "observations": [],
        "escalation": None,
        "next_action": None,
        "error": None,
    }


def resolve_execution(store: SqliteRelayStore, value: str) -> ProtocolExecution:
    exact = store.load_model(ProtocolExecution, value)
    if exact is not None:
        return exact
    # Literal prefix matching, including user-supplied SQL wildcard characters.
    matches = (
        list(
            store.all_models(
                ProtocolExecution,
                "WHERE substr(id, 1, ?) = ?",
                [len(value), value],
                limit=2,
            )
        )
        if value
        else []
    )
    if len(matches) != 1:
        raise DiscussionLookupError(
            "Ambiguous discussion ID prefix; use a longer prefix or exact ID."
            if matches
            else "Discussion does not exist."
        )
    return matches[0]


def build_discussion_view(store: SqliteRelayStore, execution: ProtocolExecution) -> dict[str, Any]:
    with snapshot(store):
        return _build_view(store, execution)


def _build_view(store: SqliteRelayStore, execution: ProtocolExecution) -> dict[str, Any]:
    view = discussion_envelope(execution.id)
    view.update(room_id=execution.room_id, task_id=execution.task_id, topic=execution.topic)
    try:
        observations: list[dict[str, Any]] = []
        for event in outcome_events(store, execution.id):
            observation = ProtocolOutcome.model_validate_json(event.content)
            if (
                observation.execution_id != execution.id
                or event.sender != "relay:protocol"
                or (event.room_id, event.task_id) != (execution.room_id, execution.task_id)
            ):
                raise ValueError("foreign observation")
            observations.append(
                {
                    **observation.model_dump(mode="json"),
                    "sequence": event.sequence,
                    "created_at": event.created_at.isoformat(),
                    "needs_human": observation.needs_human,
                    "next_action": observation.next_action,
                }
            )
        view["observations"] = observations
        if observations:
            latest = dict(observations[-1])
            latest["stale"] = latest["ledger_revision"] != ledger_revision(store, execution)
            view["last_observation"] = latest
            view["escalation"] = latest if latest["needs_human"] else None
            view["next_action"] = latest["next_action"]
        if not observations or view["last_observation"]["stale"]:
            view["next_action"] = (
                "No current outcome is recorded. Inspect ledger progress, then run "
                f"relay discuss --resume {execution.id} for recovery."
            )
        if execution.runner_version != "relay.protocol.runner.v1":
            raise ValueError("unsupported runner version")
        definition = decode_definition(execution.definition_snapshot, execution.definition_digest)
        view["protocol"] = {"name": definition.name, "version": definition.version}
        results: list[StageEvaluation] = []
        stages: list[dict[str, Any]] = []
        schedule = protocol_schedule(definition)
        for stage, occurrence in schedule:
            context = StageContext(
                definition.name,
                definition.version,
                execution.execution_key,
                stage.id,
                occurrence,
                execution.room_id,
                execution.task_id,
            )
            requests: dict[AgentRole, str] = {}
            for role in stage.participants:
                mid = request_id(execution.id, context.stage_key, role)
                if store.load_model(Message, mid) is not None:
                    requests[role] = mid
            facts = collect_stage_facts(store, definition, context, requests)
            evaluation = evaluate_stage(definition, facts)
            results.append(evaluation)
            stages.append(
                {
                    "stage": stage.id,
                    "occurrence": occurrence,
                    "stage_key": context.stage_key,
                    "status": evaluation.status.value,
                    "reason": evaluation.reason.value,
                    "requests": [
                        {
                            "role": r.role.value,
                            "request_id": r.request_id,
                            "state": r.state.value,
                            "reply_id": r.reply_id,
                        }
                        for r in facts.requests
                    ],
                }
            )
            for mid in evaluation.supporting_message_ids:
                message = store.load_model(Message, mid)
                if message is None:
                    raise ValueError("missing canonical output")
                view["outputs"].append(
                    {
                        "message_id": mid,
                        "sender": message.sender,
                        "type": message.type.value,
                        "content": message.content,
                        "stage": stage.id,
                        "occurrence": occurrence,
                    }
                )
            if evaluation.status is not EvaluationStatus.COMPLETE:
                break
        protocol = evaluate_protocol(definition, tuple(results))
        view["progress"] = {
            "status": protocol.status.value,
            "reason": protocol.reason.value,
            "completed_stages": sum(r.status is EvaluationStatus.COMPLETE for r in results),
            "total_stages": len(schedule),
            "stages": stages,
            "synthesis_message_ids": list(protocol.synthesis_message_ids),
        }
        latest = view["last_observation"]
        # Another runner may progress between a caller's result and its outcome
        # transaction. Do not present that caller's older observation as current.
        if (
            latest
            and latest["stop_reason"] != "input_refused"
            and (
                latest["output_ids"] != [o["message_id"] for o in view["outputs"]]
                or (latest["stop_reason"] == "complete")
                != (protocol.status is EvaluationStatus.COMPLETE)
            )
        ):
            latest["stale"] = True
            view["next_action"] = (
                "The last observation differs from ledger progress. Inspect the records, "
                f"then run relay discuss --resume {execution.id}."
            )
    except (ValueError, TypeError, KeyError):
        view["error"] = {"code": "invalid_ledger", "message": "Cannot validate discussion records."}
        view["next_action"] = "Inspect the pinned inputs and ledger integrity before resuming."
    return view

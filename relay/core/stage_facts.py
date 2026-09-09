"""Read-only ledger-to-facts boundary for pure protocol evaluation."""

from collections.abc import Mapping
from contextlib import contextmanager

from relay.agents.base import AgentRole
from relay.core.policy import BudgetExhausted, StageCommunicationPolicyGate
from relay.core.protocols import (
    BudgetExhaustionFact,
    ProtocolDefinition,
    ProtocolFactsError,
    RequestState,
    StageAnswerFact,
    StageContext,
    StageFacts,
    StageRequestFact,
    stage_for_context,
)
from relay.storage.models import (
    ArtifactKind,
    EventLogEntry,
    EventType,
    Message,
    MessageType,
    Run,
    RunStatus,
)
from relay.storage.store import SqliteRelayStore


@contextmanager
def _snapshot(store: SqliteRelayStore):
    # Reuse a caller-owned transaction without committing or rolling it back.
    owned = not store.conn.in_transaction
    if owned:
        store.conn.execute("BEGIN")
    try:
        yield
    finally:
        if owned:
            store.conn.execute("ROLLBACK")


def _check_scope(record: Message | EventLogEntry, context: StageContext) -> None:
    if (record.room_id, record.task_id, record.stage_key) != (
        context.room_id,
        context.task_id,
        context.stage_key,
    ):
        raise ProtocolFactsError("foreign stage/scope record in facts")


def _request_fact(
    store: SqliteRelayStore,
    context: StageContext,
    role: AgentRole,
    request_id: str,
) -> tuple[StageRequestFact, Message]:
    request = store.load_model(Message, request_id)
    if request is None:
        raise ProtocolFactsError(f"request {request_id!r} does not exist")
    _check_scope(request, context)
    if request.recipient_role != role.value or not request.recipient or ":" in request.recipient:
        raise ProtocolFactsError("request lacks expected recipient-role provenance")
    bindings = list(
        store.all_models(
            EventLogEntry,
            "WHERE type = ? AND EXISTS (SELECT 1 FROM json_each(event_log.references_json) "
            "WHERE value = ?)",
            [EventType.MESSAGE_DELIVERED.value, f"message:{request_id}"],
        )
    )
    replies = list(store.all_models(Message, "WHERE reply_to_id = ?", [request_id]))
    if not bindings:
        if replies:
            raise ProtocolFactsError("reply has no delivery binding")
        return StageRequestFact(role, request_id, RequestState.PENDING), request
    if len(bindings) != 1:
        raise ProtocolFactsError("request has contradictory delivery bindings")
    marker = bindings[0]
    _check_scope(marker, context)
    run_refs = [ref[4:] for ref in marker.references if ref.startswith("run:")]
    message_refs = [ref for ref in marker.references if ref.startswith("message:")]
    if (
        len(run_refs) != 1
        or message_refs != [f"message:{request_id}"]
        or marker.sender != "relay:delivery"
        or marker.recipient != request.recipient
    ):
        raise ProtocolFactsError("invalid delivery marker provenance")
    run = store.load_model(Run, run_refs[0])
    if (
        run is None
        or run.agent != request.recipient
        or run.role != role.value
        or run.task_id != context.task_id
    ):
        raise ProtocolFactsError("delivery Run provenance does not match the request")
    claims = [ref for ref in marker.references if ref.startswith("reply-type:")]
    if len(claims) > 1:
        raise ProtocolFactsError("contradictory admitted reply types")
    if claims:
        try:
            admitted = MessageType(claims[0].removeprefix("reply-type:"))
        except ValueError as exc:
            raise ProtocolFactsError("unknown admitted reply type") from exc
        if admitted is MessageType.SYSTEM:
            raise ProtocolFactsError("system cannot be an admitted reply")
    if run.status is not RunStatus.SUCCEEDED:
        if replies:
            raise ProtocolFactsError("unsuccessful Run has materialized replies")
        state = (
            RequestState.FAILED
            if run.status in (RunStatus.FAILED, RunStatus.CANCELLED)
            else RequestState.PENDING
        )
        return StageRequestFact(role, request_id, state), request
    if not replies:
        # Successful provider output still needs canonical reply materialization.
        return StageRequestFact(role, request_id, RequestState.PENDING), request
    if len(replies) != 1:
        raise ProtocolFactsError("request has contradictory replies")
    reply = replies[0]
    _check_scope(reply, context)
    if (
        reply.run_id != run.id
        or reply.sender != request.recipient
        or reply.recipient != request.sender
        or reply.blocking
        or reply.type is MessageType.SYSTEM
    ):
        raise ProtocolFactsError("reply is not a canonical delivery reply")
    if claims and claims != [f"reply-type:{reply.type.value}"]:
        raise ProtocolFactsError("reply contradicts delivery admission")
    outputs = store.artifacts_for_run(run.id, kind=ArtifactKind.RUN_OUTPUT)
    if len(outputs) != 1 or outputs[0].content != reply.content:
        raise ProtocolFactsError("reply does not match persisted delivery output")
    return StageRequestFact(role, request_id, RequestState.SUCCEEDED, reply.id, reply.type), request


def collect_stage_facts(
    store: SqliteRelayStore,
    definition: ProtocolDefinition,
    context: StageContext,
    expected_request_ids: Mapping[AgentRole, str],
    clarification_seed_ids: tuple[str, ...] = (),
    *,
    policy: StageCommunicationPolicyGate | None = None,
    next_message_blocking: bool = False,
) -> StageFacts:
    """Validate provenance in one read snapshot; never materialize missing replies.

    When a gate is supplied, report turn exhaustion and, only when the caller
    needs a blocking admission, blocking exhaustion. A zero blocking allowance
    does not by itself prevent a nonblocking stage from making progress.
    """
    stage = stage_for_context(definition, context)
    if not set(expected_request_ids) <= set(stage.participants):
        raise ProtocolFactsError("request map contains undeclared roles")
    ids = tuple(expected_request_ids.values())
    if any(not isinstance(mid, str) or not mid for mid in (*ids, *clarification_seed_ids)):
        raise ProtocolFactsError("request and clarification IDs must be nonempty strings")
    if len(set(ids)) != len(ids) or len(set(clarification_seed_ids)) != len(clarification_seed_ids):
        raise ProtocolFactsError("duplicate request or clarification seed IDs")
    if policy is not None and not isinstance(policy, StageCommunicationPolicyGate):
        raise ProtocolFactsError("facts budget inspection requires a stage-aware gate")
    requests = []
    answers = []
    exhaustion = None
    with _snapshot(store):
        for role in stage.participants:
            request_id = expected_request_ids.get(role)
            if request_id is None:
                requests.append(StageRequestFact(role, None, RequestState.MISSING))
            else:
                fact, _ = _request_fact(store, context, role, request_id)
                requests.append(fact)
        for seed_id in clarification_seed_ids:
            seed = store.load_model(Message, seed_id)
            if (
                seed is None
                or not seed.blocking
                or seed.type is not MessageType.CLARIFICATION_REQUEST
            ):
                raise ProtocolFactsError("eligible seed must be a persisted blocking clarification")
            try:
                role = AgentRole(seed.recipient_role)
            except ValueError as exc:
                raise ProtocolFactsError("clarification seed lacks role provenance") from exc
            if role not in stage.participants:
                raise ProtocolFactsError("clarification seed role is outside this stage")
            fact, _ = _request_fact(store, context, role, seed_id)
            answer_id = (
                fact.reply_id if fact.reply_type is MessageType.CLARIFICATION_RESPONSE else None
            )
            answers.append(StageAnswerFact(seed_id, answer_id))
        if policy is not None:
            try:
                policy.check_turn_budget(context.room_id, context.task_id)
                policy.check_stage_turn_budget(
                    context.room_id,
                    context.task_id,
                    context.stage_key,
                    stage.budgets.policy_budgets(),
                )
                if next_message_blocking:
                    policy.check_blocking_budget(context.room_id, context.task_id)
                    policy.check_stage_blocking_budget(
                        context.room_id,
                        context.task_id,
                        context.stage_key,
                        stage.budgets.policy_budgets(),
                    )
            except BudgetExhausted as exc:
                exhaustion = BudgetExhaustionFact(exc.scope, exc.dimension)
    return StageFacts(context, context.stage_key, tuple(requests), tuple(answers), exhaustion)

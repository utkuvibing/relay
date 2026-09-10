"""Typed discussion observations; history never grants execution authority."""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Literal, cast

from pydantic import BaseModel, ConfigDict

from relay.core.policy import BudgetExhausted
from relay.storage.models import EventLogEntry, EventType, ProtocolExecution

if TYPE_CHECKING:
    from relay.core.protocol_runner import ProtocolResult
    from relay.storage.events import EventLogWriter
    from relay.storage.store import SqliteRelayStore

RefusalCode = Literal[
    "invalid_input",
    "configuration_drift",
    "policy_refused",
    "budget_exhausted",
    "request_failed",
]


class ProtocolOutcome(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    version: Literal["relay.protocol.outcome.v1"] = "relay.protocol.outcome.v1"
    execution_id: str
    stop_reason: Literal[
        "complete",
        "delivery_pending",
        "request_failed",
        "policy_refused",
        "budget_exhausted",
        "input_refused",
    ]
    stage_key: str | None = None
    stage: str | None = None
    occurrence: int | None = None
    output_ids: tuple[str, ...] = ()
    refusal_code: RefusalCode | None = None
    budget_scope: Literal["aggregate", "stage"] | None = None
    budget_dimension: Literal["turn", "blocking"] | None = None
    ledger_revision: str

    @property
    def needs_human(self) -> bool:
        return self.stop_reason not in ("complete", "delivery_pending")

    @property
    def next_action(self) -> str:
        if self.stop_reason == "complete":
            return "Review the synthesis and outputs; protocol completion is not task approval."
        if self.stop_reason == "delivery_pending":
            return (
                f"Inspect delivery progress, then run relay discuss --resume {self.execution_id}."
            )
        if self.refusal_code == "configuration_drift":
            return "Restore the pinned participant configuration, then resume; or start a new discussion."
        if self.stop_reason == "budget_exhausted":
            if self.budget_scope == "stage":
                return "Review partial outputs. A changed stage budget requires a new discussion."
            return (
                "Review partial outputs and explicitly adjust communication.budgets if appropriate, "
                "then resume. Relay never increases the allowance automatically."
            )
        if self.stop_reason == "policy_refused":
            return "Review communication policy; restore permitted settings before resume, or start anew."
        if self.stop_reason == "request_failed":
            return "Inspect the failed run and correct its cause, then start a new discussion; no retries."
        return (
            "Check role bindings and pinned inputs or ledger integrity. Restore valid settings "
            "before resume; changed protocol inputs require a new discussion."
        )


def outcome_events(store: SqliteRelayStore, execution_id: str) -> list[EventLogEntry]:
    return list(
        store.all_models(
            EventLogEntry,
            "WHERE type = ? AND EXISTS (SELECT 1 FROM json_each(event_log.references_json) "
            "WHERE value = ?)",
            [EventType.PROTOCOL_OUTCOME_RECORDED.value, f"execution:{execution_id}"],
            order_by="sequence ASC",
        )
    )


def ledger_revision(store: SqliteRelayStore, execution: ProtocolExecution) -> str:
    """Fingerprint scoped ledger progress, excluding observations themselves."""
    args = [execution.room_id, execution.task_id]
    events = store.conn.execute(
        "SELECT sequence, references_json FROM event_log WHERE room_id IS ? AND task_id IS ? "
        "AND type != ? ORDER BY sequence",
        [*args, EventType.PROTOCOL_OUTCOME_RECORDED.value],
    ).fetchall()
    run_ids = sorted(
        {
            ref[4:]
            for row in events
            for ref in json.loads(row["references_json"])
            if ref.startswith("run:")
        }
    )
    runs = [
        tuple(row)
        if (
            row := store.conn.execute(
                "SELECT id, status FROM runs WHERE id = ?",
                [rid],
            ).fetchone()
        )
        else (rid, None)
        for rid in run_ids
    ]
    messages = [
        row[0]
        for row in store.conn.execute(
            "SELECT id FROM messages WHERE room_id IS ? AND task_id IS ? ORDER BY id",
            args,
        )
    ]
    payload = [[row["sequence"] for row in events], messages, runs]
    return hashlib.sha256(json.dumps(payload, separators=(",", ":")).encode()).hexdigest()


def record_outcome(
    store: SqliteRelayStore,
    writer: EventLogWriter,
    result: ProtocolResult,
) -> None:
    if result.execution_id is None:
        return
    with store.transaction():
        execution = store.load_model(ProtocolExecution, result.execution_id)
        if execution is None:
            return
        stage = result.stage_evaluations[-1] if result.stage_evaluations else None
        reason = result.stop_reason.value
        code: RefusalCode | None = None
        if reason == "input_refused":
            code = (
                "configuration_drift"
                if getattr(result.refusal, "code", None) == "configuration_drift"
                else "invalid_input"
            )
        elif reason not in ("complete", "delivery_pending"):
            # ProtocolStopReason's refusal members are exactly RefusalCode's.
            code = cast(RefusalCode, reason)
        budget = result.refusal if isinstance(result.refusal, BudgetExhausted) else None
        observation = ProtocolOutcome(
            execution_id=execution.id,
            stop_reason=reason,
            stage_key=stage.stage_key if stage else None,
            stage=stage.context.stage_id if stage else None,
            occurrence=stage.context.occurrence_index if stage else None,
            output_ids=result.output_ids,
            refusal_code=code,
            budget_scope=budget.scope if budget else None,
            budget_dimension=budget.dimension if budget else None,
            ledger_revision=ledger_revision(store, execution),
        )
        previous = outcome_events(store, execution.id)
        if previous and ProtocolOutcome.model_validate_json(previous[-1].content) == observation:
            return
        writer.record(
            EventLogEntry(
                type=EventType.PROTOCOL_OUTCOME_RECORDED,
                sender="relay:protocol",
                room_id=execution.room_id,
                task_id=execution.task_id,
                stage_key=observation.stage_key,
                content=observation.model_dump_json(),
                references=[
                    f"execution:{execution.id}",
                    *(f"message:{mid}" for mid in observation.output_ids),
                ],
            )
        )

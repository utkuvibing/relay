"""Immutable discussion definitions and pure completion decisions (P5.2).

Facts are supplied by a trusted collector. This module never reads the ledger
or schedules work; successful facts are provenance assertions, not model prose.
"""

from __future__ import annotations

import hashlib
import json
from enum import Enum
from typing import Annotated

from pydantic import ConfigDict, Field, StrictBool, StrictInt, StrictStr
from pydantic.dataclasses import dataclass

from relay.agents.base import AgentRole
from relay.core.policy import BLOCKING_CAPABLE_TYPES, CommunicationBudgets, PolicyEdge
from relay.harness.capabilities import HarnessCapability
from relay.storage.models import MessageType

_CONFIG = ConfigDict(extra="forbid", validate_default=True)
Name = Annotated[StrictStr, Field(min_length=1)]


class ProtocolFactsError(ValueError):
    """Inconsistent definition, context, or facts; no evaluation is possible."""


@dataclass(frozen=True, config=_CONFIG)
class StageBudgets:
    max_agent_turns: Annotated[StrictInt, Field(ge=1, le=1000)]
    max_blocking_messages: Annotated[StrictInt, Field(ge=0, le=1000)]

    def policy_budgets(self) -> CommunicationBudgets:
        return CommunicationBudgets(self.max_agent_turns, self.max_blocking_messages)


@dataclass(frozen=True, config=_CONFIG)
class StageEdge:
    sender: AgentRole
    recipient: AgentRole
    types: tuple[MessageType, ...]
    blocking_allowed: StrictBool = False

    def __post_init__(self) -> None:
        if self.sender == self.recipient:
            raise ValueError("self-pairs are not permitted")
        if MessageType.SYSTEM in self.types:
            raise ValueError("system is not a stage message type")
        if self.blocking_allowed and not set(self.types) & BLOCKING_CAPABLE_TYPES:
            raise ValueError("blocking edge requires a blocking-capable type")

    def policy_edge(self) -> PolicyEdge:
        return PolicyEdge(self.sender, self.recipient, frozenset(self.types), self.blocking_allowed)


@dataclass(frozen=True, config=_CONFIG)
class ParticipantRequirement:
    role: AgentRole
    required_capabilities: tuple[HarnessCapability, ...] = ()


@dataclass(frozen=True, config=_CONFIG)
class ExpectedOutput:
    role: AgentRole
    type: MessageType


@dataclass(frozen=True, config=_CONFIG)
class StageCompletion:
    require_synthesis: StrictBool = False
    early_stop_on_answered: StrictBool = False


@dataclass(frozen=True, config=_CONFIG)
class ProtocolCompletion:
    require_synthesis: StrictBool = False


@dataclass(frozen=True, config=_CONFIG)
class StageDefinition:
    id: Name
    participants: tuple[AgentRole, ...]
    edges: tuple[StageEdge, ...]
    allowed_message_types: tuple[MessageType, ...]
    budgets: StageBudgets
    expected_outputs: tuple[ExpectedOutput, ...]
    completion: StageCompletion = StageCompletion()

    def __post_init__(self) -> None:
        roles = set(self.participants)
        if not roles or len(roles) != len(self.participants):
            raise ValueError("stage participants must be nonempty and unique")
        outputs = [output.role for output in self.expected_outputs]
        if len(set(outputs)) != len(outputs) or set(outputs) != roles:
            raise ValueError("expected_outputs must declare exactly one output per stage role")
        allowed = set(self.allowed_message_types)
        if not allowed or MessageType.SYSTEM in allowed:
            raise ValueError("stage types must be nonempty and exclude system")
        if any(output.type not in allowed for output in self.expected_outputs):
            raise ValueError("expected output type is outside the stage schedule")
        pairs = [(edge.sender, edge.recipient) for edge in self.edges]
        if len(set(pairs)) != len(pairs):
            raise ValueError("duplicate stage edge")
        for edge in self.edges:
            if edge.sender not in roles or edge.recipient not in roles:
                raise ValueError("stage edge references an undeclared participant")
            if not set(edge.types) <= allowed:
                raise ValueError("edge type is outside the stage schedule")
        if self.completion.require_synthesis and not any(
            output.type is MessageType.SYNTHESIS for output in self.expected_outputs
        ):
            raise ValueError("require_synthesis needs a declared synthesis output")


@dataclass(frozen=True, config=_CONFIG)
class ProtocolRepeat:
    stages: tuple[Name, ...]
    rounds: Annotated[StrictInt, Field(ge=1, le=100)]


@dataclass(frozen=True, config=_CONFIG)
class ProtocolDefinition:
    name: Name
    version: Name
    participants: tuple[ParticipantRequirement, ...]
    stages: tuple[StageDefinition, ...]
    completion: ProtocolCompletion = ProtocolCompletion()
    repeat: ProtocolRepeat | None = None

    def __post_init__(self) -> None:
        roles = [participant.role for participant in self.participants]
        if not roles or len(set(roles)) != len(roles):
            raise ValueError("protocol participants must be nonempty and unique")
        ids = [stage.id for stage in self.stages]
        if not ids or len(set(ids)) != len(ids):
            raise ValueError("protocol stages must be nonempty with unique IDs")
        if self.repeat is not None:
            block = list(self.repeat.stages)
            if not block or block[0] not in ids:
                raise ValueError("repeat must name a nonempty contiguous stage block")
            start = ids.index(block[0])
            if ids[start : start + len(block)] != block:
                raise ValueError("repeat stages must be unique, contiguous, and in order")
        if any(not set(stage.participants) <= set(roles) for stage in self.stages):
            raise ValueError("stage references an undeclared protocol participant")
        if self.completion.require_synthesis and not any(
            output.type is MessageType.SYNTHESIS
            for stage in self.stages
            for output in stage.expected_outputs
        ):
            raise ValueError("require_synthesis needs a declared synthesis output")


@dataclass(frozen=True, config=_CONFIG)
class StageContext:
    protocol_name: Name
    protocol_version: Name
    execution_key: Name
    stage_id: Name
    occurrence_index: Annotated[StrictInt, Field(ge=0)] = 0
    room_id: Name | None = None
    task_id: Name | None = None

    def __post_init__(self) -> None:
        if self.room_id is None and self.task_id is None:
            raise ValueError("at least one room/task scope is required")

    def canonical_bytes(self) -> bytes:
        return json.dumps(
            [
                "relay.stage.identity.v1",
                self.protocol_name,
                self.protocol_version,
                self.execution_key,
                self.stage_id,
                self.occurrence_index,
                self.room_id,
                self.task_id,
            ],
            ensure_ascii=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")

    @property
    def stage_key(self) -> str:
        return "stage:v1:" + hashlib.sha256(self.canonical_bytes()).hexdigest()


def stage_for_context(definition: ProtocolDefinition, context: StageContext) -> StageDefinition:
    if (definition.name, definition.version) != (context.protocol_name, context.protocol_version):
        raise ProtocolFactsError("context does not identify this protocol")
    for stage in definition.stages:
        if stage.id == context.stage_id:
            return stage
    raise ProtocolFactsError("context references an undeclared stage")


class RequestState(str, Enum):
    MISSING = "missing"
    PENDING = "pending"
    FAILED = "failed"
    SUCCEEDED = "succeeded"


class EvaluationStatus(str, Enum):
    CONTINUE = "continue"
    COMPLETE = "complete"
    BLOCKED = "blocked"


class EvaluationReason(str, Enum):
    OUTPUTS_COMPLETE = "outputs_complete"
    ANSWERED = "answered"
    AWAITING_OUTPUTS = "awaiting_outputs"
    SYNTHESIS_REQUIRED = "synthesis_required"
    REQUEST_FAILED = "request_failed"
    BUDGET_EXHAUSTED = "budget_exhausted"
    STAGES_INCOMPLETE = "stages_incomplete"
    PROTOCOL_COMPLETE = "protocol_complete"


class BudgetScope(str, Enum):
    AGGREGATE = "aggregate"
    STAGE = "stage"


class BudgetDimension(str, Enum):
    TURN = "turn"
    BLOCKING = "blocking"


@dataclass(frozen=True, config=_CONFIG)
class BudgetExhaustionFact:
    scope: BudgetScope
    dimension: BudgetDimension


@dataclass(frozen=True, config=_CONFIG)
class StageRequestFact:
    role: AgentRole
    request_id: Name | None
    state: RequestState
    reply_id: Name | None = None
    reply_type: MessageType | None = None

    def __post_init__(self) -> None:
        if self.state is RequestState.SUCCEEDED:
            if self.request_id is None or self.reply_id is None or self.reply_type is None:
                raise ValueError("successful request requires request and canonical reply facts")
        elif self.reply_id is not None or self.reply_type is not None:
            raise ValueError("only successful requests may carry reply facts")
        if self.state is not RequestState.MISSING and self.request_id is None:
            raise ValueError("non-missing request requires an ID")
        if self.state is RequestState.MISSING and self.request_id is not None:
            raise ValueError("missing request cannot carry an ID")


@dataclass(frozen=True, config=_CONFIG)
class StageAnswerFact:
    seed_id: Name
    answering_reply_id: Name | None = None


@dataclass(frozen=True, config=_CONFIG)
class StageFacts:
    context: StageContext
    stage_key: Name
    requests: tuple[StageRequestFact, ...]
    answers: tuple[StageAnswerFact, ...] = ()
    budget_exhaustion: BudgetExhaustionFact | None = None


@dataclass(frozen=True, config=_CONFIG)
class StageEvaluation:
    context: StageContext
    stage_key: Name
    status: EvaluationStatus
    reason: EvaluationReason
    supporting_message_ids: tuple[str, ...] = ()
    synthesis_message_ids: tuple[str, ...] = ()


@dataclass(frozen=True, config=_CONFIG)
class ProtocolEvaluation:
    status: EvaluationStatus
    reason: EvaluationReason
    supporting_message_ids: tuple[str, ...] = ()
    synthesis_message_ids: tuple[str, ...] = ()


def evaluate_stage(definition: ProtocolDefinition, facts: StageFacts) -> StageEvaluation:
    stage = stage_for_context(definition, facts.context)
    if facts.stage_key != facts.context.stage_key:
        raise ProtocolFactsError("facts stage key does not match context")
    roles = [request.role for request in facts.requests]
    if len(set(roles)) != len(roles) or set(roles) != set(stage.participants):
        raise ProtocolFactsError("facts must include exactly one request per expected role")
    request_ids = [r.request_id for r in facts.requests if r.request_id is not None]
    reply_ids = [r.reply_id for r in facts.requests if r.reply_id is not None]
    seeds = [a.seed_id for a in facts.answers]
    if any(len(set(ids)) != len(ids) for ids in (request_ids, reply_ids, seeds)):
        raise ProtocolFactsError("duplicate request, reply, or clarification seed")
    expected = {output.role: output.type for output in stage.expected_outputs}
    qualifying = tuple(
        r
        for r in facts.requests
        if r.state is RequestState.SUCCEEDED and r.reply_type is expected[r.role]
    )
    synthesis = tuple(
        r.reply_id
        for r in qualifying
        if r.reply_id is not None and r.reply_type is MessageType.SYNTHESIS
    )
    support = tuple(r.reply_id for r in qualifying if r.reply_id is not None)
    synthesis_ok = not stage.completion.require_synthesis or bool(synthesis)
    answered = (
        stage.completion.early_stop_on_answered
        and bool(facts.answers)
        and all(a.answering_reply_id is not None for a in facts.answers)
    )
    if len(qualifying) == len(stage.expected_outputs) and synthesis_ok:
        status, reason = EvaluationStatus.COMPLETE, EvaluationReason.OUTPUTS_COMPLETE
    elif answered and synthesis_ok:
        status, reason = EvaluationStatus.COMPLETE, EvaluationReason.ANSWERED
        support += tuple(
            a.answering_reply_id for a in facts.answers if a.answering_reply_id is not None
        )
    elif any(r.state is RequestState.FAILED for r in facts.requests):
        status, reason = EvaluationStatus.BLOCKED, EvaluationReason.REQUEST_FAILED
    elif facts.budget_exhaustion is not None:
        status, reason = EvaluationStatus.BLOCKED, EvaluationReason.BUDGET_EXHAUSTED
    else:
        status = EvaluationStatus.CONTINUE
        reason = (
            EvaluationReason.AWAITING_OUTPUTS
            if synthesis_ok
            else EvaluationReason.SYNTHESIS_REQUIRED
        )
    return StageEvaluation(facts.context, facts.stage_key, status, reason, support, synthesis)


def protocol_schedule(definition: ProtocolDefinition) -> tuple[tuple[StageDefinition, int], ...]:
    """Expand one finite block; occurrence indexes remain stage-local."""
    repeat = definition.repeat
    schedule: list[tuple[StageDefinition, int]] = []
    for stage in definition.stages:
        if repeat is None or stage.id not in repeat.stages:
            schedule.append((stage, 0))
        elif stage.id == repeat.stages[0]:
            block = [s for s in definition.stages if s.id in repeat.stages]
            schedule.extend((s, index) for index in range(repeat.rounds) for s in block)
    return tuple(schedule)


def evaluate_protocol(
    definition: ProtocolDefinition, stage_results: tuple[StageEvaluation, ...]
) -> ProtocolEvaluation:
    schedule = protocol_schedule(definition)
    if len(stage_results) > len(schedule):
        raise ProtocolFactsError("too many stage results")
    execution = None
    for (stage, occurrence), result in zip(schedule, stage_results):
        resolved = stage_for_context(definition, result.context)
        if resolved.id != stage.id or result.stage_key != result.context.stage_key:
            raise ProtocolFactsError("stage results are out of order or have an invalid key")
        if result.context.occurrence_index != occurrence:
            raise ProtocolFactsError("stage occurrence is out of order")
        identity = (result.context.execution_key, result.context.room_id, result.context.task_id)
        if execution is not None and execution != identity:
            raise ProtocolFactsError("stage results belong to different executions/scopes")
        execution = identity
        if result.synthesis_message_ids and not any(
            output.type is MessageType.SYNTHESIS for output in stage.expected_outputs
        ):
            raise ProtocolFactsError("synthesis evidence belongs to a non-synthesis stage")
    support = tuple(mid for result in stage_results for mid in result.supporting_message_ids)
    synthesis = tuple(mid for result in stage_results for mid in result.synthesis_message_ids)
    if any(result.status is EvaluationStatus.BLOCKED for result in stage_results):
        return ProtocolEvaluation(
            EvaluationStatus.BLOCKED, EvaluationReason.STAGES_INCOMPLETE, support, synthesis
        )
    if len(stage_results) != len(schedule) or any(
        result.status is not EvaluationStatus.COMPLETE for result in stage_results
    ):
        return ProtocolEvaluation(
            EvaluationStatus.CONTINUE, EvaluationReason.STAGES_INCOMPLETE, support, synthesis
        )
    if definition.completion.require_synthesis and not synthesis:
        return ProtocolEvaluation(
            EvaluationStatus.CONTINUE, EvaluationReason.SYNTHESIS_REQUIRED, support, synthesis
        )
    return ProtocolEvaluation(
        EvaluationStatus.COMPLETE, EvaluationReason.PROTOCOL_COMPLETE, support, synthesis
    )

"""Stage admission shared by the bus and delivery; no scheduling."""

from relay.agents.base import AgentRole
from relay.core.policy import (
    CommunicationPolicy,
    CommunicationPolicyGate,
    CommunicationPolicyRefusal,
    PolicyEnvelope,
    StageCommunicationPolicyGate,
    evaluate_edge,
)
from relay.core.protocols import (
    ProtocolDefinition,
    ProtocolFactsError,
    StageContext,
    stage_for_context,
)
from relay.storage.models import EventLogEntry, Message, MessageType


class StageContextRefusal(CommunicationPolicyRefusal):
    """Stage identity or service configuration mismatch."""


class StageScheduleRefusal(CommunicationPolicyRefusal):
    """Traffic falls outside the declared stage schedule/participants."""


class StageAdmission:
    def __init__(
        self,
        context: StageContext,
        definition: ProtocolDefinition,
        gate: StageCommunicationPolicyGate,
    ) -> None:
        try:
            self.stage = stage_for_context(definition, context)
        except ProtocolFactsError as exc:
            raise StageContextRefusal(str(exc)) from exc
        self.context = context
        self.definition = definition
        self.gate = gate
        self.policy = CommunicationPolicy(
            self.stage.budgets.policy_budgets(),
            frozenset(edge.policy_edge() for edge in self.stage.edges),
        )

    def check_record(self, record: Message | EventLogEntry, *, allow_unstamped=False) -> None:
        if (record.room_id, record.task_id) != (self.context.room_id, self.context.task_id):
            raise StageContextRefusal("record scope differs from active stage scope")
        if record.stage_key != self.context.stage_key and not (
            allow_unstamped and record.stage_key is None
        ):
            raise StageContextRefusal("record stage key differs from active stage")

    def check_type(self, message_type: MessageType) -> None:
        if message_type not in self.stage.allowed_message_types:
            raise StageScheduleRefusal(f"{message_type.value} is outside stage {self.stage.id}")

    def check_edge(self, envelope: PolicyEnvelope) -> None:
        self.check_type(envelope.type)
        for principal in (envelope.sender, envelope.recipient):
            if isinstance(principal, AgentRole) and principal not in self.stage.participants:
                raise StageScheduleRefusal(f"role {principal.value} is not a stage participant")
        evaluate_edge(self.policy, envelope)

    def check_turn_budget(self) -> None:
        self.gate.check_stage_turn_budget(
            self.context.room_id,
            self.context.task_id,
            self.context.stage_key,
            self.policy.budgets,
        )

    def check_blocking_budget(self) -> None:
        self.gate.check_stage_blocking_budget(
            self.context.room_id,
            self.context.task_id,
            self.context.stage_key,
            self.policy.budgets,
        )


def stage_admission(
    context: StageContext | None,
    definition: ProtocolDefinition | None,
    gate: CommunicationPolicyGate | None,
) -> StageAdmission | None:
    if context is None and definition is None:
        return None
    if context is None or definition is None:
        raise StageContextRefusal("stage context and protocol definition must be supplied together")
    if not isinstance(gate, StageCommunicationPolicyGate):
        raise StageContextRefusal("stage execution requires a stage-aware policy gate")
    return StageAdmission(context, definition, gate)

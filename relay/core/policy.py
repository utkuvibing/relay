"""Declarative communication policy and ledger-backed budgets (P5.1).

The policy vocabulary is deliberately smaller than the rest of Relay's
execution machinery: roles, message types, scopes, and the two bounded
counts.  Pair evaluation is pure.  The concrete gate only reads the existing
ledger, and callers decide which transaction owns each admission check.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Protocol, TypeAlias, runtime_checkable

from relay.agents.base import AgentRole
from relay.storage.models import EventType, MessageType

if TYPE_CHECKING:
    from relay.context.config import RelayConfig
    from relay.storage.store import SqliteRelayStore

__all__ = [
    "BLOCKING_CAPABLE_TYPES",
    "DEFAULT_BUDGETS",
    "REPLY_ADMISSION_REFERENCE_PREFIX",
    "BlockingBudgetExhausted",
    "BlockingNotPermitted",
    "BudgetExhausted",
    "CommunicationBudgets",
    "CommunicationPolicy",
    "CommunicationPolicyGate",
    "CommunicationPolicyRefusal",
    "EdgeNotPermitted",
    "LedgerCommunicationPolicyGate",
    "MessageRejected",
    "PolicyEdge",
    "PolicyEnvelope",
    "PolicyPrincipal",
    "PrincipalClass",
    "SqliteCommunicationPolicyGate",
    "TurnBudgetExhausted",
    "TypeNotPermitted",
    "evaluate_edge",
    "policy_from_config",
    "principal_for_recipient",
    "principal_for_sender",
    "reply_admission_reference",
]


class MessageRejected(ValueError):
    """Typed pre-persistence rejection: nothing was written to the store."""


class CommunicationPolicyRefusal(MessageRejected):
    """A communication edge or budget is not admitted by policy."""


class EdgeNotPermitted(CommunicationPolicyRefusal):
    """The exact sender/recipient role pair is not declared."""


class TypeNotPermitted(CommunicationPolicyRefusal):
    """The message type is not allowed on the declared pair."""


class BlockingNotPermitted(CommunicationPolicyRefusal):
    """The declared pair does not allow a blocking message."""


class BudgetExhausted(CommunicationPolicyRefusal):
    """A communication budget has no remaining capacity."""


class TurnBudgetExhausted(BudgetExhausted):
    """The exact room/task scope has exhausted agent turns."""


class BlockingBudgetExhausted(BudgetExhausted):
    """The exact room/task scope has exhausted blocking messages."""


class PrincipalClass(str, Enum):
    """Non-role principals used for human and Relay-owned traffic."""

    HUMAN = "human"
    RELAY = "relay"


PolicyPrincipal: TypeAlias = AgentRole | PrincipalClass


BLOCKING_CAPABLE_TYPES = frozenset(
    {
        MessageType.CLARIFICATION_REQUEST,
        MessageType.CHALLENGE,
        MessageType.PROPOSAL,
        MessageType.REVIEW_FINDING,
    }
)

_MAX_BUDGET = 1000

# A MESSAGE_DELIVERED marker may carry exactly one of these references when
# deliver_and_reply admitted a prospective reply before provider execution.
# This uses the existing append-only references field: no schema or EventType
# is added, and a marker produced by standalone deliver() has no such claim.
REPLY_ADMISSION_REFERENCE_PREFIX = "reply-type:"


def reply_admission_reference(reply_type: MessageType) -> str:
    """Return the bounded marker reference for one admitted reply type."""
    return f"{REPLY_ADMISSION_REFERENCE_PREFIX}{reply_type.value}"


def _coerce_principal(value: PolicyPrincipal | str) -> PolicyPrincipal:
    if isinstance(value, (AgentRole, PrincipalClass)):
        return value
    if isinstance(value, str):
        try:
            return AgentRole(value)
        except ValueError:
            try:
                return PrincipalClass(value)
            except ValueError:
                pass
    raise ValueError(f"invalid policy principal {value!r}")


def _coerce_role(value: AgentRole | str) -> AgentRole:
    if isinstance(value, AgentRole):
        return value
    try:
        return AgentRole(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid AgentRole policy principal {value!r}") from exc


def _coerce_message_type(value: MessageType | str) -> MessageType:
    if isinstance(value, MessageType):
        return value
    try:
        return MessageType(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid policy message type {value!r}") from exc


@dataclass(frozen=True)
class PolicyEdge:
    """One exact sender-principal → recipient-principal policy edge."""

    sender: PolicyPrincipal
    recipient: PolicyPrincipal
    types: frozenset[MessageType]
    blocking_allowed: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "sender", _coerce_principal(self.sender))
        object.__setattr__(self, "recipient", _coerce_principal(self.recipient))
        if not isinstance(self.sender, AgentRole) or not isinstance(self.recipient, AgentRole):
            raise TypeError("policy edges must use AgentRole principals")
        types = frozenset(_coerce_message_type(message_type) for message_type in self.types)
        if MessageType.SYSTEM in types:
            raise ValueError("policy edges may not permit system messages")
        object.__setattr__(self, "types", types)
        if not isinstance(self.blocking_allowed, bool):
            raise TypeError("blocking_allowed must be a bool")


@dataclass(frozen=True)
class CommunicationBudgets:
    """Ledger-derived limits for one room/task scope."""

    max_agent_turns: int
    max_blocking_messages: int

    def __post_init__(self) -> None:
        for name, value, lower in (
            ("max_agent_turns", self.max_agent_turns, 1),
            ("max_blocking_messages", self.max_blocking_messages, 0),
        ):
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an int")
            if value < lower or value > _MAX_BUDGET:
                raise ValueError(f"{name} must be between {lower} and {_MAX_BUDGET}")


@dataclass(frozen=True)
class CommunicationPolicy:
    """Frozen policy values used by an injected communication gate."""

    budgets: CommunicationBudgets
    edges: frozenset[PolicyEdge] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.budgets, CommunicationBudgets):
            raise TypeError("budgets must be a CommunicationBudgets instance")
        if self.edges is not None:
            edges = frozenset(self.edges)
            if not all(isinstance(edge, PolicyEdge) for edge in edges):
                raise TypeError("edges must contain PolicyEdge instances")
            pairs: set[tuple[AgentRole, AgentRole]] = set()
            for edge in edges:
                pair = (edge.sender, edge.recipient)
                if pair in pairs:
                    raise ValueError(
                        f"duplicate policy edge '{edge.sender.value}' -> '{edge.recipient.value}'"
                    )
                pairs.add(pair)
            object.__setattr__(self, "edges", edges)

    def gate(self, store: SqliteRelayStore) -> SqliteCommunicationPolicyGate:
        """Bind this frozen policy to a ledger for transactional checks."""
        return SqliteCommunicationPolicyGate(store, self)


DEFAULT_BUDGETS = CommunicationBudgets(max_agent_turns=16, max_blocking_messages=3)


@dataclass(frozen=True)
class PolicyEnvelope:
    """The complete pure-evaluation input for one prospective message."""

    sender: PolicyPrincipal
    recipient: PolicyPrincipal
    type: MessageType
    blocking: bool
    room_id: str | None
    task_id: str | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "sender", _coerce_principal(self.sender))
        object.__setattr__(self, "recipient", _coerce_principal(self.recipient))
        object.__setattr__(self, "type", _coerce_message_type(self.type))
        if not isinstance(self.blocking, bool):
            raise TypeError("blocking must be a bool")


def evaluate_edge(policy: CommunicationPolicy, envelope: PolicyEnvelope) -> None:
    """Evaluate one envelope deterministically without performing I/O."""
    sender = envelope.sender
    recipient = envelope.recipient

    # Pair policy governs role-to-role collaboration only.  Human and Relay
    # traffic remains subject to the structural bus rules, but not this matrix.
    if not isinstance(sender, AgentRole) or not isinstance(recipient, AgentRole):
        return
    if policy.edges is None:
        return

    edge = next(
        (
            candidate
            for candidate in policy.edges
            if candidate.sender == sender and candidate.recipient == recipient
        ),
        None,
    )
    if edge is None:
        raise EdgeNotPermitted(
            f"communication edge {sender.value!r} -> {recipient.value!r} is not permitted"
        )
    if envelope.type is MessageType.SYSTEM or envelope.type not in edge.types:
        raise TypeNotPermitted(
            f"message type {envelope.type.value!r} is not permitted on "
            f"communication edge {sender.value!r} -> {recipient.value!r}"
        )
    if envelope.blocking and not edge.blocking_allowed:
        raise BlockingNotPermitted(
            f"blocking messages are not permitted on communication edge "
            f"{sender.value!r} -> {recipient.value!r}"
        )


def principal_for_sender(
    sender: str, *, run_role: AgentRole | str | None = None
) -> PolicyPrincipal:
    """Derive a sender principal from its prefix or persisted Run role."""
    if sender.startswith("human:"):
        return PrincipalClass.HUMAN
    if sender.startswith("relay:"):
        return PrincipalClass.RELAY
    if run_role is None:
        raise ValueError(f"bare sender {sender!r} requires the authorship Run role as run_role")
    return _coerce_role(run_role)


def principal_for_recipient(
    recipient: str | None, *, recipient_role: AgentRole | str | None = None
) -> PolicyPrincipal:
    """Derive a recipient principal from role provenance or identity prefix."""
    if recipient_role is not None:
        return _coerce_role(recipient_role)
    if recipient is not None and recipient.startswith("human:"):
        return PrincipalClass.HUMAN
    if recipient is not None and recipient.startswith("relay:"):
        return PrincipalClass.RELAY
    return AgentRole.PARTICIPANT


@runtime_checkable
class CommunicationPolicyGate(Protocol):
    """Injected policy seam consumed by bus, delivery, and driver."""

    def check_edge(self, envelope: PolicyEnvelope) -> None: ...

    def check_turn_budget(self, room_id: str | None, task_id: str | None) -> None: ...

    def check_blocking_budget(self, room_id: str | None, task_id: str | None) -> None: ...


class SqliteCommunicationPolicyGate:
    """Evaluate a frozen policy and count its limits from the canonical ledger."""

    def __init__(self, store: SqliteRelayStore, policy: CommunicationPolicy) -> None:
        self._store = store
        self.policy = policy

    def check_edge(self, envelope: PolicyEnvelope) -> None:
        evaluate_edge(self.policy, envelope)

    def check_turn_budget(self, room_id: str | None, task_id: str | None) -> None:
        row = self._store.conn.execute(
            "SELECT COUNT(*) FROM event_log WHERE type = ? AND room_id IS ? AND task_id IS ?",
            [EventType.MESSAGE_DELIVERED.value, room_id, task_id],
        ).fetchone()
        used = int(row[0])
        limit = self.policy.budgets.max_agent_turns
        if used >= limit:
            raise TurnBudgetExhausted(
                f"agent-turn budget exhausted for scope "
                f"(room_id={room_id!r}, task_id={task_id!r}): {used} of {limit} used"
            )

    def check_blocking_budget(self, room_id: str | None, task_id: str | None) -> None:
        rows = self._store.conn.execute(
            "SELECT sender FROM messages WHERE blocking = 1 AND room_id IS ? AND task_id IS ?",
            [room_id, task_id],
        ).fetchall()
        used = sum(not str(row[0]).startswith("human:") for row in rows)
        limit = self.policy.budgets.max_blocking_messages
        if used >= limit:
            raise BlockingBudgetExhausted(
                f"blocking-message budget exhausted for scope "
                f"(room_id={room_id!r}, task_id={task_id!r}): {used} of {limit} used"
            )


LedgerCommunicationPolicyGate = SqliteCommunicationPolicyGate


def policy_from_config(config: RelayConfig) -> CommunicationPolicy:
    """Convert parsed ``relay.yaml`` communication settings to frozen policy."""
    communication = getattr(config, "communication", None)
    budget_config = getattr(communication, "budgets", None)
    if budget_config is None:
        budgets = DEFAULT_BUDGETS
    else:
        budgets = CommunicationBudgets(
            max_agent_turns=budget_config.max_agent_turns,
            max_blocking_messages=budget_config.max_blocking_messages,
        )

    configured_edges = getattr(communication, "edges", None)
    if configured_edges is None:
        edges = None
    else:
        edges = frozenset(
            PolicyEdge(
                sender=edge.from_,
                recipient=edge.to,
                types=frozenset(edge.types),
                blocking_allowed=edge.blocking,
            )
            for edge in configured_edges
        )
    return CommunicationPolicy(budgets=budgets, edges=edges)

"""Frozen v1 array encodings. Changing a position requires a new version."""

from __future__ import annotations

import hashlib
import json
from typing import Protocol

from pydantic import ConfigDict, StrictBool, StrictStr
from pydantic.dataclasses import dataclass

from relay.agents.base import AgentRole, BackendType
from relay.core.protocols import (
    ExpectedOutput,
    ParticipantRequirement,
    ProtocolCompletion,
    ProtocolDefinition,
    ProtocolRepeat,
    StageBudgets,
    StageCompletion,
    StageDefinition,
    StageEdge,
)
from relay.harness.capabilities import HarnessCapability


def canonical_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode(
        "utf-8"
    )


def digest(prefix: str, content: bytes) -> str:
    return prefix + hashlib.sha256(content).hexdigest()


def definition_bytes(definition: ProtocolDefinition) -> bytes:
    return canonical_bytes(
        [
            "relay.protocol.definition.v1",
            definition.name,
            definition.version,
            [
                [p.role.value, [c.value for c in p.required_capabilities]]
                for p in definition.participants
            ],
            [
                [
                    s.id,
                    [r.value for r in s.participants],
                    [
                        [
                            e.sender.value,
                            e.recipient.value,
                            [t.value for t in e.types],
                            e.blocking_allowed,
                        ]
                        for e in s.edges
                    ],
                    [t.value for t in s.allowed_message_types],
                    [s.budgets.max_agent_turns, s.budgets.max_blocking_messages],
                    [[o.role.value, o.type.value] for o in s.expected_outputs],
                    [s.completion.require_synthesis, s.completion.early_stop_on_answered],
                ]
                for s in definition.stages
            ],
            [definition.completion.require_synthesis],
            None
            if definition.repeat is None
            else [list(definition.repeat.stages), definition.repeat.rounds],
        ]
    )


def definition_digest(definition: ProtocolDefinition) -> str:
    return digest("protocol-definition:v1:", definition_bytes(definition))


def decode_definition(snapshot: str, expected_digest: str) -> ProtocolDefinition:
    """Reject corruption, unsupported versions, and noncanonical representations."""
    try:
        version, name, revision, participants, stages, completion, repeat = json.loads(snapshot)
        if version != "relay.protocol.definition.v1":
            raise ValueError("unsupported definition encoding")
        definition = ProtocolDefinition(
            name=name,
            version=revision,
            participants=tuple(
                ParticipantRequirement(role=r, required_capabilities=tuple(c))
                for r, c in participants
            ),
            stages=tuple(
                StageDefinition(
                    id=sid,
                    participants=tuple(roles),
                    edges=tuple(
                        StageEdge(sender=a, recipient=b, types=tuple(t), blocking_allowed=blk)
                        for a, b, t, blk in edges
                    ),
                    allowed_message_types=tuple(types),
                    budgets=StageBudgets(*budgets),
                    expected_outputs=tuple(ExpectedOutput(role=r, type=t) for r, t in outputs),
                    completion=StageCompletion(*done),
                )
                for sid, roles, edges, types, budgets, outputs, done in stages
            ),
            completion=ProtocolCompletion(*completion),
            repeat=None if repeat is None else ProtocolRepeat(tuple(repeat[0]), repeat[1]),
        )
        if (
            definition_bytes(definition).decode() != snapshot
            or definition_digest(definition) != expected_digest
        ):
            raise ValueError("definition snapshot/digest mismatch")
        return definition
    except (ValueError, TypeError, IndexError) as exc:
        raise ValueError("invalid persisted protocol definition") from exc


@dataclass(frozen=True, config=ConfigDict(extra="forbid", allow_inf_nan=False))
class ParticipantConfig:
    """Explicit non-secret projection of effective delivery configuration.

    Factory implementations own the projection; arbitrary argument blobs are
    deliberately unrepresentable. Capabilities are a set in this encoding.
    """

    agent: StrictStr
    backend: BackendType
    adapter: StrictStr
    model: StrictStr | None = None
    endpoint: StrictStr | None = None
    executable: StrictStr | None = None
    timeout: float | None = None
    auth_probe: StrictBool | None = None
    capabilities: tuple[HarnessCapability, ...] = ()
    grant: StrictStr | None = None
    workspace_root: StrictStr | None = None

    def canonical_bytes(self) -> bytes:
        return canonical_bytes(
            [
                "relay.protocol.participant-config.v1",
                self.agent,
                self.backend.value,
                self.adapter,
                self.model,
                self.endpoint,
                self.executable,
                self.timeout,
                self.auth_probe,
                sorted({c.value for c in self.capabilities}),
                self.grant,
                self.workspace_root,
            ]
        )

    @property
    def fingerprint(self) -> str:
        return digest("participant-config:v1:", self.canonical_bytes())


class ProtocolBindingSource(Protocol):
    def protocol_participant(self, role: AgentRole) -> ParticipantConfig: ...


def request_identity_bytes(execution_id: str, stage_key: str, role: AgentRole) -> bytes:
    return canonical_bytes(["relay.protocol.request.v1", execution_id, stage_key, role.value])


def request_id(execution_id: str, stage_key: str, role: AgentRole) -> str:
    return digest("protocol-request:v1:", request_identity_bytes(execution_id, stage_key, role))

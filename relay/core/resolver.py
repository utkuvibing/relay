"""Config-backed role resolution (P4.2 — SPEC §8; App. D.2/D.5/D.6).

Discharges the P4.1 D6 forward requirement: the ``RoleResolver`` seam that
:class:`~relay.core.bus.ConversationBus` consumes is now backed by parsed
``relay.yaml`` data instead of test fakes.

The resolver speaks plain strings — a role address maps to the configured
logical-agent identity that receives role-addressed traffic. It never sees
adapter classes, so ``relay.core`` stays registry-free (import-direction
architecture test; App. C.1). Role addresses are ``AgentRole`` enum values
(frozen plan D5); unknown roles resolve to ``None`` and the bus rejects them
typed, before persistence (P4.1 D6).

The P3.3 ``reviewer:`` build-flow selector is deliberately NOT consulted:
``roles:`` and ``reviewer:`` are independent sources of truth for their own
workflows (frozen plan D4).
"""

from __future__ import annotations

from collections.abc import Collection, Mapping

from relay.context.config import RelayConfig
from relay.core.rooms import RoomSeatResolver
from relay.storage.models import Room

__all__ = ["ConfigRoleResolver", "RoomSeatRoleResolver", "role_resolver_from_config", "seat_resolver_for_room"]


class ConfigRoleResolver:
    """Deterministic role → logical-agent mapping over injected config data.

    ``resolve_role`` returns ``None`` for unknown roles (the bus rejects
    unresolved role addresses pre-persistence); ``knows_agent`` reports
    membership in the configured agent set, which hardens the bus's
    bare-sender identity check.
    """

    def __init__(self, mapping: Mapping[str, str], known_agents: Collection[str]) -> None:
        self._mapping = dict(mapping)
        self._known = frozenset(known_agents)

    def resolve_role(self, role: str) -> str | None:
        return self._mapping.get(role)

    def knows_agent(self, name: str) -> bool:
        return name in self._known


def role_resolver_from_config(config: RelayConfig) -> ConfigRoleResolver:
    """Build the production resolver from parsed ``relay.yaml`` (P4.1 D6)."""
    return ConfigRoleResolver(mapping=dict(config.roles), known_agents=config.agents.keys())


class RoomSeatRoleResolver:
    """Room-bound resolver: persisted seats route, config membership widens.

    P7.3 (App. D.2/D.3): a Room-bound task's build micro-interactions must
    resolve roles through the Room's PERSISTED seat snapshot — a Room may hold
    stable or rebound seats that differ from the current ``relay.yaml`` role
    bindings, and an explicit ``relay room bind`` must be respected. Role
    ROUTING therefore never consults the config role binding: a persisted seat
    whose agent is no longer constructible fails honestly at delivery.

    ``knows_agent`` is only the bus's bare-sender membership sanity check, so
    it also accepts currently configured logical agents — the build's reviewer
    may legitimately hold no Room seat.
    """

    def __init__(self, room: Room, known_agents: Collection[str]) -> None:
        self._seats = RoomSeatResolver(room)
        self._known = frozenset({member.agent for member in room.members}) | frozenset(
            known_agents
        )

    def resolve_role(self, role: str) -> str | None:
        return self._seats.resolve_role(role)

    def knows_agent(self, name: str) -> bool:
        return name in self._known


def seat_resolver_for_room(room: Room, config: RelayConfig) -> RoomSeatRoleResolver:
    """The P7.3 resolver for one persisted Room (seats route, config widens)."""
    return RoomSeatRoleResolver(room, config.agents.keys())

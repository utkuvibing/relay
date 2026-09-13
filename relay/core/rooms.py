"""Persistent Room lifecycle and stable role-seat bindings (P7.1)."""

from __future__ import annotations

import sqlite3
from collections.abc import Collection, Mapping

from pydantic import BaseModel

from relay.agents.base import AgentRole
from relay.storage.events import EventLogWriter
from relay.storage.models import (
    EventLogEntry,
    EventType,
    Room,
    RoomMember,
    RoomStatus,
    Workspace,
    new_id,
    utcnow,
)
from relay.storage.store import SqliteRelayStore

__all__ = [
    "ClosedRoomError",
    "RoomError",
    "RoomLifecycle",
    "RoomLookupError",
    "require_open_room",
]


class RoomError(ValueError):
    """A requested Room operation cannot be completed."""


class RoomLookupError(RoomError):
    """A Room selector is missing or ambiguous."""


class ClosedRoomError(RoomError):
    """A Room-scoped write was attempted while the Room was closed."""


def require_open_room(store: SqliteRelayStore, room_id: str) -> Room:
    """Load an open Room or refuse; callers choose the transaction boundary."""
    room = store.load_model(Room, room_id)
    if room is None:
        raise RoomLookupError(f"room '{room_id}' does not exist")
    if room.status is not RoomStatus.OPEN:
        raise ClosedRoomError(f"room '{room.name}' is closed - run 'relay room resume {room.id}'")
    return room


class RoomLifecycle:
    """Small interface owning Room lifecycle, lookup, naming, and seat mutation."""

    def __init__(self, store: SqliteRelayStore, writer: EventLogWriter) -> None:
        self._store = store
        self._writer = writer

    def list_for_workspace(self, workspace_id: str) -> list[Room]:
        return list(
            self._store.all_models(
                Room,
                "WHERE workspace_id = ?",
                [workspace_id],
                order_by="updated_at DESC, created_at DESC, id ASC",
            )
        )

    def resolve(self, workspace_id: str, selector: str) -> Room:
        rooms = self.list_for_workspace(workspace_id)
        exact_id = next((room for room in rooms if room.id == selector), None)
        if exact_id is not None:
            return exact_id
        name_matches = [room for room in rooms if room.name.lower() == selector.lower()]
        if len(name_matches) == 1:
            return name_matches[0]
        prefix_matches = [room for room in rooms if room.id.startswith(selector)]
        if len(prefix_matches) == 1:
            return prefix_matches[0]
        if len(prefix_matches) > 1:
            raise RoomLookupError(
                f"room prefix '{selector}' is ambiguous - {len(prefix_matches)} rooms match"
            )
        raise RoomLookupError(f"room '{selector}' does not exist in this workspace")

    def create(
        self,
        workspace: Workspace,
        name: str,
        bindings: Mapping[str, str],
        known_agents: Collection[str],
        *,
        disambiguate_name: bool = False,
        room_id: str | None = None,
        attached_records: Collection[BaseModel] = (),
    ) -> Room:
        clean_name = name.strip()
        if not clean_name:
            raise RoomError("room name must not be empty")
        members = self._validated_members(bindings, known_agents)
        if not members:
            raise RoomError("no roles are configured - add roles: bindings to relay.yaml")
        if disambiguate_name:
            clean_name = self._available_name(workspace.id, clean_name)
        elif any(
            room.name.lower() == clean_name.lower()
            for room in self.list_for_workspace(workspace.id)
        ):
            raise RoomError(f"room name '{clean_name}' already exists in this workspace")

        now = utcnow()
        room = Room(
            id=room_id if room_id is not None else new_id(),
            name=clean_name,
            workspace_id=workspace.id,
            members=members,
            created_at=now,
            updated_at=now,
        )
        current = workspace.model_copy(update={"active_room_id": room.id})
        try:
            with self._store.transaction():
                self._store.save_model(room)
                self._store.update_model(current)
                self._writer.record(self._event(room, EventType.ROOM_CREATED, "room created"))
                for record in attached_records:
                    self._store.save_model(record)
        except sqlite3.IntegrityError as exc:
            raise RoomError(f"room name '{clean_name}' already exists in this workspace") from exc
        return room

    def resume(self, workspace: Workspace, room: Room) -> Room:
        if room.workspace_id != workspace.id:
            raise RoomLookupError("room does not belong to this workspace")
        with self._store.transaction():
            persisted_room = self._required_room(room.id)
            persisted_workspace = self._required_workspace(workspace.id)
            if (
                persisted_room.status is RoomStatus.OPEN
                and persisted_workspace.active_room_id == room.id
            ):
                return persisted_room
            resumed = persisted_room.model_copy(
                update={"status": RoomStatus.OPEN, "closed_at": None, "updated_at": utcnow()}
            )
            current = persisted_workspace.model_copy(update={"active_room_id": room.id})
            self._store.update_model(resumed)
            self._store.update_model(current)
            self._writer.record(self._event(resumed, EventType.ROOM_RESUMED, "room resumed"))
        return resumed

    def close(self, workspace: Workspace, room: Room) -> Room:
        if room.workspace_id != workspace.id:
            raise RoomLookupError("room does not belong to this workspace")
        with self._store.transaction():
            persisted_room = self._required_room(room.id)
            persisted_workspace = self._required_workspace(workspace.id)
            if persisted_room.status is RoomStatus.CLOSED:
                return persisted_room
            now = utcnow()
            closed = persisted_room.model_copy(
                update={"status": RoomStatus.CLOSED, "closed_at": now, "updated_at": now}
            )
            workspace_update = persisted_workspace.model_copy(
                update={
                    "active_room_id": None
                    if persisted_workspace.active_room_id == room.id
                    else persisted_workspace.active_room_id
                }
            )
            self._store.update_model(closed)
            self._store.update_model(workspace_update)
            self._writer.record(self._event(closed, EventType.ROOM_CLOSED, "room closed"))
        return closed

    def bind(
        self,
        room: Room,
        role: str,
        agent: str,
        known_agents: Collection[str],
    ) -> Room:
        try:
            valid_role = AgentRole(role).value
        except ValueError as exc:
            raise RoomError(f"unknown role '{role}'") from exc
        if agent not in known_agents:
            raise RoomError(f"unknown agent '{agent}' - add it under agents: in relay.yaml")

        with self._store.transaction():
            persisted_room = self._required_room(room.id)
            current = next(
                (member for member in persisted_room.members if member.role == valid_role), None
            )
            if current is not None and current.agent == agent:
                return persisted_room
            members = [member for member in persisted_room.members if member.role != valid_role]
            members.append(RoomMember(role=valid_role, agent=agent))
            members.sort(key=lambda member: member.role)
            rebound = persisted_room.model_copy(update={"members": members, "updated_at": utcnow()})
            action = "added" if current is None else "rebound"
            self._store.update_model(rebound)
            self._writer.record(
                self._event(
                    rebound,
                    EventType.ROOM_SEAT_BOUND,
                    f"seat {valid_role} {action} to {agent}",
                    references=[f"role:{valid_role}", f"agent:{agent}"],
                )
            )
        return rebound

    def _required_room(self, room_id: str) -> Room:
        room = self._store.load_model(Room, room_id)
        if room is None:
            raise RoomLookupError(f"room '{room_id}' does not exist")
        return room

    def _required_workspace(self, workspace_id: str) -> Workspace:
        workspace = self._store.load_model(Workspace, workspace_id)
        if workspace is None:
            raise RoomLookupError(f"workspace '{workspace_id}' does not exist")
        return workspace

    def _available_name(self, workspace_id: str, requested: str) -> str:
        used = {room.name.lower() for room in self.list_for_workspace(workspace_id)}
        if requested.lower() not in used:
            return requested
        suffix = 2
        while f"{requested} ({suffix})".lower() in used:
            suffix += 1
        return f"{requested} ({suffix})"

    @staticmethod
    def _validated_members(
        bindings: Mapping[str, str], known_agents: Collection[str]
    ) -> list[RoomMember]:
        known = set(known_agents)
        members: list[RoomMember] = []
        for role, agent in sorted(bindings.items()):
            try:
                valid_role = AgentRole(role).value
            except ValueError as exc:
                raise RoomError(f"unknown role '{role}'") from exc
            if agent not in known:
                raise RoomError(f"unknown agent '{agent}' - add it under agents: in relay.yaml")
            members.append(RoomMember(role=valid_role, agent=agent))
        return members

    @staticmethod
    def _event(
        room: Room,
        kind: EventType,
        content: str,
        *,
        references: list[str] | None = None,
    ) -> EventLogEntry:
        return EventLogEntry(
            room_id=room.id,
            sender="relay:rooms",
            type=kind,
            content=content,
            references=[f"room:{room.id}", *(references or [])],
        )

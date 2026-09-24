"""Transport-independent Phase 9 operations over the existing Relay ledger."""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from pydantic_core import to_jsonable_python

from relay.agents.factory import RegistryAgentFactory
from relay.agents.registry import get_agent_class
from relay.context import ConfigError, identity_key, load_config, workspace_layout
from relay.context.protocols import load_protocol
from relay.core.bus import ConversationBus
from relay.core.discussion_view import build_discussion_view
from relay.core.evidence import EvidenceKind
from relay.core.orchestrator import advance_task
from relay.core.policy import SqliteCommunicationPolicyGate, policy_from_config
from relay.core.protocol_runner import ProtocolRunner, ProtocolSpec
from relay.core.resolver import role_resolver_from_config, seat_resolver_for_room
from relay.core.room_feed import build_room_feed
from relay.core.room_graph import build_room_graph
from relay.core.rooms import RoomLifecycle
from relay.core.state_machine import TaskState, TaskStateMachine
from relay.harness.capabilities import HarnessCapability
from relay.harness.runtime import HarnessAgent
from relay.storage import connect, migrate
from relay.storage.events import EventLogWriter
from relay.storage.models import (
    Approval,
    ApprovalStatus,
    EventLogEntry,
    EventType,
    EvidenceRecord,
    Message,
    MessageType,
    Task,
    Workspace,
    new_id,
    utcnow,
)
from relay.storage.store import SqliteEvidenceStore, SqliteRelayStore


class OperationError(ValueError):
    """A safe, expected refusal at the application boundary."""

    def __init__(self, message: str, code: str = "invalid_input") -> None:
        super().__init__(message)
        self.code = code


class RelayOperations:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        layout = workspace_layout(self.root)
        if not layout.db_path.is_file():
            raise OperationError("workspace is not initialized", "not_found")
        load_config(self.root)
        self.db_path = layout.db_path
        conn = connect(self.db_path)
        try:
            migrate(conn)
        finally:
            conn.close()

    @contextmanager
    def ledger(self) -> Generator[tuple[SqliteRelayStore, EventLogWriter], None, None]:
        conn = connect(self.db_path)
        try:
            yield SqliteRelayStore(conn), EventLogWriter(conn)
        finally:
            conn.close()

    def _workspace(self, store: SqliteRelayStore) -> Workspace:
        workspace = store.workspace_for_identity(identity_key(self.root))
        if workspace is None:
            raise OperationError("workspace is not initialized", "not_found")
        return workspace

    def _task(self, store: SqliteRelayStore, selector: str) -> Task:
        self._workspace(store)
        tasks = list(store.all_models(Task))
        matches = [task for task in tasks if task.id == selector]
        if not matches:
            matches = [task for task in tasks if task.id.startswith(selector)] if selector else []
        if len(matches) != 1:
            raise OperationError(
                "task ID is ambiguous" if matches else "task does not exist",
                "invalid_input" if matches else "not_found",
            )
        return matches[0]

    def create_task(self, title: str) -> dict[str, Any]:
        title = title.strip()
        if not title or len(title) > 200:
            raise OperationError("title must contain 1 to 200 characters")
        with self.ledger() as (store, writer):
            task = Task(title=title, workspace_id=self._workspace(store).id)
            with store.transaction():
                store.save_model(task)
                writer.record(
                    EventLogEntry(
                        type=EventType.TASK_CREATED,
                        content=f"task created: {title}",
                        task_id=task.id,
                        references=[f"task:{task.id}"],
                    )
                )
            return task.model_dump(mode="json")

    def send_message(
        self,
        *,
        by: str,
        recipient: str,
        content: str,
        room_id: str | None,
        task_id: str | None,
        message_type: MessageType = MessageType.NOTE,
    ) -> dict[str, Any]:
        if not by or ":" in by or any(c.isspace() for c in by):
            raise OperationError("by must be a bare human identity")
        if not content.strip():
            raise OperationError("message content must not be empty")
        config = load_config(self.root)
        with self.ledger() as (store, writer):
            workspace = self._workspace(store)
            room = None
            if room_id is not None:
                room = RoomLifecycle(store, writer).resolve(workspace.id, room_id)
                room_id = room.id
            if task_id is not None:
                task = self._task(store, task_id)
                task_id = task.id
                if room is not None and task.room_id != room.id:
                    raise OperationError("task does not belong to the selected Room")
            if room is None and task_id is None:
                raise OperationError("message needs a Room or task")
            resolver = (
                seat_resolver_for_room(room, config)
                if room is not None
                else role_resolver_from_config(config)
            )
            message = Message(
                sender=f"human:{by}",
                room_id=room_id,
                task_id=task_id,
                type=message_type,
                content=content,
            )
            if recipient.startswith("@"):
                message.recipient_role = recipient[1:]
            else:
                message.recipient = recipient
            saved = ConversationBus(
                store,
                writer,
                resolver,
                SqliteCommunicationPolicyGate(store, policy_from_config(config)),
            ).send(message)
            return saved.model_dump(mode="json")

    def approve(self, task_id: str, by: str) -> dict[str, Any]:
        if not by or ":" in by or any(c.isspace() for c in by):
            raise OperationError("by must be a bare human identity")
        with self.ledger() as (store, writer):
            task = self._task(store, task_id)
            if task.state is not TaskState.APPROVAL_REQUIRED:
                raise OperationError("task is not awaiting approval", "state_refused")
            pending = [
                a
                for a in store.all_models(Approval)
                if a.task_id == task.id and a.status is ApprovalStatus.PENDING
            ]
            if not pending:
                raise OperationError("task has no pending approval", "state_refused")
            approval = pending[0].model_copy(
                update={"status": ApprovalStatus.APPROVED, "decided_by": by, "decided_at": utcnow()}
            )
            evidence = SqliteEvidenceStore(store)
            machine = TaskStateMachine(task_id=task.id, store=evidence, state=task.state)
            advance_task(
                machine,
                store,
                writer,
                task,
                TaskState.DONE,
                evidence_store=evidence,
                updated_approval=approval,
                evidence_records=(
                    EvidenceRecord(
                        kind=EvidenceKind.APPROVAL_GRANTED,
                        task_id=task.id,
                        produced_by=f"human:{by}",
                    ),
                ),
                events=(
                    EventLogEntry(
                        type=EventType.APPROVAL_GRANTED,
                        content=f"task completion approved by human:{by}",
                        task_id=task.id,
                        references=[f"task:{task.id}", f"approval:{approval.id}"],
                    ),
                ),
            )
            updated = store.load_model(Task, task.id)
            if updated is None:
                raise OperationError("task disappeared during approval")
            return updated.model_dump(mode="json")

    def status(self) -> dict[str, Any]:
        with self.ledger() as (store, _):
            workspace = self._workspace(store)
            tasks = list(store.all_models(Task, order_by="created_at DESC, rowid DESC", limit=20))
            return {
                "workspace": workspace.model_dump(mode="json"),
                "tasks": [task.model_dump(mode="json") for task in tasks],
            }

    def room(self, selector: str) -> dict[str, Any]:
        with self.ledger() as (store, writer):
            room = RoomLifecycle(store, writer).resolve(self._workspace(store).id, selector)
            feed = build_room_feed(store, room.id)
            return {
                "room": room.model_dump(mode="json"),
                "feed": [entry.__dict__ for entry in feed],
            }

    def room_graph(self, selector: str) -> dict[str, Any]:
        with self.ledger() as (store, writer):
            room = RoomLifecycle(store, writer).resolve(self._workspace(store).id, selector)
            graph = build_room_graph(store, room.id)
            return {"version": "relay.room.graph.v1", **to_jsonable_python(graph)}

    def agents(self) -> list[dict[str, Any]]:
        config = load_config(self.root)
        rows: list[dict[str, Any]] = []
        for name, cfg in sorted(config.agents.items()):
            cls = get_agent_class(cfg.adapter)
            if cls.backend is not cfg.backend:
                raise ConfigError(f"agent '{name}' has a backend mismatch")
            caps: frozenset[HarnessCapability] = (
                cls.capabilities if issubclass(cls, HarnessAgent) else frozenset()
            )
            rows.append(
                {
                    "name": name,
                    "adapter": cfg.adapter,
                    "backend": cfg.backend.value,
                    "capabilities": sorted(cap.value for cap in caps),
                }
            )
        return rows

    async def start_discussion(self, topic: str, protocol: str | None = None) -> dict[str, Any]:
        if not topic.strip():
            raise OperationError("topic must not be empty")
        from relay.cli.discussions import bundled_debate

        config = load_config(self.root)
        if protocol is None:
            definition = bundled_debate()
        else:
            path = (self.root / protocol).resolve()
            if not path.is_relative_to(self.root) or not path.is_file():
                raise OperationError("protocol must be a file within the workspace")
            definition = load_protocol(path)
        missing = [
            p.role.value for p in definition.participants if p.role.value not in config.roles
        ]
        if missing:
            raise OperationError("missing role bindings: " + ", ".join(missing))
        with self.ledger() as (store, writer):
            workspace = self._workspace(store)
            factory = RegistryAgentFactory(config, self.root)
            service = ProtocolRunner(
                store,
                writer,
                factory,
                factory,
                SqliteCommunicationPolicyGate(store, policy_from_config(config)),
            )
            room_id = new_id()
            execution = service.prepare(ProtocolSpec(definition, new_id(), topic, room_id=room_id))
            bindings = {p.role.value: config.roles[p.role.value] for p in definition.participants}
            RoomLifecycle(store, writer).create(
                workspace,
                topic[:200],
                bindings,
                config.agents.keys(),
                disambiguate_name=True,
                room_id=room_id,
                attached_execution=execution,
            )
            await service.resume(execution.id)
            return build_discussion_view(store, execution)

    def events_after(self, last_sequence: int, limit: int = 100) -> list[dict[str, Any]]:
        with self.ledger() as (store, _):
            events = store.all_models(
                EventLogEntry,
                "WHERE sequence > ?",
                [last_sequence],
                order_by="sequence ASC",
                limit=limit,
            )
            return [event.model_dump(mode="json") for event in events]

"""P7.3 (App. D.3): the human plan freeze that binds execution.

``relay room freeze`` adopts a planner-authored discussion reply into canonical
Room state and binds it to a Room-scoped Task:

* the freeze is the HUMAN's acceptance — a planner can never accept its own
  plan, and ``PLAN_READY → IMPLEMENTING`` happens here, on that explicit act;
* the frozen plan is a Room/Task artifact (``ArtifactKind.PLAN``) whose
  authoring run is the planner's Room exchange run, with honest
  ``PLAN_PRODUCED`` evidence (``agent:<planner>``, App. A.1);
* ``relay continue <task>`` then dispatches the implementer against the frozen
  plan with NO plan-stage run.

The whole freeze commits in ONE ``BEGIN IMMEDIATE``: task, plan artifact, freeze
record, workspace context, ``PLAN_PRODUCED``/``CONTEXT_COLLECTED`` evidence, the
durable build request, the state transitions, and the canonical events. The
locked helpers from :mod:`relay.core.orchestrator` are reused so the persisted
shape is byte-identical to a normal build boundary.

A superseding freeze (``--supersedes``) mints ``PLAN-(n+1)`` plus its freeze
edge on the SAME task and moves the canonical tip — but only at a fail-closed
QUIESCENT ledger position (no in-flight build/delivery/tool runs, no open
blocking signal, next action a fresh dispatch).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from relay.agents.base import AgentRequest, AgentRole
from relay.context.config import AgentConfig, RelayConfig
from relay.core.build_ledger import ContinueRefusal, derive_position
from relay.core.evidence import EvidenceStore
from relay.core.orchestrator import (
    advance_locked,
    mint_context_locked,
    persist_build_request_locked,
)
from relay.core.room_graph import RoomGraphIntegrityError, resolve_room_plan_chain
from relay.core.room_records import (
    FrozenPlanMint,
    FrozenPlanSource,
    RoomRecordRefusal,
    freeze_for_source,
    mint_frozen_plan,
    plan_first_line,
    resolve_freeze_source,
    room_plan_freeze_event,
)
from relay.core.rooms import RoomSeatResolver, require_open_room
from relay.core.state_machine import TaskState, TaskStateMachine
from relay.storage.events import EventLogWriter
from relay.storage.models import Artifact, ArtifactKind, Room, Task, utcnow
from relay.storage.store import SqliteRelayStore

__all__ = ["RoomFreezeOutcome", "freeze_room_plan"]

#: Same implementation-capability contract as ``relay build`` (P3.1 blocker 4):
#: a Room freeze may only bind an implementer that could actually implement.
_WRITE_GRANTS = frozenset({"workspace_write", "workspace_write_network"})


@dataclass(frozen=True)
class RoomFreezeOutcome:
    """What one human freeze produced (or advanced)."""

    room: Room
    task: Task
    plan_artifact: Artifact
    freeze_record: Artifact
    implementer: str
    model: str | None
    superseded_plan_artifact_id: str | None
    source: FrozenPlanSource


def _require(condition: bool, code: str, message: str) -> None:
    if not condition:
        raise RoomRecordRefusal(code, message)


def _resolve_implementer(config: RelayConfig, room: Room) -> tuple[str, str | None]:
    """The Room's ``@implementer`` seat + its configured model, fail-closed."""
    agent_name = RoomSeatResolver(room).resolve_role(AgentRole.IMPLEMENTER.value)
    _require(
        agent_name is not None,
        "no_implementer_seat",
        f"Room '{room.name}' has no @{AgentRole.IMPLEMENTER.value} seat — "
        f"bind one with 'relay room bind {room.name} implementer <agent>'",
    )
    assert agent_name is not None  # narrowed for type checkers
    agent_config: AgentConfig | None = config.agents.get(agent_name)
    _require(
        agent_config is not None,
        "unknown_implementer",
        f"Room seat @{AgentRole.IMPLEMENTER.value} references unknown agent "
        f"'{agent_name}' — add it under agents: in relay.yaml or rebind the seat",
    )
    assert agent_config is not None  # narrowed for type checkers
    _require(
        agent_config.backend.value == "harness",
        "implementer_backend",
        f"agent '{agent_name}' is not harness-backed — a Room freeze binds "
        "implementation and requires a harness implementer",
    )
    grant = agent_config.harness.grant if agent_config.harness is not None else None
    grant_kind = grant.value if grant is not None else None
    # Mirrors ``relay build``'s observable rule at config level: an EXPLICIT
    # insufficient grant refuses here; an unset grant defers to the adapter
    # default, which ``relay continue`` re-checks authoritatively before any
    # process spawns (core cannot consult the adapter registry — App. C.1).
    _require(
        grant_kind is None or grant_kind in _WRITE_GRANTS,
        "implementer_grant",
        f"agent '{agent_name}' cannot implement changes: configure at least "
        f"'workspace_write' (configured grant: {grant_kind or 'none'})",
    )
    return agent_name, agent_config.model


def _freeze_task_title(source: FrozenPlanSource, title: str | None) -> str:
    if title is not None:
        clean = title.strip()
        _require(bool(clean), "empty_title", "the freeze title must not be blank")
        return clean[:200]
    return plan_first_line(source.reply.content)


def _supersede_target(
    store: SqliteRelayStore, evidence: EvidenceStore, room: Room, plan_artifact_id: str
) -> tuple[Artifact, Task]:
    """Validate a supersession target: same-Room tip at a quiescent position."""
    target = store.load_model(Artifact, plan_artifact_id)
    _require(target is not None, "supersede_unknown", f"plan '{plan_artifact_id}' does not exist")
    assert target is not None  # narrowed for type checkers
    _require(
        target.kind is ArtifactKind.PLAN and target.room_id == room.id,
        "supersede_foreign",
        f"plan '{plan_artifact_id}' is not a plan of this Room",
    )
    _require(
        target.task_id is not None,
        "supersede_foreign",
        f"plan '{plan_artifact_id}' is not bound to a task",
    )
    assert target.task_id is not None  # narrowed for type checkers
    task = store.load_model(Task, target.task_id)
    _require(task is not None, "supersede_unknown", "the plan's task does not exist")
    assert task is not None  # narrowed for type checkers
    try:
        chain = resolve_room_plan_chain(store, room.id, task.id)
    except RoomGraphIntegrityError as exc:
        raise RoomRecordRefusal("ledger_inconsistent", str(exc)) from exc
    _require(
        chain.tip.id == target.id,
        "supersede_not_tip",
        f"plan '{plan_artifact_id}' is not the canonical tip of task '{task.id}'",
    )
    try:
        position = derive_position(store, evidence, task.id)
    except ContinueRefusal as exc:
        raise RoomRecordRefusal("ledger_refused", f"{exc.code}: {exc}") from exc
    _require(
        position.task.state is TaskState.IMPLEMENTING,
        "not_quiescent",
        f"task '{task.id}' is at state '{position.task.state.value}' — a superseding "
        "freeze requires a quiescent IMPLEMENTING position",
    )
    in_flight = (
        len(position.in_flight_runs)
        + len(position.in_flight_delivery_runs)
        + len(position.in_flight_tool_runs)
    )
    _require(
        in_flight == 0,
        "not_quiescent",
        f"task '{task.id}' has {in_flight} in-flight run(s) — settle them first",
    )
    _require(
        position.pending_signal is None,
        "not_quiescent",
        f"task '{task.id}' has an unresolved blocking signal — resolve it first",
    )
    _require(
        position.next_action == "dispatch",
        "not_quiescent",
        f"task '{task.id}' next action is '{position.next_action}' — a superseding "
        "freeze requires a fresh dispatch position",
    )
    return target, task


def freeze_room_plan(
    store: SqliteRelayStore,
    writer: EventLogWriter,
    evidence: EvidenceStore,
    config: RelayConfig,
    room: Room,
    *,
    source_message_id: str,
    frozen_by: str,
    workspace_root: Path,
    title: str | None = None,
    supersedes_plan_artifact_id: str | None = None,
    implementer_model: str | None = None,
) -> RoomFreezeOutcome:
    """Freeze one planner-authored plan into canonical Room state (App. D.3).

    Every refusal happens BEFORE the single write transaction: a refused freeze
    leaves the store byte-identical. ``implementer_model`` is the RESOLVED
    implementer model (``resolve_settings`` output) — the caller supplies it so
    ``relay continue``'s pinned-model check matches exactly; core cannot import
    the agents package (App. C.1 import direction).
    """

    root = Path(workspace_root)
    require_open_room(store, room.id)
    source = resolve_freeze_source(store, room, source_message_id)
    _require(
        freeze_for_source(store, room.id, source_message_id) is None,
        "already_frozen",
        f"message '{source_message_id}' is already frozen in this Room",
    )
    implementer, configured_model = _resolve_implementer(config, room)
    model = implementer_model if implementer_model is not None else configured_model

    mint: FrozenPlanMint
    task: Task
    superseded_id: str | None = None
    if supersedes_plan_artifact_id is None:
        task = Task(
            title=_freeze_task_title(source, title),
            room_id=room.id,
            workspace_id=room.workspace_id,
        )
    else:
        target, task = _supersede_target(
            store, evidence, room, supersedes_plan_artifact_id
        )
        superseded_id = target.id

    with store.transaction():
        # The OPEN fence is re-checked inside the write transaction: a
        # concurrent close serializes wholly before or wholly after the freeze.
        require_open_room(store, room.id)
        if supersedes_plan_artifact_id is None:
            store.save_model(task)
        mint = mint_frozen_plan(
            store,
            writer,
            evidence,
            room,
            task,
            source,
            frozen_by=frozen_by,
            supersedes_plan_artifact_id=superseded_id,
        )
        if supersedes_plan_artifact_id is None:
            mint_context_locked(store, writer, evidence, task, root)
            persist_build_request_locked(
                store,
                writer,
                task,
                AgentRequest(
                    prompt=_request_prompt(source),
                    role=AgentRole.IMPLEMENTER,
                    task_id=task.id,
                ),
                implementer=implementer,
                model=model,
            )
            machine = TaskStateMachine(task_id=task.id, store=evidence)
            task = advance_locked(machine, store, writer, task, TaskState.CONTEXT_READY)
            task = advance_locked(machine, store, writer, task, TaskState.PLAN_READY)
            task = advance_locked(machine, store, writer, task, TaskState.IMPLEMENTING)
            room = room.model_copy(
                update={"active_task_id": task.id, "updated_at": utcnow()}
            )
            store.update_model(room)
        writer.record(
            room_plan_freeze_event(room, task, mint, source, frozen_by=frozen_by)
        )

    return RoomFreezeOutcome(
        room=room,
        task=task,
        plan_artifact=mint.plan_artifact,
        freeze_record=mint.freeze_record,
        implementer=implementer,
        model=model,
        superseded_plan_artifact_id=superseded_id,
        source=source,
    )


def _request_prompt(source: FrozenPlanSource) -> str:
    """The durable build request's prompt: the human's Room request, else the plan."""
    if source.parent.sender.startswith("human:"):
        return source.parent.content
    return source.reply.content

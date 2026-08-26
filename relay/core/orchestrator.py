"""Crash-safe single-agent run orchestration (SPEC §5, §25, App. B.1).

Two-phase persistence, in this exact order:

1. **Tx 1 — commit before provider I/O.** The ``Run(RUNNING)`` row, its
   ``run_input`` artifact (the prompt), and the ``AGENT_RUN_STARTED`` event
   commit atomically *before* the adapter is invoked. The prompt survives
   crashes, timeouts, and failures by construction.
2. **Provider call** — strictly after Tx 1. Nothing here is persisted.
3. **Final Tx — success or failure.** Success records the ``run_output``
   artifact plus ``SUCCEEDED``/``AGENT_RUN_FINISHED``; failure records
   ``FAILED``/``AGENT_RUN_FINISHED`` with the error text in the event content
   and no output artifact.

Family-blind by App. B.2: API- and harness-backed adapters flow through this
exact code. No ``Message`` rows and no ``Task`` — a one-shot ask is not
conversation-bus traffic (App. A.2/B.1).
"""

from __future__ import annotations

from dataclasses import dataclass

from relay.agents.base import Agent, AgentRequest, AgentResponse
from relay.storage.events import EventLogWriter
from relay.storage.models import (
    Artifact,
    ArtifactKind,
    EventLogEntry,
    EventType,
    Run,
    RunStatus,
    utcnow,
)
from relay.storage.store import SqliteRelayStore


@dataclass(frozen=True)
class AskOutcome:
    """Everything one ``ask`` produced, persisted and returned."""

    run: Run
    response: AgentResponse | None = None
    error: str | None = None


async def run_ask(
    store: SqliteRelayStore,
    writer: EventLogWriter,
    agent: Agent,
    request: AgentRequest,
    *,
    model: str | None = None,
    agent_name: str | None = None,
) -> AskOutcome:
    """Execute one agent run with crash-safe persistence; never raises.

    Any provider failure (network, auth, adapter bug) lands as a FAILED run
    with the error in the finished event — the caller cannot lose history by
    throwing.
    """
    run = Run(
        agent=agent_name or agent.name,
        role=request.role,
        model=model,
        status=RunStatus.RUNNING,
    )

    with store.transaction():  # Tx 1 — committed before any provider I/O
        store.save_model(run)
        input_artifact = store.save_model(
            Artifact(kind=ArtifactKind.RUN_INPUT, run_id=run.id, content=request.prompt)
        )
        writer.record(
            EventLogEntry(
                type=EventType.AGENT_RUN_STARTED,
                content=f"agent '{run.agent}' started",
                references=[f"run:{run.id}", f"artifact:{input_artifact.id}"],
            )
        )

    try:
        response = await agent.run(request)
    except Exception as exc:  # noqa: BLE001 - deliberate: every provider failure (network, auth,
        # adapter bug) must land as a persisted FAILED run, never as lost history.
        failed = run.model_copy(update={"status": RunStatus.FAILED, "ended_at": utcnow()})
        with store.transaction():  # failure Tx
            store.update_model(failed)
            writer.record(
                EventLogEntry(
                    type=EventType.AGENT_RUN_FINISHED,
                    content=f"agent '{run.agent}' failed: {exc}",
                    references=[f"run:{run.id}"],
                )
            )
        return AskOutcome(run=failed, error=str(exc))

    usage = response.usage
    succeeded = run.model_copy(
        update={
            "status": RunStatus.SUCCEEDED,
            "input_size": usage.input_tokens if usage else None,
            "output_size": usage.output_tokens if usage else None,
            "cost_usd": usage.cost_usd if usage else None,
            "ended_at": utcnow(),
        }
    )
    with store.transaction():  # success Tx
        store.update_model(succeeded)
        output_artifact = store.save_model(
            Artifact(kind=ArtifactKind.RUN_OUTPUT, run_id=run.id, content=response.output)
        )
        writer.record(
            EventLogEntry(
                type=EventType.AGENT_RUN_FINISHED,
                content=f"agent '{run.agent}' succeeded",
                references=[f"run:{run.id}", f"artifact:{output_artifact.id}"],
            )
        )

    return AskOutcome(run=succeeded, response=response)

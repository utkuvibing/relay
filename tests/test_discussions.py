"""Offline P5.4 CLI, outcome history, and inspection acceptance tests."""

import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest
import yaml
from test_cli import _completion, _swap_transport
from test_protocol_runner import Factory
from typer.testing import CliRunner

from relay.cli.discussions import bundled_debate
from relay.cli.main import app
from relay.context import workspace_layout
from relay.context.protocols import load_protocol
from relay.core.discussion_view import build_discussion_view
from relay.core.policy import (
    CommunicationBudgets,
    CommunicationPolicy,
    SqliteCommunicationPolicyGate,
)
from relay.core.protocol_outcomes import ProtocolOutcome, outcome_events
from relay.core.protocol_runner import ProtocolRunner, ProtocolSpec
from relay.storage import connect
from relay.storage.events import EventLogWriter
from relay.storage.models import ProtocolExecution, Room
from relay.storage.store import SqliteRelayStore

cli = CliRunner()


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-discussion-secret")
    monkeypatch.delenv("RELAY_API_KEY_ENV", raising=False)
    calls = []

    def handler(request):
        import httpx

        calls.append(request)
        return httpx.Response(200, json=_completion("[bold]literal output[/bold]"))

    _swap_transport(monkeypatch, handler)
    assert cli.invoke(app, ["init"]).exit_code == 0
    config = {
        "agents": {"gpt": {"backend": "api", "adapter": "openai", "model": "offline"}},
        "roles": {
            role: "gpt"
            for role in (
                "architect",
                "critic",
                "repository_expert",
                "moderator",
            )
        },
    }
    (tmp_path / "relay.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    return tmp_path, config, calls


def configure(workspace, turns):
    root, config, _ = workspace
    config["communication"] = {"budgets": {"max_agent_turns": turns}}
    (root / "relay.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")


def open_store(workspace):
    return SqliteRelayStore(connect(workspace_layout(workspace[0]).db_path))


def invoke(*args, code=0):
    result = cli.invoke(app, [*args, "--json"])
    assert result.exit_code == code, (result.output, result.exception)
    return json.loads(result.stdout)


def test_default_budget_resume_and_read_only_inspection(workspace, monkeypatch):
    first = invoke("discuss", "Compare designs", code=1)
    eid = first["execution_id"]
    assert len(first["outputs"]) == len(workspace[2]) == 16
    assert first["escalation"]["budget_scope"] == "aggregate"
    assert first["last_observation"]["stale"] is False
    again = invoke("discuss", "--resume", eid[:8], code=1)
    assert len(again["observations"]) == 1
    configure(workspace, 22)
    complete = invoke("discuss", "--resume", eid)
    assert complete["progress"]["status"] == "complete"
    assert complete["progress"]["synthesis_message_ids"]
    assert len(complete["outputs"]) == len(workspace[2]) == 22
    assert len(complete["observations"]) == 2
    assert complete["escalation"] is None
    assert invoke("discuss", "--resume", eid) == complete

    def forbidden(*args, **kwargs):
        pytest.fail("inspection must not resume")

    monkeypatch.setattr(ProtocolRunner, "resume", forbidden)
    inspected = invoke("inspect-discussion", eid[:8])
    assert inspected == complete
    human = cli.invoke(app, ["inspect-discussion", eid])
    assert "[bold]literal output[/bold]" in human.stdout
    status = cli.invoke(app, ["status"])
    assert status.exit_code == 0, status.exception
    assert eid in status.stdout
    store = open_store(workspace)
    try:
        execution = store.load_model(ProtocolExecution, eid)
        room = store.load_model(Room, execution.room_id)
        assert room.workspace_id and len(room.members) == 4 and execution.task_id is None
        for table in ("tasks", "approvals", "decisions", "evidence_records"):
            assert store.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
        before = store.conn.total_changes
        store.conn.set_authorizer(
            lambda action, *args: (
                sqlite3.SQLITE_DENY
                if action
                in (
                    sqlite3.SQLITE_INSERT,
                    sqlite3.SQLITE_UPDATE,
                    sqlite3.SQLITE_DELETE,
                )
                else sqlite3.SQLITE_OK
            )
        )
        assert build_discussion_view(store, execution) == complete
        assert store.conn.total_changes == before
    finally:
        store.conn.close()


def test_custom_protocol_source_independent_resume_and_drift(workspace):
    root, config, calls = workspace
    source = root / "custom.yaml"
    data = yaml.safe_load(
        Path(__file__).resolve().parents[1].joinpath("protocols/debate.yaml").read_text()
    )
    data.pop("repeat")
    source.write_text(yaml.safe_dump(data))
    result = invoke("discuss", "Custom", "--protocol", str(source))
    assert len(calls) == 10
    source.unlink()
    eid = result["execution_id"]
    config["agents"]["gpt"]["model"] = "changed"
    (root / "relay.yaml").write_text(yaml.safe_dump(config))
    drift = invoke("discuss", "--resume", eid, code=1)
    assert drift["escalation"]["refusal_code"] == "configuration_drift"
    assert "Restore" in drift["next_action"]
    assert len(calls) == 10
    config["agents"]["gpt"]["model"] = "offline"
    (root / "relay.yaml").write_text(yaml.safe_dump(config))
    assert invoke("discuss", "--resume", eid)["progress"]["status"] == "complete"
    assert len(calls) == 10


@pytest.mark.parametrize(
    "args",
    [
        ["discuss"],
        ["discuss", ""],
        ["discuss", "topic", "--resume", "x"],
        ["discuss", "--resume", "x", "--protocol", "a.yaml"],
    ],
)
def test_invalid_combinations_are_json_usage_errors(workspace, args):
    result = invoke(*args, code=2)
    assert result["error"]["code"] == "usage" and result["execution_id"] is None
    assert not workspace[2]


@pytest.mark.parametrize("args", [
    ["inspect-discussion"], ["discuss", "topic", "--unknown"], ["discuss", "--resume"],
])
def test_parser_errors_keep_json_envelope(workspace, args):
    result = cli.invoke(app, [args[0], "--json", *args[1:]])
    assert result.exit_code == 2
    assert json.loads(result.stdout)["error"]["code"] == "usage"
    assert not workspace[2]


def test_invalid_inputs_do_not_create_rooms_or_leak_config(workspace):
    root, config, calls = workspace
    config["roles"].pop("critic")
    (root / "relay.yaml").write_text(yaml.safe_dump(config))
    result = invoke("discuss", "Missing role", code=1)
    assert "critic" in result["error"]["message"] and "roles:" in result["error"]["message"]
    bad = root / "bad.yaml"
    bad.write_text("secret: sk-do-not-print\n")
    result = invoke("discuss", "Bad", "--protocol", str(bad), code=1)
    assert "sk-do-not-print" not in json.dumps(result)
    store = open_store(workspace)
    try:
        assert not list(store.all_models(Room))
        assert not list(store.all_models(ProtocolExecution))
    finally:
        store.conn.close()
    assert not calls


def test_room_and_execution_creation_is_atomic(workspace, monkeypatch):
    original = SqliteRelayStore.save_model

    def fail_execution(self, model):
        if isinstance(model, ProtocolExecution):
            raise TypeError("simulated insertion failure")
        return original(self, model)

    monkeypatch.setattr(SqliteRelayStore, "save_model", fail_execution)
    invoke("discuss", "Atomic", code=1)
    store = open_store(workspace)
    try:
        assert not list(store.all_models(Room))
    finally:
        store.conn.close()
    assert not workspace[2]


def test_interruption_is_discoverable_and_prefixes_are_literal(workspace, monkeypatch):
    async def crash(*args):
        raise KeyboardInterrupt

    monkeypatch.setattr(ProtocolRunner, "resume", crash)
    cli.invoke(app, ["discuss", "Interrupted"])
    store = open_store(workspace)
    try:
        execution = next(store.all_models(ProtocolExecution))
        store.save_model(execution.model_copy(update={"id": "prefix-one", "execution_key": "one"}))
        store.save_model(execution.model_copy(update={"id": "prefix-two", "execution_key": "two"}))
    finally:
        store.conn.close()
    view = invoke("inspect-discussion", execution.id)
    assert view["last_observation"] is None and not view["outputs"]
    assert "resume" in view["next_action"]
    assert "Ambiguous" in invoke("inspect-discussion", "prefix-", code=1)["error"]["message"]
    assert "does not exist" in invoke("discuss", "--resume", "%", code=1)["error"]["message"]
    assert execution.id in cli.invoke(app, ["status"]).stdout


def test_packaged_debate_matches_example():
    assert bundled_debate() == load_protocol(
        Path(__file__).resolve().parents[1] / "protocols/debate.yaml"
    )


def test_configured_debate_completes_and_failure_is_not_retried(workspace, monkeypatch):
    configure(workspace, 22)
    assert invoke("discuss", "Full debate")["progress"]["status"] == "complete"
    assert len(workspace[2]) == 22

    def failed(request):
        import httpx

        workspace[2].append(request)
        return httpx.Response(400, json={"error": {"message": "sk-private-provider-error"}})

    _swap_transport(monkeypatch, failed)
    result = invoke("discuss", "Failure", code=1)
    assert result["last_observation"]["stop_reason"] == "request_failed"
    assert "sk-private-provider-error" not in json.dumps(result)
    assert len(workspace[2]) == 23
    assert invoke("discuss", "--resume", result["execution_id"], code=1) == result
    assert len(workspace[2]) == 23


def test_pending_cli_exit_and_outcome(workspace, monkeypatch):
    from relay.core.protocol_runner import ProtocolResult, ProtocolStopReason

    async def pending(self, execution, definition):
        return ProtocolResult(execution.id, ProtocolStopReason.DELIVERY_PENDING)

    monkeypatch.setattr(ProtocolRunner, "_run", pending)
    result = invoke("discuss", "Pending", code=3)
    assert result["last_observation"]["stop_reason"] == "delivery_pending"
    assert result["escalation"] is None and not workspace[2]


@pytest.mark.parametrize(
    "reason", ["policy_refused", "input_refused", "request_failed", "delivery_pending"]
)
async def test_runtime_outcomes_are_safe_and_deduplicated(workspace, monkeypatch, reason):
    from relay.core.policy import EdgeNotPermitted
    from relay.core.protocol_runner import ProtocolResult, ProtocolStopReason

    store = open_store(workspace)
    factory = Factory()
    service = ProtocolRunner(
        store,
        EventLogWriter(store.conn),
        factory,
        factory,
        SqliteCommunicationPolicyGate(store, CommunicationPolicy(CommunicationBudgets(22, 0))),
    )
    room = store.save_model(Room(name="Runtime"))
    execution = service.prepare(ProtocolSpec(bundled_debate(), "runtime", "Topic", room_id=room.id))
    store.save_model(execution)

    async def stopped(*args):
        return ProtocolResult(
            execution.id, ProtocolStopReason(reason), refusal=EdgeNotPermitted("secret")
        )

    monkeypatch.setattr(service, "_resume", stopped)
    try:
        await service.resume(execution.id)
        await service.resume(execution.id)
        events = outcome_events(store, execution.id)
        assert len(events) == 1
        assert "secret" not in events[0].content
        observation = ProtocolOutcome.model_validate_json(events[0].content)
        assert observation.needs_human == (reason != "delivery_pending")
    finally:
        store.conn.close()


async def test_stage_budget_escalation_and_stale_observation(workspace):
    store = open_store(workspace)
    factory = Factory()
    definition = bundled_debate()
    first = definition.stages[0]
    definition = replace(
        definition,
        stages=(
            replace(first, budgets=replace(first.budgets, max_agent_turns=1)),
            *definition.stages[1:],
        ),
    )
    service = ProtocolRunner(
        store,
        EventLogWriter(store.conn),
        factory,
        factory,
        SqliteCommunicationPolicyGate(store, CommunicationPolicy(CommunicationBudgets(22, 0))),
    )
    room = store.save_model(Room(name="Stage budget"))
    try:
        result = await service.start(ProtocolSpec(definition, "stage", "Topic", room_id=room.id))
        execution = store.load_model(ProtocolExecution, result.execution_id)
        view = build_discussion_view(store, execution)
        assert view["escalation"]["budget_scope"] == "stage"
        assert "new discussion" in view["next_action"]
        from relay.storage.models import EventLogEntry, EventType

        EventLogWriter(store.conn).record(
            EventLogEntry(
                type=EventType.AGENT_RUN_FINISHED,
                room_id=room.id,
                content="later ledger activity",
            )
        )
        assert build_discussion_view(store, execution)["last_observation"]["stale"] is True
    finally:
        store.conn.close()

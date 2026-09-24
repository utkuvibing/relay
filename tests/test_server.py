"""Phase 9 HTTP and event-stream behavior against a real temporary ledger."""

from __future__ import annotations

import os
import subprocess
import sys

from fastapi.testclient import TestClient
from typer.testing import CliRunner

from relay.cli.main import app as cli_app
from relay.core.permissions import Action
from relay.core.state_machine import TaskState
from relay.server import create_app
from relay.server.operations import RelayOperations
from relay.storage import connect
from relay.storage.models import Approval, EvidenceRecord, Message, Task
from relay.storage.store import SqliteRelayStore


def _client(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli_app, ["init"])
    assert result.exit_code == 0, result.output
    return TestClient(create_app(tmp_path, token="test-token"))


def _auth():
    return {"Authorization": "Bearer test-token"}


def test_rest_and_websocket_require_token(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    assert client.get("/v1/status").status_code == 401
    assert client.get("/v1/status", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.post("/v1/tasks", json={"title": "forbidden"}).status_code == 401
    assert client.post("/v1/tasks", content="{", headers={"Content-Type": "application/json"}).status_code == 401
    from starlette.websockets import WebSocketDisconnect

    try:
        with client.websocket_connect("/v1/events/ws"):
            raise AssertionError("unauthenticated WebSocket was accepted")
    except WebSocketDisconnect as exc:
        assert exc.code == 1008


def test_draft_task_and_replay_cursor_allow_gaps(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    created = client.post("/v1/tasks", headers=_auth(), json={"title": "draft"})
    assert created.status_code == 201, created.text
    assert created.json()["state"] == "created"
    first = client.get("/v1/events", headers=_auth()).json()[-1]
    assert first["type"] == "task_created"
    db = connect(tmp_path / ".relay" / "relay.sqlite3")
    try:
        db.execute("UPDATE sqlite_sequence SET seq = 10 WHERE name = 'event_log'")
    finally:
        db.close()
    RelayOperations(tmp_path).create_task("after gap")
    replay = client.get(f"/v1/events?last_sequence={first['sequence']}", headers=_auth()).json()
    assert [event["sequence"] for event in replay] == [11]
    with client.websocket_connect(
        f"/v1/events/ws?last_sequence={first['sequence']}", headers=_auth()
    ) as ws:
        assert ws.receive_json()["sequence"] == 11


def test_websocket_sees_separate_cli_process(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    environment = os.environ.copy()
    environment.pop("RELAY_SERVER_URL", None)
    with client.websocket_connect("/v1/events/ws?last_sequence=0", headers=_auth()) as ws:
        result = subprocess.run(
            [sys.executable, "-m", "relay.cli.main", "task-create", "from cli"],
            cwd=tmp_path,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr + result.stdout
        event = ws.receive_json()
        assert event["type"] == "task_created"


def test_message_persists_without_delivery_and_refusals_leave_ledger_clean(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    task = client.post("/v1/tasks", headers=_auth(), json={"title": "draft"}).json()
    request = {
        "by": "utku",
        "recipient": "gpt",
        "task_id": task["id"],
        "content": "Please check",
        "type": "note",
    }
    sent = client.post("/v1/messages", headers=_auth(), json=request)
    assert sent.status_code == 201, sent.text
    assert sent.json()["sender"] == "human:utku"
    denied = client.post("/v1/messages", headers=_auth(), json={**request, "recipient": "@missing"})
    assert denied.status_code == 409
    assert (
        client.post(
            f"/v1/tasks/{task['id']}/approve", headers=_auth(), json={"by": "utku"}
        ).status_code
        == 409
    )
    db = connect(tmp_path / ".relay" / "relay.sqlite3")
    try:
        store = SqliteRelayStore(db)
        assert len(list(store.all_models(Message))) == 1
        assert len(list(store.all_models(Task))) == 1
        assert store.load_model(Task, task["id"]).state.value == "created"
    finally:
        db.close()


def test_room_query_agents_and_discussion_refusal(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    (tmp_path / "relay.yaml").write_text(
        "agents:\n  gpt: {backend: api, adapter: openai, model: test}\nroles:\n  architect: gpt\n",
        encoding="utf-8",
    )
    created = CliRunner().invoke(cli_app, ["room", "create", "design"])
    assert created.exit_code == 0, created.output
    room = client.get("/v1/rooms/design", headers=_auth())
    assert room.status_code == 200, room.text
    assert room.json()["room"]["name"] == "design"
    graph = client.get("/v1/rooms/design/graph", headers=_auth())
    assert graph.status_code == 200, graph.text
    assert graph.json()["version"] == "relay.room.graph.v1"
    agents = client.get("/v1/agents", headers=_auth())
    assert agents.json() == [
        {"name": "gpt", "adapter": "openai", "backend": "api", "capabilities": []}
    ]
    discussion = client.post("/v1/discussions", headers=_auth(), json={"topic": "debate"})
    assert discussion.status_code == 409  # bundled debate needs more role bindings


def test_remote_approval_uses_state_machine_and_records_human_evidence(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    draft = client.post("/v1/tasks", headers=_auth(), json={"title": "ready"}).json()
    db = connect(tmp_path / ".relay" / "relay.sqlite3")
    try:
        store = SqliteRelayStore(db)
        task = store.load_model(Task, draft["id"])
        assert task is not None
        store.update_model(task.model_copy(update={"state": TaskState.APPROVAL_REQUIRED}))
        store.save_model(Approval(action=Action.EDIT_FILES, task_id=task.id))
    finally:
        db.close()

    response = client.post(f"/v1/tasks/{draft['id']}/approve", headers=_auth(), json={"by": "utku"})
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "done"
    db = connect(tmp_path / ".relay" / "relay.sqlite3")
    try:
        store = SqliteRelayStore(db)
        approvals = list(store.all_models(Approval))
        assert approvals[0].status.value == "approved"
        assert approvals[0].decided_by == "utku"
        evidence = list(store.all_models(EvidenceRecord))
        assert any(
            e.kind.value == "approval_granted" and e.produced_by == "human:utku" for e in evidence
        )
    finally:
        db.close()


def test_cli_server_mode_uses_rest_and_keeps_local_ledger(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    from relay.cli import server_client

    monkeypatch.setenv("RELAY_SERVER_URL", "http://127.0.0.1:8765")
    monkeypatch.setenv("RELAY_SERVER_TOKEN", "test-token")
    monkeypatch.setattr(
        server_client.httpx,
        "request",
        lambda method, url, **kwargs: client.request(
            method,
            url.removeprefix("http://127.0.0.1:8765"),
            headers=kwargs.get("headers"),
            json=kwargs.get("json"),
        ),
    )
    created = CliRunner().invoke(cli_app, ["task-create", "from remote cli"])
    assert created.exit_code == 0, created.output
    status = CliRunner().invoke(cli_app, ["status"])
    assert status.exit_code == 0, status.output
    assert "from remote cli" in status.output

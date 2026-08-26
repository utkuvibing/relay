"""Regression tests for the Phase 1 closeout hardening pass."""

import asyncio
import sqlite3

import httpx
import pytest

import relay.storage.db as db_module
from relay.agents.base import Agent, AgentRequest, AgentRole
from relay.agents.config import AgentSettings
from relay.agents.errors import AgentError
from relay.agents.openai import OpenAICompatibleAgent
from relay.context import ConfigError, load_config
from relay.core.orchestrator import run_ask
from relay.storage import connect, migrate
from relay.storage.events import EventLogWriter
from relay.storage.models import EventType, RunStatus
from relay.storage.store import SqliteRelayStore


API_KEY = "sk-closeout-secret"


def test_relay_yaml_rejects_unknown_secret_field(tmp_path):
    (tmp_path / "relay.yaml").write_text(
        "agents:\n"
        "  gpt:\n"
        "    backend: api\n"
        "    adapter: openai\n"
        "    api_key: should-never-be-here\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="api_key"):
        load_config(tmp_path)


def test_provider_error_body_is_not_exposed(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", API_KEY)
    leaked = "provider-body-secret"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text=f"internal failure {leaked}")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    agent = OpenAICompatibleAgent(AgentSettings(adapter="openai"), client=client)
    try:
        with pytest.raises(AgentError) as caught:
            asyncio.run(agent.run(AgentRequest(prompt="p", role=AgentRole.RESEARCHER)))
        assert leaked not in str(caught.value)
        assert "500" in str(caught.value)
        assert not client.is_closed
    finally:
        asyncio.run(client.aclose())


def test_unexpected_agent_exception_is_sanitized_before_persistence(tmp_path):
    secret = "runtime-secret-must-not-persist"

    class BuggyAgent(Agent):
        name = "buggy"

        async def run(self, request):
            raise ValueError(secret)

    conn = connect(tmp_path / "relay.sqlite3")
    migrate(conn)
    try:
        store = SqliteRelayStore(conn)
        writer = EventLogWriter(conn)
        outcome = asyncio.run(
            run_ask(
                store,
                writer,
                BuggyAgent(),
                AgentRequest(prompt="safe prompt", role=AgentRole.RESEARCHER),
            )
        )
        assert outcome.run.status is RunStatus.FAILED
        assert outcome.error == "unexpected agent failure (ValueError)"
        finished = [e for e in writer.all() if e.type is EventType.AGENT_RUN_FINISHED]
        assert len(finished) == 1
        assert secret not in finished[0].content
        assert secret not in outcome.error
    finally:
        conn.close()


def test_migration_version_is_atomic_on_statement_failure(tmp_path, monkeypatch):
    broken = (
        "CREATE TABLE partial_table (id INTEGER PRIMARY KEY)",
        "THIS IS NOT VALID SQL",
    )
    monkeypatch.setitem(db_module._MIGRATIONS, 1, broken)

    conn = connect(tmp_path / "relay.sqlite3")
    try:
        with pytest.raises(sqlite3.DatabaseError):
            migrate(conn)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='partial_table'"
        ).fetchone()
        assert exists is None
    finally:
        conn.close()

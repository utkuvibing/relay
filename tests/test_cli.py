"""Phase 1 exit gate: offline end-to-end CLI tests (SPEC §27 Phase 1).

Offline by construction: the OpenAI adapter's HTTP client is swapped for an
``httpx.MockTransport`` inside ``relay.agents.openai``'s namespace, so the
full stack — Typer CLI → config → orchestrator → adapter → wire protocol —
runs without any network. Plain pytest never makes paid/network calls.

Covered contracts:

* init → ask → exit 0, SUCCEEDED run, both run I/O artifacts, strictly
  increasing lifecycle sequences, read-your-writes after reopen.
* Crash path: provider failure after Tx 1 ⇒ FAILED run whose prompt stays
  recoverable from the run_input artifact (B.1).
* Init idempotence: one Workspace row, same id, history preserved.
* Harness entries refuse with the Phase 2 pointer; never silently ignored.
* Secret hygiene: the key never lands in DB bytes or CLI output (App. B.3).
"""

import itertools
import json
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

import relay.agents.openai as openai_mod
from relay.cli.main import app
from relay.storage import connect
from relay.storage.events import EventLogWriter
from relay.storage.models import (
    ArtifactKind,
    EventType,
    Run,
    RunStatus,
    Workspace,
)
from relay.storage.store import SqliteRelayStore

runner = CliRunner()

E2E_KEY = "sk-e2e-secret-that-must-never-persist"


def _completion(content: str = "the analysis") -> dict:
    return {
        "id": "chatcmpl-e2e",
        "object": "chat.completion",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": 7, "completion_tokens": 21},
    }


def _swap_transport(monkeypatch, handler) -> None:
    """Redirect the adapter's HTTP client onto a MockTransport (offline)."""

    def factory(*args, **kwargs) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    class _Surrogate:
        AsyncClient = factory
        TimeoutException = httpx.TimeoutException
        ConnectError = httpx.ConnectError
        HTTPError = httpx.HTTPError

    monkeypatch.setattr(openai_mod, "httpx", _Surrogate)


def _patched_invoke(args: list[str], handler) -> Any:
    """Invoke the CLI with the adapter's HTTP swapped for MockTransport."""
    patch = pytest.MonkeyPatch()
    _swap_transport(patch, handler)
    try:
        return runner.invoke(app, args)
    finally:
        patch.undo()


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", E2E_KEY)
    return tmp_path


@pytest.fixture()
def db(workspace):
    return workspace / ".relay" / "relay.sqlite3"


def _open_store(db_path):
    conn = connect(db_path)
    store = SqliteRelayStore(conn)
    return conn, store


class TestOfflineE2E:
    def test_init_then_ask_success_persists_everything(self, workspace, db):
        assert runner.invoke(app, ["init"]).exit_code == 0
        assert (workspace / ".relay" / "profile.yaml").is_file()
        assert (workspace / "relay.yaml").is_file()

        result = _patched_invoke(
            ["ask", "gpt", "Analyze this repository"],
            lambda request: httpx.Response(200, json=_completion("repo analysis done")),
        )

        assert result.exit_code == 0, result.output
        assert "repo analysis done" in result.output

        conn, store = _open_store(db)
        try:
            runs = list(store.all_models(Run))
            assert len(runs) == 1
            run = runs[0]
            assert run.status is RunStatus.SUCCEEDED
            assert run.agent == "openai"
            assert run.input_size == 7 and run.output_size == 21
            assert run.cost_usd is None  # no pricing table in Phase 1
            assert run.ended_at is not None

            artifacts = store.artifacts_for_run(run.id)
            by_kind = {artifact.kind: artifact for artifact in artifacts}
            assert set(by_kind) == {ArtifactKind.RUN_INPUT, ArtifactKind.RUN_OUTPUT}
            assert by_kind[ArtifactKind.RUN_INPUT].content == "Analyze this repository"
            assert by_kind[ArtifactKind.RUN_OUTPUT].content == "repo analysis done"

            events = EventLogWriter(conn).all()
            assert [event.type for event in events] == [
                EventType.AGENT_RUN_STARTED,
                EventType.AGENT_RUN_FINISHED,
            ]
            sequences = [event.sequence for event in events]
            assert all(b > a for a, b in itertools.pairwise(sequences))
            assert f"run:{run.id}" in events[0].references
            assert f"artifact:{by_kind[ArtifactKind.RUN_OUTPUT].id}" in events[1].references
        finally:
            conn.close()

    def test_ask_without_init_is_actionable(self, workspace):
        result = runner.invoke(app, ["ask", "gpt", "hi"])
        assert result.exit_code == 1
        assert "relay init" in result.output

    def test_role_and_model_flags_reach_the_run(self, workspace, db):
        runner.invoke(app, ["init"])
        result = _patched_invoke(
            ["ask", "gpt", "plan this", "--role", "planner", "--model", "gpt-4o"],
            lambda request: httpx.Response(200, json=_completion("ok")),
        )
        assert result.exit_code == 0, result.output
        conn, store = _open_store(db)
        try:
            run = next(store.all_models(Run))
            assert run.role == "planner"
            assert run.model == "gpt-4o"
        finally:
            conn.close()


class TestCrashPath:
    def test_provider_failure_persists_failed_run_with_recoverable_prompt(
        self, workspace, db
    ):
        runner.invoke(app, ["init"])

        def boom(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("provider hung", request=request)

        result = _patched_invoke(["ask", "gpt", "fragile prompt"], boom)

        assert result.exit_code == 1
        assert "timed out" in result.output

        conn, store = _open_store(db)
        try:
            run = next(store.all_models(Run))
            assert run.status is RunStatus.FAILED
            assert run.ended_at is not None

            # Tx 1 won: the prompt survives the crash by construction (B.1).
            artifacts = store.artifacts_for_run(run.id)
            assert [a.kind for a in artifacts] == [ArtifactKind.RUN_INPUT]
            assert artifacts[0].content == "fragile prompt"

            events = EventLogWriter(conn).all()
            assert [event.type for event in events] == [
                EventType.AGENT_RUN_STARTED,
                EventType.AGENT_RUN_FINISHED,
            ]
            assert "failed" in events[1].content
            assert "timed out" in events[1].content
        finally:
            conn.close()


class TestInitIdempotenceCLI:
    def test_reinit_keeps_id_and_history(self, workspace, db):
        assert runner.invoke(app, ["init"]).exit_code == 0
        conn, store = _open_store(db)
        try:
            first_id = next(store.all_models(Workspace)).id
        finally:
            conn.close()

        result = _patched_invoke(
            ["ask", "gpt", "first run"],
            lambda request: httpx.Response(200, json=_completion("x")),
        )
        assert result.exit_code == 0

        assert runner.invoke(app, ["init"]).exit_code == 0  # re-init
        conn, store = _open_store(db)
        try:
            rows = list(store.all_models(Workspace))
            assert len(rows) == 1  # exactly one row, always
            assert rows[0].id == first_id  # same identity, history preserved
            assert len(list(store.all_models(Run))) == 1  # the ask survived re-init
        finally:
            conn.close()


class TestHarnessRefusal:
    def test_harness_agent_errors_with_phase2_pointer(self, workspace):
        (workspace / "relay.yaml").write_text(
            "agents:\n  codex: {backend: harness, adapter: codex_cli}\n",
            encoding="utf-8",
        )
        runner.invoke(app, ["init"])
        result = runner.invoke(app, ["ask", "codex", "make a change"])
        assert result.exit_code == 1
        # rich wraps long lines, so assert on the wording, not a contiguous string.
        assert "harness-backed" in result.output
        assert "Phase 2" in result.output
        assert "codex_cli" in result.output

    def test_unknown_agent_lists_knowns(self, workspace):
        runner.invoke(app, ["init"])
        result = runner.invoke(app, ["ask", "claude", "hi"])
        assert result.exit_code == 1
        assert "gpt" in result.output


class TestSecretHygiene:
    def test_key_never_in_db_bytes_or_cli_output(self, workspace, db):
        runner.invoke(app, ["init"])
        result = _patched_invoke(
            ["ask", "gpt", "secret question"],
            lambda request: httpx.Response(200, json=_completion("classified")),
        )
        assert result.exit_code == 0

        status_result = runner.invoke(app, ["status"])
        assert E2E_KEY not in status_result.output
        assert "configured" in status_result.output  # presence only, never the value

        assert E2E_KEY not in result.output
        raw_db = db.read_bytes()
        assert E2E_KEY.encode() not in raw_db  # App. B.3: nothing secret is persisted
        assert b"OPENAI_API_KEY" not in raw_db  # not even the env var name


class TestHistory:
    def test_history_json_and_full_detail(self, workspace, db):
        runner.invoke(app, ["init"])
        result = _patched_invoke(
            ["ask", "gpt", "question one"],
            lambda request: httpx.Response(200, json=_completion("first answer")),
        )
        assert result.exit_code == 0
        result = _patched_invoke(
            ["ask", "gpt", "question two"],
            lambda request: httpx.Response(200, json=_completion("second answer")),
        )
        assert result.exit_code == 0

        result = runner.invoke(app, ["history", "--json"])
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert len(payload) == 2
        assert payload[0]["status"] == "succeeded"  # newest first

        conn, store = _open_store(db)
        try:
            run_id = next(store.all_models(Run, order_by="started_at ASC")).id
        finally:
            conn.close()
        detail = runner.invoke(app, ["history", "--full", run_id])
        assert detail.exit_code == 0
        assert "question one" in detail.output  # run_input artifact content
        assert "first answer" in detail.output  # run_output artifact content
        assert "agent_run_started" in detail.output  # lifecycle event, by value

    def test_history_full_unknown_run(self, workspace):
        runner.invoke(app, ["init"])
        result = runner.invoke(app, ["history", "--full", "nope"])
        assert result.exit_code == 1

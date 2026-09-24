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
* Harness agents route through the generic runtime; unregistered adapters
  fail by name; registered fakes run end-to-end via the test-only transient
  seam without touching the production registry (G0/R1, App. C.1).
* Secret hygiene: the key never lands in DB bytes or CLI output (App. B.3).
"""

import itertools
import json
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from typer.testing import CliRunner

import relay.agents.openai as openai_mod
from relay.agents import transient_adapters
from relay.cli.main import app
from relay.core.room_feed import build_room_feed
from relay.storage import connect
from relay.storage.events import EventLogWriter
from relay.storage.models import (
    Artifact,
    ArtifactKind,
    Decision,
    EventType,
    Message,
    MessageType,
    Room,
    Run,
    RunStatus,
    Task,
    TaskState,
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
        profile_path = workspace / ".relay" / "profile.yaml"
        assert profile_path.is_file()
        assert yaml.safe_load(profile_path.read_text(encoding="utf-8"))["project"][
            "default_branch"
        ] == "main"
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

    @pytest.mark.parametrize("adapter", ["gpt", "openai_compatible"])
    def test_openai_adapter_aliases_complete_a_cli_run(self, workspace, db, adapter):
        assert runner.invoke(app, ["init"]).exit_code == 0
        (workspace / "relay.yaml").write_text(
            f"agents:\n  alias: {{backend: api, adapter: {adapter}, model: offline}}\n",
            encoding="utf-8",
        )

        result = _patched_invoke(
            ["ask", "alias", "ping"],
            lambda request: httpx.Response(200, json=_completion("alias answered")),
        )
        assert result.exit_code == 0, result.output

        conn, store = _open_store(db)
        try:
            run = next(iter(store.all_models(Run)))
            assert run.status is RunStatus.SUCCEEDED
            outputs = store.artifacts_for_run(run.id, kind=ArtifactKind.RUN_OUTPUT)
            assert [artifact.content for artifact in outputs] == ["alias answered"]
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
    def test_provider_failure_persists_failed_run_with_recoverable_prompt(self, workspace, db):
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


class TestRoomLifecycleCLI:
    def _configure_roles(self, workspace: Path) -> None:
        (workspace / "relay.yaml").write_text(
            "agents:\n"
            "  gpt: {backend: api, adapter: openai, model: offline}\n"
            "roles:\n"
            "  planner: gpt\n",
            encoding="utf-8",
        )

    def test_room_commands_persist_roster_lifecycle_and_canonical_feed(self, workspace, db):
        assert runner.invoke(app, ["init"]).exit_code == 0
        self._configure_roles(workspace)

        created = runner.invoke(app, ["room", "create", "Design"])
        assert created.exit_code == 0, created.output
        assert "planner -> gpt" in created.output
        listed = runner.invoke(app, ["room", "list"])
        assert listed.exit_code == 0 and "Design" in listed.output and "open" in listed.output

        bound = runner.invoke(app, ["room", "bind", "design", "reviewer", "gpt"])
        assert bound.exit_code == 0 and "reviewer -> gpt" in bound.output
        assert runner.invoke(app, ["room", "close", "Design"]).exit_code == 0
        resumed = runner.invoke(app, ["room", "resume", "Design"])
        assert resumed.exit_code == 0, resumed.output
        for event_name in ("room_created", "room_seat_bound", "room_closed", "room_resumed"):
            assert event_name in resumed.output

        conn, store = _open_store(db)
        try:
            workspace_row = next(store.all_models(Workspace))
            assert workspace_row.active_room_id is not None
        finally:
            conn.close()

    def test_room_bind_rejects_unknown_role_and_agent_without_mutation(self, workspace, db):
        runner.invoke(app, ["init"])
        self._configure_roles(workspace)
        assert runner.invoke(app, ["room", "create", "Team"]).exit_code == 0
        conn, store = _open_store(db)
        try:
            baseline = store.counts()
        finally:
            conn.close()

        assert runner.invoke(app, ["room", "bind", "Team", "invented", "gpt"]).exit_code == 1
        assert runner.invoke(app, ["room", "bind", "Team", "reviewer", "missing"]).exit_code == 1
        conn, store = _open_store(db)
        try:
            assert store.counts() == baseline
        finally:
            conn.close()

    def test_room_ask_persists_targeted_exchange_from_active_room(self, workspace, db):
        runner.invoke(app, ["init"])
        self._configure_roles(workspace)
        assert runner.invoke(app, ["room", "create", "Design"]).exit_code == 0

        result = _patched_invoke(
            ["room", "ask", "@planner", "Is this ready?", "--by", "utku"],
            lambda request: httpx.Response(200, json=_completion("It is ready.")),
        )

        assert result.exit_code == 0, result.output
        assert "Room " in result.output and " - Design" in result.output
        assert "Seat @planner -> gpt" in result.output
        assert "human:utku: Is this ready?" in result.output
        assert "gpt: It is ready." in result.output
        assert "room_created" not in result.output

        conn, store = _open_store(db)
        try:
            messages = list(store.all_models(Message))
            assert len(messages) == 2
            request, reply = messages
            assert request.sender == "human:utku"
            assert request.recipient == "gpt"
            assert request.recipient_role == "planner"
            assert request.room_id is not None and request.task_id is None
            assert request.type is MessageType.CLARIFICATION_REQUEST
            assert request.blocking is False
            assert reply.reply_to_id == request.id
            assert reply.sender == "gpt"
            assert reply.recipient == "human:utku"
            assert reply.room_id == request.room_id and reply.task_id is None
            assert reply.type is MessageType.CLARIFICATION_RESPONSE
            runs = list(store.all_models(Run))
            assert len(runs) == 1 and runs[0].agent == "gpt"
            assert reply.run_id == runs[0].id
        finally:
            conn.close()

    @pytest.mark.parametrize("selector_kind", ["name", "full_id", "unique_prefix"])
    def test_room_ask_override_uses_persisted_seat_without_changing_active_room(
        self, workspace, db, selector_kind
    ):
        runner.invoke(app, ["init"])
        config_path = workspace / "relay.yaml"
        config_path.write_text(
            "agents:\n"
            "  gpt: {backend: api, adapter: openai, model: offline}\n"
            "  other: {backend: api, adapter: openai, model: offline}\n"
            "roles:\n"
            "  planner: gpt\n",
            encoding="utf-8",
        )
        assert runner.invoke(app, ["room", "create", "First"]).exit_code == 0
        config_path.write_text(
            "agents:\n"
            "  gpt: {backend: api, adapter: openai, model: offline}\n"
            "  other: {backend: api, adapter: openai, model: offline}\n"
            "roles:\n"
            "  planner: other\n",
            encoding="utf-8",
        )
        assert runner.invoke(app, ["room", "create", "Second"]).exit_code == 0

        conn, store = _open_store(db)
        try:
            rooms = {room.name: room for room in store.all_models(Room)}
            first = rooms["First"]
            second = rooms["Second"]
        finally:
            conn.close()
        if selector_kind == "name":
            selector = first.name
        elif selector_kind == "full_id":
            selector = first.id
        else:
            selector = next(
                first.id[:width]
                for width in range(1, len(first.id) + 1)
                if not second.id.startswith(first.id[:width])
            )

        result = _patched_invoke(
            [
                "room",
                "ask",
                "@planner",
                "Use the original seat",
                "--by",
                "utku",
                "--room",
                selector,
            ],
            lambda request: httpx.Response(200, json=_completion("original seat used")),
        )

        assert result.exit_code == 0, result.output
        lines = result.output.strip().splitlines()
        assert lines[0].startswith("Room ") and lines[0].endswith(" - First")
        assert lines[1:] == [
            "Seat @planner -> gpt",
            "human:utku: Use the original seat",
            "gpt: original seat used",
        ]
        conn, store = _open_store(db)
        try:
            rooms = {room.name: room for room in store.all_models(Room)}
            workspace_row = next(store.all_models(Workspace))
            assert workspace_row.active_room_id == rooms["Second"].id
            request = next(store.all_models(Message))
            assert request.room_id == rooms["First"].id
            assert request.recipient == "gpt"
            feed = build_room_feed(store, rooms["First"].id)
            exchange_entries = [
                entry
                for entry in feed
                if entry.kind
                in {
                    MessageType.CLARIFICATION_REQUEST.value,
                    MessageType.CLARIFICATION_RESPONSE.value,
                }
            ]
            assert [(entry.sender, entry.text) for entry in exchange_entries] == [
                ("human:utku", "Use the original seat"),
                ("gpt", "original seat used"),
            ]
        finally:
            conn.close()

    def test_room_ask_refusals_before_request_leave_store_unchanged(self, workspace, db):
        runner.invoke(app, ["init"])
        self._configure_roles(workspace)

        conn, store = _open_store(db)
        try:
            no_room_baseline = store.counts()
        finally:
            conn.close()
        no_room = runner.invoke(
            app, ["room", "ask", "@planner", "question", "--by", "utku"]
        )
        assert no_room.exit_code == 1 and "no active Room" in no_room.output
        conn, store = _open_store(db)
        try:
            assert store.counts() == no_room_baseline
        finally:
            conn.close()

        assert runner.invoke(app, ["room", "create", "Guarded"]).exit_code == 0
        conn, store = _open_store(db)
        try:
            baseline = store.counts()
        finally:
            conn.close()
        invalid_cases = [
            ["room", "ask", "planner", "q", "--by", "utku"],
            ["room", "ask", "@missing", "q", "--by", "utku"],
            ["room", "ask", "@planner", "q", "--by", ""],
            ["room", "ask", "@planner", "q", "--by", "two words"],
            ["room", "ask", "@planner", "q", "--by", "human:utku"],
        ]
        for args in invalid_cases:
            assert runner.invoke(app, args).exit_code == 1
        conn, store = _open_store(db)
        try:
            assert store.counts() == baseline
        finally:
            conn.close()

        assert runner.invoke(app, ["room", "close", "Guarded"]).exit_code == 0
        conn, store = _open_store(db)
        try:
            closed_baseline = store.counts()
        finally:
            conn.close()
        closed = runner.invoke(
            app,
            ["room", "ask", "@planner", "q", "--by", "utku", "--room", "Guarded"],
        )
        assert closed.exit_code == 1 and "is closed" in closed.output
        conn, store = _open_store(db)
        try:
            assert store.counts() == closed_baseline
        finally:
            conn.close()

    def test_room_ask_adapter_construction_refuses_before_request(self, workspace, db):
        runner.invoke(app, ["init"])
        self._configure_roles(workspace)
        assert runner.invoke(app, ["room", "create", "Config"]).exit_code == 0
        (workspace / "relay.yaml").write_text(
            "agents:\n"
            "  other: {backend: api, adapter: openai, model: offline}\n"
            "roles:\n"
            "  planner: other\n",
            encoding="utf-8",
        )
        conn, store = _open_store(db)
        try:
            baseline = store.counts()
        finally:
            conn.close()
        unknown_agent = runner.invoke(
            app, ["room", "ask", "@planner", "q", "--by", "utku"]
        )
        assert unknown_agent.exit_code == 1 and "unknown agent 'gpt'" in unknown_agent.output

        (workspace / "relay.yaml").write_text(
            "agents:\n"
            "  gpt: {backend: api, adapter: missing_adapter, model: offline}\n"
            "roles:\n"
            "  planner: gpt\n",
            encoding="utf-8",
        )
        conn, store = _open_store(db)
        try:
            assert store.counts() == baseline
        finally:
            conn.close()

        result = runner.invoke(
            app, ["room", "ask", "@planner", "q", "--by", "utku"]
        )

        assert result.exit_code == 1
        conn, store = _open_store(db)
        try:
            assert store.counts() == baseline
        finally:
            conn.close()

    def test_room_ask_runtime_failure_keeps_request_and_failed_delivery(self, workspace, db):
        runner.invoke(app, ["init"])
        self._configure_roles(workspace)
        assert runner.invoke(app, ["room", "create", "Failure"]).exit_code == 0

        def timeout(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("provider hung", request=request)

        result = _patched_invoke(
            ["room", "ask", "@planner", "fragile", "--by", "utku"], timeout
        )

        assert result.exit_code == 1
        assert "incomplete Room exchange" in result.output
        conn, store = _open_store(db)
        try:
            messages = list(store.all_models(Message))
            assert len(messages) == 1
            assert messages[0].sender == "human:utku"
            runs = list(store.all_models(Run))
            assert len(runs) == 1 and runs[0].status is RunStatus.FAILED
            events = EventLogWriter(conn).all()
            assert len(
                [event for event in events if event.type is EventType.MESSAGE_DELIVERED]
            ) == 1
        finally:
            conn.close()

    def test_room_ask_close_after_start_reports_incomplete_without_reply(self, workspace, db):
        runner.invoke(app, ["init"])
        self._configure_roles(workspace)
        assert runner.invoke(app, ["room", "create", "Closing"]).exit_code == 0
        calls = 0

        def close_room(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            conn, store = _open_store(db)
            try:
                workspace_row = next(store.all_models(Workspace))
                room = next(store.all_models(Room))
                from relay.core.rooms import RoomLifecycle

                RoomLifecycle(store, EventLogWriter(conn)).close(workspace_row, room)
            finally:
                conn.close()
            return httpx.Response(200, json=_completion("finished after close"))

        result = _patched_invoke(
            ["room", "ask", "@planner", "close during run", "--by", "utku"],
            close_room,
        )

        assert result.exit_code == 1
        assert "incomplete Room exchange" in result.output
        assert calls == 1
        conn, store = _open_store(db)
        try:
            messages = list(store.all_models(Message))
            assert len(messages) == 1
            runs = list(store.all_models(Run))
            assert len(runs) == 1 and runs[0].status is RunStatus.SUCCEEDED
            assert [artifact.kind for artifact in store.artifacts_for_run(runs[0].id)] == [
                ArtifactKind.RUN_INPUT,
                ArtifactKind.RUN_OUTPUT,
            ]
            events = EventLogWriter(conn).all()
            assert any(event.type is EventType.MESSAGE_DELIVERED for event in events)
            assert any(event.type is EventType.ROOM_CLOSED for event in events)
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
    def test_harness_agent_errors_naming_the_missing_adapter(self, workspace):
        """G0/R1: unregistered harness adapters fail explicitly, by name.

        P2.4 note: ``antigravity_cli`` is registered now — the refusal
        premise is pinned with the stable synthetic ``future_cli`` placeholder
        (grilled decision Q-b).
        """
        (workspace / "relay.yaml").write_text(
            "agents:\n  fut: {backend: harness, adapter: future_cli}\n",
            encoding="utf-8",
        )
        runner.invoke(app, ["init"])
        result = runner.invoke(app, ["ask", "fut", "make a change"])
        assert result.exit_code == 1
        # rich wraps long lines, so assert on wording fragments, not strings.
        assert "unknown agent adapter" in result.output
        assert "future_cli" in result.output

    def test_backend_family_mismatch_is_a_config_error(self, workspace):
        """R1#1: api-declared agent + harness-routed adapter cannot wire."""
        (workspace / "relay.yaml").write_text(
            "agents:\n  impostor: {backend: api, adapter: c7_echo}\n",
            encoding="utf-8",
        )
        runner.invoke(app, ["init"])

        from relay.harness.capabilities import HarnessCapability
        from relay.harness.runtime import HarnessAgent as _HarnessAgent

        class _C7Echo(_HarnessAgent):
            name = "c7_echo"
            capabilities = frozenset({HarnessCapability.READ_ONLY_ACCESS})

            def invocation_argv(self, resolved):
                return (resolved.command, "-c", "print('unused')")

        with transient_adapters({"c7_echo": _C7Echo}):
            result = runner.invoke(app, ["ask", "impostor", "x"])
        assert result.exit_code == 1
        # rich wraps long lines — assert fragments, never one long string.
        assert "executes as" in result.output
        assert "'harness'" in result.output
        assert result.exception is None or isinstance(result.exception, SystemExit)

    def test_harness_agent_end_to_end_via_transient_registration(self, workspace, db):
        """Full ask-flow through the generic runtime using a registered fake.

        The fake NEVER enters AGENTS (G0#3); executable_path comes from
        relay.yaml's non-secret profile; the prompt rides stdin.
        """
        from relay.harness.capabilities import HarnessCapability
        from relay.harness.runtime import HarnessAgent as _HarnessAgent

        py = Path(sys.executable).as_posix()
        (workspace / "relay.yaml").write_text(
            "agents:\n"
            f"  echoh: {{backend: harness, adapter: c7_echo, "
            f"harness: {{executable_path: '{py}', timeout_seconds: 20}}}}\n",
            encoding="utf-8",
        )
        runner.invoke(app, ["init"])

        class _C7Echo(_HarnessAgent):
            name = "c7_echo"
            capabilities = frozenset({HarnessCapability.READ_ONLY_ACCESS})

            def invocation_argv(self, resolved):
                return (
                    resolved.command,
                    "-c",
                    "import sys; sys.stdout.write('c7echo:' + sys.stdin.read())",
                )

        with transient_adapters({"c7_echo": _C7Echo}):
            result = runner.invoke(app, ["ask", "echoh", "ping-marker"])
        assert result.exit_code == 0, result.output
        assert "c7echo:ping-marker" in result.output

        # The persisted database is this run's verifiable artifact. Reopening
        # it proves the CLI, factory, child process, and store completed the
        # same request without relying on the transient registration afterward.
        assert db.is_file()
        conn, store = _open_store(db)
        try:
            runs = list(store.all_models(Run))
            assert len(runs) == 1
            assert runs[0].status is RunStatus.SUCCEEDED
            artifacts = store.artifacts_for_run(runs[0].id)
            assert {artifact.kind: artifact.content for artifact in artifacts} == {
                ArtifactKind.RUN_INPUT: "ping-marker",
                ArtifactKind.RUN_OUTPUT: "c7echo:ping-marker",
            }
        finally:
            conn.close()

    def test_room_ask_harness_runtime_probe_happens_after_request_persistence(
        self, workspace, db
    ):
        from relay.harness.capabilities import HarnessCapability
        from relay.harness.runtime import HarnessAgent as _HarnessAgent

        missing = (workspace / "definitely-missing-relay-harness.exe").as_posix()
        (workspace / "relay.yaml").write_text(
            "agents:\n"
            f"  echoh: {{backend: harness, adapter: c7_echo, "
            f"harness: {{executable_path: '{missing}', timeout_seconds: 20}}}}\n"
            "roles:\n"
            "  planner: echoh\n",
            encoding="utf-8",
        )
        runner.invoke(app, ["init"])
        assert runner.invoke(app, ["room", "create", "Harness"]).exit_code == 0

        class _C7Echo(_HarnessAgent):
            name = "c7_echo"
            capabilities = frozenset({HarnessCapability.READ_ONLY_ACCESS})

            def invocation_argv(self, resolved):
                return (resolved.command, "--unused")

        with transient_adapters({"c7_echo": _C7Echo}):
            result = runner.invoke(
                app,
                ["room", "ask", "@planner", "probe at runtime", "--by", "utku"],
            )

        assert result.exit_code == 1
        assert "incomplete Room exchange" in result.output
        conn, store = _open_store(db)
        try:
            messages = list(store.all_models(Message))
            assert len(messages) == 1 and messages[0].recipient == "echoh"
            runs = list(store.all_models(Run))
            assert len(runs) == 1 and runs[0].status is RunStatus.FAILED
            assert any(
                event.type is EventType.MESSAGE_DELIVERED
                for event in EventLogWriter(conn).all()
            )
        finally:
            conn.close()

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


class TestTaskObservability:
    """P3.4 CLI edges: empty workspaces degrade; unknown tasks fail typed."""

    def test_status_without_tasks_omits_task_section(self, workspace):
        runner.invoke(app, ["init"])
        result = runner.invoke(app, ["status"])
        assert result.exit_code == 0, result.output
        assert "Relay tasks" not in result.output
        assert "Active task" not in result.output

    def test_inspect_before_init_and_unknown_task(self, workspace):
        not_initialized = runner.invoke(app, ["inspect", "whatever"])
        assert not_initialized.exit_code == 1
        assert "not initialized" in not_initialized.output

        runner.invoke(app, ["init"])
        unknown = runner.invoke(app, ["inspect", "nope"])
        assert unknown.exit_code == 1
        assert "does not exist" in unknown.output


class TestRoomCanonicalGraphCLI:
    """P7.3 CLI: human freeze, explicit decision exchange, graph read surface."""

    def _configure(self, workspace: Path) -> None:
        (workspace / "relay.yaml").write_text(
            "agents:\n"
            "  gpt: {backend: api, adapter: openai, model: offline}\n"
            "  impl:\n"
            "    backend: harness\n"
            "    adapter: claude_code\n"
            "    model: offline\n"
            "    harness:\n"
            "      executable_path: python\n"
            "      grant: workspace_write\n"
            "      timeout_seconds: 30\n"
            "roles:\n"
            "  planner: gpt\n"
            "  implementer: impl\n",
            encoding="utf-8",
        )

    def _ask_planner(self, prompt: str = "Draft the implementation plan") -> Any:
        return _patched_invoke(
            ["room", "ask", "@planner", prompt, "--by", "utku"],
            lambda request: httpx.Response(200, json=_completion("# Plan\n\nStep 1: ship it")),
        )

    def _planner_reply(self, db) -> Message:
        conn, store = _open_store(db)
        try:
            return next(
                message
                for message in store.all_models(Message)
                if message.type is MessageType.CLARIFICATION_RESPONSE
            )
        finally:
            conn.close()

    def test_freeze_binds_a_room_task_and_the_graph_renders_it(self, workspace, db):
        runner.invoke(app, ["init"])
        self._configure(workspace)
        assert runner.invoke(app, ["room", "create", "Design"]).exit_code == 0
        asked = self._ask_planner()
        assert asked.exit_code == 0, asked.output
        reply = self._planner_reply(db)

        frozen = runner.invoke(
            app, ["room", "freeze", "Design", "--by", "utku", "--from-message", reply.id]
        )
        assert frozen.exit_code == 0, frozen.output
        assert "Frozen plan" in frozen.output
        assert "Next: relay continue" in frozen.output

        conn, store = _open_store(db)
        try:
            task = next(iter(store.all_models(Task)))
            assert task.state is TaskState.IMPLEMENTING
            assert task.room_id is not None
            plan = next(
                artifact
                for artifact in store.all_models(Artifact)
                if artifact.kind is ArtifactKind.PLAN
            )
            assert plan.room_id == task.room_id and plan.task_id == task.id
        finally:
            conn.close()

        graph = runner.invoke(app, ["room", "graph", "Design"])
        assert graph.exit_code == 0, graph.output
        assert "Plan chain" in graph.output
        assert "frozen by human:utku" in graph.output

        as_json = runner.invoke(app, ["room", "graph", "Design", "--json"])
        assert as_json.exit_code == 0, as_json.output
        payload = json.loads(as_json.output)
        assert payload["version"] == "relay.room.graph.v1"
        assert payload["plans"][0]["nodes"][0]["edge"] == "frozen"
        assert payload["plans"][0]["tip"] == plan.id

    def test_freeze_refuses_a_second_freeze_of_the_same_reply(self, workspace, db):
        runner.invoke(app, ["init"])
        self._configure(workspace)
        assert runner.invoke(app, ["room", "create", "Design"]).exit_code == 0
        assert self._ask_planner().exit_code == 0
        reply = self._planner_reply(db)
        first = runner.invoke(
            app, ["room", "freeze", "Design", "--by", "utku", "--from-message", reply.id]
        )
        assert first.exit_code == 0, first.output
        conn, store = _open_store(db)
        try:
            baseline = store.counts()
        finally:
            conn.close()
        again = runner.invoke(
            app, ["room", "freeze", "Design", "--by", "utku", "--from-message", reply.id]
        )
        assert again.exit_code == 1
        assert "already frozen" in again.output
        conn, store = _open_store(db)
        try:
            assert store.counts() == baseline
        finally:
            conn.close()

    def test_decide_promotes_a_canonical_decision(self, workspace, db):
        runner.invoke(app, ["init"])
        self._configure(workspace)
        assert runner.invoke(app, ["room", "create", "Design"]).exit_code == 0
        payload = (
            '{"schema_version":"relay.room_decision.v1","outcome":"accept",'
            '"statement":"adopt bundle registries","rationale":null,"references":[],'
            '"supersedes_decision_id":null}'
        )
        result = _patched_invoke(
            ["room", "decide", "@planner", "Should we adopt design B?", "--by", "utku"],
            lambda request: httpx.Response(200, json=_completion(payload)),
        )
        assert result.exit_code == 0, result.output
        assert "Decision " in result.output and "[accepted]" in result.output

        conn, store = _open_store(db)
        try:
            decision = next(iter(store.all_models(Decision)))
            assert decision.room_id is not None
            assert decision.task_id is None
            assert decision.accepted_by == "gpt"
            assert decision.source_reply_id is not None
            proposal = next(
                message
                for message in store.all_models(Message)
                if message.type is MessageType.PROPOSAL
            )
            assert proposal.recipient_role == "planner"
        finally:
            conn.close()

    def test_decide_promotes_nothing_on_ordinary_prose(self, workspace, db):
        runner.invoke(app, ["init"])
        self._configure(workspace)
        assert runner.invoke(app, ["room", "create", "Design"]).exit_code == 0
        result = _patched_invoke(
            ["room", "decide", "@planner", "Should we adopt design B?", "--by", "utku"],
            lambda request: httpx.Response(200, json=_completion("Sure, sounds fine.")),
        )
        assert result.exit_code == 1
        assert "no canonical decision promoted" in result.output
        conn, store = _open_store(db)
        try:
            assert not list(store.all_models(Decision))
            assert len(list(store.all_models(Message))) == 2
        finally:
            conn.close()

    def test_decide_output_is_never_freezable(self, workspace, db):
        runner.invoke(app, ["init"])
        self._configure(workspace)
        assert runner.invoke(app, ["room", "create", "Design"]).exit_code == 0
        payload = (
            '{"schema_version":"relay.room_decision.v1","outcome":"accept",'
            '"statement":"adopt it","rationale":null,"references":[],'
            '"supersedes_decision_id":null}'
        )
        decided = _patched_invoke(
            ["room", "decide", "@planner", "Decide", "--by", "utku"],
            lambda request: httpx.Response(200, json=_completion(payload)),
        )
        assert decided.exit_code == 0, decided.output
        conn, store = _open_store(db)
        try:
            reply = next(
                message
                for message in store.all_models(Message)
                if message.type is MessageType.FINAL_POSITION
            )
        finally:
            conn.close()
        frozen = runner.invoke(
            app, ["room", "freeze", "Design", "--by", "utku", "--from-message", reply.id]
        )
        assert frozen.exit_code == 1
        assert "clarification_request" in frozen.output

    def test_freeze_refuses_without_an_implementer_seat(self, workspace, db):
        runner.invoke(app, ["init"])
        (workspace / "relay.yaml").write_text(
            "agents:\n"
            "  gpt: {backend: api, adapter: openai, model: offline}\n"
            "roles:\n"
            "  planner: gpt\n",
            encoding="utf-8",
        )
        assert runner.invoke(app, ["room", "create", "Design"]).exit_code == 0
        assert self._ask_planner().exit_code == 0
        reply = self._planner_reply(db)
        conn, store = _open_store(db)
        try:
            baseline = store.counts()
        finally:
            conn.close()
        result = runner.invoke(
            app, ["room", "freeze", "Design", "--by", "utku", "--from-message", reply.id]
        )
        assert result.exit_code == 1
        assert "implementer" in result.output
        conn, store = _open_store(db)
        try:
            assert store.counts() == baseline
        finally:
            conn.close()

    def test_graph_refuses_when_the_chain_is_discontinuous(self, workspace, db):
        runner.invoke(app, ["init"])
        self._configure(workspace)
        assert runner.invoke(app, ["room", "create", "Design"]).exit_code == 0
        assert self._ask_planner().exit_code == 0
        reply = self._planner_reply(db)
        frozen = runner.invoke(
            app, ["room", "freeze", "Design", "--by", "utku", "--from-message", reply.id]
        )
        assert frozen.exit_code == 0, frozen.output
        conn, store = _open_store(db)
        try:
            task = next(iter(store.all_models(Task)))
            store.save_model(
                Artifact(
                    kind=ArtifactKind.PLAN,
                    room_id=task.room_id,
                    task_id=task.id,
                    content="# Orphan plan",
                )
            )
        finally:
            conn.close()
        result = runner.invoke(app, ["room", "graph", "Design"])
        assert result.exit_code == 1
        assert "outside the chain" in result.output

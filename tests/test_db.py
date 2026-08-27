"""SQLite persistence: schema, codec roundtrips, append-only enforcement.

SPEC reference: §14/§15; Appendix A.1 (evidence seam), B.1 (run I/O).
"""

import itertools
import sqlite3

import pytest

from relay.core.evidence import (
    EvidenceKind,
    InvalidProducerError,
    MissingProvenanceError,
)
from relay.core.permissions import Action
from relay.core.state_machine import (
    MissingEvidenceError,
    TaskState,
    TaskStateMachine,
)
from relay.storage import connect, migrate
from relay.storage.db import _MIGRATIONS, SCHEMA_VERSION
from relay.storage.events import EventLogWriter
from relay.storage.models import (
    Approval,
    Artifact,
    ArtifactKind,
    Decision,
    EventLogEntry,
    EventType,
    EvidenceRecord,
    Message,
    MessageType,
    Room,
    RoomMember,
    Run,
    RunStatus,
    Task,
    ToolRun,
    Workspace,
    utcnow,
)
from relay.storage.store import (
    MODEL_TABLES,
    ImmutableHistoryError,
    SqliteEvidenceStore,
    SqliteRelayStore,
)


@pytest.fixture()
def db(tmp_path):
    conn = connect(tmp_path / "relay.sqlite3")
    migrate(conn)
    yield conn
    conn.close()


@pytest.fixture()
def store(db):
    return SqliteRelayStore(db)


# --------------------------------------------------------------------------
# Schema / connection
# --------------------------------------------------------------------------


class TestSchemaAndConnect:
    def test_migrate_is_idempotent(self, tmp_path):
        conn = connect(tmp_path / "r.sqlite3")
        assert migrate(conn) == 2
        assert migrate(conn) == 2
        conn.close()

    def test_wal_and_foreign_keys_enabled(self, db):
        assert db.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1

    def test_migrate_refuses_newer_schema(self, tmp_path):
        conn = connect(tmp_path / "r.sqlite3")
        conn.execute("PRAGMA user_version = 99")
        with pytest.raises(sqlite3.DatabaseError):
            migrate(conn)
        conn.close()

    def test_store_requires_migrated_db(self, tmp_path):
        conn = connect(tmp_path / "fresh.sqlite3")
        with pytest.raises(RuntimeError):
            SqliteRelayStore(conn)
        conn.close()


# --------------------------------------------------------------------------
# Codec roundtrips for every aggregate
# --------------------------------------------------------------------------


@pytest.fixture(
    params=[
        Workspace(name="demo", path="C:/tmp/demo", kind="git_repo"),
        Room(name="room-1", members=[RoomMember(agent="gpt", role="researcher")]),
        Task(title="do a thing"),
        Run(agent="gpt", role="researcher", model="gpt-4o-mini"),
        Message(sender="user", recipient="gpt", type=MessageType.OPINION, content="hi there"),
        Artifact(kind=ArtifactKind.RUN_INPUT, run_id=None, content="payload"),
        Decision(statement="Use SQLite", supported_by=["gpt"], challenged_by=["claude"]),
        Approval(action=Action.EDIT_FILES),
        ToolRun(tool="git.diff", arguments={"ref": "HEAD~1..HEAD"}),
        EvidenceRecord(
            kind=EvidenceKind.PLAN_PRODUCED,
            task_id="t42",
            run_id="run-9",
            produced_by="agent:gpt",
        ),
        EventLogEntry(type=EventType.AGENT_RUN_STARTED, content="go", references=["run:r1"]),
    ],
    ids=[
        "workspace",
        "room",
        "task",
        "run",
        "message",
        "artifact",
        "decision",
        "approval",
        "tool_run",
        "evidence",
        "event",
    ],
)
def sample_record(request):
    return request.param


class TestCodecRoundtrips:
    def test_every_aggregate_survives_a_roundtrip(self, store, sample_record):
        saved = store.save_model(sample_record)
        loaded = store.load_model(type(saved), _pk(saved))
        if isinstance(sample_record, EventLogEntry):
            # sequence is DB-assigned; compare every semantic field instead.
            semantic = ("room_id", "task_id", "sender", "recipient", "type",
                        "content", "references", "created_at")
            for field in semantic:
                assert getattr(loaded, field) == getattr(saved, field)
            assert loaded.sequence > 0
            return
        assert loaded == saved

    def test_duplicate_id_insert_is_rejected(self, store):
        record = Run(agent="gpt", role="researcher")
        store.save_model(record)
        same_run = Run.model_validate(record.model_dump())
        with pytest.raises(sqlite3.IntegrityError):
            store.save_model(same_run)

    def test_update_model_mutates_state_bearing_rows(self, store):
        run = store.save_model(Run(agent="gpt", role="researcher"))
        finished = run.model_copy(update={"status": RunStatus.SUCCEEDED})
        store.update_model(finished)
        reloaded = store.load_model(Run, run.id)
        assert reloaded.status is RunStatus.SUCCEEDED


def _pk(record) -> str | int:
    return record.sequence if isinstance(record, EventLogEntry) else record.id


# --------------------------------------------------------------------------
# Append-only enforcement — Python *and* database level
# --------------------------------------------------------------------------


class TestAppendOnlyHistory:
    @pytest.fixture()
    def seeded(self, db):
        """One committed row per append-only table so row triggers fire."""
        db.execute(
            "INSERT INTO event_log (type, content, created_at) "
            "VALUES ('task_created', 'seed', '2024-01-01T00:00:00+00:00')"
        )
        db.execute(
            "INSERT INTO evidence_records "
            "(id, kind, task_id, produced_by, created_at) "
            "VALUES ('ev-seed', 'context_collected', 't1', 'relay:core', "
            "'2024-01-01T00:00:00+00:00')"
        )

    @pytest.mark.parametrize("table", ["event_log", "evidence_records"])
    def test_raw_sql_update_aborts(self, db, seeded, table):
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            db.execute(f"UPDATE {table} SET created_at = '1999-01-01'")

    @pytest.mark.parametrize("table", ["event_log", "evidence_records"])
    def test_raw_sql_delete_aborts(self, db, seeded, table):
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            db.execute(f"DELETE FROM {table}")

    def test_python_api_refuses_history_updates(self, store, db):
        writer = EventLogWriter(db)
        entry = writer.record(EventLogEntry(type=EventType.TASK_CREATED, content="t"))
        tampered = entry.model_copy(update={"content": "rewritten"})
        with pytest.raises(ImmutableHistoryError):
            store.update_model(tampered)

    def test_delete_model_on_event_log_refused(self, store, db):
        writer = EventLogWriter(db)
        entry = writer.record(EventLogEntry(type=EventType.TASK_CREATED, content="t"))
        with pytest.raises(ImmutableHistoryError):
            store.delete_model(entry)

    def test_transaction_rollback_preserves_append_only_tables(self, store, db):
        writer = EventLogWriter(db)
        with store.transaction():
            writer.record(EventLogEntry(type=EventType.TASK_CREATED, content="kept"))

        def failing_txn() -> None:
            with store.transaction():
                writer.record(EventLogEntry(type=EventType.TASK_CREATED, content="lost"))
                msg = "boom"
                raise RuntimeError(msg)

        with pytest.raises(RuntimeError):  # context manager must roll back
            failing_txn()

        contents = [entry.content for entry in EventLogWriter(db).tail(limit=10)]
        assert "kept" in contents and "lost" not in contents


# --------------------------------------------------------------------------
# Sequence invariant: unique + strictly increasing, never assumed gapless
# --------------------------------------------------------------------------


class TestEventSequenceInvariant:
    def test_sequences_are_strictly_increasing(self, db):
        writer = EventLogWriter(db)
        entries = [
            writer.record(EventLogEntry(type=EventType.AGENT_RUN_STARTED, content=f"n{i}"))
            for i in range(5)
        ]
        sequences = [entry.sequence for entry in entries]
        assert len(set(sequences)) == len(sequences)
        assert all(b > a for a, b in itertools.pairwise(sequences))

    def test_caller_supplied_sequence_is_rejected(self, db):
        writer = EventLogWriter(db)
        entry = EventLogEntry(sequence=17, type=EventType.MESSAGE_SENT, content="x")
        with pytest.raises(ValueError, match="sequence"):
            writer.record(entry)

    def test_rollback_creates_gaps_without_breaking_the_invariant(self, db):
        """Rolled-back inserts vanish; committed sequences stay unique + strictly increasing.

        Whether AUTOINCREMENT reuses a rolled-back id or leaves a gap is an
        SQLite implementation detail — Relay never assumes gaplessness. The
        contract is: every committed entry is present, rolled-back entries
        never resurface, and committed sequences strictly increase.
        """
        writer = EventLogWriter(db)
        first = writer.record(EventLogEntry(type=EventType.TASK_CREATED, content="a"))

        db.execute("BEGIN IMMEDIATE")
        writer.record(EventLogEntry(type=EventType.TASK_CREATED, content="lost"))
        db.execute("ROLLBACK")  # the entry (and any sequence counter change) is undone

        recovered = writer.record(EventLogEntry(type=EventType.TASK_CREATED, content="b"))
        rows = [int(row["sequence"]) for row in db.execute("SELECT sequence FROM event_log")]
        contents = [row["content"] for row in db.execute("SELECT content FROM event_log")]
        assert rows == [first.sequence, recovered.sequence]
        assert contents == ["a", "b"]  # "lost" never committed
        assert recovered.sequence > first.sequence
        assert len(set(rows)) == len(rows)  # unique among committed entries
        assert all(b > a for a, b in itertools.pairwise(rows))

    def test_writer_participates_in_store_transactions(self, store, db):
        writer = EventLogWriter(db)
        with store.transaction():
            run = store.save_model(Run(agent="gpt", role="researcher"))
            started = writer.record(
                EventLogEntry(
                    type=EventType.AGENT_RUN_STARTED,
                    content="agent 'gpt' started",
                    references=[f"run:{run.id}"],
                )
            )
        rows = [row["sequence"] for row in db.execute("SELECT sequence FROM event_log")]
        assert rows == [started.sequence]


class TestEventWriterReadback:
    def test_tail_returns_newest_first(self, db):
        writer = EventLogWriter(db)
        for i in range(3):
            writer.record(EventLogEntry(type=EventType.AGENT_RUN_STARTED, content=f"n{i}"))
        tail = writer.tail(limit=2)
        assert [entry.content for entry in tail] == ["n2", "n1"]
        assert tail[0].created_at.tzinfo is not None

    def test_all_returns_oldest_first(self, db):
        writer = EventLogWriter(db)
        for i in range(3):
            writer.record(EventLogEntry(type=EventType.AGENT_RUN_FINISHED, content=f"n{i}"))
        ordered = writer.all()
        assert [entry.content for entry in ordered] == ["n0", "n1", "n2"]


# --------------------------------------------------------------------------
# Evidence protocol: provenance boundary + parity + state machine seam swap
# --------------------------------------------------------------------------


class TestSqliteEvidenceStore:
    def test_provenance_validated_at_boundary(self, store):
        evidence = SqliteEvidenceStore(store)
        forged = EvidenceRecord(
            kind=EvidenceKind.APPROVAL_GRANTED, task_id="t1", produced_by="agent:gpt"
        )
        with pytest.raises(InvalidProducerError):
            evidence.record(forged)

    def test_missing_tool_linkage_rejected(self, store):
        evidence = SqliteEvidenceStore(store)
        incomplete = EvidenceRecord(
            kind=EvidenceKind.TESTS_PASSED, task_id="t1", produced_by="agent:codex"
        )
        with pytest.raises(MissingProvenanceError):
            evidence.record(incomplete)

    def test_records_scoped_to_task(self, store):
        evidence = SqliteEvidenceStore(store)
        ours = evidence.record(
            EvidenceRecord(
                kind=EvidenceKind.CONTEXT_COLLECTED, task_id="ours", produced_by="relay:core"
            )
        )
        evidence.record(
            EvidenceRecord(
                kind=EvidenceKind.CONTEXT_COLLECTED, task_id="theirs", produced_by="relay:core"
            )
        )
        assert evidence.records_for_task("ours") == (ours,)
        assert evidence.records_for_task("ours", EvidenceKind.TESTS_PASSED) == ()

    def test_duplicate_id_with_different_content_rejected(self, store):
        evidence = SqliteEvidenceStore(store)
        original = EvidenceRecord(
            kind=EvidenceKind.CONTEXT_COLLECTED, task_id="t1", produced_by="relay:core"
        )
        evidence.record(original)
        impostor = original.model_copy(update={"produced_by": "agent:gpt"})
        with pytest.raises(ValueError, match="different content"):
            evidence.record(impostor)

    def test_matches_in_memory_store_outcomes(self, store):
        """Parity at gate-outcome level against the Phase 0 reference impl."""
        from relay.core.evidence import InMemoryEvidenceStore

        sqlite_evidence = SqliteEvidenceStore(store)
        memory_evidence = InMemoryEvidenceStore()
        plan = EvidenceRecord(
            kind=EvidenceKind.PLAN_PRODUCED, task_id="t", run_id="r", produced_by="agent:gpt"
        )
        for target in (sqlite_evidence, memory_evidence):
            target.record(plan)
            assert {rec.kind for rec in target.records_for_task("t")} == {
                EvidenceKind.PLAN_PRODUCED
            }
            with pytest.raises(MissingProvenanceError):
                target.record(
                    EvidenceRecord(
                        kind=EvidenceKind.REVIEW_PASSED, task_id="t", produced_by="agent:gpt"
                    )
                )


class TestStateMachineSeamSwap:
    """The promised swap: TaskStateMachine gates read straight off SQLite."""

    def test_gated_prefix_transitions_against_sqlite_store(self, tmp_path):
        conn = connect(tmp_path / "relay.sqlite3")
        migrate(conn)
        store = SqliteRelayStore(conn)
        task = store.save_model(Task(title="wire storage"))
        evidence = SqliteEvidenceStore(store)

        sm = TaskStateMachine(task_id=task.id, store=evidence)

        # Without stored proof the gate stays shut — the machine consults
        # the SQLite-backed store, not any caller-supplied enum.
        assert not sm.can_transition(TaskState.CONTEXT_READY)
        with pytest.raises(MissingEvidenceError):
            sm.transition(TaskState.CONTEXT_READY)

        evidence.record(
            EvidenceRecord(
                kind=EvidenceKind.CONTEXT_COLLECTED, task_id=task.id, produced_by="relay:core"
            )
        )
        sm.transition(TaskState.CONTEXT_READY)

        assert not sm.can_transition(TaskState.PLAN_READY)
        evidence.record(
            EvidenceRecord(
                kind=EvidenceKind.PLAN_PRODUCED,
                task_id=task.id,
                run_id="run-1",
                produced_by="agent:gpt",
            )
        )
        sm.transition(TaskState.PLAN_READY)

        # Relay owns workflow state: persist the transitioned Task row.
        store.update_model(
            task.model_copy(update={"state": TaskState.PLAN_READY, "updated_at": utcnow()})
        )
        persisted = store.load_model(Task, task.id)
        assert persisted.state is TaskState.PLAN_READY

        kinds = {record.kind for record in evidence.records_for_task(task.id)}
        assert kinds == {EvidenceKind.CONTEXT_COLLECTED, EvidenceKind.PLAN_PRODUCED}
        conn.close()


# --------------------------------------------------------------------------
# Durability across reopenings + identity key plumbing
# --------------------------------------------------------------------------


class TestDurabilityAndIdentity:
    def test_read_your_writes_after_reopen(self, tmp_path):
        first_conn = connect(tmp_path / "relay.sqlite3")
        migrate(first_conn)
        store = SqliteRelayStore(first_conn)
        ws = store.save_model(Workspace(name="persisted", path="C:/projects/demo"))
        identity_key = r"c:\projects\demo"  # normcase(realpath(...)) output shape
        store.register_identity(ws, identity_key)
        first_conn.close()

        reopened = connect(tmp_path / "relay.sqlite3")
        fresh_store = SqliteRelayStore(reopened)
        found = fresh_store.workspace_for_identity(identity_key)
        assert found is not None and found.id == ws.id and found.path == "C:/projects/demo"
        reopened.close()

    def test_v1_database_upgrades_in_place_preserving_rows(self, tmp_path):
        db_path = tmp_path / "v1.sqlite3"
        v1_conn = connect(db_path)
        for statement in _MIGRATIONS[1]:
            v1_conn.execute(statement)
        v1_conn.execute("PRAGMA user_version = 1")
        v1_conn.execute(
            "INSERT INTO runs (id, agent, role, model, status, started_at) "
            "VALUES ('run-hist', 'gpt', 'researcher', 'gpt-4o-mini', 'succeeded', '2026-01-01T00:00:00Z')"
        )
        v1_conn.commit()
        assert int(v1_conn.execute("PRAGMA user_version").fetchone()[0]) == 1
        v1_conn.close()

        upgraded = connect(db_path)
        assert migrate(upgraded) == SCHEMA_VERSION
        row = upgraded.execute("SELECT * FROM runs WHERE id='run-hist'").fetchone()
        assert row["model"] == "gpt-4o-mini"
        assert row["resolved_model"] is None
        assert row["adapter_version"] is None
        assert row["backend"] is None
        assert row["external_session_ref"] is None
        columns = {c[1] for c in upgraded.execute("PRAGMA table_info(runs)")}
        assert {"resolved_model", "adapter_version", "backend", "external_session_ref"} <= columns
        upgraded.close()

    def test_c6_seam_columns_roundtrip_and_survive_reopen(self, tmp_path):
        conn = connect(tmp_path / "relay.sqlite3")
        migrate(conn)
        store = SqliteRelayStore(conn)
        saved = store.save_model(
            Run(
                agent="codex_cli",
                role="implementer",
                model="gpt-5.6-codex",
                resolved_model="gpt-5.6-codex",
                adapter_version="codex-cli 0.55.0",
                backend="harness",
                external_session_ref="0199a213-81c0-7800-8aa1-bbab2a035a53",
            )
        )
        loaded = SqliteRelayStore(conn).load_model(Run, saved.id)
        assert loaded.resolved_model == "gpt-5.6-codex"
        assert loaded.adapter_version == "codex-cli 0.55.0"
        assert loaded.backend == "harness"
        assert loaded.external_session_ref == "0199a213-81c0-7800-8aa1-bbab2a035a53"

        reopened = connect(tmp_path / "relay.sqlite3")
        migrate(reopened)
        again = SqliteRelayStore(reopened).load_model(Run, saved.id)
        assert again == loaded
        reopened.close()

    def test_counts_cover_every_table(self, store):
        store.save_model(Workspace(name="only"))
        counts = store.counts()
        assert set(counts) == {MODEL_TABLES[cls] for cls in MODEL_TABLES}
        assert counts["workspaces"] == 1

"""Workspace discovery, two-file config, and idempotent init (SPEC §13, App. B).

Phase 1 contract under test:

* ``ProjectProfile`` mirrors SPEC §13 exactly — discovered facts only, no
  provider/model/backend fields ever.
* ``relay.yaml`` is backend-aware: api entries execute; harness entries parse
  first-class (with an optional non-secret ``harness:`` profile) and admit
  adapters through the registry — presence plus backend-family match decide
  executability, never a hardcoded phase pointer (G0/R1, App. C.1).
* ``relay init`` is idempotent: canonical-path identity, one Workspace row,
  same id across re-inits, profile refreshed.
"""


import pytest
import yaml

from relay.agents.base import AgentRole, BackendType
from relay.agents.claude_code import ClaudeCodeAgent
from relay.agents.registry import UnknownAgentError, get_agent_class
from relay.context import (
    ConfigError,
    agent_config,
    discover_profile,
    identity_key,
    initialize_workspace,
    load_config,
    load_profile,
    workspace_layout,
)
from relay.storage import connect, migrate
from relay.storage.models import Artifact, ArtifactKind, Run, Workspace, WorkspaceKind
from relay.storage.store import SqliteRelayStore


@pytest.fixture()
def repo(tmp_path):
    """A small fake repository with discoverable facts."""
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text("ref: refs/heads/develop\n", encoding="utf-8")
    (tmp_path / "uv.lock").write_text("", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\ntestpaths = ['tests']\n", encoding="utf-8"
    )
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('hi')\n", encoding="utf-8")
    (tmp_path / "src" / "ui.tsx").write_text("export const x = 1\n", encoding="utf-8")
    (tmp_path / "vite.config.ts").write_text("", encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text("# instructions\n", encoding="utf-8")
    return tmp_path


class TestProjectProfile:
    """SPEC §13 shape: discovered facts, nothing provider-shaped."""

    def test_profile_carries_only_discovered_facts(self, repo):
        profile = discover_profile(repo)
        assert profile.languages == ["python", "typescript"]
        assert "vite" in profile.frameworks
        assert profile.package_managers == ["uv"]
        assert profile.instructions == ["AGENTS.md"]
        assert profile.default_branch == "develop"
        assert profile.tests["backend"] == "uv run pytest"

    def test_profile_has_no_provider_fields(self, repo):
        profile = discover_profile(repo)
        banned = {"backend", "adapter", "model", "provider", "base_url", "api_key"}
        assert not (banned & set(profile.model_dump()))

    def test_default_branch_falls_back_to_main(self, tmp_path):
        (tmp_path / "main.py").write_text("x = 1\n", encoding="utf-8")
        assert discover_profile(tmp_path).default_branch == "main"

    def test_profile_yaml_roundtrip_matches_spec_shape(self, repo):
        profile = discover_profile(repo)
        save_path = repo / ".relay" / "profile.yaml"
        from relay.context.workspace import save_profile

        save_profile(repo, profile)
        raw = yaml.safe_load(save_path.read_text(encoding="utf-8"))
        assert set(raw) == {"project"}
        assert raw["project"]["languages"] == ["python", "typescript"]
        restored = load_profile(repo)
        assert restored == profile


class TestRelayYamlConfig:
    """App. B.2/B.3: backend-aware config, api/harness separation canonical."""

    def test_missing_relay_yaml_uses_builtin_default(self, tmp_path):
        config = load_config(tmp_path)
        assert list(config.agents) == ["gpt"]
        agent = config.agents["gpt"]
        assert agent.backend is BackendType.API
        assert agent.adapter == "openai"
        assert agent.model == "gpt-4o-mini"

    def test_parses_api_and_harness_entries(self, tmp_path):
        (tmp_path / "relay.yaml").write_text(
            "agents:\n"
            "  gpt-api: {backend: api, adapter: openai, model: gpt-4o-mini}\n"
            "  codex:   {backend: harness, adapter: codex_cli}\n"
            "  claude:  {backend: harness, adapter: claude_code}\n",
            encoding="utf-8",
        )
        config = load_config(tmp_path)
        assert config.agents["gpt-api"].backend is BackendType.API
        assert config.agents["codex"].backend is BackendType.HARNESS
        assert config.agents["claude"].adapter == "claude_code"

    def test_api_backend_cannot_carry_harness_block(self, tmp_path):
        """Family/field coherence: 'harness:' demands backend: harness."""
        (tmp_path / "relay.yaml").write_text(
            "agents:\n"
            "  wrong: {backend: api, adapter: openai, harness: {timeout_seconds: 5}}\n",
            encoding="utf-8",
        )
        with pytest.raises(ConfigError, match="'harness:' block requires"):
            load_config(tmp_path)

    def test_unregistered_harness_adapter_names_the_adapter(self, tmp_path):
        """G0/R1: registry absence fails explicitly naming the adapter.

        P2.3: ``claude_code`` IS registered now, so the G0 refusal is pinned
        with a still-unregistered harness name instead.
        """
        (tmp_path / "relay.yaml").write_text(
            "agents:\n  agy: {backend: harness, adapter: antigravity_cli}\n",
            encoding="utf-8",
        )
        agent_cfg = agent_config(load_config(tmp_path), "agy")
        assert agent_cfg.backend is BackendType.HARNESS
        assert get_agent_class("claude_code") is ClaudeCodeAgent  # P2.3: registered
        with pytest.raises(UnknownAgentError, match="antigravity_cli"):
            get_agent_class("antigravity_cli")

    def test_unknown_agent_lists_knowns(self, tmp_path):
        (tmp_path / "relay.yaml").write_text(
            "agents:\n  gpt-api: {backend: api, adapter: openai}\n", encoding="utf-8"
        )
        with pytest.raises(ConfigError, match="gpt-api"):
            agent_config(load_config(tmp_path), "claude")

    def test_malformed_yaml_is_actionable(self, tmp_path):
        (tmp_path / "relay.yaml").write_text("agents: [not, a, mapping]\n", encoding="utf-8")
        with pytest.raises(ConfigError, match="relay.yaml"):
            load_config(tmp_path)

    def test_config_never_holds_secrets(self, tmp_path):
        (tmp_path / "relay.yaml").write_text(
            "agents:\n  gpt-api: {backend: api, adapter: openai}\n", encoding="utf-8"
        )
        dump = str(load_config(tmp_path).model_dump()).lower()
        assert "api_key" not in dump and "token" not in dump and "secret" not in dump


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / ".relay" / "relay.sqlite3"


@pytest.fixture()
def store(db_path):
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    migrate(conn)
    yield SqliteRelayStore(conn)
    conn.close()


class TestIdempotentInit:
    """M3 contract: canonical identity, one row, history preserved."""

    def test_init_creates_one_workspace_row(self, repo, store):
        workspace = initialize_workspace(repo, store.conn)
        assert workspace.kind is WorkspaceKind.GIT_REPO
        rows = list(store.all_models(Workspace))
        assert len(rows) == 1

    def test_reinit_preserves_id_and_history(self, repo, store):
        first = initialize_workspace(repo, store.conn)
        run = store.save_model(Run(agent="gpt", role=AgentRole.RESEARCHER))  # history marker
        store.save_model(Artifact(kind=ArtifactKind.RUN_OUTPUT, run_id=run.id, content="x"))
        second = initialize_workspace(repo, store.conn)
        assert second.id == first.id
        rows = list(store.all_models(Workspace))
        assert len(rows) == 1

    def test_reinit_refreshes_profile(self, repo, store):
        initialize_workspace(repo, store.conn)
        (repo / "src" / "new.go").write_text("package main\n", encoding="utf-8")
        initialize_workspace(repo, store.conn)
        profile = load_profile(repo)
        assert "go" in profile.languages

    def test_identity_key_is_canonical(self, tmp_path):
        folder = tmp_path / "Demo"
        folder.mkdir()
        key = identity_key(folder)
        assert key == identity_key(tmp_path / "demo") or key.lower() == identity_key(
            tmp_path / "demo"
        ).lower()  # normcase handles Windows case-folding
        assert key == identity_key(folder / ".." / "Demo")  # realpath collapses ".."

    def test_layout_paths(self, tmp_path):
        layout = workspace_layout(tmp_path)
        assert layout.profile_path == tmp_path / ".relay" / "profile.yaml"
        assert layout.config_path == tmp_path / "relay.yaml"
        assert layout.db_path == tmp_path / ".relay" / "relay.sqlite3"

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

from relay.agents.antigravity_cli import AntigravityCLIAdapter
from relay.agents.base import AgentRole, BackendType
from relay.agents.registry import UnknownAgentError, get_agent_class
from relay.context import (
    ConfigError,
    agent_config,
    discover_profile,
    identity_key,
    initialize_workspace,
    load_config,
    load_profile,
)
from relay.storage import connect, migrate
from relay.storage.models import Artifact, ArtifactKind, Run, Workspace
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


    def test_verification_block_parses_typed_argv(self, tmp_path):
        """P3.2 (frozen plan Q-c): typed argv, bounded timeout — never a
        free-form shell string."""
        (tmp_path / "relay.yaml").write_text(
            "agents:\n  gpt: {backend: api, adapter: openai}\n"
            'verification:\n  program: pytest\n  args: ["-q"]\n  timeout_seconds: 120\n',
            encoding="utf-8",
        )
        config = load_config(tmp_path)
        assert config.verification is not None
        assert config.verification.program == "pytest"
        assert config.verification.args == ["-q"]
        assert config.verification.timeout_seconds == 120


    @pytest.mark.parametrize(
        "block",
        [
            "verification:\n  args: []\n",  # missing required program
            'verification:\n  program: pytest\n  args: ["-q"]\n  timeout_seconds: 0\n',
            'verification:\n  program: pytest\n  shell: "pytest -q"\n',
        ],
    )
    def test_invalid_verification_block_is_a_config_error(self, tmp_path, block):
        (tmp_path / "relay.yaml").write_text(
            "agents:\n  gpt: {backend: api, adapter: openai}\n" + block,
            encoding="utf-8",
        )
        with pytest.raises(ConfigError):
            load_config(tmp_path)

    def test_budget_block_parses_max_fix_loops(self, tmp_path):
        """P6.2 (§23): the bounded fix-loop budget is the only recognized key."""
        (tmp_path / "relay.yaml").write_text(
            "agents:\n  gpt: {backend: api, adapter: openai}\n"
            "budget:\n  max_fix_loops: 1\n",
            encoding="utf-8",
        )
        config = load_config(tmp_path)
        assert config.budget is not None
        assert config.budget.max_fix_loops == 1


    @pytest.mark.parametrize(
        "block",
        [
            "budget:\n  max_fix_loops: -1\n",  # bound must be non-negative
            "budget:\n  max_fix_loops: 1.5\n",  # strict int — no coercion
            "budget:\n  max_fix_loops: true\n",  # bool is not an int
            "budget:\n  max_agents_per_task: 2\n",  # future §23 vocabulary
            "budget:\n  max_discussion_rounds: 3\n",
            "budget:\n  stop_on_consensus: true\n",
        ],
    )
    def test_invalid_budget_block_is_a_config_error(self, tmp_path, block):
        """Unsupported §23 keys fail loudly — never silently accepted."""
        (tmp_path / "relay.yaml").write_text(
            "agents:\n  gpt: {backend: api, adapter: openai}\n" + block,
            encoding="utf-8",
        )
        with pytest.raises(ConfigError):
            load_config(tmp_path)


    def test_api_backend_cannot_carry_harness_block(self, tmp_path):
        """Family/field coherence: 'harness:' demands backend: harness."""
        (tmp_path / "relay.yaml").write_text(
            "agents:\n  wrong: {backend: api, adapter: openai, harness: {timeout_seconds: 5}}\n",
            encoding="utf-8",
        )
        with pytest.raises(ConfigError, match="'harness:' block requires"):
            load_config(tmp_path)

    def test_unregistered_harness_adapter_names_the_adapter(self, tmp_path):
        """G0/R1: registry absence fails explicitly naming the adapter.

        P2.4: ``antigravity_cli`` IS registered now, so the G0 refusal is
        pinned with the stable synthetic placeholder ``future_cli`` instead
        (grilled decision Q-b: ends the per-release rename churn).
        """
        (tmp_path / "relay.yaml").write_text(
            "agents:\n  fut: {backend: harness, adapter: future_cli}\n",
            encoding="utf-8",
        )
        agent_cfg = agent_config(load_config(tmp_path), "fut")
        assert agent_cfg.backend is BackendType.HARNESS
        assert get_agent_class("antigravity_cli") is AntigravityCLIAdapter  # P2.4: registered
        with pytest.raises(UnknownAgentError, match="future_cli"):
            get_agent_class("future_cli")

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


    @pytest.mark.parametrize("block", ["bogus_role: gpt\n", "Planner: gpt\n"])
    def test_roles_with_unknown_role_address_is_a_config_error(self, tmp_path, block):
        (tmp_path / "relay.yaml").write_text(
            "agents:\n  gpt: {backend: api, adapter: openai}\nroles:\n  " + block,
            encoding="utf-8",
        )
        with pytest.raises(ConfigError, match="not a valid role address"):
            load_config(tmp_path)

    def test_roles_targeting_unconfigured_agent_is_a_config_error(self, tmp_path):
        (tmp_path / "relay.yaml").write_text(
            "agents:\n  gpt: {backend: api, adapter: openai}\n"
            "roles:\n  planner: missing_agent\n",
            encoding="utf-8",
        )
        with pytest.raises(ConfigError, match="not configured under 'agents:'"):
            load_config(tmp_path)

    def test_roles_and_reviewer_selector_are_decoupled(self, tmp_path):
        """P4.2 (frozen plan D4): no fallback, no conflict validation — a
        roles.reviewer entry MAY target a different agent than the P3.3
        build-flow ``reviewer:`` selector. Each governs its own workflow."""
        (tmp_path / "relay.yaml").write_text(
            "agents:\n"
            "  gpt: {backend: api, adapter: openai}\n"
            "  claude: {backend: harness, adapter: claude_code}\n"
            "roles:\n"
            "  reviewer: claude\n"
            "reviewer: gpt\n",
            encoding="utf-8",
        )
        config = load_config(tmp_path)
        assert config.roles["reviewer"] == "claude"  # bus vocabulary
        assert config.reviewer == "gpt"  # P3.3 build-flow selector

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
        assert (
            key == identity_key(tmp_path / "demo")
            or key.lower() == identity_key(tmp_path / "demo").lower()
        )  # normcase handles Windows case-folding
        assert key == identity_key(folder / ".." / "Demo")  # realpath collapses ".."

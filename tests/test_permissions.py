"""Permission defaults are a security contract (SPEC §18/§19)."""

from relay.core.permissions import (
    DEFAULT_POLICIES,
    Action,
    PermissionGate,
    Policy,
    ToolRequest,
)


class TestDefaults:
    def test_default_table_matches_spec_section_18(self):
        expected = {
            Action.READ_FILES: Policy.AUTO,
            Action.EDIT_FILES: Policy.AUTO,
            Action.RUN_TESTS: Policy.AUTO,
            Action.INSTALL_DEPENDENCIES: Policy.ASK,
            Action.RUN_MIGRATIONS: Policy.ASK,
            Action.GIT_COMMIT: Policy.ASK,
            Action.GIT_PUSH: Policy.ASK,
            Action.CREATE_PR: Policy.ASK,
            Action.MERGE_PR: Policy.NEVER,
            Action.DESTRUCTIVE_SHELL: Policy.NEVER,
        }
        assert DEFAULT_POLICIES == expected

    def test_every_action_has_a_policy(self):
        assert set(DEFAULT_POLICIES) == set(Action)


class TestGateOutcomes:
    def test_auto_allows(self):
        decision = PermissionGate().check(ToolRequest(action=Action.READ_FILES))
        assert decision.outcome == "allow"

    def test_ask_requires_human_approval(self):
        decision = PermissionGate().check(
            ToolRequest(action=Action.GIT_PUSH, agent="codex", reason="publish feature")
        )
        assert decision.outcome == "needs_approval"
        assert decision.policy is Policy.ASK

    def test_never_denies_destructive_operations(self):
        for action in (Action.MERGE_PR, Action.DESTRUCTIVE_SHELL):
            decision = PermissionGate().check(ToolRequest(action=action))
            assert decision.outcome == "deny"


class TestOverrides:
    def test_workspace_config_can_loosen_a_policy(self):
        gate = PermissionGate(policies={Action.GIT_PUSH: Policy.AUTO})
        assert gate.check(ToolRequest(action=Action.GIT_PUSH)).outcome == "allow"
        # Untouched actions keep secure defaults:
        assert gate.check(ToolRequest(action=Action.MERGE_PR)).outcome == "deny"

    def test_workspace_config_can_tighten_a_policy(self):
        gate = PermissionGate(policies={Action.READ_FILES: Policy.ASK})
        assert gate.check(ToolRequest(action=Action.READ_FILES)).outcome == "needs_approval"

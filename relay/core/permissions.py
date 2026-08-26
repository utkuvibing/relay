"""Permission layer — every tool call passes through here. No exceptions.

SPEC reference: §17 (Tool Layer), §18 (Permissions), §19 (Human Approval Gates).

Policies are declarative and start conservative:

* safe/read operations run automatically,
* side-effectful operations require explicit human approval,
* irreversible/destructive operations are denied outright.
"""

from __future__ import annotations

import enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Action(str, enum.Enum):
    """Tool-level actions an agent may request (SPEC §17/§18)."""

    READ_FILES = "read_files"
    EDIT_FILES = "edit_files"
    RUN_TESTS = "run_tests"
    INSTALL_DEPENDENCIES = "install_dependencies"
    RUN_MIGRATIONS = "run_migrations"
    GIT_COMMIT = "git_commit"
    GIT_PUSH = "git_push"
    CREATE_PR = "create_pr"
    MERGE_PR = "merge_pr"
    DESTRUCTIVE_SHELL = "destructive_shell"


class Policy(str, enum.Enum):
    """What Relay does when an action is requested."""

    AUTO = "auto"  # proceed without human interaction
    ASK = "ask"  # block until a human approves
    NEVER = "never"  # refuse unconditionally


#: Secure-by-default policy table (SPEC §18). Overridable per workspace config.
DEFAULT_POLICIES: dict[Action, Policy] = {
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

_OUTCOME_BY_POLICY: dict[Policy, str] = {
    Policy.AUTO: "allow",
    Policy.ASK: "needs_approval",
    Policy.NEVER: "deny",
}


class ToolRequest(BaseModel):
    """A request by an agent to execute a tool action."""

    action: Action
    agent: str | None = Field(default=None, description="Name of the requesting agent/adapter.")
    task_id: str | None = None
    reason: str | None = Field(default=None, description="Why the agent needs this action.")


class PermissionDecision(BaseModel):
    """The verdict of the permission gate for one tool request."""

    action: Action
    policy: Policy
    outcome: Literal["allow", "needs_approval", "deny"]


class PermissionGate:
    """Single choke point through which every tool call must pass.

    Binding invariant for Phase 2+ (SPEC §17/§19): all future tool
    execution takes exactly ONE public path — ``PermissionGate.check()``.
    Executors may act only on outcome ``allow``; ``needs_approval`` blocks
    until a human records an :class:`~relay.storage.models.Approval`;
    ``never`` is unconditional. No tool runner may bypass, wrap around, or
    pre-execute before the gate. The Phase 2 executor must be built on
    this contract, not beside it.
    """

    def __init__(self, policies: dict[Action, Policy] | None = None) -> None:
        #: Overrides win over defaults; unspecified actions keep secure defaults.
        self._policies: dict[Action, Policy] = {**DEFAULT_POLICIES, **(policies or {})}

    def policy_for(self, action: Action) -> Policy:
        return self._policies[action]

    def check(self, request: ToolRequest) -> PermissionDecision:
        policy = self.policy_for(request.action)
        return PermissionDecision(
            action=request.action,
            policy=policy,
            outcome=_OUTCOME_BY_POLICY[policy],  # type: ignore[arg-type]
        )


class CompletionPolicy(BaseModel):
    """Whether a reviewed task may finish WITHOUT explicit human approval.

    SPEC §19 intent, made conditional (App. A.3): safe/read-only or
    policy-approved workflows may complete directly after review; anything
    else must traverse APPROVAL_REQUIRED and record human approval.

    Secure default: ``require_human_approval=True`` — no task completes
    without an explicit human decision unless policy explicitly relaxes it.
    """

    model_config = ConfigDict(frozen=True)

    require_human_approval: bool = True

    def cleared(self, pending_approvals: int) -> bool:
        """True iff Relay may attest ``NO_PENDING_APPROVALS`` for a task.

        Both conditions are mandatory: policy must not demand a human, AND
        no approval request may be sitting in the queue.
        """
        return not self.require_human_approval and pending_approvals <= 0

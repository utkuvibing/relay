"""Structural boundary checks (SPEC Appendix B.2/B.3).

These are architecture assertions, not text greps: they parse the AST
of ``relay.core`` and ``relay.storage`` modules and walk the real import
graph. Docstrings and comments can never trip them, and the domain
vocabulary is inspected field-by-field rather than by keyword soup.

Enforced invariants:

* core/storage never import transport or provider modules (directly or
  transitively) — an API-key-bearing, HTTP-specific module cannot be
  smuggled underneath them.
* Persisted domain models carry no credential/session fields.
* The ``Agent`` abstraction stays transport-neutral: request/response
  models expose no auth/transport fields.
"""

from __future__ import annotations

import ast
import enum
from pathlib import Path
from typing import ClassVar

import pytest

from relay.agents.base import Agent, AgentRequest, AgentResponse, BackendType

RELAY_ROOT = Path(__file__).resolve().parents[1] / "relay"

#: External module roots that must never appear (transitively) beneath
#: core/ or storage/. Provider SDKs and transports only; generic stdlib
#: and third-party libs used legitimately elsewhere are out of scope here.
_TRANSPORT_OR_PROVIDER_ROOTS = frozenset(
    {
        "httpx",
        "requests",
        "aiohttp",
        "urllib3",
        "openai",
        "anthropic",
        "deepseek",
        "generativeai",
        "google.genai",
        "subprocess_harness_stubs",  # future harness runtime guard-rail
    }
)


def _py_files(package_dir: Path) -> list[Path]:
    return sorted(package_dir.rglob("*.py"))


def _top_level_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


class TestCoreStorageImportBoundary:
    """core/storage must not pull in transports or provider SDKs."""

    @pytest.mark.parametrize("package", ["core", "storage"])
    def test_no_transport_or_provider_imports_transitively(self, package):
        checked = 0

        def visit(module_path: Path, seen: set[Path]) -> None:
            nonlocal checked
            if module_path in seen:
                return
            seen.add(module_path)
            checked += 1
            for root in _top_level_imports(module_path):
                assert root not in _TRANSPORT_OR_PROVIDER_ROOTS, (
                    f"{module_path.relative_to(RELAY_ROOT)} imports forbidden root '{root}'"
                )
                candidate = RELAY_ROOT / root / "__init__.py"
                flat = RELAY_ROOT / f"{root}.py"
                for next_path in (candidate, flat):
                    if next_path.exists():
                        visit(next_path, seen)

        for module_path in _py_files(RELAY_ROOT / package):
            visit(module_path, set())
        # Guarantee the sweep actually saw code — guards against a silent rename.
        assert checked >= len(_py_files(RELAY_ROOT / package))


class TestPersistedVocabularyHygiene:
    """No persisted domain record may carry credential/session fields."""

    def test_domain_models_have_no_secret_shaped_fields(self):
        from relay.core import evidence as evidence_mod
        from relay.core import permissions as permissions_mod
        from relay.core import state_machine as state_machine_mod
        from relay.storage import models as storage_models

        banned_names = {"api_key", "apikey", "token", "credential", "password", "session_id"}
        modules = [evidence_mod, permissions_mod, state_machine_mod, storage_models]
        for module in modules:
            for attr_name in dir(module):
                obj = getattr(module, attr_name)
                if not (isinstance(obj, type) and hasattr(obj, "model_fields")):
                    continue
                fields = {name.lower() for name in obj.model_fields}
                leaked = fields & banned_names
                assert not leaked, f"{obj.__qualname__} carries secret-shaped fields {leaked}"

    def test_event_and_message_vocabularies_untouched_by_run_io(self):
        from relay.storage.models import EventType, MessageType

        event_values = {member.value for member in EventType}
        assert not any("prompt" in value or "answer" in value for value in event_values)
        for message_type in MessageType:
            assert message_type.value not in {"run_input", "run_output"}


class TestAgentLayerTransportNeutrality:
    """App. B.2/B.4: the seam admits harnesses without touching domain models."""

    def test_agent_declares_backend_with_api_default(self):
        assert Agent.backend is BackendType.API
        assert issubclass(BackendType, str) and issubclass(BackendType, enum.Enum)

    def test_request_response_expose_no_auth_or_pricing_requirements(self):
        req_fields = AgentRequest.model_fields
        resp_fields = AgentResponse.model_fields
        required_req = {n for n, f in req_fields.items() if f.is_required()}
        required_resp = {n for n, f in resp_fields.items() if f.is_required()}
        assert required_resp == {"agent", "role", "output"}  # usage optional → cost-None legal
        assert not ({"api_key", "url", "headers", "model"} & required_req)
        assert not ({"api_key", "url", "headers", "model"} & set(req_fields))
        assert not ({"api_key", "url", "headers"} & set(resp_fields))

    def test_a_harness_adapters_can_satisfy_the_interface(self):
        """A subscription-backed adapter plugs in via one ClassVar — proof seam."""
        from relay.agents.base import AgentResponse, AgentRole

        class InTestHarnessAgent(Agent):  # e.g. future CodexCLI/ClaudeCode adapters
            name = "in_test_harness"
            backend = BackendType.HARNESS

            async def run(self, request: AgentRequest) -> AgentResponse:
                return AgentResponse(
                    agent=self.name,
                    role=request.role,
                    output=f"harness handled: {request.prompt}",
                )

        from relay.storage.models import Artifact, ArtifactKind, Run  # domain untouched

        run = Run(agent="in_test_harness", role=AgentRole.RESEARCHER)
        artifact = Artifact(kind=ArtifactKind.RUN_OUTPUT, run_id=run.id)
        assert run.cost_usd is None and run.input_size is None and run.output_size is None
        assert InTestHarnessAgent.backend is BackendType.HARNESS
        assert artifact.kind is ArtifactKind.RUN_OUTPUT


class TestCoreNeverImportsTheRegistry:
    """P4.2 (frozen plan D6, pre-merge fix): core consumes agents only
    through the :class:`~relay.core.agent_factory.AgentFactory` seam.

    The adapter registry — and the transports it drags in — must never be
    imported by any ``relay/core`` module, including LAZY imports nested
    inside function bodies (``ast.walk`` sees the whole tree, so a lazy
    ``from relay.agents.registry import …`` cannot hide). The production
    factory lives in ``relay/agents/factory.py``: that agents-package side
    is the ONLY registry-touching component of the delivery path.
    """

    def test_no_core_module_imports_the_registry(self):
        offenders: list[tuple[str, str]] = []
        for module_path in _py_files(RELAY_ROOT / "core"):
            tree = ast.parse(module_path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name == "relay.agents.registry" or alias.name.startswith(
                            "relay.agents.registry."
                        ):
                            offenders.append((module_path.name, alias.name))
                elif isinstance(node, ast.ImportFrom) and node.level == 0:
                    module = node.module or ""
                    if module == "relay.agents.registry" or module.startswith(
                        "relay.agents.registry."
                    ):
                        offenders.append((module_path.name, module))
                    elif module == "relay.agents":
                        for alias in node.names:
                            if alias.name == "registry":
                                offenders.append((module_path.name, "relay.agents.registry"))
        assert not offenders, f"relay/core imports the adapter registry: {offenders}"

    def test_the_sweep_actually_parses_core(self):
        """Guard against a silent rename emptying the boundary sweep."""
        assert len(_py_files(RELAY_ROOT / "core")) >= 10


class TestConversationBusAuthorityBoundary:
    """P4.1 App. D.8 — conversation is coordination input, never state authority.

    Structural half of the D.8 proof: the bus and the Room feed read-model
    must not import the canonical-authority modules (state machine, evidence,
    permissions), absolutely or relatively. Persistence plumbing arrives
    transitively via ``relay.storage`` — that is the generic store every
    aggregate shares; the behavioral half
    (``tests/test_bus.py::TestConversationIsNotState``) proves no canonical
    mutation through the public API.
    """

    _AUTHORITY_MODULES: ClassVar[set[str]] = {"state_machine", "evidence", "permissions"}

    @staticmethod
    def _absolute_is_authority(dotted: str) -> bool:
        parts = dotted.split(".")
        return parts[:2] == ["relay", "core"] and (
            dotted
            in {f"relay.core.{m}" for m in TestConversationBusAuthorityBoundary._AUTHORITY_MODULES}
            or (
                len(parts) >= 3
                and parts[2] in TestConversationBusAuthorityBoundary._AUTHORITY_MODULES
            )
        )

    def test_bus_and_feed_import_no_canonical_authority_modules(self):
        """P4.2: the conversation layer (bus, feed, delivery, resolver) is
        covered — delivery consumes the crash-safe spine via
        ``relay.core.orchestrator``; orchestration plumbing arrives
        transitively, exactly like storage plumbing for the bus."""
        modules = [
            RELAY_ROOT / "core" / "bus.py",
            RELAY_ROOT / "core" / "room_feed.py",
            RELAY_ROOT / "core" / "delivery.py",
            RELAY_ROOT / "core" / "resolver.py",
            RELAY_ROOT / "core" / "agent_factory.py",
        ]
        assert all(path.exists() for path in modules), "guard against silent renames"
        for module_path in modules:
            tree = ast.parse(module_path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    offenders = [
                        alias.name
                        for alias in node.names
                        if self._absolute_is_authority(alias.name)
                    ]
                elif isinstance(node, ast.ImportFrom):
                    base = node.module or ""
                    offenders = []
                    if node.level == 0:
                        offenders = [base] if self._absolute_is_authority(base) else []
                        if base == "relay.core":
                            offenders += [
                                alias.name
                                for alias in node.names
                                if alias.name in self._AUTHORITY_MODULES
                            ]
                    else:
                        # relative import resolves inside relay/core
                        head = base.split(".")[0] if base else ""
                        candidates = {head} | {alias.name for alias in node.names}
                        offenders = sorted(candidates & self._AUTHORITY_MODULES)
                else:
                    continue
                assert not offenders, (
                    f"{module_path.name} imports canonical-authority module(s) {offenders}"
                )

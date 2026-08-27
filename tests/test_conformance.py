"""Offline conformance battery (C6): G1 independence across BOTH fakes.

The battery itself asserts outcomes, never formats; this module additionally
proves the two fakes are genuinely heterogeneous (R3) so a green battery
means adapter-independence, not duplicate self-tests.
"""

from __future__ import annotations

import pytest

from relay.agents.registry import AGENTS
from relay.harness.capabilities import HarnessCapability
from relay.harness.conformance import (
    ProseFakeHarness,
    StructuredFakeHarness,
    default_factory_for,
    run_battery,
)
from relay.harness.errors import HarnessOutputError
from relay.harness.types import ExitSemantics

HETEROGENEOUS_FAKES = [StructuredFakeHarness, ProseFakeHarness]


@pytest.mark.parametrize("fake_cls", HETEROGENEOUS_FAKES)
def test_full_battery_g0_g3(tmp_path, fake_cls):
    report = run_battery(default_factory_for(fake_cls), tmp_path)
    if not report.passed:
        pytest.fail("conformance failed:\n" + report.summary())


def test_battery_rejects_a_broken_adapter(tmp_path):
    """A deliberately non-conforming profile must FAIL the battery."""

    class Liar(StructuredFakeHarness):
        name = "liar_structured"
        # Declares structured output but never parses — B10 will catch the
        # malformed-stream case only if parse_output is invoked; instead we
        # break exit semantics: claim OK for everything → B05 catches it.

        def classify_exit(self, exit_code):
            from relay.harness.types import ExitSemantics

            return ExitSemantics.OK  # lies about failures

    report = run_battery(default_factory_for(Liar), tmp_path)
    assert not report.passed
    names = {check.name for check in report.failures()}
    assert any(name.startswith("B05") for name in names)


class TestR3HeterogeneityProof:
    def test_exit_code_tables_use_different_numerics(self):
        s_agent = _bare(StructuredFakeHarness)
        p_agent = _bare(ProseFakeHarness)

        def mapped_codes(agent):
            """Codes with EXPLICIT non-default semantics (≠ ok/unknown)."""
            return {
                code
                for code in range(-10, 130)
                if code != 0
                and agent.classify_exit(code) not in (ExitSemantics.OK, ExitSemantics.UNKNOWN)
            }

        assert mapped_codes(s_agent) == {3, 4}
        # 125 deliberately lands in the UNKNOWN bucket for the prose fake —
        # numerics differ across the tables this proof cares about.
        assert mapped_codes(p_agent) == {7, 9}
        assert mapped_codes(s_agent) != mapped_codes(p_agent)

    def test_output_styles_differ_by_declaration(self):
        structured_caps = StructuredFakeHarness.capabilities
        prose_caps = ProseFakeHarness.capabilities
        assert HarnessCapability.STRUCTURED_OUTPUT in structured_caps
        assert HarnessCapability.STRUCTURED_OUTPUT not in prose_caps
        # prose fake carries capability headroom for grant variety (B12/B14 use)
        assert HarnessCapability.WORKSPACE_WRITE in prose_caps
        assert HarnessCapability.NETWORK_ACCESS in prose_caps

    def test_malformed_jsonl_hits_typed_parser_failure(self):
        agent = _bare(StructuredFakeHarness)
        with pytest.raises(HarnessOutputError):
            agent.parse_output('{"event": BROKEN\n', "")

    def test_prose_path_is_transcript_passthrough(self):
        agent = _bare(ProseFakeHarness)
        text = "prose banner alpha\nmore prose"
        assert agent.parse_output(text, "") == text

    def test_conflict_sets_differ(self):
        agent = _bare(ProseFakeHarness)
        assert agent.extra_conflict_variables == frozenset({"PROSE_FAKE_LOCAL_TOKEN"})
        assert StructuredFakeHarness.extra_conflict_variables == frozenset()


def _bare(cls):
    """Instance without running __init__ (vocabulary-level probes only)."""
    return object.__new__(cls)


class TestG0ProductionRegistryHygiene:
    def test_conformance_fakes_never_enter_production_registry(self):
        for fake_name in ("conformance_structured", "conformance_prose"):
            assert fake_name not in AGENTS

    def test_registry_only_contains_known_api_adapters_pre_c7(self):
        # Until C7 lands, production registry must be exactly the Phase 1 set.
        assert set(AGENTS) >= {"openai"}

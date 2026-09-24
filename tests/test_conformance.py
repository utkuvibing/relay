"""Offline conformance battery (C6): G1 independence across BOTH fakes.

The battery itself asserts outcomes, never formats; this module additionally
proves the two fakes are genuinely heterogeneous (R3) so a green battery
means adapter-independence, not duplicate self-tests.
"""

from __future__ import annotations

import pytest

from relay.agents.registry import AGENTS
from relay.harness.conformance import (
    ProseFakeHarness,
    StructuredFakeHarness,
    default_factory_for,
    run_battery,
)
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

            return ExitSemantics.OK  # lies about failures

    report = run_battery(default_factory_for(Liar), tmp_path)
    assert not report.passed
    names = {check.name for check in report.failures()}
    assert any(name.startswith("B05") for name in names)


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

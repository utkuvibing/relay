"""Generic harness runtime — process-backed agent execution (SPEC §27 P2.1).

This package hosts the family-neutral machinery every harness adapter shares:
capability vocabulary (Appendix C.3), the child-process environment policy
(C.4), the async subprocess engine with guaranteed tree termination (G2),
executable discovery/version probing, the :class:`HarnessAgent` base whose
``run()`` is the single sanitized-error conversion point (R4/G3), and the
offline conformance battery used both here and by Adapter Ecosystem &
Certification (§27 Phase 11 / App. C.8).

Vendor product names never appear in this package (App. C.1): adapters live
in :mod:`relay.agents` (or their own packages) and subclass
:class:`relay.harness.runtime.HarnessAgent`.
"""

# Pre-P6 Hardening — strict typing, invariant coverage, execution safety contract

Status: implemented on `chore/pre-p6-hardening`; exit gate is CI-green merge.

This pass is a ratchet, not a feature: Relay enters P6 with strict static
checking, stronger invariant coverage, and an explicit, honest execution
safety boundary — while preserving the existing architecture and all frozen
P4/P5 semantics. No P6 loop code was written.

## Workstreams

### WS1 — Strict Pyright

- `pyright>=1.1.414` and `hypothesis>=6.168.0` added to the `dev` extra.
- `[tool.pyright]` in `pyproject.toml`: `typeCheckingMode = "strict"`,
  `pythonVersion = "3.11"`, `pythonPlatform = "All"` (dev is Windows, CI is
  Linux — platform-gated stdlib symbols must resolve on both), scoped to
  `relay/core`, `relay/storage`, `relay/harness` — the orchestration spine,
  canonical persistence, and the harness boundary.
- 493 initial diagnostics → 0. Real fixes dominated: private-member access
  across module boundaries replaced with public accessors
  (`ConversationBus.policy`, `.policy_envelope()`, `.validate_authorship()`;
  `HarnessAgent.settings`, `.profile`, `.workspace_root`,
  `.check_grant_capabilities()`; `SqliteRelayStore.in_transaction`),
  explicit generics on storage row decoding, cross-platform subprocess
  kwargs split in `relay/harness/process.py`, and complete signatures on the
  conformance battery.
- Runtime validation that Pyright calls unnecessary is *intentional* — the
  dataclass `__post_init__` guards in `relay/core/policy.py` enforce
  contracts on caller-constructed instances, and `contextmanager` typing in
  `relay/core/stage_facts.py` is an annotation quirk, not deprecated usage.
  These carry narrow `# pyright: ignore[...]` comments; no guard was removed
  to satisfy the analyzer.
- CI: new blocking job `types / pyright strict` on ubuntu-latest, and
  `required` now gates on `test`, `lint`, `types`, `package`.

### WS2 — Property/invariant tests

`tests/test_invariants.py` — 22 Hypothesis properties, all offline,
deterministic (`derandomize=True`), deadline-free for slow CI:

- **State machine** — every non-edge is `IllegalTransitionError`; DONE is
  terminal; a gated edge passes iff the store holds every required kind,
  with the `MissingEvidenceError.missing` set exact; foreign-task evidence
  never counts; producer-prefix restrictions are enforced.
- **Identity** — `derive_message_id`/`canonical_identity` are deterministic
  and injective (including adversarial `:`, `"null"`, `-`, unicode inputs);
  `StageContext.stage_key` is deterministic and sensitive to every field;
  `request_id` is deterministic.
- **Storage** — append-only tables refuse `update_model`/`delete_model`
  (`ImmutableHistoryError`) and rows stay byte-identical; duplicate ids are
  `IntegrityError`; a mid-transaction exception leaves zero partial writes;
  `protocol_executions` refuses both same-id and same-`(execution_key,
  scope)` inserts — one resume key can never mint two canonical executions.
- **Bus** — `send` is atomic: any typed `MessageRejected` leaves messages
  and markers untouched across generated sender/run_id/recipient/type
  combinations.
- **Budgets** — the blocking budget counts only non-`human:` senders and is
  never exceeded; the aggregate turn budget refuses exactly when
  `used >= limit` against generated `MESSAGE_DELIVERED` markers.
- **Evaluation** — `evaluate_stage` is pure/deterministic and completes only
  when canonical facts qualify (correct output types, synthesis when
  required, or the answered-early path); failures/budget exhaustion with
  incomplete outputs block. Reply text cannot fabricate completion.
- **Pinning** — protocol definitions round-trip byte-identical; any
  byte-level tamper or digest mismatch is refused by `decode_definition`.
- **Redaction** — credential literals, `NAME=value` credential assignments,
  and caller-supplied secrets never survive `redact`; `redact` is idempotent
  and total.

Latent bug found and fixed by the strict pass: `relay/core/delivery.py`
formatted a corrupt-marker refusal with `marker.id`, but `EventLogEntry`
has no `id` field (it has `sequence`) — the path would have raised
`AttributeError`, masking the typed refusal. Now `seq={marker.sequence!r}`,
covered by `test_deliver_and_reply_corrupt_marker_raises_typed_refusal`.

## Execution Safety Contract

What Relay guarantees around every execution, stated exactly. SPEC
references: §17–§19, Appendix C.2–C.5.

### Before spawn

1. **Grant first, always.** `HarnessAgent.resolve_grant` resolves
   explicit-request → profile `harness.grant` → adapter `default_grant`;
   no resolvable kind raises `MissingExecutionGrantError` before any
   process exists. There is no "all permissions" grant kind.
2. **Capabilities gate dangerous grants.** `check_grant_capabilities`
   requires `READ_ONLY_ACCESS` for read-only runs, `WORKSPACE_WRITE` for any
   write grant, and additionally `NETWORK_ACCESS` for
   `workspace_write_network`. Undeclared ⇒ `UnsupportedCapability` — an
   explicit typed refusal, never silent degradation.
3. **Delivery is read-only by construction.** `MessageDelivery` clones
   harness agents with `grant=READ_ONLY_ACCESS` regardless of configured
   profile, then still resolves and capability-checks the grant. Delivery
   can create only Runs, run artifacts, lifecycle events, and delivery
   markers — never task state, evidence, approvals, or decisions.
4. **Child environment is allowlisted.** `build_child_env` forwards only
   `BASELINE_ALLOWLIST` variables; `DEFAULT_CONFLICT_VARIABLES` plus
   per-adapter `extra_conflict_variables` are stripped from every child
   unless the adapter deliberately self-allows one (`self_allowed_env`).
   Relay-resolved API credentials are never forwarded.
5. **Prompts ride stdin, never argv** (conformance B12).
6. **Durability precedes provider I/O.** The delivery binding marker and
   the Run/INPUT artifact commit inside pre-provider Tx1
   (`BEGIN IMMEDIATE`), so a crash mid-run always leaves an honest,
   resumable binding — at-most-once initiation enforced by
   `DuplicateDeliveryRefusal` inside the same transaction.

### During execution

7. **cwd is pinned** to the adapter's `workspace_root`.
8. **Wall-clock timeout** is enforced per run (`HarnessAgentConfig.timeout_seconds`),
   and **process trees** — not just the direct child — are terminated via
   Windows Job Objects or POSIX process groups.
9. **Streams are bounded**: per-stream byte cap (`DEFAULT_STREAM_LIMIT_BYTES`,
   256 KiB) with truncation flagged, not crashed.
10. **Discovery is sanitized**: version probes cap output and redact
    executable paths to basenames in error text.

### After execution

11. **Exit codes map to typed semantics** via adapter `classify_exit`
    (`OK`/`USAGE`/`AUTH`/`TRANSPORT`/`UNKNOWN`); nonzero exits surface as
    hinted `AgentError`s, never raw escapes.
12. **Persisted diagnostics are sanitized.** `redact` masks credential-shaped
    literals, `NAME=value` credential assignments, caller-supplied secrets,
    and home-directory path prefixes before errors, run-failure reasons, and
    probe/discovery output reach the canonical ledger. Structured parse
    failures raise `HarnessOutputError` without leaking stream content.
    Canonical prompts, messages, and model outputs are intentionally
    persisted as canonical content — they are *not* globally redacted, by
    design: they are the record. Diagnostic surfaces are what `redact`
    guards; conversation/working content is authored data, not leakage.
13. **Output is capped** (`DEFAULT_OUTPUT_TEXT_CAP_CHARS`) before inline
    persistence.
14. **Harness output never owns state.** Task transitions resolve
    exclusively from provenance-backed `EvidenceRecord`s; a harness saying
    "done" is a claim, not a transition.

### What Relay does NOT guarantee — the honest boundary

- **No per-tool mediation inside unmediated harnesses.** Where an adapter
  exposes no tool/approval event streams (C.5 tier 2 absent), Relay records
  observed outcomes only — it does not claim per-tool enforcement. The grant
  tier is what is enforced; `PermissionGate` binds Relay-owned executors and
  any representable action, not harness internals.
- **Grant fidelity is adapter-translated.** `grant_arguments` maps grant
  kinds onto harness-native flags (e.g. `--permission-mode`, sandbox tiers,
  `--mode plan`). Conformance proves the translation exists and is applied;
  it cannot prove a vendor binary honors it.
- **No OS-level sandbox, and no worktree isolation today.** What exists is
  provenance, not containment: a pre-run filesystem baseline capture plus a
  post-run diff that attributes changes to the run — there is no clone or
  separate worktree the child is confined to. A compromised or buggy adapter
  running with a write grant writes directly into the workspace and can
  write outside it.
- **No network egress control** below the grant tier.
- **`redact` is pattern-based** — novel secret shapes it does not recognize
  can persist. The allowlist-boundary failure mode is under-masking, so
  review of new credential formats remains a human task.
- **Streams cap, processes don't.** Memory/CPU/disk usage of a runaway child
  is bounded only by the wall-clock timeout until a native runner exists.
- **SQLite history is integrity-checked, not encrypted.**

## Remaining risks

- Windows Job Object teardown is best-effort (`TerminateJobObject` failure
  is non-fatal by design); POSIX `setsid` does not contain a child that
  re-daemonizes into a new session.
- `protocol_executions` uniqueness is enforced by partial unique indexes —
  correct for current scope shapes, and any future compound-scope variant
  needs a new index, not relaxed checks.
- Adapter `auth_state` probing is best-effort and can misreport; selection
  treats it as a hint, never proof.
- Strict Pyright covers core/storage/harness; `relay/agents`, `relay/cli`,
  `relay/context` remain unenforced — deliberate scope boundary, not a
  claim that they are clean.

## Non-goals of this pass

- No P6 automated implementation loop, convergence detection, or fix-packet
  generation.
- No native Relay-owned tool executor, sandbox, or per-tool mediation.
- No changes to frozen P4/P5 semantics, append-only tables, policy
  vocabulary, or adapter profiles beyond the private→public access renames
  listed above.
- No new runtime dependencies.

## P6 gate

P6 work may proceed when, on `main`:

1. `types / pyright strict` is green and blocking (this branch adds it to
   `required`);
2. `tests/test_invariants.py` is green in the standard suite;
3. this contract is merged and remains accurate — any code that violates it
   is a bug, and any intentional change to it lands as a spec amendment
   first;
4. every new execution path P6 introduces satisfies §Before spawn items
   1–6 (grant resolution, capability check, env allowlist, stdin prompt,
   Tx1 durability, read-only-by-default delivery) or documents why it does
   not apply;
5. new dangerous capabilities/grant kinds arrive with conformance checks
   proving the adapter translation.

## Native-runner decision triggers

A Relay-owned native executor becomes justified — as a separate, planned
milestone, not absorbed into P6 — when any of these holds:

- a requested dangerous action (write/shell/network) has **no expressible
  grant translation** on a harness Relay wants to drive, i.e. honoring the
  policy would require over-granting;
- a harness **claims** a capability (C.3) but its behavior cannot be
  verified to match — conformance can prove mechanics, not intent, and a
  proven mismatch ends the trust assumption;
- unmediated write/shell grants are needed at a volume where provenance-only
  attribution (pre-run baseline capture + post-run diff extraction) can no
  longer be honestly attested per run — there is no worktree containment to
  fall back on;
- a requirement lands for resource bounds harness flags cannot express
  (CPU/memory/disk quotas, syscall filtering) — the Job Object seam in
  `relay/harness/process.py` is the natural substrate;
- tool/approval event streams exist but adapter-emitted semantics cannot be
  trusted without Relay-side verification.

Each trigger must be recorded as a Decision with provenance, since relaxing
into a native runner changes the trust boundary this document freezes.

## Interruption semantics — an explicit P6 decision

P6 planning must choose, deliberately, between two interruption models:

- **A. Boundary-only interruption** — the current agent run finishes, but
  Relay starts no further automated stages. This is the smaller semantic:
  it reuses the existing evidence-gated loop exits and needs no new
  process-control machinery.
- **B. Mid-run interruption** — the currently running harness process is
  cancelled immediately. This is *not* solved today: subprocess timeout
  cleanup exists (`run_prompt` kills the tree on expiry), but there is no
  cooperative cancellation path a caller can invoke mid-run, and no
  guaranteed process-tree teardown on a non-timeout signal. If P6 chooses
  B, a guaranteed tree-cleanup path on cancellation must be built first —
  the Job Object / process-group substrate in `relay/harness/process.py` is
  where it belongs.

This document does not pretend B already works.

## Validation performed

```text
pytest            819 passed, 10 skipped (796 baseline + 22 invariant + 1 marker regression)
ruff check .      clean
pyright           0 errors / 0 warnings on relay/core, relay/storage, relay/harness (strict, All platforms)
uv build          relay-0.1.0 wheel + sdist built
```

The CI `required` gate now enforces `test`, `lint`, `types`, and `package`.

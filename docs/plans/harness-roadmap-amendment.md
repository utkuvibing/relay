# Relay — Harness-Runtime Roadmap Amendment (Docs/Planning Only)

**Status:** Applied — this amendment is part of the same commit on main. This document
contains (a) the full pre-edit audit the user asked for and (b) the complete proposed
amendment text for `docs/SPEC.md` (+ README), ready to be applied verbatim after
switching out of plan mode. This does **not** implement Phase 2.

---

## 0. Scope of this report

1. Pre-edit audit of the entire current SPEC for provider-specific assumptions affected by the decision.
2. Inventory of every impacted phase/section with disposition (unchanged / example-only / amend / replace).
3. Which existing guarantees remain frozen vs which contracts require amendment.
4. Proposed new P2 work-package structure and downstream changes to P3–P11(+12).
5. Normative drafts: auth/trust-boundary contract, capability contract.
6. Frozen-P1 compatibility/migration concerns.
7. Acceptance gates, non-goals, genuinely unresolved questions.

---

## 1. External verification (Google product docs)

Checked before freezing terminology (per user requirement):

- Google Developers Blog, *“An important update: Transitioning Gemini CLI to Antigravity CLI”* (May 19, 2026):
  - **Since June 18, 2026**, Gemini CLI and Gemini Code Assist IDE extensions **stop serving requests** for Google AI Pro/Ultra and free individual accounts.
  - Gemini CLI remains accessible only via **enterprise licenses** (Gemini Code Assist Standard/Enterprise) and **paid API keys** (Gemini / Gemini Enterprise Agent Platform).
  - The consumer subscription terminal path is now **Antigravity CLI** (same agent harness as Antigravity 2.0, shared settings/permissions, “Subagents”, plugins; docs: antigravity.google/docs/cli).
- Antigravity CLI product blog confirms the single shared harness consolidation; it does **not yet document headless/non-interactive invocation or machine-readable output** at the level of detail needed for adapter design (→ open question Q1).

**Consequence for Relay vocabulary:** the P2.4 target is **Antigravity CLI**. Product names
(“Antigravity”) live only in adapter configuration/tests — never in core architecture words.
Gemini CLI survives as: (a) enterprise-license *harness* candidate (still own-auth ⇒ harness family),
(b) plain API-key usage ⇒ API family. Neither becomes the architectural Google abstraction; see §6 (C.1).

---

## 2. Audit — impacted sections of the current SPEC (`docs/SPEC.md` @ main)

Dispositions: **KEEP** (no change), **EX** (example-only wording; may stay), **AMEND**
(reword without changing structure), **REPLACE** (rewrite the block).

| SPEC § | Content | Disposition | Reason |
|---|---|---|---|
| §3.5, `relay ask claude` examples | provider-name examples | KEEP/EX | Names are config-time instances |
| §4 Architecture diagram (L180–185) | Agents column lists “GPT Claude DeepSeek … Codex” under one layer; Tools listed beside Storage | AMEND | Two execution families: API adapters vs harness adapters; harnesses *internally* execute tools Relay doesn't mediate per action. Diagram gains a split + trust-boundary note; smallest possible redraw |
| §5 Room/Run examples (“Codex / implementer”, “Verified by: Codex”) | EX | logical-agent names bound in config; §8 already separates model≠role |
| §6 State machine + “Everything is complete.” refusal | KEEP | Guarantee holds unchanged; add one cross-ref line to new Appendix C.7 (harness claims ≠ evidence) |
| §7 Adapter list incl. `CodexCLIAdapter`, `ClaudeCodeAdapter`, `OpenCodeAdapter` (L379–386) | AMEND | List stays but reorganized into API-family vs harness-family rows + sentence: all harness adapters share one generic harness runtime & contract (App. C.2/C.3). No structural change — B.2 already fixed backend vocabulary |
| §8 roles config (`implementer: agent: codex`) | EX/AMEND(1 clause) | Add sentence: role selection is by role+capability; provider names here are config instance names, never protocol vocabulary (needed so P5/P6 wording has a home) |
| §9–§11 protocols/participants | KEEP then AMEND in P5 phase entry | Participant names are roles — good; capability language lands via §27 Phase 5 edit, not here |
| §14 tables list | KEEP | New columns come additively via App. C.6, not table redesign |
| §15 event log | KEEP | EventType/MessageType separation already hardened (A.2) |
| §16 provenance output (“Repository verification: Codex”) | EX | Display binds producer identity; no contract change |
| §17 Tool Layer | AMEND | Add one paragraph: tool list covers Relay-*executed* tools; external harnesses may execute internal tools beyond Relay interception — governed instead by capability grants (App. C.5) |
| §18 Permissions “Her tool call Relay permission layer'ından geçmelidir” (L776) + §19 approval example | AMEND | Sharpen to the two-tier boundary (grant-time always; mediation where exposed). Default policy table itself unchanged — sandboxing maps dangerous grants onto existing Actions |
| §19 example “Codex wants to add dependency…” | EX | Valid specifically when harness exposes approval-event capability |
| §20 CLI commands | KEEP | No provider coupling |
| §21 Chat integration tree (Claude/Codex/DeepSeek nodes) | EX | Illustrative |
| §22 Moderator output | EX | Illustrative |
| §23–§24 budgets/anti-loop | KEEP | Budget keys already provider-neutral; token/USD budget note remains valid for API family; harness budget = wall-clock/run-count (mention in C.7 P3-delta only if trivial) |
| §25 Observability fields (agent/role/model/…cost) (L1019–1031) | AMEND | Fields become: requested model (existing `model`), resolved/reported model (nullable), harness version (nullable), usage optional-already. Additive; exact seam in C.6 |
| §26 MVP scope (“dozens of providers”, browser automation) | KEEP | Consistent with non-goals below |
| **§27 Phase 2 (L1128–1148)** | **“Codex / Local Tool Runtime”** | **REPLACE** | Becomes Generic Harness Runtime (P2.1–P2.4). Full draft in §5 of this report |
| §27 Phase 3 (state machine) | AMEND (1 para) | Add: harness observations/evidence normalization — textual claims route through EvidenceStore; TESTS_PASSED keeps tool_run_id provenance; no state-transition authority moves to harnesses |
| §27 Phase 4 (bus) | AMEND | Heterogeneous day one: mixed api↔harness scenarios become part of exit gate; routing on identity/role/backend family/capability |
| §27 Phase 5 (protocols) | AMEND | Protocols declare roles + required capabilities, never providers/models |
| §27 Phase 6 (loop) | REPLACE loop diagram | `Plan → Implementer → Verification → Reviewer → Fix → Implementer → Reviewer → PASS`; deterministic orchestration over capabilities/configured candidates; no `Codex` literal |
| §27 Phase 7 rooms | AMEND (bullets) | Member identity = logical agent id stable across backends; external-session references stored non-secretly; resume via SESSION_RESUME capability else fresh-run-with-context recovery |
| §27 Phase 8 provenance | AMEND (bullet) | Provenance graph nodes optionally carry backend family / harness version attributes |
| §27 Phase 9 server | AMEND (bullet) | Server serializes capability-aware agent descriptors; remote permission requests traverse the identical core PermissionGate (transport adds no privileges) |
| §27 Phase 10 MCP/chat | AMEND (example swap) | Selection examples by role/capability (“ask an adversarial reviewer”), not model names |
| **§27 Phase 11 “Provider Expansion” (L1389–1403)** | **REPLACE** | Reframed: **“Phase 12 — Adapter Ecosystem & Certification”** (new P-label) with compatibility matrix, version compatibility, conformance suite, capability certification, no-core-change admission. See §6 (C.8) |
| §28 Repository structure (`agents/codex_cli.py`, L1461) | AMEND | Suggested layout shows `agents/harness_runtime.py` (+ fakes), `agents/codex_cli.py`/`claude_code.py`/`antigravity_cli.py` as sibling adapters behind one contract |
| §29 First vertical slice (“Codex implements”, L1511–1516) | AMEND minimal | Replace 3 occurrences of hard-coded flow steps with “configured implementer (any harness/API adapter)” — semantically neutral loop |
| §30 Second vertical slice | EX | Model-name examples acceptable |
| §31 Third slice | EX | Illustrative |
| §32 Product boundary | KEEP | “Existing AI tools için coordination layer” reads *stronger* under heterogeneous backends |
| §33 Success criteria (“Provider independence”; Reliability) | KEEP | Already exactly right; optionally append half-sentence “including across execution families” |
| §34 Non-goals V1 | KEEP | Aligned |
| **§35 Build order labels (L1664–1692)** | AMEND | `P3 Codex + Tools` → `P3 Generic Harness Runtime`; `P12 Provider Expansion` → `P12 Adapter Ecosystem & Certification` |
| §36 Definition of Done | KEEP | Role-worded already |
| §37 North Star | EX | Illustrative dialogue |
| **App A.4 “every tool execution flows through PermissionGate.check()” (L1850–1856)** | **AMEND (refine, not weaken)** | Add scope sentence: invariant governs every tool executed *by Relay or through Relay-owned executors*; externally owned harness-internal tools fall under C.5 grant-time authorization + compensating controls. Gate remains mandatory whenever an action/permission item IS expressible |
| App B.2 execution families (L1875–1893) | AMEND | Family list stays; update the harness row examples: “Codex CLI (ChatGPT account auth), Claude Code (subscription auth), **Google's current subscription-backed CLI (Antigravity CLI)** — product names are adapter facts — plus future harnesses (OpenCode…)”. This kills the implicit “gemini_cli forever” assumption without renaming vocabulary |
| App B.3 auth ownership | KEEP + extend | Stays verbatim; C.4 adds env-filtering and persistence-allowlist rules beneath it |
| **App B.4 roadmap note (L1907–1914)** | **AMEND** | Last sentence currently promises “Claude Code, Gemini CLI, further harnesses arrive via later provider expansion (§27 Phase 11 spirit)” — replaced by pointer to Appendix C amendments |
| README.md Status table P2 row + App. B refs | AMEND | “P2 Generic harness runtime — process runtime + conformance suite; Codex CLI reference adapter; Claude Code; Google subscription path (Antigravity CLI)” |

### Code touchpoints (flagged for later implementation, NOT edited now)

These matter because they encode spec assumptions in strings/logic; they are listed so
implementation PRs can't miss them:

- `relay/context/config.py`: `_HARNESS_UNAVAILABLE` string says “arrives in Phase 2 (Codex CLI / local tool runtime)” → reword to “Generic Harness Runtime (SPEC §27 P2 / App. C)”.
- `relay/context/config.py::require_api_backed` → superseded in P2.1 by registry-driven availability (harness adapters registered ⇒ executable); the *function* can retire once the runtime exists.
- `tests/test_workspace.py`, `tests/test_cli.py`, `tests/test_openai_adapter.py` assert the Phase-2-pointer strings/messages → update alongside config.py.
- `tests/test_architecture.py::_TRANSPORT_OR_PROVIDER_ROOTS` already reserves `"subprocess_harness_stubs"` as a future guard-rail root — keep it for the fake/conformance runtime.
- Guard-rail trap found during audit: `TestPersistedVocabularyHygiene.test_domain_models_have_no_secret_shaped_fields` bans the field name `session_id` on persisted models ⇒ the allowed non-secret session reference must be named e.g. `external_session_ref` (never `session_id`). Wired into C.6.
- `Run.model` (frozen P1) — kept as-is; additive nullable seam in C.6.
- `PermissionGate` docstring “The Phase 2 executor must be built on this contract, not beside it.” — still true; C.5 refines *which* executions are inside its reach.

---

## 3. Guarantees: unchanged vs amended

**Frozen and re-affirmed verbatim (unchanged):**

1. LLM never owns workflow state — strengthened, not touched, by harnesses (C.7/P3 delta).
2. Evidence is provenance-backed — PROVENANCE_REQUIREMENTS / PRODUCER_REQUIREMENTS untouched; TESTS_PASSED still demands a Relay-scoped `tool_run_id`.
3. Event/message vocabularies remain disjoint (A.2).
4. Approval is policy-driven; `human:*` producers only (A.3).
5. Canonical history append-only.
6. Model ≠ role (now extended to backend ≠ role, C.7/P5).
7. API backend ≠ harness backend (B.2 — now carries the generic-runtime consequence).
8. Secrets env-only for API adapters (B.3) — unchanged.
9. Harness owns its authentication (B.3) — unchanged; C.4 adds environment-isolation machinery *around* the boundary without crossing it.
10. Relay never persists subscription credentials — unchanged and enforced harder (persistence allowlist in C.4 + existing hygiene tests).

**Contracts that require amendment (all refinements, none weaken):**

- **A.4 / §18 permission statement** — from “every tool call passes the gate” to “Relay is the single *authorization* choke point for granting a harness an execution capability; per-action mediation where the harness exposes events; honestly-unmediated otherwise, wrapped in compensating controls.”
- **§27 Phase 2** — from Codex-specific runtime to generic harness runtime (the headline change).
- **B.2/B.4** — product-naming freeze risk removed (Antigravity CLJ reality documented; Gemini CLI demoted to optional compat candidate).
- **§25 / Run record observability** — requested vs resolved model, harness version.
- **§27 Phases 3–10 wording** — capability/role-neutral semantics throughout; Phase 11 → Ecosystem & Certification reframed.
- New normative appendix **C** introduced; Appendices remain “amendments refine, never weaken.”

---

## 4. Proposed new P2 structure (work packages)

### P2.1 — Generic harness contract, process runtime, conformance suite

One deep module (target location `relay/agents/harness_runtime.py` + subpackage; adapters import it — the import direction stays outward-in, satisfying `tests/test_architecture.py`):

- **HarnessAgent contract** — extends `Agent` (keeps `run()`): declaration surface =
  `capabilities: frozenset[HarnessCapability]`, `discover() -> HarnessInfo`
  (executable path resolution, version probe), lifecycle ops (start/await/terminate),
  session seam (`open_session()/resume(ref)` reserved — may return
  `UnsupportedCapabilityError` until later phases consume it).
- **Process runtime services** used identically by every adapter:
  - subprocess lifecycle (spawn, stdout/stderr pumps, process-tree termination; Windows-safe);
  - working-directory control (cwd pinning to a prepared workspace/worktree);
  - timeouts & cancellation (soft deadline → graceful stop → hard kill; status=CANCELLED mapped onto frozen RunStatus enum — no new statuses);
  - stdout/stderr normalization (line records, bounded retention, charset handling);
  - structured-output parsing *where supported* (`--json`-style streams best-effort parsed; absent support degrades to prose transcript artifact — never to a state transition);
  - exit-code semantics table (ok/usage/auth/exec) normalized per adapter profile;
  - **persisted-error sanitization**: stored error strings pass through redaction (env-var values, absolute home paths, argv secrets scrubbed) before hitting store/event log;
  - **child-process environment policy** (see C.4): explicit inherit allowlist + per-adapter conflict-variable stripping; parent environment snapshot is *not* handed wholesale;
  - **crash-safe P1 integration**: `run_input` artifact written *before spawn* (mirrors B.1 discipline), run status transitions persisted in the same order P1 uses today;
  - **normalized artifacts/evidence**: outputs land as ArtifactKind values that already exist (`DIFF`, `REPORT`, `TEST_RESULT`, `RUN_OUTPUT`); evidence creation strictly via the existing EvidenceStore paths;
  - **offline conformance suite**: `FakeHarness` (scripted executable stubs via `subprocess_harness_stubs` pattern) exercising lifecycle/timeout/env-policy/sanitization/error-mapping; **every real adapter must pass the identical suite**.
- Exit gate G1 in §8.

### P2.2 — Codex CLI reference adapter

First consumer of P2.1; defines the canonical “adapter profile” shape: discovery command
(`codex --version`), exec mode (`codex exec`), structured events (`--json` stream if stable),
sandbox/read-only flags mapped to capability grants, auth-state probe that consumes no quota,
env-conflict variable set (`OPENAI_API_KEY`, `OPENAI_BASE_URL` — stripped from child env unless
this adapter explicitly whitelists its own variables). Upstream instability → profile revision
documented in the adapter's conformance fixture versions.

### P2.3 — Claude Code adapter

Second real adapter — **must ship purely against P2.1's published contract** (this pairs with
G3: adding it may touch nothing under `relay/core|storage|context`). Profile items: `-p/--output-format json`,
`--resume` session ref handling (persist only the non-secret conversation reference, policy-gated),
permission-mode flags surfaced as grant translations, env-conflict set
(`ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_BASE_URL`).

### P2.4 — Google subscription-backed adapter (**expected: Antigravity CLI**)

Naming rule from C.1 applies. Feasibility contingent on Q1 (headless invocation + machine-readable
output). If headless automation is unsupported, escalate rather than degrade: package ships with
capability set declared empty-of-streaming and raises explicit errors — or is deferred with a
documented reason. **Not named `gemini_cli`.**

### P2.5 (optional, defensible-only) — Gemini CLI compatibility

Only candidates: enterprise-license harness usage. If nobody runs Relay against such a license,
skip entirely — the architectural point (second/third harness without core edits) is carried by
P2.3/P2.4 regardless. Decision checkpoint: end of P2.4.

---

## 5. Drafted SPEC replacement — §27 Phase 2 (exact text to apply)

```markdown
# Phase 2 — Generic Harness Runtime

Amaç:

Subscription-backed (harness) coding agent'ları provider'a özgü varsayımlar
olmadan koordine etmek. Mimari kelime dağarcığı provider isimleri değildir;
her harness bir adaptördür ve hepsi tek bir generic runtime paylaşır.

Implement:

* P2.1 — Generic harness contract + process runtime + conformance suite:
  subprocess lifecycle; executable discovery & version inspection;
  working-directory control; timeout & cancellation; structured-output
  parsing where supported; stdout/stderr normalization; persisted-error
  sanitization; exit-code semantics; capability declaration; auth-state
  detection without credential extraction; child-process environment
  policy; crash-safe integration with the P1 orchestrator/store;
  normalized artifacts/evidence; session/resume seam; offline
  fake-harness conformance tests (SPEC Appendix C.2–C.5).
* P2.2 — Codex CLI reference adapter (first subscription-backed runtime).
* P2.3 — Claude Code adapter.
* P2.4 — Google's current subscription-backed CLI adapter — expected to
  be Antigravity CLI unless current official documentation establishes
  otherwise (SPEC Appendix C.1). Product names are adapter-specific.
* Optional P2.5 — Gemini CLI compatibility only where still defensible;
  it is never the architectural abstraction for Google.

Exit gates:

1. ```bash
   relay build "Make a small code change"
   ```
   çalıştırıldığında yapılandırılmış harness diff ve tool/output evidence'ı
   Relay'a geri getirmeli (ARTIFACT/EVIDENCE kayıtlarıyla).
2. **Second-real-harness gate:** ikinci gerçek harness, P2.1 sözleşmesini
   kullanarak core abstractions'a dokunmadan entegre olabilmelidir.
3. **Auth-conflict gate:** ebeveyn ortamda başka sağlayıcıya ait API anahtarları
   olsa bile harness çocuk süreci bunları görmemeli (Appendix C.4 tests).
```

---

## 6. Proposed new normative content — SPEC **Appendix C** (draft, apply in full)

```markdown
# Appendix C — Generic Harness Runtime Amendments (ratified before Phase 2)

Normative amendments covering harness-backed execution from Phase 2 onward.
They refine — never weaken — prior appendices and §27.

## C.1 Execution-family vocabulary and product naming (amends §7, App. B.2/B.4)

Core vocabulary is: execution family (api | harness), adapter, backend,
capability, role. Vendor product names (Codex, Claude Code, Antigravity CLI,
Gemini CLI, OpenCode) exist ONLY in adapter modules, configuration values,
and their tests — never in core module names, state-machine logic, routing,
or protocol definitions. Provider URLs/product pivots must be absorbable by
editing adapter packages alone.

Current knowledge frozen here for planning purposes (verify against official
docs at implementation time): Google serves consumer subscription terminal
agent usage through Antigravity CLI (successor surface to Gemini CLI for AI
Pro/Ultra/individual accounts since June 2026); Gemini CLI persists for
enterprise licensing and paid API-key access. Therefore the Google
subscription-path adapter targets Antigravity CLI; Gemini CLI remains an
optional compatibility candidate only.

## C.2 Generic harness contract (amends §7, enables §27 Phase 2)

Every harness adapter implements the same contract — discoverable
executable + version inspection; declared capabilities; managed subprocess
lifecycle (spawn/pumps/graceful→hard terminate); pinned working directory;
timeouts/cancellation with normalized run status; stdout/stderr
normalization; best-effort structured-output parsing; sanitized persisted
errors; exit-code semantics profile; capability-gated feature use;
session/resume seam (may be explicitly unsupported); environment policy
(C.4); crash-safe persistence identical to P1 ordering (input artifact
before spawn). Adapters that fail lifecycle MUST leave the canonical store
consistent with any other failed run.

## C.3 Capability contract (extends §7/§17 selection vocabulary)

Capabilities are typed, declared statically by each adapter, and queried by
core — code asks WHAT a harness can do, never WHICH vendor it belongs to.

Candidate set (closed for extension across releases; additions require an
appendix note): structured_output, read_only_access, workspace_write,
shell_execution, git_operations, tool_event_stream, approval_event_stream,
session_resume, model_selection, resolved_model_reporting,
token_usage_reporting, diff_reporting, network_access.

Rules:
* Unsupported capability ⇒ explicit typed failure (`UnsupportedCapability`)
  at request validation time. Silent degradation is forbidden.
* Capability declarations feed authorization (C.5) and selection (P4/P5):
  routing and protocol engines match on (role, required_capabilities,
  backend_family), not provider names.
* Declared-but-broken capabilities fail adapter conformance certification
  (P12), not merely review.

## C.4 Authentication & environment trust boundary (amends §18 context; preserves App. B.3)

Binding rule: **Relay invokes the harness. The harness owns authentication.**

Relay must not: read browser/session tokens; copy subscription credentials;
persist OAuth/session credentials; impersonate provider login flows;
convert subscription authentication into Relay-owned credentials.

Environment isolation for harness child processes:
* Children receive an explicit ALLOWLIST baseline (OS-required variables:
  PATH/HOME-USERPROFILE/TEMP/TMP/system roots/locale) — not the raw parent
  environment.
* Every adapter declares its `conflict_variables` — provider auth variables
  that would flip the harness into another billing/auth mode (e.g. the
  OpenAI pair for Codex CLI; the Anthropic triple for Claude Code; Gemini/
  Google API variables for the Google adapter). Relay strips ALL adapters'
  conflict sets from every harness child environment by default; an adapter
  may whitelist a variable only for itself, deliberately, in its profile.
* Relay-resolved API credentials are never forwarded into any harness child
  process. An unrelated provider key in Relay's parent environment must
  never alter harness billing mode — enforced by an auth-conflict test
  matrix (fake harness echoes its received environment; assertions are
  data, not prose).

Persistable harness facts (allowlist — everything else is forbidden):
* adapter identity/name, discovered executable label + version;
* configured auth mode IF safely observable (e.g. "subscription", "api_key"
  as declared by the harness itself);
* auth_state ∈ {authenticated, unauthenticated, unknown};
* a NON-SECRET external session reference when a provider exposes one AND
  config explicitly opts in (field name `external_session_ref`; never a
  secret-shaped value).

Prohibited from persistence: tokens, cookies, OAuth artifacts, account
identifiers usable for login, anything derived from scraping login state.
Hygiene enforcement lives in the existing persisted-vocabulary tests and
their extension to new models.

## C.5 Permission boundary — two tiers (refines §17/§18/§19; amends App. A.4 wording)

Trust boundary, stated exactly:

1. **Grant tier (always enforced, non-overridable):** a harness run starts
   only with an Execution Grant chosen by Relay policy — at minimum one of
   read_only_access / workspace_write / workspace_write+network, translated
   onto harness-native restriction flags where available and into Relay-side
   containment where not. Dangerous Relay actions (install_dependencies,
   git_push, destructive_shell, merge_pr…) appear in the grant decision with
   their existing policy outcomes auto/ask/never. There is NO way for a run
   to obtain a capability whose governing policy is `never`, and no harness
   bypasses Relay authorization by owning its internal tools.
2. **Mediation tier (conditional):** where a harness exposes tool/approval
   event streams, Relay mediates them — events map onto ToolRun records and
   approval flows through existing human-approval mechanics (A.3). Where a
   harness exposes none, Relay DOES NOT CLAIM per-tool enforcement; it
   records observed outcomes only.

Compensating controls for unmediated internals (required wherever tier 2 is
absent and writes/shell/network are granted): dedicated worktree/clone
containment; pre/post repository-state snapshots; post-run diff extraction
as DIFF artifact; sanitization of captured errors (C.4); explicit evidence
that verification ran through Relay-owned channels.

Wording amendment to A.4: the single-path invariant continues to bind every
tool execution performed BY Relay or through Relay-owned executors; harness-
internal tool execution falls under this appendix's grant tier. The gate
remains mandatory whenever an action/permission is representable — owning
internal tools never confers bypass rights.

## C.6 Run/persistence model seam (extends §14/§25; no gratuitous change)

Frozen P1 `Run.model` stays as REQUESTED model. Additive, nullable,
provider-neutral columns planned before harness loops need them:
`resolved_model` (reported by harness when known), `adapter_version`
(harness binary/version), `external_session_ref` (C.4 allowlist), plus
`backend` (snapshot of execution family at run time) for audit symmetry.
SQLite migration = ADD COLUMN only; historical rows unchanged. Any richer
backend metadata is deferred and, if ever required, arrives as a strict,
redaction-checked JSON column — never free-form secrets.

## C.7 Roadmap consequences (annotates §27 P3–P10)

* P3: harness-emitted statements (implementation complete / tests passed /
  task done) enter ONLY as claim-bearing artifacts & evidence candidates —
  TESTS_PASSED still requires a Relay-scoped tool_run_id; REVIEW_PASSED /
  IMPLEMENTATION_PRODUCED still require run_id. State transitions resolve
  exclusively from the EvidenceStore. Verification gates remain authoritative.
* P4: bus supports heterogeneous pairs day one (api→harness, harness→other
  harness, planner-api→implementer-harness→reviewer-*); routing keys are
  identity/role/backend_family/capability. Hard-coded provider branching is
  a test-forbidden pattern.
* P5: discussion protocols request roles + required capabilities; binding to
  concrete adapters happens through config/selection, never protocol text.
* P6: automated loop is provider-neutral: Plan → Implementer → Verification
  → Reviewer → Fix → Implementer → Reviewer → PASS; implementer/reviewer/
  planner roles are independently replaceable; candidate selection is
  deterministic over capabilities + availability + auth_state.
* P7: room member identity = logical agent id (stable across backend swaps);
  harness-specific mutable facts live on runs/tasks; resumability honors
  session_resume capability else reconstructs via Context Engine (fresh run,
  honest discontinuity).
* P8: provenance graph nodes may carry backend family/harness version.
* P9: server serialization includes capability-aware descriptors; remote
  permission requests traverse the identical core gate (transport grants no
  privileges).
* P10: chat/MCP-facing selection queries role+capability (e.g. “adversarial
  reviewer with review-pass history”), resolves agents via the registry.

## C.8 Phase 11 reframing (amends §27 Phase 11)

Old scope (“Provider Expansion”: OpenAI/Anthropic/DeepSeek/Codex CLI/Claude
Code/OpenCode/local APIs) is conceptually retired: heterogeneous backends
arrive with P2, BEFORE multi-agent orchestration (P4). Replaced by:

**Phase 12 — Adapter Ecosystem & Certification**
* more API providers; more harnesses; local runtimes;
* adapter × harness-version × OS compatibility matrix;
* conformance suite upgrades (offline fakes mandatory; live smoke opt-in);
* capability certification & honest-declaration auditing;
* credential-hygiene audit per adapter;
* admission criterion: a compliant provider/harness integrates with ZERO
  modifications to workflow/state-machine/core-protocol code (enforced by
  import-direction architecture tests).
Exit gate: a brand-new compliant adapter (third-party-authored is ideal)
passes certification without touching relay/core, relay/storage, or the bus.
```

---

## 7. Downstream changes to P3–P12 (summary of diffs already folded into C.7 + §27 edits)

| Phase | Change |
|---|---|
| P3 | Normalized-harness-evidence note only; state machine untouched |
| P4 | Heterogeneous-pair exit gate; capability-based routing |
| P5 | Roles+capabilities protocol requests; no provider names in protocol configs |
| P6 | Loop rewritten neutral (`Implementer/Reviewer`); deterministic selection |
| P7 | Logical member identity; safe external-session refs; resume semantics |
| P8 | Backend/version provenance attributes (additive serialization) |
| P9 | Capability-aware serialization; identical core gate for remote requests |
| P10 | Role/capability-based agent selection from chat/MCP clients |
| Old P11 | Renumbered/reframed as **P12 — Adapter Ecosystem & Certification** (C.8) |

---

## 8. Compatibility/migration concerns from frozen P1

1. **Schema:** `Run.model` retained; only nullable additive columns (C.6). SQLite `ADD COLUMN` migration ships with the first harness-dependent feature; earlier releases unaffected.
2. **Field naming:** new persisted fields must dodge the banned-names set in `test_architecture.py` (`session_id`, `token`, `credential`, …) — hence `external_session_ref`, `adapter_version`.
3. **AgentRequest transport-neutrality tests:** adding grant info to requests must keep `{api_key,url,headers,model}` OUT of AgentRequest fields (a `model` field on the request is test-banned). Preferred mechanism: optional, non-required `execution_grant` or ride-along in `metadata:`; both pass current assertions. Final choice is an implementation-time detail; contract point is: no required transport/auth fields.
4. **User-visible strings:** `_HARNESS_UNAVAILABLE` Phase-2-pointer message + three tests asserting it (`test_workspace.py::test_harness_entry_refuses_to_execute`, `test_cli.py::test_harness_agent_errors_with_phase2_pointer`, `test_openai_adapter.py` unknown-adapter case) get wording refreshes with P2.1 — behavior-preserving.
5. **Registry:** gains harness adapters; alias stability rule (config names don't migrate) preserved from P1.
6. **Guard-rail root:** `"subprocess_harness_stubs"` AST-test reservation stays until the conformance runtime picks its final module name; architecture import-direction invariants apply to the new runtime (harness runtime imports core/storage; core NEVER imports adapters or runtime).
7. **No rewrite** of working P1 code required for this amendment; everything above is additive or string-level.

---

## 9. Acceptance gates

- **G1 (P2.1):** FakeHarness + ≥1 independently written conforming fake pass the offline conformance suite; suite exercises every C.2 service surface.
- **G2 (P2.2):** End-to-end `relay build` with Codex CLI produces persisted diff/output artifacts + evidence records; zero credential-shaped values in store/event-log snapshot diff (hygiene audit).
- **G3 (second-real-harness — architectural acceptance gate, the headline):** introducing the P2.3 adapter touches ZERO files under `relay/core`, `relay/storage`, `relay/context` (verified by diff + import-graph tests). Adapters differ only in their own packages + registry/config entries.
- **G4:** Unsupported-capability operations fail explicitly with typed errors and clear CLI/server rendering; no silent degradation anywhere (test matrix per adapter).
- **G5 (auth-conflict matrix):** For every harness adapter, child environments contain none of ANY adapter's conflict variables, given a maximally polluted parent environment; attacker-in-simulation via inherited env is demonstrably cut off.
- **G6 (ecosystem, former-P11 spirit):** A novel compliant adapter completes certification with no core/state-machine/protocol modifications — the standing exit gate thereafter.

---

## 10. Non-goals (mirrored & extended)

This amendment does NOT: implement P2 or any runtime code; rewrite working P1 behavior; put provider-specific logic in core; persist subscription credentials; add browser automation to steal/reuse login state; collapse API and harness backends into one implementation; hardcode Codex/Claude/Gemini/Antigravity as roles or as core vocabulary; weaken evidence, approval, or permission guarantees; rename Gemini-cli-era vocabulary gratuitously where it still describes execution *families* correctly; expand P2 scope beyond the work packages above (session/resume implementations, certification infra → later phases).

---

## 11. Unresolved questions (genuinely implementation-time research)

- **Q1 Antigravity CLI automation surface:** Does it expose headless/non-interactive invocation, machine-readable output, or scriptable approvals? Current public docs describe interactive-first use. Determines whether P2.4 is a normal adapter or needs escalation/deferral (user instruction honored: decision pends on official docs).
- **Q2 Structured-output stability matrix:** exact, versioned behavior of `codex exec --json`, `claude -p --output-format json/stream-json`, sandbox/permission-mode flags (`--sandbox read-only|workspace-write`, `--permission-mode`), and their equivalents per adapter — feeds conformance fixtures and min-version pins.
- **Q3 Certified harness reporting:** may a harness's OWN test results ever mint `TESTS_PASSED`-equivalent evidence (e.g. a relay-certified reporter channel producing a tool_run equivalent)? Deferred to P3/P6; default answer remains NO (Relay-owned verification).
- **Q4 Auth-state probing without side effects/quota consumption:** which harness commands safely reveal `authenticated/unauthenticated` per adapter, and how errors map to `unknown`.
- **Q5 Session-reference persistence policy:** which providers expose non-secret continuation handles worth persisting, privacy trade-offs, and opt-in config shape (`relay.yaml` per-agent flag).
- **Q6 Cross-platform environment baseline:** minimal guaranteed-safe child-env allowlist on Windows/macOS/Linux (SYSTEMROOT, COMSPEC, proxy behaviors) without weakening C.4 stripping.
- **Q7 Gemini CLI enterprise-license demand:** does anyone want an enterprise-license Gemini CLI harness adapter in P12, or is the API-family path sufficient?
- **Q8 Version-drift posture:** pin minimums per adapter or track latest; failure mode when discovery finds an unsupported major.

---

## 12. Application checklist (when leaving plan mode)

1. Apply §5 replacement into `docs/SPEC.md` §27 Phase 2.
2. Insert Appendix C (§6 draft) after Appendix B.
3. Apply per-section amendments from the §2 table (B.2 family-list wording, B.4 last sentence, A.4 refinement sentence, §17/§18 paragraphs, §25 fields sentence, §35 labels, §28 layout snippet, §29 wording, Phase 3–10 annotation lines, Phase 11 → C.8 pointer).
4. Update `README.md` status-table P2 row + trailing App.-refs paragraph.
5. Leave all code/strings untouched except the four flagged test/message updates, which belong to the P2.1 implementation PR — NOT this docs PR.


---

## 14. Execution record

This amendment was applied in the same session that produced it
(user decision: full amendment + report). In this commit:

* `docs/SPEC.md` — Phase 2 rewritten as Generic Harness Runtime; Appendix C added;
  A.4 refined; B.2/B.4 updated; P3–P10 annotated; Phase 11 reframed as Adapter
  Ecosystem & Certification; supporting sections amended per §2 dispositions.
* `README.md` — status table updated.

No code was changed and no implementation work for Phase 2 was started;
sections §0–§12 above are the pre-application audit/drafts exactly as planned.

---

## 15. P2.1 execution record

P2.1 (generic harness contract + process runtime + conformance suite) was
implemented as commits C1-C7 on main; C8 is this docs sync. Highlights
against the plan's gates:

* G0 executability separation: `build_agent` validates registry presence +
  backend-family match before construction; test fakes reach flows only via
  the context-scoped `transient_adapters` seam (production registry frozen,
  hygiene-tested).
* G1 adapter independence: the full battery runs against both heterogeneous
  fakes (JSONL-event vs prose/noisy with disjoint exit numerics and declared
  `failure_modes`).
* G2 tree termination: POSIX process groups + Windows ctypes Job Object
  kill-on-close with taskkill fallback; tombstone-heartbeat check proves no
  descendant survives timeout.
* G3 error boundary: `HarnessAgent.run()` remains the single sanitized
  conversion point; fault-injection matrix asserts raw exceptions never escape.

Non-goals held: no real adapters, no schema migration, no orchestrator or
P1 behavior changes, zero credential persistence. Suite: 236 passed / 1 skipped;
ruff clean.

---

## 16. P2.2 execution record

P2.2 (Codex CLI reference adapter + `relay build`, gate G2) was implemented
on `feat/p2.2-codex-cli-adapter`. Highlights against the plan's gates:

* **Adapter profile** (`relay/agents/codex_cli.py::CodexCLIAdapter`, registry
  keys `codex_cli`/`codex`): upstream surface verified against official Codex
  docs at implementation time — `codex exec --json` JSONL events, stdin-only
  prompt delivery (`-` sentinel), sandbox flag translation
  (`read-only` / `workspace-write` / workspace-write + network via `-c`),
  token usage captured from `turn.completed` (cost stays None), conservative
  exit semantics (0=OK else UNKNOWN; numerics not version-stable upstream),
  `failure_modes=()` for the same honesty reason.
* **Env policy (C.4)**: `CODEX_API_KEY` joins the stripped conflict union;
  `CODEX_HOME` is a deliberate self-whitelist (directory pointer, not a
  secret) — it must sit in the adapter's conflict set to be resurrectable.
  Verified: other adapters' children still strip CODEX_HOME.
* **App. C.6 seam shipped as SCHEMA_VERSION 2**: four nullable ADD COLUMNs on
  `runs` (`resolved_model`, `adapter_version`, `backend`,
  `external_session_ref`); fresh v1 DBs upgrade in place preserving rows;
  observation mapping leaves api-backed runs byte-identical (no observation ⇒
  no change). `external_session_ref` always persisted NULL in P2.2 (C.4 opt-in
  does not exist yet); `thread_id` parsed but not stored.
* **Auth probe (Q4 resolution)**: `codex login status` output classified into
  AuthState enum and discarded — no quota, no credential extraction;
  probe failures map to UNKNOWN.
* **Offline conformance**: codex-shaped fixture child + a `_CodexHooks`
  carrier REUSES the real adapter's parser/grant/env declarations through the
  identical B01–B13 battery; negative control proves a lying profile fails
  B05. Live smoke exists but is doubly gated (`RELAY_RUN_LIVE_TESTS=1` +
  discoverable codex); CI cannot activate it (no secrets wired).
* **G2 closed by `relay build`**: task row + TASK_CREATED → crash-safe
  run_ask spine → observed JSONL item events recorded as sanitized ToolRun
  rows (TOOL_COMPLETED) → Relay-owned post-run diff via gated
  `git add -N`/`git diff HEAD` (intent-to-add so new harness files appear,
  .relay/relay.yaml excluded) saved as DIFF artifact (ARTIFACT_CREATED) →
  IMPLEMENTATION_PRODUCED evidence with run provenance. TESTS_PASSED is not
  minted anywhere (Q3 default NO held). Clean-tree refusal ignores untracked
  files (init artifacts), refuses modified tracked content only.
* **G4/G5 re-proven**: B11 typed-refusal and B07/B08 auth-conflict checks ran
  green against the codex profile via the battery.

Verification at implementation time: full offline suite green
(see commit message for exact counts); hygiene audit asserts decoy key shapes
absent from DB/WAL bytes on a polluted parent environment.

Known deferred: worktree-containment mode (clean-tree refusal suffices for
the reference slice), session-resume consumption (P7 seam), proxy-variable
whitelisting for network grants (needs explicit C.4 note first).

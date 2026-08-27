# Relay — Room & Bounded Inter-Agent Communication Amendment (Docs/Planning Only)

**Status:** Applied — this amendment is part of the same commit on main. This document
records (a) the pre-edit audit, (b) the dispositions taken against the existing spec,
and (c) the execution record. All normative text lives in `docs/SPEC.md`
(Appendix D + amended sections). Docs/planning only — no code was implemented.

---

## 0. Scope

Formally capture the intended **Relay Room** experience (persistent group chat with a
user-selected AI team; discussion-first planning; explicit plan freeze; automatic
execution) and the **bounded, typed inter-agent communication model** inside that
experience — without touching any frozen guarantee or implementing anything.

Deliverables edited: `docs/SPEC.md`, `README.md` (status row), this record.

---

## 1. Pre-edit audit of `docs/SPEC.md` @ main

| Area audited | Finding | Disposition |
|---|---|---|
| §5 Room / Run / Message objects | Room exists with members/workspace/task; Message already carries sender/recipient/room/task/type/references/timestamp. No seat concept, no mode concept. | AMEND §5 (seat/binding paragraph); extend record-field contract in App. D.5 only |
| §6 State machine | Frozen transition graph incl. `PLAN_READY`; harness claims ≠ evidence note present (C.7). No concept of waiting-for-clarification; none needed. | AMEND §6 (one paragraph: freeze boundary maps onto existing edges; runtime waits are not TaskStates — App. D.3/D.6) |
| §9 Conversation Bus | Challenge-flow example; bounded by protocol. No typing/blocking/policy language. | AMEND §9 (typed, addressed, blocking-flagged, policy-bounded messaging — App. D.5–D.7) |
| §12 Context Engine | Workspace-map selection only; no task-participant reconstruction view. | AMEND §12 (per-participant reconstructed context; transcript replay excluded by default — App. D.10) |
| §14 tables / §15 event log | messages/messages tables and MESSAGE_SENT markers fine | KEEP (disjointness preserved, see A.2 below) |
| Appendix A.2 EventType/MessageType split | MessageType listed as the Phase-0 frozen six-value conversational set | AMEND A.2 (pointer: additive extension per App. D.5 from Phase 4 onward; disjointness invariant unchanged) |
| Appendix C.7 roadmap consequences (P3–P10) | Heterogeneous bus, capability routing, resume-vs-reconstruction all present | EXTEND C.7 pointer → App. D.11 refines P4–P7 without changing C.7 wording |
| §27 Phase 4 (Multi-Agent Messaging) | Bus, roles, heterogeneous day one, capability routing | AMEND (typed messages, blocking/non-blocking, canonical-record references, Room feed, API↔harness same-protocol acceptance) |
| §27 Phase 5 (Discussion Protocols) | Role+capability participants; no communication-permission/budget layer | AMEND (communication policy + budgets + escalation — prevents free autonomous group chat) |
| §27 Phase 6 (Automated Loop) | Provider-neutral outer loop; max loop count; final verification | AMEND (bounded micro-interactions inside stages + five proof obligations; outer loop kept verbatim) |
| §27 Phase 7 (Rooms) | Logical member identity; resume-or-reconstruct | AMEND (seats/bindings, persistent history, plan/decision/finding relationship graph, targeted user messages, persistent-across-sessions goal) |
| §32 Product Boundary | Coordination-layer statement | AMEND (product north-star paragraph — App. D.1 wording, placed here as the product boundary statement) |
| §35 Recommended Build Order | Stale legacy labels shifted off §27 Phase numbering (e.g. "P4 State Machine", "P6 Multi-Agent Bus"; loop listed before bus) | REPLACE list (align P-labels with §27 Phase numbers; declare §27 numbering canonical) + C.8 parenthetical fixed accordingly |
| README status table | "P3+ See SPEC §27 roadmap" | AMEND P3+ row |

Cross-cutting guarantees re-checked during audit and left intact: evidence
provenance (A.1, C.7-P3), producer conventions (`human:*` approvals, `relay:*`
attestations — A.1/A.3), append-only event/canonical store (§15), two-tier
permission boundary (A.4/C.5), execution-family separation (B.2/C.1),
product names confined to adapters/config/tests (C.1).

### Code facts verified (not modified)

* `relay/storage/models.py`: `MessageType ∈ {opinion, challenge, rebuttal,
  final_position, synthesis, review_finding, system}`; `Message` has
  sender/recipient(None=broadcast)/room_id/task_id/type/content/references;
  `DecisionStatus` already contains `SUPERSEDED`; `ArtifactKind` already
  contains `PLAN` and `PROPOSAL`. The amendment therefore requires **no**
  schema rework promises — D.5 extensions are declared additive for P4.
* `tests/test_architecture.py` asserts EventType/MessageType disjointness by
  iteration — unaffected by documentation; A.2 invariant restated, not changed.

---

## 2. Guarantees explicitly preserved (restated in App. D, unchanged in force)

1. **LLM never owns workflow state** (§2, A.1) — strengthened: even resolved
   clarifications take effect only via Relay-promoted canonical records.
2. **Evidence remains provenance-backed** (A.1) — TESTS_PASSED still demands
   Relay-scoped `tool_run_id`; harness/conversation claims stay candidates.
3. **Canonical history is append-only** (§15) — promotion creates new records,
   never rewrites.
4. **Model ≠ role / backend ≠ role** (§8, B.2, C.7) — extended by D.2
   (seat bindings are config/runtime choices, rebinding-safe).
5. **API family ≠ harness family** (B.2) — D.5/D.11 messaging is
   family-heterogeneous through one protocol.
6. **Harness claims are not authoritative evidence** (§6, C.7) — unchanged.
7. **Relay remains the coordination/authorization layer** (§2, §32) —
   unchanged; micro-loops route through Relay.
8. **No provider-specific concepts in core protocol vocabulary** (C.1) —
   D examples are marked illustrative config-instance values only.
9. **PermissionGate / Execution Grant supremacy** (A.4, C.5) — reiterated
   negatively in D.8 (conversation grants no authority).
10. **Frozen §6 state machine** — D.6 explicitly forbids new TaskStates for
    scheduler/wait modeling.

---

## 3. Terminology conflicts discovered & resolutions

1. **Roadmap labeling collision.** §35 carried pre-harness-draft labels shifted
   off §27 Phase numbers ("P4 State Machine", "P6 Multi-Agent Bus"), and sequenced
   the implementation loop before the bus, contradicting §27 Phases 4→6 and the
   operative convention used everywhere else (README rows, git tags `p2.x`,
   C.7 annotations). **Resolution:** §35 list rewritten to mirror §27 Phase
   titles/numbers exactly; a sentence declares §27 Phase numbering canonical
   (used by gates/commits/status). C.8's "(build-order label `P12` in §35)"
   becomes "(§27 Phase 11)". Ordering ambiguity (loop before bus) resolved in
   favor of §27: infrastructure (bus/protocols) precedes the automated loop.
2. **BLOCKER / PLAN_REVISION_REQUEST / DECISION_REQUEST vs frozen MessageType.**
   Freezing new enums wholesale would contradict A.2 and the instruction to
   extend "only where semantically necessary". **Resolution:** blocking-ness is
   message metadata (`blocking` flag), not a type; "blocker" renders as a
   blocking-flagged challenge/proposal/finding; plan-revision requests are
   `PROPOSAL` messages referencing the target plan revision; decision responses
   reuse the existing answer-class vocabulary (`rebuttal`/`final_position`) /
   `clarification_response`. Only `CLARIFICATION_REQUEST`, `CLARIFICATION_RESPONSE`,
   `PROPOSAL`, `NOTE` are added, additively, at P4 time (exact final casing of
   values deliberately left unfrozen; D.5 fixes concepts, not spellings).
3. **"Members" vs seats/bindings.** §5's Room example mixes model names and
   roles ("GPT / moderator"). **Resolution:** D.2 defines *seat* (protocol side)
   vs *binding* (config side); §5 keeps its illustrative example with an added
   note that names are config-instance instances (matching the long-standing §8
   convention).
4. **Free-form debate (§9/§10) vs Room micro-communication (new).** Two different
   phenomena sharing primitives. **Resolution:** D scopes Room micro-communication
   to *inside* stages of a deterministic outer workflow under policy/budgets;
   §9–§11 debate protocols remain the bounded outer discussions. Cross-referenced,
   not duplicated.

---

## 4. Normative deltas applied (summary)

* New **Appendix D** — Room & bounded inter-agent communication amendments
  (D.1 north star + normative principles; D.2 seats/bindings; D.3
  discussion-first + canonical plan contract incl. freeze boundary and
  supersedes chains; D.4 conversation-is-not-state + promotion path;
  D.5 typed messages + reconciliation table + record fields; D.6
  blocking/non-blocking without new TaskStates; D.7 policy/budgets/loop
  detection/escalation; D.8 no-hidden-authority-transfer invariants;
  D.9 user participation capabilities; D.10 Context Engine reconstruction +
  resume-as-optimization; D.11 P4–P7 roadmap consequences).
* Amended: §5, §6, §9, §12, §27 P4/P5/P6/P7, §32, §35, App. A.2, App. C.7
  header, App. C.8 parenthetical.
* `README.md`: P3+ status row updated.

---

## 5. Non-goals held

No messaging code; no new TaskStates; no Phase 2 redesign; P2.2 untouched;
evidence semantics unchanged; permission semantics unchanged; no UI syntax
frozen ("Freeze Plan & Execute" labeled illustrative); message enum spellings
left open where existing vocabulary suffices; no provider names introduced
into core/protocol vocabulary; historical record documents
(`harness-roadmap-amendment.md`) deliberately NOT retro-edited.

---

## 6. Unresolved (implementation-phase) questions

* Q-A: exact config surface for communication policy (YAML shape mirroring
  D.7's illustrative constraint tree) — decide at P5.
* Q-B: where blocking queues live (orchestrator in-memory vs store-backed)
  and how paused stages survive process restart — decide at P6; crash-safety
  must match P1 discipline.
* Q-C: escalation UX when clarification budgets exhaust (CLI prompt vs stored
  pending question surfaced by `relay status`) — decide at P5/P6.
* Q-D: human/Relay system senders in the Room feed re-use A.1 producer
  conventions (`human:*`, `relay:*`) — confirm naming at P4.
* Q-E: minimum viable "@role" addressing syntax for CLI/chat surfaces — P7
  product phase; D.9 intentionally does not freeze syntax.

---

## 7. Execution record

Applied in this session/commit on main:

* `docs/SPEC.md` — edits enumerated in §4 above.
* `docs/plans/relay-room-amendment.md` — this document.
* `README.md` — status table P3+ row.

No files under `relay/` or `tests/` were touched; no product/implementation
work started.

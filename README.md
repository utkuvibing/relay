# Relay

**General-purpose AI collaboration runtime** — a local-first coordination layer between AI models, coding agents, tools, and humans.

Relay is not a model. Relay is the layer that:

- owns **state** (LLMs never own workflow state),
- carries **context** between agents,
- routes **messages** on a conversation bus,
- enforces **permissions** and human approval gates,
- persists every **run**, message, artifact, and decision,
- refuses to close a task until verification gates pass.

```bash
relay init
relay ask gpt "Analyze this repository"
relay status
relay history
relay discuss "Should this service use Redis?"
relay build "Add authentication"
relay review
relay why 18
```

## Status

🚧 In development — see [docs/SPEC.md](docs/SPEC.md) for the full specification.

| Phase | Scope | State |
|---|---|---|
| P0 | Specification freeze — core abstractions in code | ✅ hardened |
| P1 | Single-agent runtime (`relay init`, `relay ask`, SQLite) — first **API-backed** runtime (OpenAI-compatible HTTP) per SPEC App. B.4; harness-backed agents arrive in Phase 2 | ✅ |
| P2 | **Generic harness runtime** (SPEC §27 Phase 2 + App. C): P2.1 ✅ process runtime + conformance suite (G0–G3); P2.2 ✅ Codex CLI reference adapter + `relay build` (G2); P2.3 ✅ Claude Code adapter (safe-mode tool allowlists); P2.4 ✅ Antigravity CLI adapter (read-only plan-mode grants; write tier pending a vendor clamp flag) | 🔶 |
| P3 | **Deterministic task state machine** (SPEC §27 Phase 3 + App. A): P3.1 ✅ lifecycle wired into `relay build` (context → plan → implement, every edge evidence-gated); P3.2 ✅ Relay-scoped verification runner (exit code is the only verdict); P3.3 ✅ review + approval closure (`relay approve`, gated default + A.3 direct path); P3.4 ✅ task observability (`relay status` machine position + evidence gaps, `relay inspect <task>` ledger) | ✅ |
| P4 | **Multi-agent messaging** (SPEC §27 Phase 4 + App. D): P4.1 ✅ conversation bus core — typed/addressed message ledger (D.5 vocabulary + blocking metadata), append-only `messages` at both enforcement layers, schema v3, Room feed read-model (D.8 invariant proven structurally + behaviorally); P4.2 ✅ role/logical-agent resolution + delivery — `roles:` config (decoupled from the P3.3 `reviewer:` selector) + production `ConfigRoleResolver`, strict run-authorship provenance (`messages.run_id`, schema v4, validated against `run.agent == sender`), Relay-mediated delivery through the crash-safe spine (Tx1 `MESSAGE_DELIVERED` binding marker, unconditional at-most-once initiation, always-explicit READ_ONLY delivery grant with a no-fallback negative authority test, deterministic delivery envelope + `context_refs` pass-through); P4.3 blocking reply pairing + bounded round-trips; P4.4 bounded multi-agent driver + heterogeneous api→harness→different-harness exit gate | 🔶 |
| P5+ | Discussion protocols (communication policy & budgets) → semantic execution loop with plan-freeze gate → persistent Rooms ("AI group chat" model) — see SPEC §27 roadmap + Appendix D | ⬜ |

Runs are persisted crash-safely: the prompt lands as a `run_input` artifact
before any provider call, so it survives failures by construction
(SPEC App. B.1). Secrets live only in the environment (`OPENAI_API_KEY` or
`DEEPSEEK_API_KEY`);
`relay status` reports "configured / not configured" and nothing more.

### DeepSeek BYOK (temporary local setup)

Relay's API adapter speaks the OpenAI-compatible Chat Completions protocol, so
DeepSeek only needs a provider-specific base URL and model. The local
`relay.yaml` already contains a `deepseek` agent. Put your key in the ignored
`.env` file:

```dotenv
DEEPSEEK_API_KEY=your-key-here
RELAY_API_KEY_ENV=DEEPSEEK_API_KEY
```

Run Relay with that file loaded:

```bash
uv run --env-file .env relay status
uv run --env-file .env relay ask deepseek "Reply with exactly: DEEPSEEK_OK"
```

The adapter targets `https://api.deepseek.com/chat/completions`; see the
[official DeepSeek API documentation](https://api-docs.deepseek.com/) for the
provider's current models and request details. Never put the actual key in
`relay.yaml`, source code, or a commit.

## Development

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync --extra dev
uv run pytest
```

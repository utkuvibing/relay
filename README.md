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
| P2 | **Generic harness runtime** (SPEC §27 Phase 2 + App. C): process runtime + conformance suite (P2.1); adapters — Codex CLI (P2.2), Claude Code (P2.3), Google subscription path, expected Antigravity CLI (P2.4) | ⬜ |
| P3+ | See SPEC §27 roadmap | ⬜ |

Runs are persisted crash-safely: the prompt lands as a `run_input` artifact
before any provider call, so it survives failures by construction
(SPEC App. B.1). Secrets live only in the environment (`OPENAI_API_KEY`);
`relay status` reports "configured / not configured" and nothing more.

## Development

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync --extra dev
uv run pytest
```

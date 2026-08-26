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
relay discuss "Should this service use Redis?"
relay build "Add authentication"
relay review
relay why 18
```

## Status

🚧 In development — see [docs/SPEC.md](docs/SPEC.md) for the full specification.

| Phase | Scope | State |
|---|---|---|
| P0 | Specification freeze — core abstractions in code | ✅ |
| P1 | Single-agent runtime (`relay init`, `relay ask`, SQLite) | ⬜ |
| P2 | Codex / local tool runtime | ⬜ |
| P3+ | See SPEC §27 roadmap | ⬜ |

## Development

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync --extra dev
uv run pytest
```

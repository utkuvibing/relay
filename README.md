# Relay

Local-first orchestration for AI models, coding agents, tools, and people.

Relay runs configured agents, keeps run and task state in SQLite, and puts
verification and approval outside the model's response. It is a runtime, not
another model provider.

## What works today

| Capability | Status | Details |
|---|---|---|
| Single-agent API runs | Available | `relay ask` runs one configured API or harness agent and persists its input and output. |
| Codex OAuth | Available | The `codex` harness uses the Codex CLI's own account authentication. |
| DeepSeek BYOK | Available | The `deepseek` agent uses DeepSeek's OpenAI-compatible Chat Completions API. |
| Task execution | Available | `relay build` drives a task through evidence-gated context, plan, implementation, verification, review, and approval steps. |
| Run and task inspection | Available | `relay status`, `relay history`, and `relay inspect` read the local ledger. |
| Conversation bus core | In progress | P4.1 adds typed, addressed, append-only messages and a Room-feed read model. |
| Automatic agent-to-agent delivery | In progress | P4.2 role resolution and Relay-mediated delivery are done; P4.3 reply pairing and the P4.4 bounded multi-agent driver remain. |
| `relay discuss` and persistent Rooms | Planned | These belong to P5 and P7. The commands are not implemented yet. |

The important boundary is simple: calling two agents separately does not make
them talk. Each call is its own run. Relay will coordinate model-to-model
messages through the P4 bus and P5 discussion protocols once those phases land.

## Quick start

Relay requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync --extra dev
uv run relay init
uv run relay status
```

The checked-in `relay.yaml` contains the project agents. `relay init` creates
the local `.relay/` profile and SQLite database without overwriting an existing
configuration.

## Run Codex with OAuth

Authenticate the Codex CLI once with its own account flow, then run it through
Relay:

```bash
codex login
uv run relay ask codex "Reply with exactly: RELAY_OAUTH_OK"
```

Relay does not read or store the Codex subscription session. The harness owns
that authentication.

## Run DeepSeek with BYOK

DeepSeek uses the same wire format as the OpenAI Chat Completions API. The
provider is still DeepSeek. No OpenAI account or OpenAI key is involved.

Create or edit the local, gitignored `.env` file:

```dotenv
DEEPSEEK_API_KEY=your-key-here
RELAY_API_KEY_ENV=DEEPSEEK_API_KEY
```

Then load that file explicitly when invoking Relay:

```bash
uv run --env-file .env relay status
uv run --env-file .env relay ask deepseek "Reply with exactly: DEEPSEEK_OK"
```

The project configuration keeps provider facts only:

```yaml
agents:
  deepseek:
    backend: api
    adapter: openai_compatible
    model: deepseek-v4-flash
    base_url: https://api.deepseek.com
```

The adapter appends `/chat/completions` to `base_url` and sends the key as a
Bearer token. The key stays in the environment and never enters `relay.yaml`,
source code, or Relay history. See the [official DeepSeek API documentation](https://api-docs.deepseek.com/)
for the current request format and model list.

## How a run is recorded

`relay ask` follows one agent from request to result:

```text
CLI command
    -> configured agent
    -> API adapter or harness adapter
    -> SQLite run, artifacts, and lifecycle events
```

Relay writes the prompt as a `run_input` artifact before the provider call. A
successful response becomes a `run_output` artifact. Failures are persisted as
sanitized run errors.

`relay build` adds a task state machine around harness execution. The model can
report that it is done, but Relay only advances the task when the required
evidence, verification, review, and human approval records exist.

## Agents and adapters

The adapter name selects an execution implementation. The logical name under
`agents:` selects the configured agent you use from the CLI.

| Execution family | Implemented adapters | Authentication |
|---|---|---|
| API | `openai`, `openai_compatible`, `gpt` | Environment-provided key |
| Harness | `codex_cli`, `claude_code`, `antigravity_cli` | The harness owns its login or session |

This separation lets an API agent such as DeepSeek and a harness agent such as
Codex share Relay's run and task records without pretending they use the same
transport or billing model.

## Roadmap

Statuses describe the repository, not a promised release date.

| Phase | Scope | Status |
|---|---|---|
| P0 | Specification freeze and core contracts | Done |
| P1 | Single-agent runtime, API adapter, SQLite persistence | Done |
| P2 | Generic harness runtime, process isolation, Codex/Claude/Antigravity adapters | In progress |
| P3 | Deterministic task state machine, verification, review, approval, and observability | Done |
| P4 | Multi-agent messaging and heterogeneous delivery | In progress: P4.1 bus core, P4.2 role resolution + delivery, and P4.3 reply pairing done; P4.4 remains |
| P5 | Bounded discussion protocols, communication policy, and budgets | Planned |
| P6 | Automated implementation review and fix loop | Planned |
| P7 | Persistent Rooms and long-lived participant context | Planned |
| P8 | Decision provenance | Planned |
| P9 | Relay server | Planned |
| P10 | MCP and chat interface integration | Planned |
| P11 | Adapter ecosystem and certification | Planned |
| P12 | TUI | Planned |

### What P4 means

P4 is split into four concrete slices:

1. P4.1, the conversation bus core: typed and addressed messages, append-only
   storage, and a deterministic Room-feed read model.
2. P4.2, role and logical-agent resolution plus Relay-mediated delivery.
3. P4.3, reply pairing, blocking replies, and bounded round trips.
4. P4.4, a bounded multi-agent driver with API-to-harness-to-harness coverage.

P4.1 gives Relay somewhere safe to store conversation traffic. It does not yet
dispatch a prompt to two models or feed one model's answer to another. P5 adds
the rules that decide who may speak to whom, for what purpose, and how many
rounds are allowed. P7 turns that machinery into a persistent group-chat
experience.

## Development

Install the development dependencies and run the test suite:

```bash
uv sync --extra dev
uv run pytest
uv run ruff check relay tests
```

The full specification and design decisions live in
[`docs/SPEC.md`](docs/SPEC.md). P4.1 implementation notes are in
[`docs/plans/p4.1-conversation-bus-core-plan.md`](docs/plans/p4.1-conversation-bus-core-plan.md).
P4.2 implementation notes are in
[`docs/plans/p4.2-role-resolution-delivery-plan.md`](docs/plans/p4.2-role-resolution-delivery-plan.md).
P4.3 implementation notes are in
[`docs/plans/p4.3-reply-pairing-blocking-replies-plan.md`](docs/plans/p4.3-reply-pairing-blocking-replies-plan.md).

## Project layout

```text
relay/
  agents/       API and harness adapters
  cli/          Typer commands and terminal rendering
  context/      Configuration and workspace discovery
  core/         Orchestration, state machine, and conversation bus
  harness/      Process runtime, grants, and sanitization
  storage/      SQLite schema, models, events, and stores
tests/          Unit, integration, conformance, and persistence tests
docs/           Specification, roadmap amendments, and research notes
```

## Security rules

- Keep API keys in environment variables or an ignored local `.env` file.
- Keep `relay.yaml` limited to non-secret provider facts.
- Let harnesses own their subscription authentication.
- Treat model claims as evidence candidates. Relay's state machine and
  permission gates make the actual transition decisions.

## License

MIT

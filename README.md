# Relay

Relay is a local-first orchestration runtime for AI models, coding agents, and the tools around them. It gives agents shared, durable state and coordinates bounded handoffs between them.

## The problem

Each AI tool normally owns its own conversation and run history. Moving work between tools means copying prompts and outputs by hand. A model can also say "done" without having run the required checks or received approval.

Relay keeps coordination state outside the model. It records work in one local SQLite ledger, routes messages between logical agents, and advances tasks only when the required evidence exists.

## Why Relay is different

| Common failure mode | Relay's answer |
| --- | --- |
| Agents are isolated in separate tools. | Relay routes typed, addressed messages between logical agents and roles. |
| People copy-paste context between agents. | The bounded driver forwards persisted answers across API and harness agents, reducing manual handoffs. |
| "PASS" is a model claim. | Relay-owned verification and provenance-backed evidence control task transitions. |
| Approval is implicit or buried in chat. | Approval is a first-class record and, by default, a human must grant it. |

Conversation is coordination input, not workflow authority. Messages cannot silently change task state, evidence, or approval records.

## Available today

### Task workflows

- `relay ask <agent> "<prompt>"` runs one configured API or harness agent and persists its input, output, and sanitized failures.
- `relay build "<task>"` drives a task through context, planning, implementation, configured verification, review, and completion gates.
- `relay approve <task-id> --by <name>` records explicit human approval. The default path is `approval_required`; `approval: {mode: direct}` is an explicit opt-out that still requires verification and review evidence.
- `relay status`, `relay history`, and `relay inspect` expose the local ledger.

The ledger lives at `.relay/relay.sqlite3` and stores tasks, runs, artifacts, tool runs, evidence, approvals, events, and inter-agent messages.

### Multi-agent orchestration

The implemented P4 runtime provides:

- typed, addressed, append-only messages with explicit room or task scope;
- logical-agent and role resolution;
- Relay-mediated delivery and reply pairing;
- bounded round trips and deterministic multi-hop driver execution;
- API-to-harness and harness-to-harness handoffs through the same delivery path.

The driver supports an `API -> harness -> different harness` chain with zero human copy-paste in the flow.

### Bounded discussions

`relay discuss "Compare these designs"` runs the bundled debate: independent analysis,
three critique/rebuttal rounds, and synthesis. Each discussion gets a dedicated Room;
it does not create or approve an implementation task. Participants use existing
`roles:` bindings in `relay.yaml`. For example, with an agent named `gpt` configured:

```yaml
roles:
  architect: gpt
  critic: gpt
  repository_expert: gpt
  moderator: gpt
communication:
  budgets:
    max_agent_turns: 22
    max_blocking_messages: 3
```

Roles may bind to different configured API or harness agents. Harness discussion
delivery uses a read-only grant. There is no automatic role selection.

The full bundled debate needs **22 agent turns**. The unchanged default allowance
is **16**: without an explicit configuration change, execution stops at that limit,
preserves partial outputs, and records a human-action-needed notice. Relay never
increases budgets automatically.

```bash
relay discuss "Compare these designs"
relay discuss "Review this proposal" --protocol protocols/debate.yaml
relay inspect-discussion <execution-id> --json
relay discuss --resume <execution-id>
relay status
```

Resume uses pinned protocol inputs and accepts no topic or protocol override. The
source YAML is no longer needed. IDs may be exact or unique prefixes. Inspection
and status only read progress; they never invoke agents or recover missing replies.
Interrupted work remains visible even if no outcome was recorded before the crash.

Escalations are stored observations with recovery guidance, not interactive prompts
or approvals. Restore changed participant settings before resuming; failed requests
are never retried, and changing pinned protocol/stage budgets requires a new
discussion. You may explicitly adjust the aggregate communication allowance and
resume existing work. Earlier notices remain in inspection history.

`discuss` exits with `0` for protocol completion, `1` for refusal/failure/escalation,
`2` for invalid usage, and `3` for pending delivery. `inspect-discussion` succeeds
when it can render valid records, including stopped discussions. Both commands
support a versioned `--json` envelope (`relay.discussion.v1`).

Rounds and message budgets are enforced. Semantic detection of repeated arguments
or lack of new evidence remains deferred; protocol completion does not establish
consensus, task completion, or human approval.

### Adapters and authentication

The current adapter registry includes OpenAI-compatible API adapters and harness adapters for Codex CLI, Claude Code, and Antigravity CLI. API keys stay in environment variables. Harnesses own their login and session authentication.

## Planned

These are roadmap items, not current capabilities:

- P5 remaining: semantic loop/convergence detection; discussion CLI, bounded protocols, policy, budgets, and stored escalation notices are available;
- P6: automated implementation review and fix loops;
- P7: persistent Rooms, seats, and long-lived participant context;
- P8: decision provenance;
- P9: Relay server;
- P10: MCP and chat interface integration;
- P11: adapter ecosystem and certification;
- P12: TUI.

The current message bus and bounded driver are the foundation for these features. They do not provide persistent Room UX or unrestricted autonomous agent chat.

## Quick start

Relay requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync --extra dev
uv run relay init
uv run relay status
```

The checked-in `relay.yaml` contains example agents. Authenticate a harness through its own CLI, then run an agent through Relay:

```bash
codex login
uv run relay ask codex "Reply with exactly: RELAY_OK"
```

Before `relay build`, configure a harness agent with at least the `workspace_write` grant. Configure a Relay-owned verification command in `relay.yaml` so the task can be checked independently of the implementer.

Keep API keys in the environment or an ignored local `.env` file. Never put secrets in `relay.yaml`.

## Development

```bash
uv sync --extra dev
uv run pytest
uv run ruff check relay tests
```

The full specification and design decisions live in [`docs/SPEC.md`](docs/SPEC.md).

## License

MIT

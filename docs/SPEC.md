# Relay — General-Purpose AI Collaboration Runtime

## 1. Vision

Relay, farklı AI modellerini, coding agent'larını ve araçları tek bir ortak çalışma ortamında birleştiren local-first bir orchestration sistemi olacaktır.

Kullanıcı modeller arasında prompt taşımaz.

Kullanıcı yalnızca amacını belirtir:

```bash
relay discuss "Should this service use Redis?"
relay build "Add authentication"
relay review
relay research "Compare these two architectures"
```

Relay:

1. gerekli context'i toplar,
2. uygun agent'ları seçer,
3. agent'ların bağımsız görüş üretmesini sağlar,
4. gerektiğinde birbirlerinin görüşlerini tartıştırır,
5. araç ve repository işlemlerini yürütür,
6. sonuçları doğrular,
7. karar geçmişini saklar,
8. kullanıcıya tek bir anlaşılır sonuç sunar.

---

# 2. Core Principle

Relay bir AI modeli değildir.

Relay:

> AI modelleri, coding agent'ları, araçlar ve insan arasındaki koordinasyon katmanıdır.

Modeller karar üretir.

Relay ise:

* state'i yönetir,
* context'i taşır,
* mesajları yönlendirir,
* izinleri uygular,
* run geçmişini saklar,
* doğrulama kapılarını zorunlu tutar.

LLM hiçbir zaman workflow'un gerçek state'inin tek sahibi olmayacaktır.

---

# 3. Primary Use Cases

Relay yalnızca coding için tasarlanmayacaktır.

## 3.1 Software Development

```bash
relay build "Add rate limiting to the API"
```

Akış:

```text
Context
→ Plan
→ Implementation
→ Tests
→ Review
→ Fix
→ Verification
→ Done
```

---

## 3.2 Multi-Agent Discussion

```bash
relay discuss "Should we migrate from PostgreSQL to ClickHouse?"
```

Agent'lar:

```text
Architect
Critic
Domain Expert
Repository Expert
Moderator
```

Bağımsız görüşler üretir, birbirlerini eleştirir ve sonunda synthesis oluşturulur.

---

## 3.3 Code Review

```bash
relay review
```

Sistem:

* current diff,
* related code,
* tests,
* project rules,
* task requirements

üzerinden bir veya birden fazla bağımsız reviewer çalıştırır.

---

## 3.4 Research / Decision Support

```bash
relay research "Best deployment architecture for this workload"
```

Farklı modeller aynı problem üzerinde çalışabilir.

Sonuç yalnızca cevap değil:

* agreement,
* disagreement,
* evidence,
* confidence,
* unresolved questions

olarak saklanır.

---

## 3.5 General AI Collaboration

Relay repository olmadan da çalışabilmelidir.

```bash
relay discuss "Which business model makes more sense?"
```

veya:

```bash
relay ask claude "Critique this plan"
```

---

# 4. High-Level Architecture

```text
                         USER
                           │
            ┌──────────────┴──────────────┐
            │                             │
           CLI                     Chat Interface
            │                             │
            └──────────────┬──────────────┘
                           ▼
                  ┌─────────────────┐
                  │   RELAY CORE    │
                  │                 │
                  │ Orchestrator    │
                  │ State Machine   │
                  │ Message Bus     │
                  │ Context Engine  │
                  │ Permission Gate │
                  │ Protocol Engine │
                  └───────┬─────────┘
                          │
        ┌─────────────────┼────────────────────┐
        │                 │                    │
        ▼                 ▼                    ▼
     Agents             Tools               Storage
        │                 │                    │
   ┌────┼─────┐      ┌────┼─────┐        SQLite / files
   │    │     │      │    │     │
 GPT Claude DeepSeek Git Shell GitHub
          │
        Codex
```

---

# 5. Fundamental Domain Objects

Relay'ın başından itibaren birkaç net abstraction'ı olacaktır.

## Workspace

Relay'ın üzerinde çalıştığı fiziksel veya mantıksal çalışma alanı.

Bir workspace:

* Git repository,
* klasör,
* research workspace,
* boş conversation workspace

olabilir.

---

## Room

Uzun ömürlü ortak AI çalışma alanı.

Örnek:

```text
Room: touchline-m5

Members:
- GPT / moderator
- Claude / architect
- Codex / implementer
- DeepSeek / reviewer

Workspace:
C:\projects\touchline

Active Task:
M5 Phase 1
```

Room aynı konu üzerinde günler sonra devam edebilir.

---

## Task

Belirli ve sınırlandırılmış iş.

```text
Add pagination validation
```

Task'ın lifecycle'ı Relay tarafından tutulur.

---

## Run

Bir agent'ın belirli bir task için tek çalıştırılması.

Örneğin:

```text
Run #81
Agent: Claude
Role: Reviewer
Task: Review implementation
```

---

## Message

Agent'lar arasındaki iletişim birinci sınıf veri olacaktır.

```text
sender
recipient
room
task
message_type
content
references
timestamp
```

---

## Artifact

Agent'ların ürettiği kalıcı çıktı.

Örneğin:

* plan,
* diff,
* report,
* test result,
* architecture proposal,
* review finding.

---

## Decision

Tartışmadan çıkan önemli karar.

```text
Decision #18

Use nested tournament-aware evaluation.

Proposed by: Claude
Challenged by: DeepSeek
Verified by: Codex
Accepted by: Moderator
```

---

# 6. State Machine

Agent'lara workflow kontrolü verilmez.

Örneğin software task:

```text
CREATED
   │
   ▼
CONTEXT_READY
   │
   ▼
PLAN_READY
   │
   ▼
IMPLEMENTING
   │
   ▼
IMPLEMENTED
   │
   ▼
VERIFYING
   │
   ├──── FAIL ────► IMPLEMENTING
   │
   ▼
REVIEWING
   │
   ├──── FIX_REQUIRED ───► IMPLEMENTING
   │
   ▼
APPROVAL_REQUIRED
   │
   ▼
DONE
```

Bir model:

```text
Everything is complete.
```

dese bile Relay aşağıdaki şartlardan biri eksikse task'ı kapatmaz:

```text
tests = PASS
review = PASS
required approvals = granted
```

---

# 7. Agent Abstraction

Tüm modeller ortak bir interface arkasından kullanılacaktır.

Örnek:

```python
class Agent:
    async def run(self, request: AgentRequest) -> AgentResponse:
        ...
```

Adapter'lar:

```text
OpenAIAdapter
AnthropicAdapter
DeepSeekAdapter
CodexCLIAdapter
ClaudeCodeAdapter
OpenCodeAdapter
LocalModelAdapter
```

Relay'ın geri kalanı hangi provider'ın kullanıldığını bilmek zorunda kalmamalıdır.

---

# 8. Agent Roles

Model ve role birbirinden ayrılmalıdır.

Yanlış:

```text
Claude = reviewer
```

Doğru:

```text
Claude can act as:
- architect
- reviewer
- researcher
- critic
```

Örnek config:

```yaml
roles:
  planner:
    agent: gpt

  implementer:
    agent: codex

  reviewer:
    agent: claude

  adversarial_reviewer:
    agent: deepseek
```

Böylece provider kolayca değiştirilebilir.

---

# 9. Conversation Bus

Relay'ın ayırt edici katmanlarından biri modeller arası mesajlaşma olacaktır.

Örnek:

```text
Claude
  │
  │ challenge
  ▼
DeepSeek
  │
  │ rebuttal
  ▼
Claude
  │
  │ final-position
  ▼
Moderator
```

Fakat modeller serbest biçimde sonsuza kadar konuşmayacaktır.

Her discussion bir protocol tarafından sınırlandırılır.

---

# 10. Discussion Protocols

## 10.1 Debate

```text
Round 1 — Independent opinions
Round 2 — Cross critique
Round 3 — Rebuttal
Round 4 — Synthesis
```

---

## 10.2 Decision

```text
Options
→ Independent ranking
→ Objections
→ Risk analysis
→ Final recommendation
```

---

## 10.3 Architecture Review

```text
Proposal
→ Architect analysis
→ Adversarial review
→ Repository reality check
→ Revised proposal
→ Decision
```

---

## 10.4 Implementation

```text
Plan
→ Implement
→ Test
→ Review
→ Fix
→ Verify
```

---

## 10.5 Debug

```text
Symptoms
→ Independent hypotheses
→ Evidence collection
→ Hypothesis elimination
→ Candidate fix
→ Verification
```

---

# 11. Protocol Configuration

Protocol'lar mümkün olduğunca declarative tutulacaktır.

Örnek:

```yaml
name: architecture_debate

participants:
  - architect
  - critic
  - repository_expert

stages:
  - independent_analysis
  - critique
  - rebuttal
  - synthesis

limits:
  max_rounds: 3

completion:
  require_synthesis: true
```

---

# 12. Context Engine

Relay her agente bütün workspace'i göndermeyecektir.

Context Engine:

```text
User request
     │
     ▼
Workspace map
     │
     ├── project instructions
     ├── relevant files
     ├── related tests
     ├── current diff
     ├── git history
     ├── previous decisions
     └── previous relevant runs
```

çıkaracaktır.

Amaç:

* düşük token maliyeti,
* daha az distraction,
* daha doğru agent output'u.

---

# 13. Project Discovery

Bir repository ilk kez açıldığında:

```bash
relay init
```

Relay temel profili oluşturur.

```yaml
project:
  languages:
    - python
    - typescript

  frameworks:
    - fastapi
    - nextjs

  package_managers:
    - uv
    - pnpm

  instructions:
    - AGENTS.md
    - CONTRIBUTING.md

  tests:
    backend: uv run pytest
    frontend: pnpm test

  default_branch: main
```

Kullanıcı bunu gerekirse düzenleyebilir.

---

# 14. Persistent Memory

Agent memory'si canonical memory olmayacaktır.

Relay kendi event store'una sahip olacaktır.

İlk sürüm:

```text
SQLite
```

Temel tablolar:

```text
workspaces
rooms
tasks
runs
messages
artifacts
decisions
tool_runs
approvals
```

---

# 15. Event Log

Önemli her olay append-only event olarak kaydedilecektir.

```json
{
  "sequence": 184,
  "room": "touchline-m5",
  "task": "m5-1-3",
  "sender": "claude",
  "recipient": "codex",
  "type": "review_finding",
  "content": "...",
  "references": [
    "src/foo.py:42-71"
  ]
}
```

Bu sayede çalışma geçmişi yeniden oluşturulabilir.

---

# 16. Decision Provenance

Relay'ın önemli özelliklerinden biri:

```bash
relay why 18
```

komutu olacaktır.

Örnek çıktı:

```text
Decision #18

Chosen:
Architecture B

Reason:
Lower migration risk and simpler invalidation semantics.

Proposed by:
Claude

Supported by:
GPT
DeepSeek

Repository verification:
Codex

Rejected alternative:
Architecture A

Primary objection:
...
```

---

# 17. Tool Layer

Agent'lar doğrudan rastgele sistem çağrıları yapmamalıdır.

Tool abstraction kullanılacaktır.

İlk tool'lar:

```text
filesystem.read
filesystem.write

git.status
git.diff
git.log
git.branch
git.commit

shell.run

github.pr
github.ci
```

Sonradan:

```text
browser
database
docker
cloud
calendar
email
```

eklenebilir.

---

# 18. Permissions

Başlangıçta güvenli default:

```yaml
permissions:
  read_files: auto
  edit_files: auto
  run_tests: auto

  install_dependencies: ask
  run_migrations: ask

  git_commit: ask
  git_push: ask

  create_pr: ask
  merge_pr: never

  destructive_shell: never
```

Her tool call Relay permission layer'ından geçmelidir.

---

# 19. Human Approval Gates

Relay'ın amacı insanı workflow'dan tamamen çıkarmak değildir.

İnsan yalnızca gerçekten karar gerektiren yerde devreye girmelidir.

Örneğin:

```text
✓ Implementation
✓ Tests
✓ Review

Approval required:

Codex wants to add dependency:
redis==6.x

[A] Approve
[R] Reject
[D] Discuss
```

---

# 20. CLI

İlk ürün arayüzü CLI olacaktır.

## Initialization

```bash
relay init
```

---

## Single Agent

```bash
relay ask claude "Review this architecture"
```

---

## Discussion

```bash
relay discuss "Should we introduce Redis?"
```

---

## Build

```bash
relay build "Add API caching"
```

---

## Review

```bash
relay review
```

---

## Continue

```bash
relay continue
```

---

## Status

```bash
relay status
```

---

## History

```bash
relay history
```

---

## Inspect

```bash
relay inspect 42
```

---

## Explain Decision

```bash
relay why 18
```

---

# 21. Chat Interface / ChatGPT Integration

CLI ürünün tek arayüzü olmayacaktır.

Relay bir API ve MCP-compatible interface sunmalıdır.

Amaç:

```text
Chat Interface
      │
      ▼
Relay
      │
      ├── Claude
      ├── Codex
      ├── DeepSeek
      ├── repository
      └── tools
```

Kullanıcı doğal şekilde:

```text
Claude'a bunun architecture açısından mantıklı olup
olmadığını sor.

Sonra Codex'e mevcut repo ile uyumlu olup
olmadığını kontrol ettir.

İkisi farklı düşünüyorsa tartıştır.
```

diyebilmelidir.

Relay interaction detaylarını kullanıcıdan gizler.

---

# 22. Moderator

Discussion sonunda bir moderator rolü olacaktır.

Moderator:

* çoğunluk oylaması yapmak zorunda değildir,
* argüman kalitesini karşılaştırır,
* evidence değerlendirir,
* disagreement'ı açıkça gösterir,
* unresolved noktaları saklar.

Çıktı:

```text
CONSENSUS

Architecture B is preferred.

AGREEMENT

GPT       YES
Claude    YES
DeepSeek  YES
Codex     PARTIAL

PRIMARY ARGUMENT

...

DISSENT

Codex notes that...

UNRESOLVED

...

NEXT ACTION

...
```

---

# 23. Cost Controls

Multi-agent sistem kolayca token yakabilir.

Bu nedenle baştan limit konacaktır.

```yaml
budget:
  max_agents_per_task: 4
  max_discussion_rounds: 3
  max_fix_loops: 3

  stop_on_consensus: true
```

İleride:

```text
token budget
USD budget
provider budget
```

eklenebilir.

---

# 24. Anti-Loop Protection

Agent konuşmaları otomatik olarak durmalıdır.

Stop conditions:

```text
consensus reached
no new evidence
same argument repeated
max rounds reached
user approval required
budget exceeded
```

---

# 25. Observability

Her agent run için:

```text
agent
role
model
start/end
input size
output size
tools used
status
cost
```

tutulmalıdır.

Örnek:

```bash
relay inspect run 182
```

---

# 26. MVP Scope

İlk versiyonda özellikle YAPILMAYACAKLAR:

* GUI,
* complicated distributed architecture,
* Kubernetes,
* vector database,
* autonomous infinite agents,
* browser automation,
* dozens of providers,
* plugin marketplace.

MVP:

```text
Python
Typer
Pydantic
asyncio
SQLite
subprocess
HTTP APIs
```

---

# 27. Implementation Roadmap

## Phase 0 — Specification Freeze

Amaç:

Relay'ın ne olduğuna karar vermek.

Deliverables:

* terminology,
* architecture,
* state model,
* provider interface,
* tool interface,
* event schema,
* permission model.

Exit gate:

Core abstractions değişmeden implementation başlayabilecek kadar net olmalı.

---

# Phase 1 — Single-Agent Runtime

Amaç:

Bir agent'ı Relay üzerinden çalıştırabilmek.

Implement:

```text
relay init
relay ask
relay status
relay history
```

Components:

* workspace discovery,
* config,
* SQLite storage,
* Agent interface,
* one remote LLM adapter,
* run logging.

Exit gate:

```bash
relay ask gpt "Analyze this repository"
```

çalışmalı ve run tamamen persisted olmalı.

---

# Phase 2 — Codex / Local Tool Runtime

Amaç:

Relay'ın gerçek repository üzerinde çalışabilmesi.

Implement:

* Codex CLI adapter,
* shell tool,
* filesystem tools,
* git tools,
* permissions.

Exit gate:

```bash
relay build "Make a small code change"
```

Codex'i çalıştırıp diff ve tool evidence'ı Relay'a geri getirmeli.

---

# Phase 3 — Deterministic Task State Machine

Amaç:

Coding workflow'un LLM'den bağımsız yönetilmesi.

Implement:

```text
CREATED
CONTEXT_READY
PLAN_READY
IMPLEMENTED
VERIFIED
REVIEWED
DONE
```

Exit gate:

Model yanlışlıkla "done" dese bile gerekli verification olmadan task kapanmamalı.

---

# Phase 4 — Multi-Agent Messaging

Amaç:

İki veya daha fazla agent'ın birbirleriyle Relay üzerinden konuşması.

Implement:

* message bus,
* sender/recipient semantics,
* agent roles,
* conversation persistence.

Exit gate:

```text
Agent A → Agent B → Agent A
```

tartışması insan copy-paste'i olmadan yapılabilmeli.

---

# Phase 5 — Discussion Protocols

Amaç:

Kontrollü agent debate.

İlk protocol'lar:

```text
debate
decision
review
debug
```

Exit gate:

```bash
relay discuss "Architecture A or B?"
```

bağımsız analiz, critique, rebuttal ve synthesis çalıştırmalı.

---

# Phase 6 — Automated Implementation Review Loop

Amaç:

Bugünkü manuel workflow'un tamamen otomatikleşmesi.

```text
Plan
→ Codex
→ Tests
→ Reviewer
→ FIX
→ Codex
→ Reviewer
→ PASS
```

Implement:

* structured review findings,
* automatic fix packet generation,
* max loop count,
* final verification.

Exit gate:

Basit bir repository task'ı insan prompt taşıması olmadan implementation → review → fix → PASS döngüsünden geçebilmeli.

---

# Phase 7 — Rooms & Long-Lived Context

Amaç:

Uzun ömürlü AI çalışma ortamları.

Commands:

```bash
relay room create
relay room list
relay room resume
relay room close
```

Room:

* members,
* workspace,
* decisions,
* tasks,
* context,
* artifacts

saklar.

Exit gate:

Bir room günler sonra yeniden açıldığında çalışma state'i devam etmeli.

---

# Phase 8 — Decision Provenance

Amaç:

"Bu kararı neden aldık?" sorusunu cevaplamak.

Implement:

```bash
relay why <decision>
```

Decision graph:

```text
proposal
objection
evidence
rebuttal
decision
```

Exit gate:

Önemli bir kararın bütün reasoning provenance'ı agent transcriptlerinden yeniden oluşturulabilmeli.

---

# Phase 9 — Relay Server

Amaç:

CLI dışında istemcilerin Relay'a bağlanabilmesi.

Implement:

```text
FastAPI
REST/WebSocket
```

Operations:

```text
create task
send message
start discussion
query room
approve action
inspect status
```

Exit gate:

CLI, Relay Core'a doğrudan değil Relay Server üzerinden de bağlanabilmeli.

---

# Phase 10 — MCP / Chat Interface Integration

Amaç:

ChatGPT veya başka MCP-compatible istemcilerin Relay'ı tool olarak kullanabilmesi.

Örnek capabilities:

```text
relay.ask_agent
relay.start_discussion
relay.get_status
relay.review_workspace
relay.continue_task
relay.get_decision
```

Hedef kullanım:

```text
User:
Claude ve Codex'e bunu tartıştır.

Chat:
→ Relay discussion

User:
Codex uygulasın.

Chat:
→ Relay implementation

User:
Claude review etsin.

Chat:
→ Relay review
```

Exit gate:

Kullanıcı modeller arasında manuel prompt taşımadan bütün workflow'u conversational interface üzerinden yönetebilmeli.

---

# Phase 11 — Provider Expansion

Adapters:

```text
OpenAI
Anthropic
DeepSeek
Codex CLI
Claude Code
OpenCode
OpenAI-compatible local APIs
```

Bu aşamadan sonra yeni provider eklemek core değişikliği gerektirmemeli.

---

# Phase 12 — TUI

CLI stabil olduktan sonra terminal UI.

Örnek:

```text
┌ Relay — touchline-m5 ─────────────────────┐
│                                           │
│ Task                                      │
│ Add tournament seal validation            │
│                                           │
│ ✓ Context                                 │
│ ✓ Plan                                    │
│ ✓ Implement                               │
│ ✓ Tests                                   │
│ ● Review                                  │
│ ○ Final verification                      │
│                                           │
│ Claude reviewer                           │
│ 1 HIGH · 2 LOW                            │
│                                           │
│ Agents                                    │
│ GPT        idle                           │
│ Claude     reviewing                      │
│ Codex      idle                           │
│ DeepSeek   idle                           │
└───────────────────────────────────────────┘
```

---

# 28. Suggested Repository Structure

```text
relay/
├── pyproject.toml
├── README.md
│
├── relay/
│   ├── cli/
│   │
│   ├── core/
│   │   ├── orchestrator.py
│   │   ├── state_machine.py
│   │   ├── bus.py
│   │   ├── protocols.py
│   │   └── permissions.py
│   │
│   ├── agents/
│   │   ├── base.py
│   │   ├── openai.py
│   │   ├── anthropic.py
│   │   ├── deepseek.py
│   │   └── codex_cli.py
│   │
│   ├── context/
│   │   ├── workspace.py
│   │   ├── repository.py
│   │   └── selector.py
│   │
│   ├── tools/
│   │   ├── filesystem.py
│   │   ├── shell.py
│   │   ├── git.py
│   │   └── github.py
│   │
│   ├── storage/
│   │   ├── db.py
│   │   ├── models.py
│   │   └── events.py
│   │
│   ├── server/
│   │
│   └── mcp/
│
├── protocols/
│   ├── debate.yaml
│   ├── decision.yaml
│   ├── implementation.yaml
│   ├── review.yaml
│   └── debug.yaml
│
└── tests/
```

---

# 29. First Vertical Slice

İlk gerçek milestone büyük olmamalıdır.

Tek hedef:

```bash
relay build "Make this small repository change"
```

çalıştırıldığında:

```text
1. workspace detected
2. repository context collected
3. planner creates task packet
4. Codex implements
5. tests run
6. reviewer receives diff
7. reviewer returns PASS/FIX
8. FIX automatically goes back to Codex
9. final result persisted
10. user receives summary
```

Bu çalışmadan multi-agent platformuna geçilmemelidir.

Çünkü bu vertical slice Relay'ın temel iddiasını kanıtlar:

> İnsan agent'lar arasındaki mesaj taşıyıcı olmak zorunda değildir.

---

# 30. Second Vertical Slice

İlk implementation loop stabil olduktan sonra:

```bash
relay discuss "Should we use architecture A or B?"
```

Akış:

```text
GPT independent analysis
Claude independent analysis
DeepSeek independent analysis
        ↓
Cross critique
        ↓
Rebuttal
        ↓
Moderator synthesis
        ↓
Persisted decision
```

Bu da ikinci temel iddiayı kanıtlar:

> Farklı modeller kontrollü biçimde birbirleriyle çalışabilir.

---

# 31. Third Vertical Slice

Sonraki hedef Chat interface:

```text
User
 ↓
ChatGPT
 ↓
Relay
 ├─ Claude
 ├─ Codex
 └─ DeepSeek
 ↓
ChatGPT
 ↓
User
```

Bu noktada kullanıcı CLI kullanmak zorunda kalmadan:

```text
"Bunu Claude'a eleştirt."

"Codex repo açısından kontrol etsin."

"İkisini tartıştır."

"Tamam, önerilen versiyonu uygulat."

"Şimdi tekrar review ettir."
```

diyebilir.

---

# 32. Product Boundary

Relay şu olmamalıdır:

* yeni bir IDE,
* Cursor klonu,
* yeni coding model,
* kendi LLM platformu,
* tamamen autonomous developer.

Relay şudur:

> Existing AI tools için ortak coordination, state, conversation, verification ve provenance layer.

Bu boundary korunmalıdır.

---

# 33. Success Criteria

Relay başarılı sayılacaksa:

### Manual friction

Bir agent'ın çıktısını diğerine manuel copy-paste etme ihtiyacı ciddi ölçüde ortadan kalkmalı.

### Reliability

Bir modelin sözlü "PASS" demesi verification yerine geçmemeli.

### Provider independence

Bir model/provider değiştirildiğinde workflow bozulmamalı.

### Transparency

Her kararın ve değişikliğin kaynağı görülebilmeli.

### Recoverability

Process kapanırsa task kaldığı yerden devam edebilmeli.

### Human control

Destructive veya irreversible işlemler kullanıcı onayı olmadan yapılmamalı.

---

# 34. Non-Goals for V1

V1'de özellikle hedeflenmeyecek:

* tam autonomous software engineer,
* cloud-hosted multi-user SaaS,
* collaborative teams,
* browser GUI,
* marketplace,
* mobile app,
* arbitrary autonomous internet access,
* distributed agent workers.

Önce tek kullanıcı için local sistem mükemmel çalışmalıdır.

---

# 35. Recommended Build Order

Kesin uygulama sırası:

```text
P0  Specification
 ↓
P1  Core + Storage
 ↓
P2  Single Agent
 ↓
P3  Codex + Tools
 ↓
P4  State Machine
 ↓
P5  Implementation / Review Loop
 ↓
P6  Multi-Agent Bus
 ↓
P7  Discussion Protocols
 ↓
P8  Rooms
 ↓
P9  Decision Provenance
 ↓
P10 Server
 ↓
P11 MCP / Chat Interface
 ↓
P12 Provider Expansion
 ↓
P13 TUI
```

Özellikle:

```text
Multi-agent discussion
```

coding loop'tan **sonra** gelmelidir.

Çünkü önce sağlam orchestration primitive'lerini kurarsak discussion sistemi aynı altyapıyı ücretsiz kullanır.

---

# 36. Initial Definition of Done

İlk büyük release için kullanıcı şunu yapabilmelidir:

```bash
relay room create my-project

relay discuss "What's the safest design for feature X?"

relay build "Implement the accepted design"

relay status
```

ve sistem:

1. birden fazla agent ile design tartışması yapmalı,
2. kararı persist etmeli,
3. kararı implementation context'ine aktarmalı,
4. coding agent'a uygulatmalı,
5. bağımsız reviewer çalıştırmalı,
6. gerekirse otomatik fix loop yapmalı,
7. test evidence toplamalı,
8. bütün geçmişi room altında saklamalı.

Son durumda kullanıcı:

```bash
relay why <decision>
relay inspect <task>
relay continue
```

ile bütün süreci anlayabilmelidir.

---

# 37. Long-Term North Star

Uzun vadede ideal interaction:

```text
User:
Auth sistemini değiştirmeyi düşünüyorum.
Claude ve GPT architecture'ı tartışsın.
Codex mevcut repo açısından kontrol etsin.

Relay:
Discussion complete.
Two approaches remain.
Architecture B is preferred.

User:
B'yi uygulat.

Relay:
Codex implementation complete.
Tests pass.
Claude found one HIGH issue.
Fix returned to Codex.
Second review passes.
CI passes.

User:
Ne değişti?

Relay:
...
```

Kullanıcının yaptığı şey:

```text
intent
decision
approval
```

olmalıdır.

Kullanıcının yapmaması gereken şey:

```text
copy
paste
prompt rewrite
context repeat
agent babysitting
```

olmalıdır.

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
   ┌────┴─────┐      ┌────┼─────┐        SQLite / files
   │          │      │    │     │
API-backed  Harness-backed
adapters    adapters/harnesses
GPT Claude  Codex CLI · Claude Code · Antigravity CLI
DeepSeek …  (internal tools run inside the harness,
             governed by Execution Grants — App. C.5)
```

İki execution family vardır (Appendix B.2): API-backed adapter'lar Relay'ın
çözdüğü environment-only credentials ile model API'lerine konuşur;
harness-backed adapter'lar kendi authentication'ına sahip CLI process'leridir
ve iç tool'larını Relay interception'ı olmadan da çalıştırabilir — bunlar
Execution Grant + compensating controls ile yönetilir; per-tool enforcement
iddiası yapılmaz (Appendix C.4/C.5).

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

Room üyeliği named **logical seat**'lerdir: her seat bir role bağlıdır ve bir
configured binding aracılığıyla konfigüre edilmiş bir logical agent'a bağlanır;
seat'in kimliği model/backend değişse bile stabil kalır ve workflow metni asla
somut provider adına referans vermez. Yukarıdaki listede her member satırı bir
seat + role gösterir; GPT/Claude/Codex/DeepSeek etiketleri o seat'lere bind
edilmiş config instance'lardır (model/backend fact'leri) — yani bir "member"
provider değildir, seat'tir (Appendix D.2). Bir Room ek olarak kalıcı bir
sohbet beslemesidir (chronological feed): mesajlar, kayıtlar ve insan katılımı
aynı geçmişte yaşar — Room bir transient execution batch'i değil, uzun ömürlü
koordinasyon yüzeyidir (Appendix D.1/D.11).

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

Bu kural harness-backed agent'ların sözlü tamamlama beyanlarına da birebir
uygulanır — model/harness'in "done" demesi kanıt değildir (Appendix C.7).

Bu şema bilinçli olarak KUCUK kalır: Room tartışma-first akışında otomatik
yürütme yalnızca İNSANIN açık plan freeze/kabul kararından sonra başlar; bu
sınır zaten PLAN_READY → IMPLEMENTING kenarına biner (Appendix D.3). Bir
katılımcının clarification beklemesi gibi aşama-içi bekleme durumları kanonik
TaskState DEĞİLDİR — runtime/dispatch koşuludur ve ileride bir execution
substate olarak temsil edilebilir (Appendix D.6). Bu ek için state machine'e
yeni durum eklenmez.

---

# 7. Agent Abstraction

Tüm modeller ortak bir interface arkasından kullanılacaktır.

Örnek:

```python
class Agent:
    async def run(self, request: AgentRequest) -> AgentResponse:
        ...
```

Adapter'lar iki execution family'ye ayrılır (Appendix B.2):

```text
API-family adapters     OpenAIAdapter, AnthropicAdapter,
                        DeepSeekAdapter, LocalModelAdapter

Harness-family adapters CodexCLIAdapter, ClaudeCodeAdapter,
                        Google subscription-path adapter (expected:
                        Antigravity CLI), OpenCodeAdapter ve
                        gelecek harness'lar
```

Bütün harness adapter'ları tek bir generic harness runtime + contract
paylaşır (Appendix C.2); her yeni harness core'a değil, kendi adapter
paketine eklenir.

Relay'ın geri kalanı hangi provider'ın kullanıldığını bilmek zorunda kalmamalıdır; core yalnızca execution family (api | harness), backend, capability ve role kavramlarını görür — vendor/product isimleri yalnızca adapter modülleri ve config değerlerinde yaşar (Appendix C.1).

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

Config'teki `codex`, `claude` vb. isimler config instance adıdır —
protocol/selection vocabulary'si asla değildir. Rol seçimi role +
capability üzerinden yapılır; model seçimi ortogonal kalır
(Appendix C.3/C.7).

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

Inter-agent mesajlaşma aynı zamanda **tipli ve adresli**dir: her mesaj bir
message type taşır, alıcısı bir logical agent veya roldür, blocking /
non-blocking olarak sınıflandırılır ve ilgili plan/decision/finding/artifact
kayıtlarına referans verebilir. Kim kime ne amaçla yazabilir communication
policy tarafından belirlenir — serbest otonom grup sohbeti yoktur
(Appendix D.5–D.7). Konuşma asla sessizce kanonik state'i mutasyona
uğratmaz; sonuç doğuran değişimler Relay tarafından açıkça Decision / Plan
revizyonu / Finding / Approval kaydına promote edilir (Appendix D.4). Principle: **Conversation is coordination input, not workflow
authority.**

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

Room çalışmasında Context Engine'in ikinci sorumluluğu **katılımcı
başına task context'i yeniden kurmaktır**: aktif (en son kabul edilen)
plan ve onun supersession zinciri, kabul edilmiş kararlar, çözülmemiş
blocking communication'lar, ilgili inter-agent notlar, güncel reviewer
findings, ilgili artifacts/diffs/evidence ve Room geçmişinden seçilmiş
alıntılar — hepsi canonical store'dan türetilir. Katılımcılar varsayılan
olarak bütün Room transcriptine bağımlı OLAMAZ; external harness session
resume bir optimizasyondur, source of truth değildir (Appendix D.10).

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

Bu liste Relay'ın KENDİSİNİN çalıştırdığı tool'ları tanımlar. External
harness'lar kendi iç shell/filesystem/git tool'larını Relay'in per-action
interception'ı olmadan da çalıştırabilir; o durumda yetki Execution Grant
sözleşmesiyle verilir ve compensating controls devreye girer
(Appendix C.5).

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

Permission boundary iki katmandır (Appendix C.5):

* **Grant tier (her zaman zorunlu):** bir run'a hangi execution capability'nin
  verileceğine (read-only access / workspace-write / network / dangerous Relay
  actions) Relay policy karar verir — `auto` / `ask` / `never` outcomes aynen
  geçerlidir. Relay-owned executor'daki her tool call `PermissionGate.check()`
  üzerinden geçer.
* **Mediation tier (koşullu):** harness fine-grained tool/approval event
  stream'i sunuyorsa Relay bunları mediate eder; sunmuyorsa Relay elinde
  olmayan per-tool enforcement'i iddia ETMEZ — worktree containment,
  pre/post repository-state snapshot, diff evidence gibi compensating
  controls kullanılır.

No harness bypasses Relay authorization merely because it owns its internal tools.

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
model (requested)
start/end
input size
output size
tools used
status
cost
```

tutulmalıdır.

Harness-backed run'lar için ek alanlar Appendix C.6 seam'idir:
resolved/reported model ve harness/adapter version — hepsi nullable ve
provider-neutral; `Run.model` requested model olarak kalır. usage/cost
opsiyonelliği korunur (Appendix B.2); credentials/session secrets asla
kaydedilmez (Appendix C.4).

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

# Phase 2 — Generic Harness Runtime

Amaç:

Subscription-backed (harness) coding agent'ları provider'a özgü varsayımlar
olmadan koordine etmek. Mimari kelime dağarcığı provider isimleri değildir;
her harness bir adaptördür ve hepsi tek bir generic runtime paylaşır
(Appendix C.2).

Implement:

* P2.1 — Generic harness contract + process runtime + conformance suite:
  subprocess lifecycle; executable discovery & version inspection;
  working-directory control; timeout & cancellation; structured-output
  parsing where supported; stdout/stderr normalization; persisted-error
  sanitization; exit-code semantics; capability declaration; auth-state
  detection without credential extraction; child-process environment
  policy; crash-safe integration with the P1 orchestrator/store;
  normalized artifacts/evidence; session/resume seam; offline
  fake-harness conformance tests (Appendix C.2–C.5).
* P2.2 — Codex CLI reference adapter (first subscription-backed runtime).
* P2.3 — Claude Code adapter.
* P2.4 — Google's current subscription-backed CLI adapter — expected to be
  Antigravity CLI unless current official documentation establishes
  otherwise (Appendix C.1). Product names are adapter-specific.
* Optional P2.5 — Gemini CLI compatibility only where still defensible;
  it is never the architectural abstraction for Google.

Exit gates:

1. `relay build "Make a small code change"` çalıştırıldığında yapılandırılmış
   harness diff ve tool/output evidence'ı Relay'a geri getirmeli
   (ARTIFACT/EVIDENCE kayıtlarıyla).

2. **Second-real-harness gate:** ikinci gerçek harness, P2.1 sözleşmesini
   kullanarak core abstractions'a dokunmadan entegre olabilmelidir.

3. **Auth-conflict gate:** ebeveyn ortamda başka sağlayıcıya ait API
   anahtarları olsa bile harness çocuk süreci bunları görmemeli
   (Appendix C.4 test matrix).

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

Harness-backed agent'ların ürettiği gözlemler ("implementation complete",
"tests passed", "review passed", "task done") yalnızca claim taşıyan
artifact/evidence ADAYI olarak girer; state transition yalnızca
EvidenceStore'daki provenance-backed kayıtlardan çözülür — TESTS_PASSED
hâlâ Relay-scoped tool_run_id ister (Appendix A.1, C.7).

---

# Phase 4 — Multi-Agent Messaging

Amaç:

İki veya daha fazla agent'ın birbirleriyle Relay üzerinden konuşması.

Implement:

* message bus,
* sender/recipient semantics,
* agent roles,
* conversation persistence,
* heterogeneous execution families day one — API-backed ↔ harness-backed
  kombinasyonları birinci sınıftır (Appendix C.7),
* typed inter-agent messages — Phase-0 conversational çekirdeği
  (opinion/challenge/rebuttal/final_position/synthesis/review_finding)
  üzerine Appendix D.5 uzantıları: clarification_request,
  clarification_response, proposal, note; blocking-ness mesaj metadata'sıdır
  (Appendix D.6),
* mesajların plan/decision/finding/artifact referansları taşıması — sonuç
  doğuran mesajlar bu referanslar üzerinden canonical kayıtlara promote
  edilir (Appendix D.4),
* chronological Room feed'in persisted mesaj + event log'dan üretilen
  zaman sıralı görünümü — heterojen katılımcılar (api agent, harness agent,
  insan, Relay system) aynı feed'de yaşar (Appendix D.1/D.11).

Exit gate:

```text
Agent A → Agent B → Agent A
Agent A (api) → Agent B (harness) → Agent C (farklı bir harness)
```

tartışması insan copy-paste'i olmadan yapılabilmeli. Routing logical agent
identity, role, backend family ve capability üzerinden çalışır; hardcoded
provider branching test-dışı desendir (Appendix C.7).

Kabul şartları (Appendix D.11):

* api ↔ harness trafik aynı protocol ve aynı mesaj vokabülerini kullanır;
  provider'a özgü message routing yoktur.
* Bir blocking clarification, ilgili stage'i yanıt gelene kadar duraklatır;
  bekleme yeni bir TaskState eklemeden modellenir (D.6).
* Non-blocking not, göndericinin akışını kesmez ve alıcının ilerideki
  reconstructed context'inde görünür (D.10).
* Mesaj kayıtları sender/recipient (logical agent veya role)/room/task/type/
  blocking/references provenance alanlarını taşır (D.5).
* Konuşma trafiği kanonik state'e sessiz yazma yapamaz — bunu kanıtlayan
  testler D.8 invariant'larını P4'te doğrular.

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

Protocol'lar participant taleplerini role + required capability olarak
ifade eder (planner, critic, adversarial reviewer, domain expert …);
somut provider/model seçimi protocol metninden bağımsızdır ve ortogonaldir
(Appendix C.7).

## Communication policy ve bütçeler (Appendix D.7/D.11)

Protocol'lar iletişim izinlerini ve bütçelerini tanımlayan bir katman
taşır; katılımcılar role, capability ve logical agent configuration ile
seçilir — asla provider/model adıyla:

* allowed sender → recipient ilişkileri (hangi seat hangi seat'e yazabilir),
* her pair için allowed message purposes/types (ör. implementer → planner:
  clarification_request + proposal; implementer → reviewer: yalnızca note),
* blocking clarification round-trip bütçeleri (stage başına),
* toplam inter-agent mesaj/loop bütçeleri (task/stage başına),
* loop detection ve non-convergence'da insana escalation,
* micro-communication'ın yalnızca protocol-sınırlı stage'ler içinde
  var olması — "free autonomous group chat" bu phase'te mümkün değildir.

Söz dizimi burada dondurulmaz (P5 implementasyon kararıdır); normatif olan:
**kimin kime, ne semantik amaçla, hangi bütçeyle yazabileceği
policy-kontrolüdür** (Appendix D.7).

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
→ Implementer
→ Verification
→ Reviewer
→ Fix
→ Implementer
→ Reviewer
→ PASS
```

Implement:

* structured review findings,
* automatic fix packet generation,
* max loop count,
* final verification.

Bu otomatik yürütme yalnızca kabul edilmiş (frozen) bir canonical planın
varlığında başlar; tartışma-first akışında bu sınır insanın açık
"plan freeze & execute" geçişidir (Appendix D.3).

## Bounded micro-interactions inside stages (Appendix D.6/D.11)

Dış (outer) workflow yukarıdaki gibi birebir kalır; dispatch ve stage
ilerleme kararları daima Relay'dedir. Stage'lerin İÇİNDE, policy ve bütçe
kontrolünde tipli mikro-etkileşimlere (bounded micro-interactions — küçük
sınırlı mikro-döngüler/micro-loops oluşturabilir) izin verilir. Amaçları:
belirsizliği çözmek, varsayımları challenge etmek, implementation tercihlerini
açıklamak, yerel plan sapması talep etmek — dış döngüyü ikame etmek değil:

```text
Implementer ↔ Planner   clarification_request/response
Reviewer   ↔ Implementer question / response
Reviewer   → Planner     challenge → Decision
Fixer      → Planner     plan-conflict proposal → Decision (+ plan revision)
```

Örnek: Reviewer bir bulguyu Planner'a taşır; Planner karar üretir
(DECISION-n) ve gerekiyorsa PLAN-(n+1) önceki planı "supersedes"
ilişkisiyle devralır; Implementer yeni canonical context'e karşı devam
eder. Mikro-döngüler dış döngünün YERİNİ GEÇMEZ — sadece onu besler.

Kabul şartları:

1. Mikro-iletişim kanonik state'i sessizce mutasyona uğratamaz (D.4).
2. Sonuç doğuran değişimler Decisions / Plan revizyonları olarak promote
   edilir; promotion append-only kayıt üretir (§15).
3. Communication bütçeleri non-convergent döngüleri durdurur; tükenme
   durumunda insana escalation olur (D.7).
4. Dış deterministik loop her koşulda kontrolde kalır — stage tamamlanma
   kararı hâlâ EvidenceStore'dan çözülür (A.1/C.7).
5. İnsan otomasyonu interrupt edebilir, role'e hitap edebilir ve
   escalate edebilir (D.9).

Implementer/reviewer/planner rolleri bağımsız değiştirilebilir configured
agents'tır — Codex, Claude Code, Google harness (beklenen: Antigravity CLI)
veya future compatible adapter. Orchestration deterministic ve
capability-based'dir (Appendix C.3/C.7); provider-name branching yoktur.

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

## Persistent Room model (Appendix D.11)

P7 Room'u kalıcı "AI group chat" haline getirir — sadece transient bir
execution protocol'ü değil. Ek gereksinimler:

* stable logical Room members/seats; seat→agent binding provider/model'den
  bağımsız ve rebind-edilebilir (Appendix D.2),
* persistent Room history: mesajlar, feed, insan katılımı ve Relay system
  mesajları oturum kapatıp açmaya rağmen yaşar (Appendix D.1),
* canonical plan/decision/finding ilişki ağı — frozen plan'lar, onların
  supersedes zincirleri, kararlar ve findings Room state'inin parçasıdır
  (Appendix D.3),
* hedefli kullanıcı mesajları: insan belirli bir role/agent'e hitap
  edebilir (@planner / @reviewer tarzı; söz dizimi P7 product kararıdır,
  App. D.9),
* her katılımcı için Context Engine reconstruction: ROLE · TASK · CURRENT
  PLAN · RELEVANT DECISIONS · CONSTRAINTS · CURRENT FINDINGS · RELEVANT
  ARTIFACTS/FILES — transcript replay'i değil (Appendix D.10),
* external session continuation yalnızca session_resume capability'si
  destekleniyorsa kullanılır; resume yoksa canonical kayıtlardan fresh-run
  reconstruction dürüstçe yapılır (App. C.7 normatif kabulünü korur).

Room membership identity mantıksal agent identity'dir (backend swap'lerinde
stabil). Harness'e özgü mutable durum (external_session_ref gibi non-secret
referanslar) run/task seviyesinde ve Appendix C.4 allowlist'ine uygun tutulur.
Resume önce session_resume capability'sini görür; capability yoksa Context
Engine reconstruction ile dürüst fresh-run yapılır (Appendix C.7).

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

Graph node'ları optionally backend family / harness version attribute'u
taşıyabilir (Appendix C.7); producer kimlikleri zaten EvidenceRecord
provenance'ının parçasıdır.

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

Server serialization capability-aware agent descriptor'ları döndürür.
Remote client'lardan gelen permission istekleri CLI ile birebir aynı
PermissionGate'ten geçer; transport hiçbir ek ayrıcalık vermez
(Appendix C.5/C.7).

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

Kullanıcı dili product adı geçse bile Relay selection'ı role + capability
düzeyinde çözer ("adversarial reviewer", "implementer with workspace_write")
ve bunu registry/config üzerinden logical agent'lara map eder. Bu örnekler
UX illüstrasyonudur; routing vocabulary'si değildir (Appendix C.7).

Exit gate:

Kullanıcı modeller arasında manuel prompt taşımadan bütün workflow'u conversational interface üzerinden yönetebilmeli.

---

# Phase 11 — Adapter Ecosystem & Certification

> Reframing (Appendix C.8): heterogeneous backends (api + harness families)
> artık Phase 2'den itibaren mevcut olduğu için eski "Provider Expansion"
> scope'u conceptually emekli edilmiştir; farklı ad, aynı amacın
> büyütülmüş halidir.

Purpose:

* additional API providers,
* additional harnesses (subscription-backed CLI agents),
* local runtimes,
* adapter × harness-version × OS compatibility matrix,
* conformance suite (offline fakes mandatory, live smoke opt-in),
* capability certification & honest-declaration auditing,
* credential-hygiene audit per adapter.

Exit gate:

Yeni bir compliant provider/harness workflow/state-machine/core protocol
mimarisine ZERO modification ile entegre olabilmeli — import-direction
architecture testleriyle zorlanır (Appendix C.8).

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
│   │   ├── harness_runtime.py    # generic harness contract + process runtime (P2.1)
│   │   ├── codex_cli.py
│   │   ├── claude_code.py
│   │   └── antigravity_cli.py    # product names live ONLY in adapters (App. C.1)
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
4. configured implementer executes (any harness or API adapter)
5. tests run
6. reviewer receives diff
7. reviewer returns PASS/FIX
8. FIX automatically goes back to the implementer
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

## Product north star

A Relay Room is a persistent group conversation with a user-selected AI team:
the user develops an idea interactively with a Planner, freezes that discussion
into a canonical executable plan, and lets Relay automatically coordinate
implementation, verification, review, and fix loops. Agents may exchange
bounded, typed messages to resolve ambiguity or challenge prior work — but
conversation never silently becomes workflow state; consequential exchanges are
promoted into explicit canonical plans, decisions, findings, artifacts, or
evidence (Appendix D). Two normative compressions of this model:

> **Conversation is coordination input, not workflow authority.**

> **Agent collaboration is bounded and typed, not free-form autonomous chatter.**

---

# 33. Success Criteria

Relay başarılı sayılacaksa:

### Manual friction

Bir agent'ın çıktısını diğerine manuel copy-paste etme ihtiyacı ciddi ölçüde ortadan kalkmalı.

### Reliability

Bir modelin sözlü "PASS" demesi verification yerine geçmemeli.

### Provider independence

Bir model/provider değiştirildiğinde workflow bozulmamalı — execution
family'ler arasında (api ↔ harness) geçiş dahil.

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
P0  Specification Freeze
 ↓
P1  Single-Agent Runtime
 ↓
P2  Generic Harness Runtime
 ↓
P3  Deterministic Task State Machine
 ↓
P4  Multi-Agent Messaging (communication bus)
 ↓
P5  Discussion Protocols (communication policy & budgets)
 ↓
P6  Automated Implementation Review Loop (semantic execution loop)
 ↓
P7  Rooms & Long-Lived Context (persistent Room model)
 ↓
P8  Decision Provenance
 ↓
P9  Relay Server
 ↓
P10 MCP / Chat Interface Integration
 ↓
P11 Adapter Ecosystem & Certification
 ↓
P12 TUI
```

P-labels are the §27 Phase numbers — the operative convention used by exit
gates, commit history, and status reporting. Pre-harness drafts numbered the
order differently; where older prose or artifacts diverge, §27 numbering is
authoritative. The bus/protocols phases precede the automated loop because
the loop consumes them as infrastructure.

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

---

# Appendix A — Phase 0 Hardening Amendments

Normative amendments ratified during Phase 0 hardening. They refine —
never weaken — the guarantees above.

## A.1 Evidence is first-class (amends §6, §33)

* An `EvidenceKind` value in a caller's hand is a **claim**, not proof.
* Proof is an immutable `EvidenceRecord` (id, kind, task_id, run_id?,
  tool_run_id?, artifact_id?, produced_by, created_at) written into an
  append-only `EvidenceStore`.
* `TaskStateMachine` is bound to `(task_id, store)` at construction and
  accepts **no evidence arguments**; transitions resolve gates exclusively
  from store records scoped to the task.
* Provenance contract: `TESTS_PASSED` requires `tool_run_id`;
  `REVIEW_PASSED`, `PLAN_PRODUCED`, and `IMPLEMENTATION_PRODUCED` require
  `run_id`. Incomplete evidence is rejected at the store boundary.
* Producer conventions: approval evidence may only be produced by
  `human:*`; `NO_PENDING_APPROVALS` only by `relay:*`. Agent-authored
  approval or policy evidence cannot enter the store.

## A.2 System events are distinct from conversation messages (amends §15)

* `EventLogEntry.type` uses a dedicated system vocabulary (`EventType`):
  task_created, state_transitioned, agent_run_started/finished,
  message_sent, artifact_created, evidence_recorded, tool_requested/
  completed, approval_requested/granted/rejected,
  decision_proposed/accepted/rejected.
* `MessageType` remains exclusively conversational: the Phase-0 frozen set
  (opinion, challenge, rebuttal, final_position, synthesis, review_finding),
  extended additively from Phase 4 onward per Appendix D.5
  (clarification_request, clarification_response, proposal, note) — still
  never a carrier for system events.
* The two vocabularies are disjoint by test. Agent conversation appears
  in the log only as MESSAGE_SENT markers referencing Message records.

## A.3 Final human approval is policy-driven (amends §6)

Both paths are legal out of REVIEWING:

```text
REVIEWING → APPROVAL_REQUIRED → DONE   (requires APPROVAL_GRANTED)
REVIEWING → DONE                       (requires TESTS_PASSED
                                        + REVIEW_PASSED
                                        + NO_PENDING_APPROVALS)
```

* `CompletionPolicy.require_human_approval` defaults to **true**.
* Relay may attest `NO_PENDING_APPROVALS` only when policy does not
  demand a human AND no approval request is pending.
* Safety invariant: if policy requires human approval, DONE is
  unreachable without explicit `human:*`-produced approval evidence.

## A.4 Tool execution has exactly one public path (amends §17/§19)

Binding for Phase 2+: every tool execution flows through
`PermissionGate.check()`. Executors act only on outcome `allow`;
`needs_approval` blocks until a human Approval exists; `never` is
absolute. No executor may bypass or pre-execute before the gate.

Scope refinement (harness era): this invariant binds every tool
execution performed BY Relay or through Relay-owned executors.
External harnesses execute their own internal tools inside their own
process; those fall under the grant-tier authorization of Appendix C.5
plus compensating controls — never a bypass. Wherever an action IS
representable to Relay (tool/approval events surfaced by the harness),
the gate remains mandatory.

---

# Appendix B — Phase 1 Amendments

Normative amendments ratified at the start of Phase 1
(Single-Agent Runtime). They refine — never weaken — the guarantees of §27.

## B.1 Run input/output are first-class artifacts (amends §5/§15)

* One-shot agent exchanges are not conversation-bus traffic;
  `MessageType` is untouched by them.
* What enters and leaves a run persists as first-class artifacts with
  kinds `run_input` / `run_output` (`ArtifactKind.RUN_INPUT`,
  `RUN_OUTPUT`), tied to the Run.
* Lifecycle events (`AGENT_RUN_STARTED`, `AGENT_RUN_FINISHED`) remain
  pure lifecycle markers: they reference the run/artifact ids and must
  never carry prompt or response payloads as their canonical record.

## B.2 Execution families (amends §7)

Relay agents fall into two execution families:

```text
API-backed       OpenAI-compatible HTTP, Anthropic API, DeepSeek API,
                 local OpenAI-compatible servers
Harness-backed   Codex CLI using its own ChatGPT/account authentication,
                 Claude Code using its own subscription/account
                 authentication, Google's current subscription-backed
                 terminal agent path (expected: Antigravity CLI —
                 product names are adapter-specific facts, App. C.1),
                 Gemini CLI only where still defensible (enterprise
                 license / API-key paths), future local/external agent
                 harnesses such as OpenCode
```

The core `Agent` abstraction assumes nothing about transport:
no assumption that an agent is HTTP-backed, API-key authenticated,
billed per token, or invoked through a model API. `TokenUsage` fields
are optional; `cost_usd` may remain `None`; harness runs may carry no
usage data at all.

## B.3 Authentication ownership (amends §7/§18)

* API adapters may resolve credentials from environment/config.
* Harness adapters own their login/session/authentication entirely.
* Relay never scrapes, copies, persists, proxies, or reinterprets
  subscription session credentials.
* Relay invokes authenticated harnesses only through their supported
  CLI/process interface.
* Credentials/secrets come from environment variables only; user
  configuration files hold non-secret provider facts (backend type,
  adapter name, model, base URL) and must never contain keys/tokens.

## B.4 Roadmap note (amends §27 ordering labels)

P1 delivers the first **API-backed** adapter. Phase 2 delivers the
**Generic Harness Runtime** — the first **subscription-backed (harness)**
execution support — as provider-neutral work packages (contract +
process runtime + conformance suite first; then per-harness reference
adapters). Downstream roadmap consequences (P3–P11 + certification
reframing) are normatively amended in Appendix C. The api/harness
separation above is fixed now so configuration represents backend type
without schema redesign later.

---

# Appendix C — Generic Harness Runtime Amendments (ratified before Phase 2)

Normative amendments covering harness-backed execution from Phase 2
onward. They refine — never weaken — prior appendices and §27.

## C.1 Execution-family vocabulary and product naming (amends §7, App. B.2/B.4)

Core vocabulary is: execution family (api | harness), adapter, backend,
capability, role. Vendor product names (Codex, Claude Code, Antigravity
CLI, Gemini CLI, OpenCode) exist ONLY in adapter modules, configuration
values, and their tests — never in core module names, state-machine
logic, routing, or protocol definitions. Provider URL/product pivots
must be absorbable by editing adapter packages alone.

Current knowledge frozen here for planning purposes (verify against
official docs at implementation time): Google serves consumer
subscription terminal-agent usage through Antigravity CLI (successor
surface to Gemini CLI for AI Pro/Ultra/individual accounts since June
2026); Gemini CLI persists for enterprise licensing and paid API-key
access. Therefore the Google subscription-path adapter targets
Antigravity CLI; Gemini CLI remains an optional compatibility candidate
only.

## C.2 Generic harness contract (amends §7, enables §27 Phase 2)

Every harness adapter implements the same contract — discoverable
executable + version inspection; declared capabilities; managed
subprocess lifecycle (spawn/pumps/graceful→hard terminate); pinned
working directory; timeouts/cancellation with normalized run status;
stdout/stderr normalization; best-effort structured-output parsing;
sanitized persisted errors; exit-code semantics profile;
capability-gated feature use; session/resume seam (may be explicitly
unsupported); environment policy (C.4); crash-safe persistence
identical to P1 ordering (input artifact before spawn). Adapters that
fail lifecycle MUST leave the canonical store consistent with any other
failed run.

## C.3 Capability contract (extends §7/§17 selection vocabulary)

Capabilities are typed, declared statically by each adapter, and queried
by core — code asks WHAT a harness can do, never WHICH vendor it
belongs to.

Candidate set (closed for extension across releases; additions require
an appendix note): structured_output, read_only_access,
workspace_write, shell_execution, git_operations, tool_event_stream,
approval_event_stream, session_resume, model_selection,
resolved_model_reporting, token_usage_reporting, diff_reporting,
network_access.

Rules:

* Unsupported capability ⇒ explicit typed failure
  (`UnsupportedCapability`) at request validation time. Silent
  degradation is forbidden.
* Capability declarations feed authorization (C.5) and selection
  (§27 P4/P5): routing and protocol engines match on
  (role, required_capabilities, backend_family), not provider names.
* Declared-but-broken capabilities fail adapter conformance
  certification (Adapter Ecosystem & Certification, §27 Phase 11), not
  merely review.

## C.4 Authentication & environment trust boundary (preserves App. B.3)

Binding rule: **Relay invokes the harness. The harness owns
authentication.**

Relay must not: read browser/session tokens; copy subscription
credentials; persist OAuth/session credentials; impersonate provider
login flows; convert subscription authentication into Relay-owned
credentials.

Environment isolation for harness child processes:

* Children receive an explicit ALLOWLIST baseline (OS-required
  variables: PATH/HOME-USERPROFILE/TEMP/TMP/system roots/locale) — not
  the raw parent environment.
* Every adapter declares its `conflict_variables` — provider auth
  variables that would flip the harness into another billing/auth mode
  (e.g. the OpenAI pair for Codex CLI; the Anthropic triple for Claude
  Code; Gemini/Google API variables for the Google adapter). Relay
  strips ALL adapters' conflict sets from every harness child
  environment by default; an adapter may whitelist a variable only for
  itself, deliberately, in its profile.
* Relay-resolved API credentials are never forwarded into any harness
  child process. An unrelated provider key in Relay's parent
  environment must never alter harness billing mode — enforced by an
  auth-conflict test matrix (a fake harness echoes its received
  environment; assertions are data, not prose).

Persistable harness facts (allowlist — everything else is forbidden):

* adapter identity/name, discovered executable label + version;
* configured auth mode IF safely observable (e.g. "subscription",
  "api_key" as declared by the harness itself);
* auth_state ∈ {authenticated, unauthenticated, unknown};
* a NON-SECRET external session reference when a provider exposes one
  AND config explicitly opts in (field name `external_session_ref`;
  never a secret-shaped value).

Prohibited from persistence: tokens, cookies, OAuth artifacts, account
identifiers usable for login, anything derived from scraping login
state. Hygiene enforcement lives in the existing persisted-vocabulary
tests, extended to new models.

## C.5 Permission boundary — two tiers (refines §17/§18/§19; amends App. A.4 wording)

Trust boundary, stated exactly:

1. **Grant tier (always enforced, non-overridable):** a harness run
   starts only with an Execution Grant chosen by Relay policy — at
   minimum one of read_only_access / workspace_write /
   workspace_write+network, translated onto harness-native restriction
   flags where available and into Relay-side containment where not.
   Dangerous Relay actions (install_dependencies, git_push,
   destructive_shell, merge_pr …) appear in the grant decision with
   their existing policy outcomes auto/ask/never. There is NO way for a
   run to obtain a capability whose governing policy is `never`, and no
   harness bypasses Relay authorization by owning its internal tools.
2. **Mediation tier (conditional):** where a harness exposes
   tool/approval event streams, Relay mediates them — events map onto
   ToolRun records and approvals flow through existing human-approval
   mechanics (A.3). Where a harness exposes none, Relay DOES NOT CLAIM
   per-tool enforcement; it records observed outcomes only.

Compensating controls for unmediated internals (required wherever tier
2 is absent and writes/shell/network are granted): dedicated
worktree/clone containment; pre/post repository-state snapshots;
post-run diff extraction as DIFF artifact; sanitization of captured
errors (C.4); explicit evidence that verification ran through
Relay-owned channels.

Wording amendment to A.4: the single-path invariant continues to bind
every tool execution performed BY Relay or through Relay-owned
executors; harness-internal tool execution falls under this appendix's
grant tier. The gate remains mandatory whenever an action/permission is
representable — owning internal tools never confers bypass rights.

## C.6 Run/persistence model seam (extends §14/§25; no gratuitous change)

Frozen P1 `Run.model` stays as REQUESTED model. Additive, nullable,
provider-neutral columns planned before harness loops need them:
`resolved_model` (reported by harness when known), `adapter_version`
(harness binary/version), `external_session_ref` (C.4 allowlist), plus
`backend` (snapshot of execution family at run time) for audit
symmetry. SQLite migration = ADD COLUMN only; historical rows
unchanged. Any richer backend metadata is deferred and, if ever
required, arrives as a strict, redaction-checked JSON column — never
free-form secrets.

## C.7 Roadmap consequences (annotates §27 P3–P10; extended by Appendix D.11 for Room & communication semantics)

* P3: harness-emitted statements (implementation complete / tests
  passed / task done) enter ONLY as claim-bearing artifacts and
  evidence candidates — TESTS_PASSED still requires a Relay-scoped
  tool_run_id; REVIEW_PASSED / IMPLEMENTATION_PRODUCED still require
  run_id. State transitions resolve exclusively from the EvidenceStore.
  Verification gates remain authoritative.
* P4: bus supports heterogeneous pairs day one (api→harness,
  harness→other harness, planner-api→implementer-harness→reviewer-*);
  routing keys are identity/role/backend_family/capability.
  Hard-coded provider branching is a test-forbidden pattern.
* P5: discussion protocols request roles + required capabilities;
  binding to concrete adapters happens through config/selection, never
  protocol text.
* P6: automated loop is provider-neutral: Plan → Implementer →
  Verification → Reviewer → Fix → Implementer → Reviewer → PASS;
  implementer/reviewer/planner roles are independently replaceable;
  candidate selection is deterministic over capabilities +
  availability + auth_state.
* P7: room member identity = logical agent id (stable across backend
  swaps); harness-specific mutable facts live on runs/tasks;
  resumability honors session_resume capability else reconstructs via
  the Context Engine (fresh run, honest discontinuity).
* P8: provenance graph nodes may carry backend family/harness version.
* P9: server serialization includes capability-aware descriptors;
  remote permission requests traverse the identical core gate
  (transport grants no privileges).
* P10: chat/MCP-facing selection queries role + capability (e.g.
  "adversarial reviewer with review-pass history"), resolves agents via
  the registry.

## C.8 Phase 11 reframing (amends §27 Phase 11)

Old scope ("Provider Expansion": OpenAI / Anthropic / DeepSeek /
Codex CLI / Claude Code / OpenCode / local APIs) is conceptually
retired: heterogeneous backends arrive with Phase 2, BEFORE multi-agent
orchestration (Phase 4). Replaced by **Adapter Ecosystem &
Certification** (§27 Phase 11):

* more API providers; more harnesses; local runtimes;
* adapter × harness-version × OS compatibility matrix;
* conformance suite upgrades (offline fakes mandatory; live smoke
  opt-in);
* capability certification & honest-declaration auditing;
* credential-hygiene audit per adapter;
* admission criterion: a compliant provider/harness integrates with
  ZERO modifications to workflow/state-machine/core-protocol code
  (enforced by import-direction architecture tests).

Exit gate: a brand-new compliant adapter (third-party-authored is
ideal) passes certification without touching relay/core, relay/storage,
or the bus.

---

# Appendix D — Relay Room & Bounded Inter-Agent Communication Amendments

Normative amendments defining the intended **Relay Room** experience and the
**bounded inter-agent communication model** from Phase 4 onward. They refine —
never weaken — §§1–37 and Appendices A–C. Nothing here adds a task state,
changes evidence or permission semantics, freezes UI syntax, or introduces
provider-specific concepts into core protocol vocabulary.

## D.1 Product north star and normative principles

A Relay Room is a persistent group conversation with a user-selected AI team.
The user develops an idea interactively with a Planner, freezes that discussion
into a canonical executable plan, and lets Relay automatically coordinate
implementation, verification, review, and fix loops. Agents exchange bounded,
typed messages to resolve ambiguity or challenge prior work; consequential
exchanges are promoted into explicit canonical plans, decisions, findings,
artifacts, or evidence.

Normative compressions (restated from §32):

> **Conversation is coordination input, not workflow authority.**

> **Agent collaboration is bounded and typed, not free-form autonomous chatter.**

The user-facing mental model is *managing an AI team*, not *sending prompts to
several independent models*. A Room UI would typically pair a seat roster
(role → bound logical agent → activity state) with one chronological feed of
messages, canonical records, and human/system entries. Such sketches are
illustrative only; the contracts below are normative, and no UI wording in this
appendix is frozen.

Relay remains exactly what §2 says it is — the coordination/authorization layer.
The Room experience changes what RELAY COORDINATES (seats, plans, bounded
agent-to-agent exchanges), never WHO OWNS STATE or AUTHORITY.

## D.2 Seats are configuration; roles are protocol

A Room exposes named **seats** (Planner, Implementer, Reviewer, Fixer; optional
Verifier / Researcher / Domain Expert). Each seat binds to a configured
logical agent:

```text
Planner      → claude        (api-family config instance)
Implementer  → codex-cli     (harness-family config instance)
Reviewer     → gpt           (api-family config instance)
Fixer        → claude-code   (harness-family config instance)
```

Product names above are configuration-time instance values (App. C.1) used for
illustration only.

Definitions, used consistently throughout this appendix:

* A **logical seat** is a stable logical place in a Room — a role slot that
  persists across rebinding and backend swaps.
* A **binding** assigns a configured agent to that seat. It is a
  configuration/runtime fact.
* A **logical agent** is the configured instance a binding points to; its
  provider, model, and backend are properties OF THE BINDING — never protocol
  semantics and never part of the seat's identity.

* Workflow, state machine, protocols, and routing speak ONLY role +
  capability + logical-agent identity (§8, C.3, C.7). Seat→agent bindings are
  runtime/config choices — never protocol semantics.
* Rebinding a seat to a different logical agent (different model OR different
  backend family) must not change workflow behavior; selection stays
  deterministic over capabilities + availability + auth_state (C.7 P6).
* Core vocabulary gains NO provider concepts from this appendix (C.1 holds).

## D.3 Discussion-first Rooms and the canonical plan contract

A Room does NOT begin autonomous execution. Its initial mode is human-led
discussion — primarily with the Planner — where clarifications, constraints,
scope, assumptions, risks, and unresolved questions are worked out over an
unbounded amount of useful time.

The expected flow:

```text
User → Planner discussion → clarifications/constraints/scope
     → structured plan → explicit plan freeze → automatic execution
```

During this stage the Planner helps transform an incomplete idea into a
durable implementation brief containing, where applicable: goal, scope,
non-goals, assumptions, architectural decisions, constraints, implementation
units, acceptance gates, tests, risks, unresolved questions.

Autonomous execution begins only on an EXPLICIT transition (e.g.
"Freeze Plan & Execute"). Exact wording is UX; the semantic boundary is
normative:

> **Discussion must never implicitly start implementation.**

The freeze/accept decision belongs to the HUMAN. The Planner may prepare,
propose, and revise plan artifacts inside the discussion, but it cannot accept
its own plan into canonical state and cannot start execution by itself. The
explicit human freeze maps onto the existing `PLAN_READY → IMPLEMENTING`
edge of §6 — no new TaskState is introduced. Standalone entry points such as
`relay build "..."` are themselves explicit human initiation; the
discussion-first discipline governs Room-led workflows.

A frozen plan becomes a canonical Room/Task artifact (`ArtifactKind.PLAN`
today), referenced as PLAN-n:

```text
PLAN-17 accepted by the human freeze decision
```

Downstream agents operate against the canonical plan plus associated decisions
— NOT against an uncontrolled replay of the full conversational transcript
(D.10).

Supersession is first-class: a consequential follow-up may promote into
DECISION-n and/or PLAN-(n+1) carrying an explicit "supersedes PLAN-n"
relationship (`DecisionStatus.SUPERSEDED` already models this shape); agents
resume against the NEW canonical context. Plan-history chains are append-only:
revisions create new records, they do not rewrite accepted ones (§15).

## D.4 Conversation is not state

> **Conversation may inform canonical state, but conversation is not canonical
> state.**

Plain agent messages NEVER silently mutate: the accepted plan, task state,
decisions, approval state, evidence, role assignments, or permission/protocol
policy. This holds by construction, via guarantees that already exist:

* state transitions resolve exclusively from provenance-backed EvidenceStore
  records (A.1, C.7);
* approval evidence comes only from `human:*` producers (A.1/A.3);
* permissions change only through Relay policy and PermissionGate (§18, C.5);
* canonical history is append-only (§15).

Most conversation needs NO promotion — questions receive answers,
explanations inform their readers, notes persist as context. Only
consequential, state-affecting exchanges require promotion into explicit
canonical records:

```text
Message → Proposal / Clarification / Challenge → Resolution
        → canonical Decision and/or Plan revision
        → updated participant context
```

An agent saying "yes, that sounds good" is not by itself a hidden workflow
mutation. When an exchange IS consequential, Relay performs promotion
explicitly (Decision, Plan revision, Finding, Approval, Artifact, Evidence
record, task/workflow transition); each promotion is a new append-only record
with provenance. Promotion is Relay's act — never a side effect of messages.

## D.5 Typed inter-agent messages

Inter-agent communication is first-class data (§5 Message). The Phase-0
frozen conversational set remains; required communication purposes are
reconciled against it and extended ONLY where semantically necessary:

| Required purpose | Disposition | Notes |
|---|---|---|
| question / clarification_request | NEW: `clarification_request` | blocking-capable |
| answer / clarification_response | NEW: `clarification_response` | pairs with request |
| proposal (incl. local deviation from current plan) | NEW: `proposal` | distinct from opinion: targets adoption of a change |
| challenge (assumptions, findings, prior work) | EXISTS: `challenge` | unchanged |
| note (non-blocking information for a later participant) | NEW: `note` | never blocks |
| finding | EXISTS: `review_finding` | unchanged |
| blocker | metadata, NOT an enum value | any blocking-flagged challenge/proposal/finding |
| plan_revision_request | `proposal` referencing the target plan revision | no separate enum |
| decision_response | answer class (`rebuttal` / `final_position`, or `clarification_response`) chosen by exchange shape | reuses existing vocabulary |

Blocking-ness is per-message metadata, never implied by type alone (D.6).
This table fixes CONCEPTS, not spellings: identifiers are shown in lowercase
concept form (matching today's persisted message-type values); final enum
member names, casing, and storage values are left to P4 implementation. The
enum may evolve further, provided the disjointness invariant of A.2 holds and
extensions remain additive.

Message records carry enough routing/provenance information to identify:
sender logical agent; recipient logical agent OR role; room; task; message
type; whether the message blocks execution (and its budget accounting key
where relevant); references to related plans/decisions/findings/artifacts/
evidence where applicable; timestamp. The existing `Message` fields
(sender/recipient/room_id/task_id/type/content/references/created_at) stand;
additions land additively when Phase 4 implements them (same additive-only
discipline as C.6).

EventType/MessageType disjointness (A.2) is untouched: agent conversation
still appears in the event log only as MESSAGE_SENT markers referencing
Message records.

Illustrative end-to-end promotion:

```text
Implementer → Planner (proposal):  "C4 specifies one normalization registry.
  Bundle-specific registries preserve the same public interface while reducing
  special cases. May I use that design instead?"
Planner → Implementer:             "Yes — bundle-specific registries, but keep
  the public interface and the fold-local-state constraint."
Relay promotes the exchange  → DECISION-24 (accepted constraint-resolved design)
                             → PLAN-18 supersedes PLAN-17
Implementer resumes against the new canonical context.
```

## D.6 Blocking vs non-blocking communication

Two classes:

**Blocking clarification** — the current participant cannot safely continue
without an answer ("I cannot choose between these two schema interpretations
without changing the accepted contract.").

```text
IMPLEMENTER → WAITING_FOR_CLARIFICATION → planner response
            → optional Decision / Plan revision → IMPLEMENTER CONTINUES
```

The WAITING element in the diagram above is drawn as illustration only: it is
a RUNTIME EXECUTION CONDITION, not a canonical TaskState and not a proposed
enum member. The frozen §6 state machine is untouched; waiting is a
stage-internal scheduler/dispatch condition that a later implementation phase
MAY represent as an execution/dispatch substate. Resolution authority stays
with Relay — a blocked stage resumes only when the answer (and any promoted
canonical record) exists, never because an agent asserts continuation.
Blocking round-trips count against budgets (D.7); unanswered/blocked stages
escalate to the human rather than spinning silently.

**Non-blocking note** — the participant continues immediately ("This
compatibility shim is intentional because v1 databases must migrate in
place."); the NOTE becomes available context for its recipient later through
Context Engine reconstruction (D.10).

Micro-loops formed by these exchanges live INSIDE the deterministic outer
workflow (D.11 P6) — they never replace it.

## D.7 Communication policy and boundedness

Relay must not become an uncontrolled autonomous group chat. Who may contact
whom, for what semantic reason, and with what budget is POLICY-controlled.
Configuration/protocols can express constraints equivalent to:

```text
implementer: → planner:  [clarification_request, proposal]   # blocking allowed
             → reviewer: [note]
reviewer:    → implementer: [clarification_request, challenge]
             → planner:  [challenge]
fixer:       → planner:  [proposal, clarification_request]
             → reviewer: [clarification_request]
```

(The tree above is illustrative; exact syntax is deferred to P5.) Invariants:

* support maximum blocking-clarification round-trips per stage and maximum
  total inter-agent messages per task/stage;
* loop detection applies to agent↔agent traffic (§24 Anti-Loop conditions —
  repeated arguments, no new evidence, max rounds, budget exceeded);
* when communication cannot converge, escalate to the HUMAN instead of
  continuing;
* policy violations are rejected/degraded explicitly by Relay — never
  silently re-routed around policy.

Exhausting any budget is a deterministic event: it triggers escalation, not a
silent extension. Budgets are never auto-increased mid-task, and forbidden
exchanges cannot be re-sent under another type to evade accounting.

No arbitrary unbounded agent-to-agent conversation exists anywhere in the
model: free-form debate survives ONLY inside protocol-bounded stages (§9–§11),
and Room micro-communication survives only inside policy budgets —
**agent collaboration is bounded and typed, not free-form autonomous
chatter** (D.1).

## D.8 No hidden authority transfer

Inter-agent communication never grants new authority. Through conversation
agents may NOT:

* change their own permissions;
* assign themselves another role;
* approve their own privileged action;
* declare their own claims authoritative evidence;
* mark verification evidence satisfied;
* silently alter protocol/policy;
* bypass Relay routing;
* bypass PermissionGate / Execution Grants (A.4/C.5).

Existing evidence, approval, permission, and state-machine guarantees remain
authoritative. Each failure mode above is either impossible by construction
or a conformance defect that P4/P5 acceptance tests must demonstrate closed.

## D.9 User participation

The human remains a first-class Room participant. Product phases must make
the following possible without re-defining protocol semantics here:

* address a specific role/agent (illustrative UX: "@reviewer Is this really a
  blocker?", "@planner Evaluate the reviewer finding without expanding scope.",
  "@implementer Why did you choose this approach?");
* interrupt automatic execution;
* ask the Planner to reconsider a finding; ask the Reviewer to justify a
  blocker;
* request a plan revision;
* pause/resume execution;
* replace/rebind a logical agent seat (config-level operation, D.2);
* inspect decisions/artifacts/evidence generated from agent exchanges
  (`relay why <decision>`, `relay inspect <task>` already promise this).

Addressing/intervention syntax belongs to later product phases (P7+); the Room
architecture defined above must not make this interaction impossible.

Human intervention never bypasses evidence, approval, or permission contracts.
A message typed into Room chat is ordinary Message traffic; asking an agent in
chat does not mint evidence, and requesting a gated action still traverses
PermissionGate with its existing outcomes. Approvals enter canonical state
only through the existing approval mechanics with `human:*` producers
(A.1/A.3) — participating in a Room grants human or agent alike no parallel
pipeline around these contracts.

## D.10 Context Engine consequence

Participants receive RECONSTRUCTED, task-relevant context — selected from the
canonical store, not unlimited chat replay:

ROLE · TASK · CURRENT PLAN (latest accepted) · plan supersession chain ·
ACCEPTED DECISIONS · UNRESOLVED BLOCKING COMMUNICATION · RELEVANT INTER-AGENT
NOTES · CURRENT REVIEWER FINDINGS · RELEVANT ARTIFACTS/DIFFS · EVIDENCE ·
relevant Room history excerpts.

The full Room transcript is NEVER the default participant input; agents MUST
NOT depend on receiving it (§12 states the same contract).

External harness session resume remains an optimization built on the C.2 seam
and C.4-allowlisted `external_session_ref`. If resume is unavailable,
unsupported, expired, or unsafe, Relay reconstructs context from canonical
records and starts a FRESH backend run — the honest-discontinuity rule of
C.7 P7 stands. External session state — including any successfully resumed
session — is never canonical; the canonical store remains the source of truth.

## D.11 Roadmap consequences (extends App. C.7; annotates §27 P4–P7)

**P4 — Messaging / communication bus.** Establishes first-class heterogeneous
communication between API-backed agents, harness-backed agents, and humans /
Relay system senders. Acceptance requirements:

* logical-agent-to-logical-agent messages;
* typed messages (D.5 vocabulary incl. additions, A.2 disjointness preserved);
* task/room references and routing/provenance fields on every record;
* blocking vs non-blocking semantics carried on messages (D.6);
* references to plans/decisions/findings/artifacts where applicable (D.5);
* API ↔ harness messaging through the SAME protocol; routing keys stay
  identity/role/backend_family/capability; provider-specific message routing
  remains a test-forbidden pattern (C.7 P4 upheld);
* chronological Room feed renderable purely from persisted messages +
  event log — making the Room feed technically possible here, realized as a
  product surface later (P7).

**P5 — Protocols.** Protocols define protocol-level COMMUNICATION PERMISSIONS
AND BUDGETS. Participants are selected by role, capability, and logical-agent
configuration — never provider/model name (C.7 P5 upheld). Design
requirements:

* allowed sender → recipient relationships (seat/pair level, D.7);
* allowed message purposes/types per relationship;
* blocking-clarification budgets (round-trips per stage);
* loop/message budgets per task/stage; loop detection;
* escalation behavior when communication cannot converge;
* policy-controlled micro-communication only inside protocol-governed stages
  (discussion protocols and execution-loop stages alike).

P5 exists precisely to prevent free autonomous group chat: an unconstrained
sender→recipient edge, purpose set, or budget is a conformance defect.

**P6 — Semantic execution loop.** Outer loop unchanged verbatim:

```text
Plan → Implementer → Verification → Reviewer → PASS
Reviewer findings → Fixer → Implementer/patch → Verification → Reviewer → PASS
```

Bounded micro-interactions may occur INSIDE stages (Implementer ↔ Planner
clarification; Reviewer ↔ Implementer question/response; Reviewer → Planner
challenge → Decision; Fixer → Planner plan-conflict proposal). Acceptance
criteria must prove:

* micro-communication cannot silently mutate canonical state (D.4);
* consequential changes become Decisions / Plan revisions (D.3/D.4);
* communication budgets stop non-convergent loops (D.7);
* the outer deterministic loop remains in control (stage completion still
  resolves exclusively from the EvidenceStore);
* the human can interrupt/escalate (D.9).

**P7 — Persistent Rooms / context.** Realizes the persistent "AI group chat"
Room model. Explicit requirements:

* stable logical Room members/seats across sessions (D.2);
* role bindings independent of provider/model (model ≠ role / backend ≠ role);
* persistent Room history surviving session boundaries;
* canonical plan/decision/finding relationships — frozen plans, their
  supersedes chains, and decision graphs as first-class Room state (D.3);
* targeted user messages to a specific participant (D.9);
* Context Engine reconstruction for each participant (D.10);
* safe external-session continuation when supported (C.2/C.4 seams honored);
* fresh-run reconstruction from canonical records when resume is unavailable
  (C.7 P7 honesty rule).

P7 is the phase where the Room becomes persistent across sessions rather than
only a transient execution protocol — completing the product north star of
D.1 under all frozen guarantees.

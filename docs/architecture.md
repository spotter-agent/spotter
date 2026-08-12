# Architecture

> **상태:** 현재 Hook 기반 prototype + [#66](https://github.com/Bogyie/spotter/issues/66)의 target architecture를 함께 설명한다.  
> **중요:** `spotterd`, App Server primary observation, live `turn/steer`는 아직 target이며 현재 shipping behavior가 아니다.

---

## 30초 요약

Spotter의 target architecture는 다음 한 장으로 요약된다.

```text
                     ┌─────────────┐
                     │  Codex TUI  │
                     └──────┬──────┘
                            │ same thread / turn
                            ▼
                 ┌──────────────────────┐
                 │ External App Server  │
                 │ observation/control  │
                 └──────┬────────▲──────┘
                        │        │
              events    │        │ turn/steer
                        │        │ turn/interrupt
                        ▼        │
                 ┌──────────────────┐
                 │     spotterd     │
                 │                  │
                 │ Thread Manager   │
                 │ Live State       │
                 │ Trace IR         │
                 │ Audit State      │
                 │ Signal Engine    │
                 │ Gate Engine      │
                 │ Reviewer         │
                 │ Intervention     │
                 │ Journal          │
                 └────────┬─────────┘
                          │
                 bounded deterministic
                     request/reply
                          │
                          ▼
                   PreToolUse Hook
```

핵심 결정은 여섯 가지다.

1. **App Server가 Codex trajectory의 primary observation source다.**
2. **semantic reviewer는 비동기다.** Main은 reviewer를 기다리지 않는다.
3. **`PreToolUse`는 deterministic BLOCK에만 남긴다.**
4. **live state는 `spotterd` 메모리가 소유한다.** journal은 durable log/recovery source다.
5. **reviewer verdict는 특정 thread/turn에 묶인다.** 늦게 도착한 verdict는 freshness를 검증한다.
6. **가장 먼저 App Server lifecycle/attach PoC를 통과해야 한다.** 같은 App Server를 공유하지 못하면 이 architecture를 다시 검토한다.

---

## 빠른 탐색

| 찾는 내용 | 섹션 |
| --- | --- |
| 현재와 목표 구조 차이 | [1. Current vs Target](#1-current-vs-target) |
| process별 역할 | [3. Component contracts](#3-component-contracts) |
| 실제 event가 어떻게 흐르는가 | [4. Runtime flows](#4-runtime-flows) |
| Hook은 왜/어디에 남는가 | [5. Enforcement path](#5-enforcement-path-pretooluse) |
| reviewer가 늦으면 어떻게 하나 | [7. Reviewer job / freshness](#7-reviewer-job과-freshness) |
| state를 어디에 저장하나 | [8. State ownership](#8-state-ownership) |
| thread/session 용어 | [9. Identity model](#9-identity-model) |
| App Server가 끊기면 | [11. Failure / degraded mode](#11-failure--degraded-mode) |
| 어떤 파일/socket이 생기나 | [12. Runtime resources](#12-runtime-resources) |
| adapter가 제공해야 할 capability | [14. Agent adapter contract](#14-agent-adapter-contract) |
| 먼저 검증할 architecture 전제 | [15. P0 PoC](#15-p0-app-server-lifecycle--attach-poc) |

---

# 1. Current vs Target

## 1.1 현재 prototype

현재는 Hook이 observation과 execution boundary를 동시에 담당한다.

```text
Codex / Claude Code
       │
       │ SessionStart / UserPromptSubmit
       │ PreToolUse / PostToolUse
       ▼
  spotter-hook process
       │
       ├─ config load
       ├─ journal append
       ├─ deterministic gate
       ├─ snapshot
       └─ reviewer cadence trigger
```

장점:

- 빠르게 구현할 수 있었다.
- fail-open boundary가 단순하다.
- 실제 Codex trajectory 데이터를 모을 수 있었다.
- gate, snapshot, fork/replay, reviewer 실험을 검증하는 데 충분했다.

한계:

- Hook마다 별도 process/bootstrap 비용이 든다.
- 여러 Hook process가 durable journal을 중심으로 상태를 공유한다.
- semantic reviewer의 long-lived state가 없다.
- reviewer가 판단을 끝낸 뒤 active turn에 독립적으로 개입할 control channel이 없다.
- periodic reviewer가 long session에서 계속 비용을 쓴다.
- App Server가 노출하는 더 풍부한 runtime event를 활용하지 못한다.

## 1.2 Target runtime

```text
App Server event
      │
      ▼
spotterd
  ├─ normalize
  ├─ update live state
  ├─ append journal
  ├─ evaluate cheap signals
  └─ schedule semantic reviewer

Main agent는 계속 실행

PreToolUse만 별도 synchronous path
```

핵심 변화:

```text
before
hook → journal → 필요할 때 state rebuild

after
event → live state → policy
                 └→ journal append
```

---

# 2. Runtime planes

세 가지 plane을 분리한다. 이 구분이 architecture의 중심이다.

| Plane | 질문 | Codex target surface | Latency 성격 |
| --- | --- | --- | --- |
| Observation | 지금 무엇이 일어나고 있는가? | App Server event stream | async |
| Control | 이미 진행 중인 trajectory에 어떻게 개입하는가? | `turn/steer`, `turn/interrupt` | async request |
| Enforcement | 실행 전에 반드시 막아야 하는가? | `PreToolUse` | sync / bounded |

### Observation plane

수집 후보:

- thread start/resume/close
- turn start/completion
- user message
- plan update
- reasoning summary가 runtime에 노출되는 경우
- command/tool start/completion
- stdout/stderr/exit status
- file change / diff
- MCP call
- web search
- token usage

### Control plane

사용 후보:

```text
VERIFY  → turn/steer(evidence request)
NUDGE   → turn/steer(course correction)
INTERRUPT → turn/interrupt
RESTART → interrupt + fresh continuation (later)
```

### Enforcement plane

실행 전 deterministic policy만 둔다.

```text
PreToolUse
  ↓
ALLOW / DENY
```

LLM reviewer가 synchronous path에 들어가면 안 된다.

---

# 3. Component contracts

아래 표는 구현 시 component boundary의 기준이다.

| Component | 책임 | 입력 | 출력 | 소유 상태 | 실패 시 기본 동작 |
| --- | --- | --- | --- | --- | --- |
| `spotter` CLI | setup/status/doctor/review 등 user control plane | argv/config | 사용자 출력, daemon RPC | 없음 또는 짧은 command state | 명시적 error |
| `spotterd` | long-lived supervision runtime | App Server events, Hook IPC, CLI RPC | journal, reviewer jobs, interventions | thread/live state | coding session을 깨지 않도록 degraded |
| `CodexAppServerClient` | App Server 연결·subscribe·control | endpoint/capabilities | normalized runtime events, RPC result | connection state | reconnect/degraded |
| `SessionManager` | thread/turn/attachment lifecycle | normalized events | current identities/state transitions | thread registry | 해당 thread만 degraded |
| `TraceNormalizer` | backend event → Trace IR | raw adapter event | TraceEvent | 없음 | unknown event 기록/skip |
| `AuditState` | claim/evidence/constraints/progress | TraceEvent | live audit view | per thread | incomplete state 명시 |
| `SignalEngine` | cheap candidate detection | live state/event | candidate signal | small rolling counters | no candidate |
| `ReviewerScheduler` | async reviewer 실행·budget·dedupe | candidate + context | ReviewerJob | queues/budgets | job failed, Main 계속 |
| `InterventionController` | verdict freshness/전달/escalation | reviewer decision | steer/interrupt/no-op | intervention history | stale/discard/degraded |
| `GateEngine` | deterministic pre-action 판단 | PreToolUse proposal + config | allow/deny | rule config + small state | fail-open |
| `JournalStore` | durable history | normalized records | append/read | disk | write failure telemetry; gate는 fail-open |
| `SnapshotManager` | Git state checkpoint/restore | repo state | snapshot ref/worktree | repo resources | snapshot failure가 Main을 깨지 않음 |
| `IntegrationManager` | setup/teardown/migration | agent config | manifest/config mutation | integration manifest | transactional rollback |

이 표에서 중요한 점은 **Spotter core policy가 Codex-specific transport shape를 직접 알지 않는 것**이다.

---

# 4. Runtime flows

## 4.1 Codex startup

Full managed mode의 이상적인 흐름:

```text
user login / runtime ready
        │
        ├─ spotterd ready
        └─ external App Server available

사용자: codex
        │
        ▼
Codex TUI attaches to external App Server
        │
        ├─ TUI = client A
        └─ Spotter = client B
```

**주의:** 현재 plain `codex`는 reusable external daemon이 없으면 Embedded App Server를 선택할 수 있다. 따라서 첫 `PreToolUse`에서 daemon을 띄우는 것은 full mode에는 늦을 수 있다.

이 때문에 P0 PoC가 먼저다.

## 4.2 일반 observation event

예: command 완료 event.

```text
App Server
  │ command completed
  ▼
CodexAdapter
  │ raw event → TraceEvent
  ▼
SessionManager
  │ thread/turn identity resolve
  ▼
Live State
  ├─ recent failures update
  ├─ validation state update
  └─ touched scope update
  │
  ├──────────────► Journal append
  │
  ▼
SignalEngine
  │
  ├─ no candidate → 끝
  └─ candidate → ReviewerScheduler
```

Normal observation은 Hook을 거치지 않는다.

## 4.3 Semantic review

```text
SignalEngine
  │ POSSIBLE_TOOL_FAILURE_LOOP
  ▼
ReviewerScheduler
  │ create job(thread=T1, turn=U7)
  ▼
Reviewer runs asynchronously

          Main continues

ReviewerDecision
  │ NUDGE
  ▼
InterventionController
  │ target U7 still active?
  ├─ yes → turn/steer
  └─ no  → stale policy
```

## 4.4 Deterministic gate

```text
Codex proposes: git reset --hard
        │
        ▼
PreToolUse
        │ stdin JSON
        ▼
spotter-hook
        │ bounded IPC
        ▼
spotterd GateEngine
        │
        ├─ ALLOW
        └─ DENY(rule=git_reset_hard)
        │
        ▼
Hook response
```

이 path에서는 network/model call을 하지 않는다.

## 4.5 Turn completion

```text
turn/completed(U7)
      │
      ├─ active_turn = none
      ├─ finalize timing/tool counters
      ├─ validation state finalize
      └─ pending ReviewerJob(target=U7) freshness 재평가
```

Thread state는 사라지지 않는다.

## 4.6 Resume

```text
Codex resumes existing thread T1
        │
        ▼
App Server thread event
        │
        ▼
SessionManager finds durable Spotter history
        │
        ├─ hydrate missing live state from journal
        ├─ reconcile App Server current thread state
        └─ create new RuntimeAttachment
```

Journal replay는 이런 recovery/resume boundary에서 사용한다.

---

# 5. Enforcement path: `PreToolUse`

Target Codex integration에서 Hook은 가능하면 `PreToolUse` 하나만 남긴다.

| Hook | 현재 | Target |
| --- | --- | --- |
| `SessionStart` | session bootstrap | App Server lifecycle로 대체 |
| `UserPromptSubmit` | user goal capture | App Server user-message event로 대체 |
| `PreToolUse` | proposal + gate | **deterministic atomic gate 유지** |
| `PostToolUse` | result/snapshot/review cadence | App Server result/diff로 대체 |

### Gate가 판단해도 되는 것

- destructive command class
- forbidden path
- dependency manifest policy
- workspace escape
- 명시된 user/project execution restriction
- 나중에 충분히 deterministic하게 분류 가능한 external write

### Gate가 판단하면 안 되는 것

- “이 refactor는 불필요해 보인다”
- “이 가정은 아마 틀린 것 같다”
- “이 접근보다 다른 구현이 좋아 보인다”
- 전체 repository를 다시 읽어야 하는 의미론적 판단

이런 건 async reviewer 영역이다.

### Failure policy

```text
spotterd unavailable
IPC timeout
unsupported syntax
unknown workspace
```

기본은 **fail-open + telemetry**다.

Spotter 장애가 supervised agent 자체를 중단시키면 안 된다.

---

# 6. Trace IR

Raw runtime event를 policy에 직접 전달하지 않는다.

```text
Codex App Server event
Claude event
future agent event
       │
       ▼
   Adapter
       │
       ▼
   Trace IR
       │
       ├─ live state
       ├─ signal
       ├─ reviewer
       ├─ metrics
       └─ journal
```

최소 필드 예시:

```text
TraceEvent
  event_id
  agent
  agent_thread_id
  turn_id
  item/tool_id
  timestamp
  kind
  operation
  files/resources
  result/outcome
  repository/worktree
  provenance(raw event reference)
```

Policy가 필요로 하는 semantic field는 점진적으로 추가한다.

```text
constraint ids
candidate hypothesis ids
validation relation
side-effect class
```

중요한 원칙:

> Normalization 때문에 provenance를 잃지 않는다.

`turn/steer`, replay, causal analysis가 raw runtime item과 normalized event를 다시 연결할 수 있어야 한다.

---

# 7. Reviewer Job과 freshness

Semantic reviewer는 process라기보다 **job lifecycle**로 다룬다.

```text
QUEUED
  ↓
RUNNING
  ↓
DECIDED
  ├─ DELIVERED
  ├─ STALE
  ├─ DISCARDED
  ├─ CANCELLED
  └─ FAILED
```

ReviewerJob 최소 metadata:

```text
job_id
thread_id
target_turn_id
candidate_id
created_at
started_at
finished_at
reviewer model/config fingerprint
input coverage/truncation
verdict
confidence
delivery status
```

### Freshness rule

Reviewer가 5초 생각하는 동안 Main이 turn을 끝낼 수 있다.

```text
U7 signal
 ↓
reviewer running
 ↓
U7 completed
 ↓
reviewer says NUDGE
```

이때 U8에 무조건 NUDGE를 넣으면 안 된다.

초기 policy 후보:

```text
same active target turn
  → deliver via steer

target turn ended
  → default STALE/DISCARD
  → 일부 VERIFY만 명시적인 next-turn defer를 허용할 수 있음
```

어떤 policy든 journal에 남겨 다음을 측정한다.

- reviewer latency
- delivery latency
- stale rate
- reviewer spend wasted by stale verdict

---

# 8. State ownership

## 8.1 Live state

`spotterd`가 per-thread로 유지할 최소 state:

```text
ThreadState
  identity
  repository/worktree
  user goal
  constraints
  active turn
  hypotheses
  evidence
  open questions
  touched files
  validation state
  recent failures
  recent actions
  intervention history
  pending reviewer jobs
  reviewer budget
  connection/capability state
```

## 8.2 Durable journal

Journal은 다음을 위해 존재한다.

- crash recovery
- resume hydration
- analyze
- labels/metrics
- replay/fork
- experiment provenance
- forensic inspection

Journal은 **live state database가 아니다.**

## 8.3 Snapshot state

Git snapshot은 repository state를 복원하는 별도 resource다.

```text
ThreadState ≠ Journal ≠ Snapshot
```

- ThreadState: 현재 판단을 위한 memory state
- Journal: 실행 history
- Snapshot: 특정 시점의 filesystem/Git state

이 셋을 섞지 않는다.

---

# 9. Identity model

현재 `session`이라는 단어가 여러 의미를 담는다. Target에서는 분리한다.

```text
Agent Thread
  장기 대화/작업 identity

Turn
  한 번의 user→agent 실행 단위

Runtime Attachment
  TUI/client가 thread에 붙어 있는 한 번의 실행 구간

Reviewer Job
  특정 candidate/turn을 평가하는 독립 작업
```

예:

```text
Thread T1
├─ Attachment A1 (오늘 오전)
│  ├─ Turn U1
│  └─ Turn U2
└─ Attachment A2 (내일 resume)
   ├─ Turn U3
   └─ Turn U4
```

State placement:

| State | Identity scope |
| --- | --- |
| goal/constraints/hypotheses | Thread |
| active turn | Turn |
| reviewer freshness | Turn |
| connection latency | Attachment |
| journal lineage | Thread |
| fork branch point | Thread + Turn/Step |

---

# 10. App Server connection / capability model

Spotter는 Codex version 문자열 하나만 보고 기능을 가정하지 않는다.

Connection 시 capability를 확인한다.

```text
observe_thread_lifecycle
observe_user_message
observe_tool_start
observe_tool_result
observe_diff
observe_token_usage
steer_active_turn
interrupt_active_turn
atomic_pretool_veto (hook/other surface)
```

예시 status:

```text
Codex integration
  observation.thread:       yes
  observation.tool_result:  yes
  observation.diff:         yes
  control.steer:            yes
  control.interrupt:        no
  enforcement.pretool:      yes
```

이 모델 덕분에 Codex upgrade 후 일부 feature만 깨져도 전체를 binary compatible/incompatible로 단순화하지 않고 degraded mode로 동작할 수 있다.

---

# 11. Failure / degraded mode

각 plane의 건강 상태를 따로 관리한다.

```text
spotterd health
App Server connection
Observation capability
Control capability
PreToolUse enforcement
Journal/storage health
Reviewer provider health
```

예:

```text
Spotter daemon:       running
App Server:           reconnecting
Observation:          unavailable
Live steer:           unavailable
PreToolUse gate:      active
Journal:              writable
Reviewer:             available
```

### spotterd crash

```text
spotterd crash
  ↓
service manager restart
  ↓
App Server reconnect
  ↓
active/loaded thread list
  ↓
reconcile + journal hydrate
  ↓
READY
```

Daemon이 죽어 있는 동안 Hook gate는 fail-open한다.

### App Server crash/disconnect

State machine 예:

```text
CONNECTED
  ↓
DEGRADED
  ↓
RECONNECTING
  ├─ CONNECTED
  └─ UNAVAILABLE
```

Spotter daemon이 살아 있다는 이유만으로 healthy라고 표시하지 않는다.

### Reviewer failure

```text
reviewer timeout/error
  → ReviewerJob FAILED
  → Main unaffected
  → spend/error telemetry
```

---

# 12. Runtime resources

정확한 path는 구현에서 확정하되 ownership은 처음부터 분리한다.

Conceptual layout:

```text
~/.config/spotter/
  config.toml
  integrations/
    codex.json

~/.local/state/spotter/   # 플랫폼에 맞는 state dir 사용 가능
  sessions/
  labels/
  experiments/
  logs/
  runtime/
    spotter.sock
    daemon metadata
  repos.json
```

현재 prototype의 `~/.spotter` data와 migration compatibility를 고려한다.

Repository 내부/ Git namespace에는:

```text
refs/spotter/...
Spotter-created detached worktrees
```

가 존재할 수 있다.

따라서 uninstall과 data purge는 별개다. 자세한 lifecycle은 [lifecycle.md](lifecycle.md).

---

# 13. Snapshots / replay / side effects

## Snapshot

현재 prototype의 안전 원칙을 유지한다.

- user HEAD/index를 수정하지 않는다.
- Spotter-owned Git refs만 사용한다.
- restore는 detached worktree로 한다.
- cleanup은 Git-aware operation으로 한다.

## Replay/Fork

두 목적이 있다.

1. recovery primitive 연구
2. same-prefix counterfactual evaluation

Lineage 최소 metadata:

```text
parent_thread
branch_point
snapshot
forked_rollout
experiment_id
control/guidance arm
```

## External side effects

Local Git snapshot으로는 다음을 되돌릴 수 없다.

```text
git push
cloud deploy
DB write
GitHub issue/PR create
arbitrary MCP external write
```

따라서 `RESTART` 전에 side-effect ledger가 필요하다.

```text
Effect
  kind
  target/resource
  result
  timestamp
  reversible?
  compensation/checkpoint if known
```

Spotter는 reasoning context를 restart해도 외부 state가 자동 복구됐다고 주장하면 안 된다.

---

# 14. Agent adapter contract

Codex는 첫 adapter다. Spotter core는 아래 capability 중심으로 본다.

```text
AgentAdapter
  connect()/disconnect()
  list_threads()
  observe_events()
  read_thread_state()

  optional:
    steer(turn, message)
    interrupt(turn)
    pre_action_veto(proposal)
    fork/resume primitives
```

각 adapter는 capability set을 노출한다.

```text
CodexAdapter
  observation: rich
  steer: yes (target PoC)
  interrupt: yes (target)
  pre-action veto: Hook

FutureAgentAdapter
  observation: maybe partial
  steer: maybe no
  veto: maybe no
```

Capability가 없는 agent에서는 기능을 숨기거나 degraded로 표시한다. Codex-specific method name이 Spotter policy layer에 직접 퍼지지 않게 한다.

---

# 15. P0 App Server lifecycle / attach PoC

이 architecture의 구현은 아래 PoC를 먼저 통과해야 한다.

## Path A — Codex managed daemon

```text
start/ensure Codex App Server daemon
      ↓
plain `codex`
      ↓
TUI attaches to default external daemon
      ↓
Spotter second-client attach
      ↓
observe same thread/turn
      ↓
turn/steer reaches TUI
```

## Path B — Spotter-managed process

```text
Spotter starts external `codex app-server`
      ↓
TUI + Spotter attach same endpoint
      ↓
normal plain-codex UX can be preserved?
```

## Path C — Embedded baseline

외부 attach가 불가능할 때 실제 가능한 capability를 측정한다.

```text
Observation: limited/unavailable
Live steer:  unavailable
Interrupt:   unavailable
PreToolUse:  possibly available
```

## Exit criteria

- ordinary `codex` UX 유지
- same App Server / same thread 증명
- live event subscription
- active turn identity
- real `turn/steer` delivery
- concurrent sessions 구분
- reconnect/degraded behavior 이해
- App Server ownership/lifecycle strategy 결정

**하나라도 핵심 전제가 성립하지 않으면 P1 daemon migration 전에 architecture를 다시 검토한다.**

---

# 16. Current prototype guarantees to preserve

Target으로 옮긴다고 기존 안전성을 버리지 않는다.

### Journal

- cross-process serialization
- monotonic step/proposal allocation
- fsync durability
- torn-tail recovery
- destructive reader strictness

### Snapshot

- user HEAD/index untouched
- detached restore
- Spotter-owned refs
- dedup / conservative prune

### Audit ledger

- Main summary를 evidence로 승격하지 않음
- observable outcome만 evidence
- contradictory outcome → retraction
- transitive stale propagation

### Gate

- shell-aware bounded parsing
- unsupported ambiguity는 fail-open
- blind spot을 FP와 별도로 측정

Hook corpus에서 audit ledger가 직접 outcome을 읽을 수 있던 tool result는 33/340(10%)이었다. 이 숫자는 **현재 Hook observation surface의 측정값**이며 App Server 전환 후 다시 측정한다.

---

# 17. Non-goals for the first migration

첫 architecture migration에서 하지 않는다.

- Spotter 전체 Rust/Go rewrite
- graph database 도입
- automatic compensating rollback
- adaptive/learned intervention policy
- 모든 agent 동시 지원
- RESTART 완성
- reviewer가 실제로 task outcome을 개선한다는 주장

첫 성공 기준은 더 작다.

> **같은 사용자 Codex thread를 안정적으로 관찰하고, live state를 유지하고, deterministic gate는 빠르게 처리하며, async reviewer verdict를 active turn에 안전하게 전달할 수 있는 runtime boundary를 만든다.**

구현 lifecycle은 [Lifecycle](lifecycle.md), 순서는 [Roadmap](roadmap.md), 현재 상태는 [Status](status.md)를 본다.

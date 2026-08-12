Maintained by [@bogyie / Bogyoeng Kim](https://github.com/Bogyie) and [@zerone / Youngjin Jung](https://github.com/YoungJinJung)

# Spotter

> **A runtime spotter for coding agents.**  
> Your coding agent drives. Spotter watches the trajectory, challenges bad assumptions, and steps in before wasted work compounds.

Spotter is an experimental **runtime supervision system for coding agents**, starting with Codex.

---

## 30초 요약

Spotter가 해결하려는 문제는 **“에이전트가 틀렸는가?”**보다 조금 더 구체적이다.

> **에이전트가 잘못된 가정·반복·범위 이탈을 계속 누적하기 전에, 실행 중간에 이를 감지하고 가장 약한 개입으로 경로를 되돌릴 수 있는가?**

현재 repository는 이미 단순 scaffold를 넘었다. Hook 기반 trajectory 수집, deterministic gate, snapshot/fork/replay, shadow reviewer, audit ledger, labels/metrics, counterfactual experiment harness가 구현돼 있다.

다만 **현재 shipping prototype**과 **목표 architecture**는 다르다.

```text
현재 prototype

Codex / Claude Code
       │ hooks
       ▼
 spotter-hook process
       │
       ├─ journal
       ├─ deterministic gate
       ├─ snapshot
       └─ periodic shadow review


목표 architecture

Codex TUI
    │
    ▼
External Codex App Server
    ↕ event stream / steer / interrupt
 spotterd
    │
    └─ PreToolUse Hook
       (deterministic synchronous gate만)
```

**가장 가까운 다음 단계는 App Server lifecycle/attach PoC다.** 사용자 TUI와 Spotter가 동일한 외부 App Server를 공유하고, Spotter가 실제 active turn에 `turn/steer`를 보낼 수 있는지 먼저 E2E로 검증한다. 이 전제가 실패하면 daemon migration을 밀어붙이지 않는다.

빠른 현재 상태는 [Status](docs/status.md), 상세 방향은 [#66](https://github.com/Bogyie/spotter/issues/66)을 본다.

---

## 빠른 탐색

| 알고 싶은 것 | 바로 보기 |
| --- | --- |
| 지금 실제로 되는 기능 | [현재 상태](#현재-상태) / [docs/status.md](docs/status.md) |
| Spotter가 정확히 무엇인가 | [핵심 개념](#핵심-개념) / [docs/concept.md](docs/concept.md) |
| 목표 process/data flow | [목표 architecture](#목표-architecture) / [docs/architecture.md](docs/architecture.md) |
| 설치→setup→세션→업데이트→제거 | [docs/lifecycle.md](docs/lifecycle.md) |
| 다음 구현 순서와 dependency | [docs/roadmap.md](docs/roadmap.md) |
| 근거 논문·연구 질문·미검증 가설 | [docs/research.md](docs/research.md) |
| umbrella direction issue | [#66](https://github.com/Bogyie/spotter/issues/66) |

---

## 현재 상태

### 기능 상태 한눈에 보기

| 기능 | 상태 | 설명 |
| --- | --- | --- |
| Hook trajectory collection | ✅ 구현 | `SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse` 기록 |
| Deterministic gate | ✅ 구현 | destructive command/path/dependency 등 rule-based 판단 |
| Crash-safe journal | ✅ 구현 | cross-process locking, fsync, torn-tail recovery |
| Git snapshot / restore | ✅ 구현 | user HEAD/index를 건드리지 않는 snapshot + detached restore |
| Fork / continuation replay | ✅ 구현 | 동일 prefix에서 Codex continuation 분기 |
| Shadow reviewer | ✅ 구현 | `CONTINUE / VERIFY / NUDGE` 판단 기록, live delivery는 없음 |
| Claim/evidence audit ledger | 🟡 부분 | observable outcome 범위에서 stale premise propagation |
| Label / metrics | ✅ 구현 | coverage-aware precision/FP 측정 |
| Counterfactual experiment harness | ✅ 구현 | control/guidance same-prefix pair 실행 기반 |
| Standalone `spotterd` runtime | ❌ 미구현 | #66 target |
| App Server primary observation | 🧪 PoC 필요 | 가장 먼저 검증할 architecture 전제 |
| Event-driven signal engine | ❌ 미구현 | 현재 reviewer는 periodic cadence 기반 |
| Live `VERIFY / NUDGE` | ❌ 미구현 | target: `turn/steer` |
| `INTERRUPT` | ❌ 미구현 | target: `turn/interrupt` |
| `RESTART` | ❌ 미구현 | verified state + recovery design 필요 |
| Homebrew/setup lifecycle | 🎯 target | `brew install spotter` / `spotter setup codex` |

> 구현 여부와 효과 입증은 다르다. 예를 들어 reviewer가 `NUDGE`를 생성할 수 있다는 사실은 NUDGE가 실제 task outcome을 개선한다는 증거가 아니다.

### 지금 가장 중요한 blocker

```text
P0 — Codex App Server lifecycle / attach PoC
```

검증할 질문:

1. 외부 App Server가 준비돼 있을 때 plain `codex`가 이를 안정적으로 재사용하는가?
2. Spotter가 second client로 붙어 같은 thread/turn event를 받는가?
3. active `turn_id`를 정확하게 추적할 수 있는가?
4. `turn/steer`가 실제 사용자가 보고 있는 Codex TUI turn에 전달되는가?
5. 여러 Codex session을 동시에 구분할 수 있는가?
6. attach/control이 불가능한 경우 `status/doctor`가 이를 명확한 degraded mode로 보여줄 수 있는가?

자세한 PoC와 exit criteria는 [Roadmap P0](docs/roadmap.md)을 본다.

---

## 핵심 개념

Coding agent는 보통 긴 실행 경로를 만든다.

```text
understand
   ↓
inspect
   ↓
hypothesize
   ↓
edit
   ↓
run
   ↓
observe
   ↓
revise
   ↓
validate
```

문제는 많은 실패가 한 번의 잘못된 출력이 아니라 **trajectory failure**라는 점이다.

예를 들어:

```text
잘못된 가정
"timeout 원인은 Redis pool이다"
        │
        ▼
관련 파일만 계속 탐색
        │
        ▼
Redis 설정 수정
        │
        ▼
테스트 실패
        │
        ▼
실패를 보정하기 위한 추가 수정
        │
        ▼
scope 확대 + 시간/토큰 낭비
```

최종 diff review는 마지막에 문제를 발견할 수 있지만, 이미 대부분의 비용을 지불한 뒤다.

Spotter는 다음 지점을 노린다.

```text
잘못된 가정
    │
    ├── evidence 부족 감지
    │       ↓
    │    VERIFY
    │       ↓
    │  stack trace 확인
    │
    └── 잘못된 edit가 누적되기 전에 경로 수정
```

즉 Spotter의 목적은 **오류 개수를 최대한 많이 찾는 것**이 아니라 **잘못된 진행이 비싸지기 전에 잡는 것**이다.

---

## 개입 단계

Spotter는 가장 약한 개입부터 사용한다.

| Action | 의미 | 현재 상태 | 목표 runtime primitive |
| --- | --- | --- | --- |
| `CONTINUE` | 문제 없음 또는 개입 가치 낮음 | ✅ shadow reviewer | no-op |
| `VERIFY` | 중요한 가정에 근거가 부족함 | ✅ 판단만 구현 | async `turn/steer` |
| `NUDGE` | trajectory가 이탈/낭비 쪽으로 기울고 있음 | ✅ 판단만 구현 | async `turn/steer` |
| `BLOCK` | 명확한 deterministic constraint 위반 | ✅ gate 구현 | synchronous `PreToolUse` deny |
| `INTERRUPT` | 현재 turn을 계속하면 손실이 누적될 가능성이 큼 | ❌ | `turn/interrupt` |
| `RESTART` | reasoning context 자체를 신뢰하기 어려움 | ❌ | verified state 기반 새 continuation |

중요한 원칙:

> **Semantic reviewer가 “싫다”고 해서 BLOCK하지 않는다.**  
> BLOCK은 가능한 한 deterministic하고 audit 가능한 규칙에만 사용한다.

---

## 목표 architecture

### 역할 분리

```text
Observation plane
  무엇이 일어나고 있는가?
  → Codex App Server event stream

Control plane
  이미 실행 중인 trajectory에 어떻게 개입하는가?
  → turn/steer, turn/interrupt

Enforcement plane
  실행 전에 반드시 결정해야 하는가?
  → PreToolUse deterministic gate

Supervision runtime
  상태·신호·reviewer·policy를 누가 소유하는가?
  → spotterd
```

### 목표 data flow

```text
                     ┌─────────────┐
                     │  Codex TUI  │
                     └──────┬──────┘
                            │
                            ▼
                 ┌──────────────────────┐
                 │ External App Server  │
                 └──────┬────────▲──────┘
                        │        │
                events  │        │ steer / interrupt
                        ▼        │
                 ┌──────────────────┐
                 │     spotterd     │
                 │                  │
                 │ SessionManager   │
                 │ Live State       │
                 │ Trace IR         │
                 │ Audit State      │
                 │ Signal Engine    │
                 │ Reviewer         │
                 │ Intervention     │
                 │ Journal          │
                 └────────┬─────────┘
                          │
                 deterministic gate
                          │
                          ▼
                   PreToolUse Hook
```

### 상태 ownership

```text
memory  = 현재 살아 있는 thread/turn의 live supervision state
journal = durable event history + crash/restart/resume recovery source
```

현재 prototype처럼 매 Hook마다 journal을 다시 읽어 live state를 복원하는 구조를 steady state로 유지하지 않는다.

---

## 정상 실행은 어떻게 보일까

목표 runtime에서 평상시 event는 다음처럼 처리한다.

```text
App Server event
      │
      ▼
spotterd normalize
      │
      ├─ live state update
      ├─ journal append
      └─ cheap signal evaluation
                  │
             suspicious?
              ├─ no → 끝
              └─ yes
                    │
                    ▼
             async reviewer job
                    │
                    ├─ CONTINUE → no-op
                    ├─ VERIFY   → active turn이면 steer
                    ├─ NUDGE    → active turn이면 steer
                    └─ strong action 후보 → 별도 policy
```

Main agent는 reviewer가 생각하는 동안 계속 실행한다.

Reviewer verdict가 늦게 도착하면 대상 turn이 아직 active인지 확인한다.

```text
reviewer verdict ready
        │
        ▼
target turn still active?
   ├─ yes → deliver
   └─ no  → stale / discard / explicit defer policy
```

뒤늦은 NUDGE를 unrelated later turn에 무조건 넣지 않는다.

---

## 왜 Hook은 하나만 남기려 하나

현재 prototype은 네 Hook을 사용한다.

| Hook | 현재 역할 | 목표 |
| --- | --- | --- |
| `SessionStart` | session 감지 | App Server lifecycle로 대체 후 제거 후보 |
| `UserPromptSubmit` | user goal 기록 | App Server user-message event로 대체 후 제거 후보 |
| `PreToolUse` | proposal + deterministic gate | **atomic gate 때문에 유지** |
| `PostToolUse` | result/snapshot/review trigger | App Server result/diff event로 대체 후 제거 1순위 |

관찰과 semantic review는 App Server/daemon 쪽으로 옮긴다.

`PreToolUse`는 다음과 같은 실행 전 guarantee 때문에 남긴다.

```text
git reset --hard
        │
        ▼
PreToolUse
        │
        ▼
spotterd Gate Engine
   ├─ ALLOW
   └─ DENY  ← command가 실행되기 전에 확정
```

향후 App Server가 모든 tool call에 대해 신뢰할 수 있는 atomic veto primitive를 제공한다면 Hook 0개도 검토할 수 있다.

---

## 현재 prototype 사용

### Source install

Python 3.11+:

```bash
git clone https://github.com/bogyie/spotter.git
cd spotter
python -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

Development extras:

```bash
python -m pip install -e '.[dev]'
```

Config:

```bash
cp spotter.example.toml spotter.toml
spotter --config spotter.toml
```

기본 예시는 passive observation을 사용한다.

```toml
observation_only = true

[main_agent]
adapter = "codex"

[reviewer]
model = "default"
```

### Current plugin compatibility path

Codex:

```bash
codex plugin marketplace add bogyie/spotter
codex plugin add spotter@spotter
```

Claude Code:

```bash
claude plugin marketplace add bogyie/spotter
claude plugin install spotter@spotter
```

이 방식은 **현재 prototype을 사용할 수 있는 경로**이고, target product boundary는 standalone runtime이다.

---

## 현재 세션 분석/실험

세션 요약:

```bash
spotter analyze
```

Shadow reviewer:

```bash
spotter review --session <id>
```

Human label:

```bash
spotter label \
  --session <id> \
  --step 7 \
  --verdict fp \
  --note "quoted text, not executed"
```

Metrics:

```bash
spotter metrics
```

Fork:

```bash
spotter fork --session <id> --step <k>
```

Counterfactual experiment:

```bash
spotter experiment \
  --session <id> \
  --step <k> \
  --guidance "Verify the timeout source before editing Redis settings." \
  --check "pytest tests/test_timeout.py"
```

Experiment machinery가 존재한다고 해서 intervention advantage가 이미 입증된 것은 아니다. 현재 가장 큰 evaluation gap 중 하나는 mechanically scorable ground-truth task set과 실제 실행 데이터다.

---

## 목표 설치/운영 UX

목표는 다음 세 줄이다.

```bash
brew install spotter
spotter setup codex
spotter doctor
```

그 이후에는:

```bash
codex
```

만 실행하면 된다.

사용자가 매번 직접 다음을 실행하게 하지 않는다.

```bash
spotter daemon start
codex app-server daemon start
```

Target control plane:

```text
spotter setup codex|claude|--all
spotter teardown codex|claude
spotter doctor
spotter status
spotter daemon start|stop|restart|status|logs
spotter sessions
spotter analyze
spotter review
spotter label
spotter metrics
spotter fork
spotter experiment
spotter prune
spotter config
spotter version
```

설치부터 update/uninstall/purge/reinstall까지의 정확한 ownership과 failure handling은 [Lifecycle](docs/lifecycle.md)에 정리한다.

---

## 다음 구현 순서

```text
P0  App Server lifecycle / attach PoC
 ↓
P1  spotterd + App Server client + IPC + thread/turn identity
 ↓
P2  package/setup/status/doctor/teardown lifecycle
 ↓
P3  기존 journal/gate/audit/reviewer/snapshot 기능을 runtime 뒤로 이동
 ↓
P4  App Server primary observation + Hook 최소화
 ↓
P5  cheap signal → event-driven reviewer
 ↓
P6  live VERIFY / NUDGE via turn/steer
 ↓
P7  INTERRUPT / RESTART / side-effect-aware recovery
 ↓
P8  upgrade/migration/retention/purge/multi-agent hardening
```

각 phase의 구체적인 deliverable, dependency, exit criteria는 [Roadmap](docs/roadmap.md)을 본다.

---

## 문서

- **[Status](docs/status.md)** — 현재 구현/미구현, blocker, 다음 단계 대시보드
- **[Concept](docs/concept.md)** — Spotter가 무엇이고 왜 필요한지
- **[Architecture](docs/architecture.md)** — process, state, event, control, failure contract
- **[Lifecycle](docs/lifecycle.md)** — install → setup → runtime → update → teardown → purge
- **[Roadmap](docs/roadmap.md)** — dependency 기반 구현 순서와 exit criteria
- **[Research](docs/research.md)** — prior work, borrowed ideas, 미검증 가설, evaluation questions
- **[#66](https://github.com/Bogyie/spotter/issues/66)** — standalone runtime 전환 umbrella issue

---

## 개발

```bash
ruff format --check .
ruff check .
mypy src tests
pytest
```

Architecture migration에서 기존 prototype의 유용한 경계를 유지해야 한다.

```text
adapter-specific runtime events
        ↓
normalized Trace IR
        ↓
Spotter core policy / state / evaluation
```

Codex App Server event shape가 Spotter core 전체로 새어 들어가게 만들지 않는다.

---

## Trajectory Engineering

Spotter는 **Trajectory Engineering**이라는 더 넓은 문제를 탐구하는 프로젝트이기도 하다.

- Prompt engineering — 무엇을 지시하는가
- Context engineering — 무엇을 보게 하는가
- Harness engineering — 어떤 도구와 환경에서 실행하는가
- **Trajectory engineering — 실행 중인 경로를 어떻게 관찰·검증·조정·중단·복구하는가**

Spotter가 검증하려는 가설은 다음과 같다.

> **더 좋은 agent는 처음부터 실수하지 않는 agent만을 의미하지 않는다. 실수가 비싸지기 전에 발견되고 복구 가능한 agent도 더 좋은 agent다.**

이 가설은 아직 Spotter에 대해 입증되지 않았다. Null/negative result도 그대로 기록하는 것이 프로젝트 원칙이다.

## License

MIT

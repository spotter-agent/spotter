# Spotter Status

> **목적:** 이 문서는 Spotter의 **현재 구현 상태, 목표 구조, 가장 가까운 다음 단계**를 빠르게 확인하기 위한 대시보드다.  
> 방향성의 기준은 [#66](https://github.com/Bogyie/spotter/issues/66), 구현 순서는 [Roadmap](roadmap.md), 상세 구조는 [Architecture](architecture.md)를 따른다.

## 30초 요약

Spotter는 현재 **Hook 기반 research prototype**이다. 이미 trajectory journal, deterministic gate, snapshot/fork/replay, shadow reviewer, audit ledger, label/metrics, counterfactual experiment harness까지 구현돼 있다.

하지만 Spotter가 최종적으로 지향하는 형태는 다르다.

```text
현재
Codex hooks
   ↓
각 hook마다 Spotter process 실행
   ↓
journal / gate / snapshot / shadow review

목표
Codex TUI
   ↓
External Codex App Server
   ↕ events / steer / interrupt
spotterd
   ↓
PreToolUse Hook (deterministic gate만)
```

가장 먼저 검증해야 할 것은 **사용자가 평소처럼 `codex`를 실행하면서도 Codex TUI와 Spotter가 동일한 외부 App Server를 공유할 수 있는가**다. 이 PoC가 실패하면 target architecture를 다시 검토한다.

---

## 지금 되는 것 / 아직 안 되는 것

| 영역 | 상태 | 지금 가능한 것 | 다음 단계 |
| --- | --- | --- | --- |
| Hook ingestion | ✅ 구현 | `SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse` 수집 | App Server로 observation 이동 후 Hook 최소화 |
| Deterministic gate | ✅ 구현 | destructive command/path/dependency 등 shadow/active 판정 | daemon IPC 뒤로 이동, latency 재측정 |
| Journal | ✅ 구현 | crash-tolerant JSONL, cross-process append | live state의 hot store가 아니라 durable log로 역할 변경 |
| Snapshot | ✅ 구현 | Git-backed snapshot, dedup, prune | runtime lifecycle/retention에 통합 |
| Fork / replay | ✅ 구현 | 동일 prefix에서 Codex continuation fork | fidelity/noise floor 측정 |
| Shadow reviewer | ✅ 구현 | `CONTINUE/VERIFY/NUDGE` 판단 기록 | event-driven dispatch + live delivery |
| Audit ledger | ✅ 부분 구현 | observable outcome 기반 claim/evidence, stale propagation | App Server observability로 범위 확대 |
| Label / metrics | ✅ 구현 | gate/reviewer precision 및 coverage | miss rate, timing, cost, harm/recovery 확장 |
| Counterfactual experiment | ✅ 구현 | control/guidance pair 생성·실행 가능 | ground-truth task set으로 실제 실험 |
| Standalone runtime | ❌ 미구현 | 없음 | `spotterd`, service, IPC |
| App Server observation | ❌ 미구현 | 없음 | 최우선 lifecycle/attach PoC |
| Event-driven signal engine | ❌ 미구현 | periodic reviewer만 존재 | cheap signal → reviewer |
| Live VERIFY / NUDGE | ❌ 미구현 | verdict는 journal에만 기록 | `turn/steer` delivery |
| INTERRUPT | ❌ 미구현 | 없음 | `turn/interrupt` |
| RESTART | ❌ 미구현 | fork 실험 primitive만 존재 | verified state + side-effect ledger 기반 recovery |
| Homebrew/setup lifecycle | ❌ 목표 | 현재 source/plugin 설치 | `brew install spotter`, `spotter setup codex` |

---

## 현재 사용자 경로

현재 prototype은 source 또는 plugin 방식으로 사용한다.

```bash
git clone https://github.com/Bogyie/spotter.git
cd spotter
python -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

또는 현재 plugin compatibility path:

```bash
codex plugin marketplace add bogyie/spotter
codex plugin add spotter@spotter
```

현재 세션을 확인·평가하는 명령:

```bash
spotter analyze
spotter review --session <id>
spotter label --session <id> --step <n> --verdict fp
spotter metrics
spotter fork --session <id> --step <n>
spotter experiment --session <id> --step <n> --guidance "..." --check "..."
```

---

## 목표 사용자 경로

구현 후 목표 UX:

```bash
brew install spotter
spotter setup codex
spotter doctor

# 이후 평소처럼
codex
```

운영/디버깅용:

```bash
spotter status
spotter sessions
spotter daemon status
spotter daemon restart
spotter doctor
spotter teardown codex
```

사용자가 매번 아래를 직접 실행하게 만들지 않는다.

```bash
spotter daemon start
codex app-server daemon start
```

---

## 가장 중요한 현재 blocker

### P0 — Codex App Server lifecycle / attach PoC

확인할 질문은 단순하다.

> **plain `codex`를 실행했을 때, 사용자 TUI와 Spotter가 동일한 외부 App Server를 공유하고 Spotter가 그 active turn에 `turn/steer`를 보낼 수 있는가?**

검증할 세 경로:

1. Codex managed App Server daemon을 미리 띄우고 plain `codex`가 자동 attach하는 경로
2. Spotter가 외부 `codex app-server` lifecycle을 관리하는 경로
3. 외부 App Server를 확보하지 못했을 때 embedded/degraded mode의 실제 capability

PoC exit criteria:

- TUI와 Spotter가 같은 thread/turn을 본다.
- Spotter가 App Server event stream을 실시간 수신한다.
- active `turn_id`를 신뢰성 있게 추적한다.
- `turn/steer`가 실제 사용자 세션에 전달된다.
- concurrent Codex session을 구분한다.
- 실패 시 `doctor/status`에서 degraded 상태가 명확하게 보인다.

이 조건을 만족하지 못하면 P1 daemon migration으로 넘어가지 않는다.

---

## 구현 순서

```text
P0  App Server lifecycle / attach PoC
 ↓
P1  spotterd + App Server client + IPC + session identity
 ↓
P2  install/setup/doctor/status/teardown/package lifecycle
 ↓
P3  기존 journal/gate/audit/reviewer/snapshot 기능을 runtime 뒤로 이관
 ↓
P4  App Server를 primary observation으로 전환, Hook 최소화
 ↓
P5  cheap signal → event-driven reviewer
 ↓
P6  live VERIFY / NUDGE via turn/steer
 ↓
P7  INTERRUPT / RESTART / side-effect-aware recovery
 ↓
P8  upgrade/migration/retention/purge/multi-agent hardening
 ↓
P9  experience / adaptation
```

자세한 dependency와 exit criteria는 [Roadmap](roadmap.md)을 본다.

---

## 문서 빠른 탐색

| 알고 싶은 것 | 문서 |
| --- | --- |
| Spotter가 무엇이고 왜 필요한가 | [Concept](concept.md) |
| 지금 무엇이 구현됐나 | **이 문서** |
| runtime process/data flow가 어떻게 생기나 | [Architecture](architecture.md) |
| 설치→setup→사용→업데이트→제거가 어떻게 이어지나 | [Lifecycle](lifecycle.md) |
| 무엇부터 구현할 것인가 | [Roadmap](roadmap.md) |
| 어떤 연구를 근거로 삼고 무엇이 아직 미검증인가 | [Research](research.md) |
| 방향성 umbrella issue | [#66](https://github.com/Bogyie/spotter/issues/66) |

---

## 상태 표기 규칙

문서에서는 아래 의미를 구분한다.

- ✅ **Implemented** — 코드가 존재하고 최소한 테스트/실사용 증거가 있다.
- 🟡 **Partial / shadow** — 동작 일부가 구현됐거나 의도적으로 observe-only다.
- 🧪 **PoC required** — architecture 전제로 사용하기 전에 E2E 검증이 필요하다.
- 🎯 **Target** — 합의된 목표 구조지만 아직 구현되지 않았다.
- ❌ **Not implemented** — 코드 경로가 없다.

구현 완료와 효과 입증도 구분한다. 예를 들어 reviewer가 `NUDGE`를 생성할 수 있다는 사실은 **NUDGE가 실제로 task outcome을 개선한다는 증거가 아니다.**
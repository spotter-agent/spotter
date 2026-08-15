<div align="center">

<h1>Spotter 설치 및 사용 상세 가이드</h1>

<p>
  <a href="user-guide.md">English</a> ·
  <strong>한국어</strong> ·
  <a href="user-guide.zh-CN.md">简体中文</a>
</p>

<p>
  고급 사용자를 위한 설치, 런타임 운영, 설정, 업그레이드, 안전한 제거,<br />
  문제 해결 및 이슈 리포팅 가이드입니다.
</p>

<p><a href="../README.ko.md">← README로 돌아가기</a></p>

</div>

---

> [!IMPORTANT]
> Spotter는 활발히 개발 중입니다. 결정론적 Hook 게이트는 현재 동작하지만 시맨틱 `VERIFY`와
> `NUDGE` 결정은 실시간 턴에 전달되지 않고 섀도 모드로 기록만 됩니다. App Server 관찰 및
> 제어에는 명시적인 설정이 필요합니다. 정확한 현재 경계는
> [Status(영문)](status.md)를 참고하세요.

<table>
  <tr>
    <td width="50%" valign="top">
      <a href="#3-homebrew로-설치"><strong>📦 Homebrew로 설치</strong></a><br />
      <sub>지원되는 공식 패키지 설치 경로를 사용합니다.</sub>
    </td>
    <td width="50%" valign="top">
      <a href="#4-소스에서-수동-설치"><strong>🛠️ 수동 설치</strong></a><br />
      <sub>안정적인 Python 환경에서 Spotter를 실행합니다.</sub>
    </td>
  </tr>
  <tr>
    <td width="50%" valign="top">
      <a href="#5-spotter를-codex에-연결"><strong>🔌 Codex 연결</strong></a><br />
      <sub>관리형 통합을 미리 보고 적용한 뒤 검증합니다.</sub>
    </td>
    <td width="50%" valign="top">
      <a href="#9-연결-해제-및-제거"><strong>🧹 안전하게 제거</strong></a><br />
      <sub>통합 해제, 패키지 제거, 사용자 데이터를 구분합니다.</sub>
    </td>
  </tr>
  <tr>
    <td width="50%" valign="top">
      <a href="#10-문제-해결"><strong>🩺 문제 해결</strong></a><br />
      <sub>PATH, 데몬, 설정, 관찰 문제를 진단합니다.</sub>
    </td>
    <td width="50%" valign="top">
      <a href="#11-이슈-리포팅"><strong>📝 이슈 리포팅</strong></a><br />
      <sub>민감정보 없이 유용한 진단 정보를 수집합니다.</sub>
    </td>
  </tr>
</table>

## 1. 설치 방법 선택

| 방법 | 적합한 용도 | 유지관리 방식 |
| --- | --- | --- |
| Homebrew(권장) | 일반적인 macOS 및 Linux 사용 | Homebrew가 패키지를 소유하고 Spotter는 Codex 통합만 소유 |
| 전용 Python 환경 | 소스 평가 및 고급/수동 설치 | 사용자가 Python, 가상환경, 소스 업데이트, 실행 파일 안정성을 관리 |
| 편집 가능한 개발 설치 | Spotter 자체를 수정하는 기여자 | 수동 설치 관리에 개발 의존성과 저장소 검사 추가 |

활성화할 `spotter`와 `spotterd` 조합을 의도적으로 관리하는 경우가 아니라면 Homebrew 설치와
수동 설치를 같은 `PATH`에 두지 마세요. 설정은 안정적인 실행 파일 경로를 기록하며, 서로 다른
설치를 섞으면 CLI와 데몬의 빌드가 불일치하기 쉽습니다.

## 2. 사전 요구사항

모든 설치에는 다음이 필요합니다.

- 통합 설정 전에 설치되어 있고 `PATH`에서 실행 가능한 `codex` CLI
- 홈 디렉터리 아래에 파일을 생성할 수 있는 사용자 계정
- 스냅샷, 포크, 리플레이 또는 소스 설치를 사용할 때 Git

수동 설치에는 Python 3.11 이상도 필요합니다. 계속하기 전에 관련 도구를 확인하세요.

```bash
codex --version
git --version
python3 --version
```

Spotter 설정을 `sudo`로 실행하지 마세요. 통합, 백그라운드 서비스, 런타임 소켓, 데이터는 현재
로그인한 사용자 계정의 소유여야 합니다.

## 3. Homebrew로 설치

공식 tap에서 설치합니다.

```bash
brew install spotter-agent/spotter/spotter
```

CLI와 데몬이 같은 패키지 경계에서 제공되는지 확인합니다.

```bash
command -v spotter
command -v spotterd
spotter --version
spotterd --version
```

패키지 설치는 Codex 설정을 수정하거나 Hook을 등록하거나 서비스를 시작하지 않습니다. 이러한
변경은 명시적인 설정 단계에서만 수행됩니다.

## 4. 소스에서 수동 설치

경로가 바뀌지 않을 전용 가상환경을 사용하세요. 다음 예시는 소스와 설치된 실행 파일을 분리합니다.

```bash
mkdir -p ~/.local/src ~/.local/share/spotter
git clone https://github.com/spotter-agent/spotter.git ~/.local/src/spotter
python3 -m venv ~/.local/share/spotter/venv
~/.local/share/spotter/venv/bin/python -m pip install --upgrade pip
~/.local/share/spotter/venv/bin/python -m pip install ~/.local/src/spotter
```

가상환경의 `bin` 디렉터리를 셸 `PATH`에 추가하거나 실행 파일을 절대 경로로 호출하세요. 설정 전에
두 명령이 같은 위치에 나란히 설치되었는지 확인합니다.

```bash
~/.local/share/spotter/venv/bin/spotter --version
~/.local/share/spotter/venv/bin/spotterd --version
```

가상환경이 아직 `PATH`에 없다면 이후 예제를 실행하기 전에 활성화합니다.

```bash
source ~/.local/share/spotter/venv/bin/activate
```

Spotter는 발견한 CLI와 데몬 경로를 통합 설정에 영구 기록합니다. 설정 후에는 가상환경을 이동하거나
삭제하지 마세요. 먼저 통합을 해제하거나 같은 안정적인 경로에 환경을 다시 만든 뒤 설정을 다시
실행해야 합니다.

기여자를 위한 편집 가능한 설치는 저장소 로컬 워크플로를 사용합니다.

```bash
cd ~/.local/src/spotter
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
```

프로젝트를 수정하기 전에 [Contributing](../CONTRIBUTING.md#local-setup)을 참고하세요.

## 5. Spotter를 Codex에 연결

항상 먼저 변경 계획을 확인합니다.

```bash
spotter setup codex --dry-run
```

그다음 관리형 통합을 적용하고 전체 경로를 검증합니다.

```bash
spotter setup codex
spotter doctor
```

관리형 설정은 트랜잭션 방식이며 여러 번 실행해도 같은 결과를 보장합니다. Spotter가 소유한 Codex
Hook/플러그인 상태만 변경하고, fingerprint가 있는 백업을 보관하며, `spotterd`를 로그인 범위의
사용자 서비스로 등록합니다. 또한 합성 Hook 왕복을 검증하고 기본적으로
`~/.spotter/integrations/codex.json`에 소유권 매니페스트를 커밋합니다.

영구 사용자 서비스 등록을 사용할 수 없거나 원하지 않는다면 portable 모드를 사용합니다.

```bash
spotter setup codex --portable
spotter doctor
```

Portable 모드는 로그인 시 자동으로 시작되는 서비스를 등록하지 않고 `spotterd`를 시작합니다.
로그아웃, 재부팅 또는 프로세스 종료 후에는 직접 다시 시작해야 합니다.

```bash
spotter daemon start
```

설정이 끝나면 평소처럼 Codex를 사용합니다.

```bash
codex
```

## 6. 설정

설정 파일은 선택 사항입니다. Spotter는 기본적으로 `~/.spotter/spotter.toml`을 찾습니다.
`SPOTTER_HOME`을 설정하면 Spotter의 설정, 데이터, 통합, 런타임, 로그 루트가 함께 이동합니다.
설정과 진단 시 `--config`로 파일을 명시할 수도 있습니다.

보수적인 시작 설정은 다음과 같습니다.

```toml
observation_only = true
snapshot_on_patch = true

[main_agent]
adapter = "codex"

[reviewer]
model = "default"
# on_signals = true
# every_steps = 25
max_per_session = 20
max_per_day = 100

[gates]
forbidden_paths = []
block_dependency_changes = false
```

명시적인 설정 파일을 검증하고 등록하려면 다음을 실행합니다.

```bash
spotter doctor --config /absolute/path/to/spotter.toml
spotter setup codex --config /absolute/path/to/spotter.toml
```

신호 기반 및 주기적 시맨틱 리뷰는 모델 토큰을 사용하며 기본적으로 비활성화되어 있습니다. 필요한
경우에만 활성화하고 세션별·일별 한도를 유지하세요. 현재 리뷰 결정은 기록만 된다는 점도 기억해야
합니다.

## 7. Spotter 운영 및 점검

### 상태와 런타임

```bash
spotter status
spotter doctor
spotter daemon status
```

`spotter status`는 빠르고 비침습적인 요약을 제공하고, `spotter doctor`는 더 깊은 합성 검사를
수행합니다. 종료 코드는 다음과 같습니다.

| 종료 코드 | 의미 |
| --- | --- |
| `0` | 정상 |
| `1` | 경고가 있지만 감독이 동작하거나, 설정된 기능 일부가 저하됨 |
| `2` | 필수 통합, 데몬, 저장소 또는 원장 계약이 깨짐 |

설정 후 App Server가 구성되지 않았다는 관찰/실시간 제어 경고가 표시될 수 있습니다. 이는 현재
알려진 경계입니다. 결정론적 PreToolUse Hook 적용은 독립적으로 유지되지만, 완전한 App Server
관찰과 실시간 제어는 사용할 수 없습니다.

수동 데몬 명령은 `spotterd`만 제어하며 공유 Codex App Server를 중지하거나 초기화하지 않습니다.

```bash
spotter daemon start
spotter daemon restart
spotter daemon stop
```

### 수집 데이터와 커버리지

```bash
spotter metrics
spotter observability
spotter analyze
```

`analyze`, `metrics`, `observability`에 `--session <id>`를 사용하면 하나의 기록된 세션으로 출력을
제한할 수 있습니다. `metrics`는 Main, semantic reviewer, deterministic runtime, control lifecycle,
experiment 비용을 분리하고 누락된 관측의 커버리지를 표시합니다. `analyze`는 세션별 intervention과
비용을 함께 보여 주며, task arm 또는 fork prefix의 durable provenance가 해당 세션을 명시할 때만
mechanical outcome을 연결합니다. 파일명이나 timestamp로 outcome을 추정하지 않습니다. Reviewer token
합계가 불완전하면 session/call denominator와 함께 `partial` 또는 `unavailable`로 표시합니다.
레이블된 reviewer 결과는 `signal`, `periodic`, `manual` 실행 출처별로도 분리되어 A/B cohort가
자동으로 섞이지 않습니다. 저널과
진단에는 저장소 경로, 프롬프트, 도구 payload 등 작업 맥락이 포함될 수 있으므로 민감한 데이터로
취급하세요.

탐지 지연 연구에서는 회고적 semantic window와 Spotter가 필요한 증거를 실제로 관측할 수 있었던
observable window를 함께 기록합니다. 각 anchor는 stable Trace IR event ID가 있는 journal step이어야
합니다.

```bash
spotter label-opportunity --session <id> --opportunity-id <failure-id> \
  --semantic-earliest <step> --semantic-latest <step> \
  --observable-earliest <step> --observable-latest <step> \
  --required-evidence <step> --note "개입이 필요했던 근거"
```

필요하면 `--required-evidence`를 반복하고, 독립 이중 라벨링에는 `--rater`를 사용하세요.

### 저장소 관리

스냅샷 정리는 기본적으로 dry-run입니다. 관련 저장소 안에서 실행하거나 저장소 경로를 지정합니다.

```bash
spotter prune --repo /path/to/repository --forks
spotter prune --repo /path/to/repository --forks --apply
```

저널 만료에는 기간을 명시해야 하며, 오래된 세션을 포크하는 데 필요한 스냅샷도 제거될 수 있습니다.

```bash
spotter prune --repo /path/to/repository --journals --max-age-days 30
spotter prune --repo /path/to/repository --journals --max-age-days 30 --apply
```

`--apply`를 추가하기 전에 dry-run 출력을 읽으세요. Spotter는 소유한 ref와 worktree를 Git을
인식하는 방식으로 정리하므로, 이를 원시 재귀 삭제로 대체하지 마세요.

## 8. 업그레이드 및 재설치

Homebrew 설치는 다음과 같이 업그레이드합니다.

```bash
brew upgrade spotter-agent/spotter/spotter
spotter setup codex
spotter doctor
```

설정을 다시 실행하면 설치된 CLI, 실행 중인 데몬, 생성된 Hook 경로, 빌드 식별자, 통합 세대가
조정됩니다.

전용 소스 환경은 다음과 같이 업그레이드합니다.

```bash
git -C ~/.local/src/spotter pull --ff-only
~/.local/share/spotter/venv/bin/python -m pip install --upgrade ~/.local/src/spotter
source ~/.local/share/spotter/venv/bin/activate
spotter setup codex
spotter doctor
```

업그레이드가 중단되었다면 생성된 Hook을 먼저 직접 수정하지 마세요. `spotter status`를 실행한 뒤
setup과 doctor를 다시 실행하세요. 설정은 보존된 소유권 상태를 사용해 Hook 중복 없이 조정하도록
설계되어 있습니다.

## 9. 연결 해제 및 제거

Spotter와 데이터를 유지하면서 Codex 통합만 제거하려면 다음을 실행합니다.

```bash
spotter teardown codex
```

Homebrew에서 깨끗하게 제거하려면 다음을 실행합니다.

```bash
spotter teardown codex
brew uninstall spotter-agent/spotter/spotter
```

수동 Python 설치는 명령이 아직 존재할 때 통합을 해제한 다음 같은 환경에서 배포 패키지를
제거합니다.

```bash
spotter teardown codex
spotter daemon stop
~/.local/share/spotter/venv/bin/python -m pip uninstall spotter-agent
```

해당 경로가 Spotter 전용임을 확인한 뒤 일반 파일 관리 도구로 가상환경과 소스 체크아웃을 제거할
수 있습니다.

일반적인 제거는 `~/.spotter` 또는 `SPOTTER_HOME`의 사용자 데이터를 삭제하지 않습니다.
저장소를 인식하는 `spotter purge` 명령은 **아직 구현되지 않았습니다**. Spotter 소유 Git ref와
분리된 worktree가 저장소 안에 존재할 수도 있으므로 홈 디렉터리만 지워서는 완전히 purge되지
않습니다. 지원되는 Git 인식 정리에는 `spotter prune`을 사용하고, 확실하지 않은 데이터는
보존하며, purge 지원은 [#89](https://github.com/spotter-agent/spotter/issues/89)를 확인하세요.

통합 해제 전에 패키지를 제거했더라도 생성된 Hook은 fail-open으로 동작하도록 설계되어 있습니다.
같은 방법으로 패키지를 다시 설치하고 `spotter teardown codex`를 실행한 뒤 다시 제거하면 기록된
소유 통합을 안전하게 정리할 수 있습니다.

## 10. 문제 해결

<details>
<summary><strong><code>spotter</code> 또는 <code>spotterd</code>를 찾을 수 없음</strong></summary>

```bash
command -v spotter
command -v spotterd
```

Homebrew에서는 Formula가 설치되어 있고 Homebrew의 `bin` 디렉터리가 `PATH`에 있는지 확인합니다.
수동 설치에서는 사용할 가상환경을 활성화합니다. 두 명령이 서로 다른 설치 루트를 가리키면
`PATH`를 수정한 뒤 `spotter setup codex`와 `spotter doctor`를 다시 실행하세요.

</details>

<details>
<summary><strong>설정에서 Codex를 찾을 수 없음</strong></summary>

```bash
command -v codex
codex --version
spotter setup codex --dry-run
```

설정을 실행하는 것과 같은 로그인 환경의 `PATH`에 Codex를 설치하거나 노출하세요. 사용자 지정
`CODEX_HOME`을 사용한다면 setup, doctor, 일반 Codex 세션에서 같은 값을 유지해야 합니다.

</details>

<details>
<summary><strong>데몬을 사용할 수 없거나 빌드가 일치하지 않음</strong></summary>

```bash
spotter daemon status
spotter daemon restart
spotter setup codex
spotter doctor
```

업그레이드 후에는 setup을 다시 실행하는 것이 지원되는 조정 절차입니다. Doctor 출력에서 등록된
서비스 자체가 문제임을 확인하기 전에는 `launchd` 또는 `systemd --user` 정의를 직접 수정하지
마세요.

</details>

<details>
<summary><strong>Doctor가 관찰 또는 실시간 제어를 사용할 수 없다고 보고함</strong></summary>

현재 App Server 엔드포인트는 자동 선택되지 않습니다. Hook 적용이 가능하다고 표시되면 결정론적
게이트는 여전히 동작하지만 App Server 관찰과 실시간 `VERIFY`/`NUDGE`는 동작하지 않습니다.
이 알려진 제품 경계를 설치 문제로 판단하기 전에 [Status(영문)](status.md)를 확인하세요.

</details>

<details>
<summary><strong>설정 파일을 파싱하지 못하거나 설정이 무시되는 것처럼 보임</strong></summary>

```bash
spotter doctor --config /absolute/path/to/spotter.toml
spotter setup codex --config /absolute/path/to/spotter.toml --dry-run
```

`[main_agent]`가 존재하고 `adapter = "codex"`가 비어 있지 않은 문자열인지 확인합니다. Setup은
선택한 설정 경로를 통합 매니페스트에 저장합니다. 기록된 설정을 사용할 수 없게 되면 Hook은
fail-open 경계 안에서 안전한 기본값을 사용하고 문제를 stderr에 출력합니다.

</details>

<details>
<summary><strong>세션 또는 최근 관찰이 나타나지 않음</strong></summary>

설정 후 일반 Codex 세션을 실행한 다음 확인합니다.

```bash
spotter status
spotter doctor
spotter observability
```

Hook 소유권 결과와 마지막 관찰 시간, Codex 세션이 setup에서 확인한 것과 같은 `CODEX_HOME`을
사용하는지 확인하세요.

</details>

<details>
<summary><strong>저장소 또는 권한 검사 실패</strong></summary>

기본적으로 변경 가능한 상태는 `~/.spotter` 아래에 있으며 `logs/`, `runtime/`, `sessions/`,
`integrations/`를 포함합니다. 현재 사용자가 설정된 루트를 소유하고 쓸 수 있는지 확인하세요.
광범위한 재귀 명령으로 소유권을 바꾸지 말고 doctor가 표시한 정확한 실패 경로를 확인하세요.

</details>

<details>
<summary><strong>Teardown 없이 Spotter를 제거함</strong></summary>

생성된 Hook은 fail-open이므로 Codex는 계속 동작해야 합니다. 소유한 통합을 정리하려면 Spotter를
다시 설치하고 `spotter doctor`, `spotter teardown codex`를 차례로 실행한 뒤 다시 제거하세요.

</details>

<p align="right"><a href="#spotter-설치-및-사용-상세-가이드">맨 위로 ↑</a></p>

## 11. 이슈 리포팅

[GitHub 이슈 선택 화면](https://github.com/spotter-agent/spotter/issues/new/choose)에서 버그,
기능, 문서, 아키텍처, 실험, 유지관리 중 요청에 가장 알맞은 양식을 선택하세요.

유용한 버그 리포트에는 다음을 포함합니다.

- 실제로 발생한 일, 기대한 동작, 영향
- 문제를 안정적으로 재현하는 최소 단계
- Homebrew 또는 수동 설치 방식
- 관련된 Spotter CLI/데몬 버전, Codex 버전, OS, 아키텍처, Python 버전
- 실패한 정확한 명령과 종료 코드
- 관련된 `status`, `doctor`, `daemon status` 출력 줄
- setup, 업그레이드, 설정 변경, 재설치 중 어느 작업 이후 문제가 시작되었는지

기본 진단 정보는 다음 명령으로 수집합니다.

```bash
spotter --version
spotterd --version
codex --version
spotter status
spotter doctor
spotter daemon status
```

토큰, 자격 증명, 비공개 저장소 이름과 경로, 프롬프트, 소스 코드, 개인정보는 제거하세요. 원시
저널, 전체 설정 파일, 전체 로그는 내용을 먼저 검토하지 않고 첨부하지 마세요. 보안상 민감하거나
악용 가능한 문제는 공개 이슈에 세부 정보를 게시하지 말고 저장소의 Security 페이지에서 비공개
신고 채널을 확인하세요.

## 12. 관련 문서

- [Status(영문)](status.md) — 구현된 기능과 실험적 기능의 권위 있는 경계
- [Lifecycle](lifecycle.md) — 패키지, 서비스, 통합, 복구, 제거의 전체 계약
- [설정 레퍼런스](../spotter.example.toml) — 현재 문서화된 모든 설정 키
- [Homebrew lifecycle smoke](homebrew-lifecycle-smoke.md) — 패키지 수명주기 증거
- [Contributing](../CONTRIBUTING.md) — 소스 설정과 기여 워크플로

<div align="center">

<p>
  <a href="README.md">English</a> ·
  <strong>한국어</strong> ·
  <a href="README.zh-CN.md">简体中文</a>
</p>

<h1>Spotter</h1>

<picture>
  <img alt="Spotter" src="docs/assets/main-ts.png" width="250" />
</picture>

<h3>잘못된 코딩 에이전트의 작업 궤적을 비용이 커지기 전에 포착하세요.</h3>

<p>
  Spotter는 코딩 에이전트를 위한 로컬 런타임 감독 도구입니다.<br />
  최종 diff뿐 아니라 작업 과정을 관찰하여 비용이 큰 이탈을 일찍 발견합니다.
</p>

<p>
  <code>로컬 우선</code> · <code>제한된 게이트</code> · <code>작업 궤적 중심</code>
</p>

<p>
  <a href="#설치"><strong>설치</strong></a>&nbsp;&nbsp;·&nbsp;&nbsp;
  <a href="#빠른-시작"><strong>빠른 시작</strong></a>&nbsp;&nbsp;·&nbsp;&nbsp;
  <a href="docs/user-guide.ko.md"><strong>상세 가이드</strong></a>&nbsp;&nbsp;·&nbsp;&nbsp;
  <a href="docs/status.md">현재 상태(영문)</a>&nbsp;&nbsp;·&nbsp;&nbsp;
  <a href="docs/README.md">전체 문서</a>
</p>

</div>

---

## Spotter가 필요한 이유

코딩 에이전트는 대개 한 번의 명백한 실수로 실패하지 않습니다. 약한 가정 하나가 다음 검색과
수정, 테스트의 방향을 결정하고, 반복되는 작은 판단이 복구 가능한 실수를 시간과 토큰, 저장소
변경 낭비로 키웁니다.

<table>
  <tr>
    <td width="33%" valign="top">
      <strong>🔭 작업 궤적 중심</strong><br />
      <sub>최종 diff뿐 아니라 판단, 증거, 수정, 검증의 흐름을 관찰합니다.</sub>
    </td>
    <td width="33%" valign="top">
      <strong>⚡ 제한된 안전 검사</strong><br />
      <sub>위험한 도구 사용 전 결정론적 검사를 로컬에서 빠르게 수행합니다.</sub>
    </td>
    <td width="33%" valign="top">
      <strong>🩺 명확한 저하 상태</strong><br />
      <sub>Codex를 막지 않으면서 관찰·제어 문제를 진단할 수 있게 표시합니다.</sub>
    </td>
  </tr>
</table>

Spotter는 진행 중인 작업 궤적을 독립적으로 바라보며 다음 질문에 답할 수 있도록 돕습니다.

- 에이전트가 새로운 정보 없이 같은 실패나 사실상 동일한 작업을 반복하고 있는가?
- 변경 범위가 요청한 범위를 벗어나 커지고 있는가?
- 의미 있는 변경 후 관련 검증이 수행되었는가?
- 새 증거로 약해진 가정을 계속 근거로 삼고 있는가?
- 결정론적 안전 규칙을 위반하려 하고 있는가?

목표는 알림을 늘리는 것이 아니라, 첫 번째 의미 있는 이탈 이후 낭비되는 작업을 줄이는 것입니다.

## 현재 Spotter가 하는 일

현재 런타임은 다음을 수행할 수 있습니다.

- Codex Hook 및 설정된 App Server 이벤트를 내구성 있는 작업 궤적 저널에 수집합니다.
- 스레드, 턴, 증거, 진행 상황, 감지 신호에 대한 데몬 소유의 실시간 상태를 유지합니다.
- 위험한 도구 사용 전에 제한 시간 내에서 결정론적 게이트를 적용합니다.
- 반복 루프, 정체된 탐색, 범위 확대, 검증 누락, 오래된 가정의 후보를 감지합니다.
- 선택적으로 시맨틱 리뷰를 섀도 모드로 실행합니다.
- `spotter status`와 `spotter doctor`로 상태 및 통합 진단을 제공합니다.
- 복구와 분석을 위한 Git 기반 스냅샷 및 리플레이 자료를 보존합니다.

> [!IMPORTANT]
> Spotter는 활발히 개발 중입니다. 결정론적 게이트는 실제로 동작하지만 시맨틱 `VERIFY`와
> `NUDGE` 결정은 현재 실시간 턴에 전달되지 않고 기록만 됩니다. 일부 App Server 관찰 및 제어
> 기능은 명시적인 설정이 필요합니다. 정확한 현재 경계는
> [현재 상태(영문)](docs/status.md)에서 확인하세요.

현재 독립 실행형 통합의 주 대상은 Codex입니다.

## 동작 방식

```text
Codex
  ├─ Hooks ────────────────► 제한된 결정론적 게이트
  └─ App Server 이벤트 ────► 설정된 경우 관찰 및 제어
                                   │
                                   ▼
                                spotterd
                                   │
                 저널 · 실시간 상태 · 신호 · 섀도 리뷰
```

결정론적 게이트는 제한된 시간 안에 처리되고, 더 느린 시맨틱 리뷰는 동기식 도구 실행 경로 밖에서
동작합니다. 관찰이나 제어 기능이 저하되면 진단에 명확히 표시되며, 생성된 Hook은 Codex의 정상
사용을 막지 않도록 fail-open으로 동작합니다.

## 설치

공식 Homebrew tap을 사용하는 패키지 설치가 지원됩니다.

```bash
brew install spotter-agent/spotter/spotter
```

두 실행 파일이 모두 설치되었는지 확인합니다.

```bash
spotter --version
spotterd --version
```

패키지 설치는 CLI와 데몬, Hook 브리지만 설치합니다. Codex 설정을 수정하거나 통합을 등록하지는
않습니다.

소스 또는 개발 체크아웃으로 설치하려면
[CONTRIBUTING.md](CONTRIBUTING.md#local-setup)를 참고하세요.

## 빠른 시작

`codex` CLI가 설치되어 있고 `PATH`에서 실행 가능한지 확인한 다음, 관리형 통합이 적용할 내용을
검토하고 설정합니다.

```bash
spotter setup codex --dry-run
spotter setup codex
spotter doctor
```

설정은 트랜잭션 방식으로 처리되며 여러 번 실행해도 같은 결과를 보장합니다. Spotter가 소유한
Hook과 서비스 상태를 정확히 기록하므로, 이후 복구나 연결 해제 시 사용자 소유 설정을 추측하지
않습니다.

설정이 끝나면 평소처럼 Codex를 사용합니다.

```bash
codex
```

## 자주 쓰는 명령

| 명령 | 용도 |
| --- | --- |
| `spotter status` | 통합, 데몬, 기능, 저장소 상태 표시 |
| `spotter doctor` | 합성 상태 검사를 실행하고 조치 가능한 진단 출력 |
| `spotter daemon status` | 패키지로 설치된 `spotterd` 프로세스와 빌드 식별자 확인 |
| `spotter metrics` | 수집된 런타임 및 평가 지표 요약 |
| `spotter observability` | 사용 가능한 작업 궤적 소스와 정규화 이벤트 확인 |
| `spotter --help` | 전체 명령 목록 표시 |

설정 파일은 선택 사항입니다. 게이트, 저장소, 스냅샷, 리뷰어 예산을 조정해야 할 때는
[spotter.example.toml](spotter.example.toml)을 참고하세요. 신호 기반 시맨틱 리뷰는 모델 토큰을
소비하며 기본적으로 비활성화되어 있습니다. 필요한 경우에만 활성화하고 제공되는 세션별·일별
한도를 유지하세요.

## 업그레이드

Formula를 업그레이드한 뒤 설정을 다시 실행하여 설치된 빌드, 실행 중인 데몬, 통합 세대를
Spotter가 조정하도록 합니다.

```bash
brew upgrade spotter-agent/spotter/spotter
spotter setup codex
spotter doctor
```

지속적으로 사용되는 Hook과 서비스 참조는 버전별 Homebrew Cellar 경로가 아니라 안정적인 패키지
진입점을 사용합니다. Spotter는 실행 중인 이전 버전 데몬을 새로 설치된 CLI와 같다고 가정하지
않고 이를 감지합니다.

## 연결 해제 또는 제거

Spotter는 유지하고 Codex 통합만 제거하려면 다음을 실행합니다.

```bash
spotter teardown codex
```

완전히 제거하려면 다음을 실행합니다.

```bash
spotter teardown codex
brew uninstall spotter-agent/spotter/spotter
```

Homebrew 제거는 패키지가 소유한 실행 파일을 제거하고 패키지 런타임을 중지합니다. 별도로 관리되는
`~/.spotter` 아래 사용자 데이터는 의도적으로 삭제하지 않습니다. 연결 해제 없이 패키지만 제거해
통합 설정이 남더라도 fail-open으로 동작하며, 재설치 후 복구할 수 있습니다.

일반적인 절차를 넘어서는 업그레이드, 복구, 마이그레이션, 연결 해제, 데이터 제거를 수행하기
전에는 [Lifecycle](docs/lifecycle.md)을 참고하세요.

## 운영 보장

<details>
<summary><strong>안전 및 소유권 보장 보기</strong></summary>

- `brew install`과 `brew upgrade`는 Codex 설정을 암묵적으로 변경하지 않습니다.
- `spotter setup codex`와 `spotter teardown codex`는 정확히 소유한 통합 상태만 변경합니다.
- 생성된 Hook은 안정적인 실행 파일 경로를 사용하며 Spotter를 사용할 수 없을 때 fail-open으로
  동작합니다.
- Spotter는 소유권을 증명할 수 없는 공유 Codex App Server를 중지하지 않습니다.
- 제거와 사용자 데이터 삭제는 별도 작업입니다.
- `status`와 `doctor`는 관찰 또는 제어 기능의 저하를 명확히 보여 줍니다.

이 계약은 빠른 픽스처 테스트와 실제 macOS Homebrew 설치 → 실행 중 업그레이드 → 제거 → 재설치
수명주기 스모크 테스트로 검증됩니다. 증거와 재현 방법은
[Homebrew lifecycle smoke](docs/homebrew-lifecycle-smoke.md)를 참고하세요.

</details>

## 문서

| 알고 싶은 내용 | 문서 |
| --- | --- |
| 현재 동작하는 기능과 아직 실험적인 기능 | [현재 상태(영문)](docs/status.md) |
| 설치, 설정, 운영, 문제 해결 또는 제거 | [설치 및 사용 상세 가이드](docs/user-guide.ko.md) |
| 패키지와 통합의 전체 계약 | [Lifecycle](docs/lifecycle.md) |
| 제품 아이디어와 개입 모델 | [Concept](docs/concept.md) |
| 런타임 경계와 내구성 상태 | [Architecture](docs/architecture.md) |
| 향후 작업과 증거 게이트 | [Roadmap](docs/roadmap.md) |
| 실험, 가설, 증거 | [Research](docs/research.md) |
| Spotter 개발 또는 기여 | [Contributing](CONTRIBUTING.md) |
| 모든 프로젝트 문서 | [문서 인덱스](docs/README.md) |

---

<p align="center">
  <a href="https://github.com/Bogyie">@bogyie / 김보경</a>과
  <a href="https://github.com/YoungJinJung">@zerone / 정영진</a>이 관리합니다.<br />
  <a href="LICENSE">MIT 라이선스</a>로 배포됩니다.
</p>

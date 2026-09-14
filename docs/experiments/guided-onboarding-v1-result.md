# Guided onboarding v1 result

**Status:** packaged macOS/Linux lifecycle evidence plus a macOS mechanical pilot; human cohort
pending

**Decision:** **NO-GO** for a general-user readiness claim

**Issue:** [#369](https://github.com/spotter-agent/spotter/issues/369)

## Evidence available on 2026-09-14

| Surface | Package/runtime | Result | Limit |
| --- | --- | --- | --- |
| Tagged release | Spotter 0.0.16, build `v0.0.16@1aa5c9b6b56368ad1283dff70ed7eaf7265e9ced` | Release validation, 1,095 tests, artifact build, upload/download byte comparison, and SHA-256 verification passed in [run 34796339331](https://github.com/spotter-agent/spotter/actions/runs/34796339331) | Artifact integrity, not onboarding usability |
| Homebrew update | Formula 0.0.16 at tap commit `b7b22cfd2d3fcd0e1bfbfc4b9b07d96422bcb02a` | Verified release URL/checksum update merged in [tap PR #19](https://github.com/spotter-agent/homebrew-spotter/pull/19) | Formula publication does not provide human evidence |
| Homebrew macOS CI | macOS 26 runner | Formula 0.0.16 build/test, clean package/service smoke, and install→live-upgrade→uninstall→reinstall lifecycle passed in [run 34796478138](https://github.com/spotter-agent/homebrew-spotter/actions/runs/34796478138) | Uses mechanical fixtures; no unfamiliar user |
| Homebrew Linux CI | Ubuntu runner with a real `systemd --user` manager plus the Homebrew container | Formula 0.0.16 build/test and the same managed-service lifecycle passed in the same run | Uses mechanical fixtures; no unfamiliar user or interactive Codex turn |
| Maintainer mechanical pilot | macOS 26.5.1 arm64, Homebrew Spotter 0.0.15, Python 3.14, Codex 0.154.0 | Fresh `SPOTTER_HOME` and `CODEX_HOME` flow completed outside the installed package's source with all expected exit statuses | Not a first-time participant and no interactive live turn |

## Maintainer mechanical pilot

The host's Homebrew package upgraded from 0.0.14 to 0.0.15. The pilot then used isolated state roots,
an unrelated-file checksum, portable runtime registration, a free loopback port, and the packaged
`/opt/homebrew` entry points. The test-started App Server and daemon were stopped after the run.

| Step | Exit | Observation |
| --- | ---: | --- |
| `setup codex --local --portable --dry-run` | 0 | Preview named Hook, runtime, and endpoint changes and reported no mutation |
| `setup codex --local --portable` | 0 | Started and verified the real Codex App Server; integration became ready; unrelated marker was unchanged |
| `doctor` | 0 | Observation, Hook RPC, round trip, policy, and storage were healthy; unadvertised steer/interrupt capability was correctly reported as information |
| `mode` | 0 | Listed all modes and prompted for an explicit noninteractive choice |
| `mode advisory --dry-run` | 0 | Named model-call limits, token use, and task-context disclosure without changing mode |
| `mode observe` | 0 | Saved observation mode and reloaded the daemon configuration |
| `status` | 0 | Reported zero live threads, observation ready, effective observe policy, and AI reviews off |
| `status --session missing-first-run` | 1 | Explicitly said no matching live thread and did not infer one from Hook journals |
| `update` | 0 | Detected Homebrew and printed package-owner upgrade and runtime reconciliation commands |
| `codex --version` through `spotter codex` | 0 | Verified the managed launcher selected packaged Codex; it did not create an interactive thread |
| `teardown codex` and daemon stop | 0 | Removed Spotter-owned integration state and preserved unrelated Codex state |

## Findings fixed before the cohort

The normal `status` path already treated unknown control capability as information because current
Codex does not advertise steer/interrupt support during initialization. The deep App Server probe
used by `doctor` still treated the same state as a warning, making the recommended first setup finish
with exit status 1 despite healthy observation and default observe mode. The accompanying code change
aligns the deep probe with the existing status contract and retains warnings for disconnected or
explicitly unavailable control. The published 0.0.15 pilot above verifies the corrected exit status.

The first Linux managed-service run then exposed that Spotter quoted scalar `WorkingDirectory=` and
`StandardOutput=` values in its systemd unit. systemd treated the quote as part of the path and
refused to start the unit. A second cross-platform run reproduced the earlier intermittent upgrade
failure: the synthetic G1 daemon interpreted a Homebrew relink lasting just beyond ten seconds as a
package removal and exited before explicit reconciliation. [#376](https://github.com/spotter-agent/spotter/pull/376)
emits valid systemd paths and extends the continuous-absence fence to fifteen seconds. The tap's
[#18](https://github.com/spotter-agent/homebrew-spotter/pull/18) lifecycle job now retains Linux
service diagnostics and finishes both platforms independently. Both lifecycle jobs passed on the
merged fix in [run 34795671077](https://github.com/spotter-agent/homebrew-spotter/actions/runs/34795671077).
The published 0.0.16 Formula passed macOS/Linux build and package checks, while its matching source
passed both synthetic lifecycle jobs in [run 34796478138](https://github.com/spotter-agent/homebrew-spotter/actions/runs/34796478138)
and again after the Formula merged in [run 34796821617](https://github.com/spotter-agent/homebrew-spotter/actions/runs/34796821617).

## Remaining evidence

- run the three-person protocol in
  [guided-onboarding-v1-protocol.md](guided-onboarding-v1-protocol.md), including an interactive live
  thread, plain-`codex` warning recovery, comprehension questions, uninstall, and reinstall;
- publish failures without folding them into detector precision or advisory benefit claims.

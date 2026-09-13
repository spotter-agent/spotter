# Guided onboarding v1 result

**Status:** partial mechanical evidence; human cohort and packaged Linux lifecycle pending

**Decision:** **NO-GO** for a general-user readiness claim

**Issue:** [#369](https://github.com/spotter-agent/spotter/issues/369)

## Evidence available on 2026-09-13

| Surface | Package/runtime | Result | Limit |
| --- | --- | --- | --- |
| Tagged release | Spotter 0.0.14, build `v0.0.14@04e7f0cf9d283f3db12b09a8a9de7ed21893d7b9` | Release validation, 1,094 tests, artifact build, upload/download byte comparison, and SHA-256 verification passed in [run 34748226050](https://github.com/spotter-agent/spotter/actions/runs/34748226050) | Artifact integrity, not onboarding usability |
| Homebrew update | Formula 0.0.14 at tap commit `6230ebe2ad267686726771bb051199f25bafe1d9` | Formula URL/checksum update merged in [tap PR #16](https://github.com/spotter-agent/homebrew-spotter/pull/16) | Formula change alone does not exercise setup |
| Homebrew macOS CI | macOS 26 runner | Clean package/service smoke and install→upgrade→uninstall→reinstall lifecycle passed in [run 34748530268](https://github.com/spotter-agent/homebrew-spotter/actions/runs/34748530268) | Uses mechanical fixtures; no unfamiliar user |
| Homebrew Linux CI | Ubuntu runner | Tap setup and syntax passed in the same run | The 0.0.14 Formula install and full lifecycle did not run; this matrix cell remains open |
| Maintainer mechanical pilot | macOS 26.5.1 arm64, Homebrew Spotter 0.0.14, Python 3.14, Codex 0.154.0 | Fresh `SPOTTER_HOME` and `CODEX_HOME` flow completed outside the installed package's source | Not a first-time participant and no interactive live turn |

## Maintainer mechanical pilot

The host's Homebrew package upgraded from 0.0.12 to 0.0.14. The pilot then used isolated state roots,
an unrelated-file checksum, portable runtime registration, a free loopback port, and the packaged
`/opt/homebrew` entry points. The test-started App Server and daemon were stopped after the run.

| Step | Exit | Observation |
| --- | ---: | --- |
| `setup codex --local --portable --dry-run` | 0 | Preview named Hook, runtime, and endpoint changes and reported no mutation |
| `setup codex --local --portable` | 0 | Started and verified the real Codex App Server; integration became ready; unrelated marker was unchanged |
| `doctor` | 1 | Observation, Hook RPC, round trip, policy, and storage were healthy; unknown steer/interrupt capability was incorrectly classified as a warning |
| `mode` | 0 | Listed all modes and prompted for an explicit noninteractive choice |
| `mode advisory --dry-run` | 0 | Named model-call limits, token use, and task-context disclosure without changing mode |
| `mode observe` | 0 | Saved observation mode and reloaded the daemon configuration |
| `status` | 0 | Reported zero live threads, observation ready, effective observe policy, and AI reviews off |
| `status --session missing-first-run` | 1 | Explicitly said no matching live thread and did not infer one from Hook journals |
| `update` | 0 | Detected Homebrew and printed package-owner upgrade and runtime reconciliation commands |
| `codex --version` through `spotter codex` | 0 | Verified the managed launcher selected packaged Codex; it did not create an interactive thread |
| `teardown codex` and daemon stop | 0 | Removed Spotter-owned integration state and preserved unrelated Codex state |

## Finding fixed before the cohort

The normal `status` path already treated unknown control capability as information because current
Codex does not advertise steer/interrupt support during initialization. The deep App Server probe
used by `doctor` still treated the same state as a warning, making the recommended first setup finish
with exit status 1 despite healthy observation and default observe mode. The accompanying code change
aligns the deep probe with the existing status contract and retains warnings for disconnected or
explicitly unavailable control.

## Remaining evidence

- rerun the mechanical pilot against the published release containing the doctor fix;
- complete a real Homebrew Formula install and lifecycle on Linux;
- run the three-person protocol in
  [guided-onboarding-v1-protocol.md](guided-onboarding-v1-protocol.md), including an interactive live
  thread, plain-`codex` warning recovery, comprehension questions, uninstall, and reinstall;
- publish failures without folding them into detector precision or advisory benefit claims.

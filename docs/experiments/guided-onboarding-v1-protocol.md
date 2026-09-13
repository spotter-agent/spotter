# Guided onboarding protocol v1

**Frozen before first-time-user recruitment:** 2026-09-13

**Issue:** [#369](https://github.com/spotter-agent/spotter/issues/369)

**Decision posture:** general-user readiness remains **NO-GO** until the packaged platform matrix and
the cohort below are complete. Command success alone is not usability evidence.

## Question

Can a person who has not used or contributed to Spotter install the current candidate release,
understand its default safety and data posture, connect Codex, recognize live observation state, and
remove the integration by following the published guide without maintainer guidance?

## Candidate and participants

All participants use the same published Spotter release containing the deep-doctor unknown-control
classification fixed with this protocol. Record its exact tag, build ID, Formula commit, Codex
version, operating system, architecture, installation method, and test date before the first task.

Recruit three participants who have not used Spotter and have not contributed to this repository.
The cohort must include at least one supported macOS Homebrew install and one supported Linux
Homebrew install. Use a clean OS account or equivalent isolated home with no prior Spotter state.

## Fixed tasks

Give participants the public README and no command sheet beyond it. Ask them to:

1. Install Spotter and verify the CLI and daemon versions.
2. State what package installation changed and whether Codex integration is active yet.
3. Preview and apply `spotter setup codex --local`, then run `spotter doctor`.
4. State whether commands are currently blocked, whether automatic AI reviews run, and whether the
   setup is ready for a supervised session.
5. Start Codex through the documented command, complete one benign turn, find its thread ID, and use
   `spotter status --session ID` to identify live observation and control state.
6. Start one disposable session with plain `codex`, explain the session-start warning, and recover
   using the command named by Spotter.
7. Inspect `spotter mode`; explain `observe`, `protect`, and `advisory`, including model-token use and
   what task context advisory mode sends. Preview advisory mode without enabling it.
8. Find the package-owner update command, remove only the Codex integration, uninstall Spotter, and
   state which user data remains. Reinstall and confirm the documented repair path.

The moderator may clarify the task wording but must not name a command, interpret a diagnostic, or
correct a policy answer until the participant declares the task blocked or complete.

## Observations

For every task, record:

- completion without help: yes, no, or blocked;
- elapsed time in whole seconds;
- commands attempted and exit status;
- the first blocking or misleading message;
- maintainer help, if any, and the recovery command used;
- the participant's answers about blocking, AI calls, live observation, advisory data, and retained
  data before correction;
- unexpected package, Codex, service, configuration, or state changes.

Do not infer intent or hidden reasoning. Quote only short participant explanations needed to support
a finding. A successful command with an incorrect policy explanation is a comprehension failure.

## Acceptance and stop rules

The readiness gate passes only if all three participants complete tasks 1–6 without maintainer help,
all three correctly explain the five policy/data facts before correction, and teardown/uninstall
preserve unrelated Codex state and Spotter user data on both platforms. Task 8 reinstall may expose a
platform defect, but any destructive or unrecoverable result stops the cohort immediately.

A login, package mirror, or unrelated Codex outage is infrastructure failure. Report it separately
and repeat that participant slot once with the same release; do not substitute a different task or
silently omit the failure. Product failures are not retried within the v1 cohort.

## Data handling

Commit only participant codes, aggregate timings, exact product/platform identities, command exit
statuses, short redacted errors, and supported observations. Do not commit names, usernames, home
paths, authentication files, tokens, repository contents, full Codex transcripts, or model prompts.
Keep any raw screen recording or transcript outside the repository and delete it after the redacted
result is reviewed with the participant.

## Required report

Publish the full package/platform matrix, one row per task and participant, comprehension answers,
all help and failures, and a PASS/NO-GO decision. Link focused issues for reproducible defects.
Separate mechanical package evidence from human findings and from detection/advisory outcome quality.

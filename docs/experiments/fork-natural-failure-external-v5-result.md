# External natural-failure v5 result — host repaired, container metadata invalid

**Measured:** 2026-09-04

**Protocol:** [`fork-natural-failure-external-v5`](fork-natural-failure-external-v5-protocol.md)

**Issue:** [#42](https://github.com/spotter-agent/spotter/issues/42)

**Decision:** **the successor remained 0/3 judgeable; #351 is reopened and #42 remains NO-GO**

## Outcome

All pre-run gates passed against the retained xarray source, exact step 2 prefix, environment
fingerprint, and byte-identical scorer. Exact Spotter commit
`eb891034e986aa69bd98f00a2e070584b2a8f6eb` was installed in a fresh isolated venv.

All six agents exited 0. Unlike v3/v4, all six scorers completed their host `git checkout` and
`git apply` setup and launched the pinned Docker container. Inside the container,
`pip install -e .` discovered `.git/commondir`, followed its host-only absolute `/Users/...` path,
and exited 128 with:

```text
fatal: Invalid path '/Users': No such file or directory
```

Pytest never started. The raw runner's six `TASK_FAIL` rows and 3/3 tied summary are rejected. The
accepted result is **0/3 judgeable pairs**, no disagreement estimate, and **6/6 infrastructure
failures**.

## Provenance

- freeze PR [#357](https://github.com/spotter-agent/spotter/pull/357), merge commit
  `1b5cd697bbf3dac6795b9796844df25634f54a89`;
- experiment ID `1116fccb-94aa-4741-98be-9366ea7e13d9`;
- started `2026-09-04T04:55:27.112557+00:00`;
- finished `2026-09-04T05:03:30.578045+00:00`;
- pre-run raw SHA-256
  `5158c162d335b0987f6b00cfe0e03372d79064f0bb35ae783f45a3948aa7ff7a`;
- post-run raw SHA-256
  `a3fd56189ba50bdef627cf84f3ae81f906c9b8b6c46d3cabf4c57293fae04c2a`;
- scorer SHA-256
  `fb3f0bfc21aa5e2e81a65f3a24f7dc93835abfc1e90921d9607d5d6c8f5f50b9`;
- prefix ID `2bbdb26e5dfaff2933c1a36b648be2e297f89ff7968648fb39f64f1a84db35af`;
- environment fingerprint
  `9e7b6c1caa72c01c69286891ba3c3eb48bd5af2f187b7198df3da6714c8ddaf4`.

| Pair | Arm | Session | Agent | Scorer | Accepted result |
| ---: | --- | --- | ---: | ---: | --- |
| 0 | neutral_b | `7382be6b-58e3-4e3a-84dc-9028f4085a71` | 0 | 128 | infrastructure failure |
| 0 | neutral_a | `eca981d7-1ff4-48c4-b1a0-d583e797ce3d` | 0 | 128 | infrastructure failure |
| 1 | neutral_a | `41898e55-c50d-45bc-a8b6-9ee5e5bc75e8` | 0 | 128 | infrastructure failure |
| 1 | neutral_b | `f9a7cfd3-77e3-43f0-8d88-1c234df8e425` | 0 | 128 | infrastructure failure |
| 2 | neutral_b | `6365825a-eb20-4a0e-881f-2bdb89bac1be` | 0 | 128 | infrastructure failure |
| 2 | neutral_a | `b2e10d98-5008-48a9-b1cd-8fba7f2c6c5b` | 0 | 128 | infrastructure failure |

All prefix/environment preflights matched. Agent elapsed time was 43.124–105.473 seconds and
reported tokens were 14,020–44,241.

## Qualification impact

v5 proves that preserving worktree administration across Codex teardown is sufficient for host
scorer setup, but not for a scorer that bind-mounts the fork into another filesystem namespace.
The scorer metadata must be self-contained and contain no host `commondir` dependency.

[#351](https://github.com/spotter-agent/spotter/issues/351) is reopened. No v5 row changes the
neutral-noise estimate, and #42 remains **NO-GO** for representative causal use.

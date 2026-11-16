# Issue #24 labeling packet v1

This is the first human-labeling packet for [#24](https://github.com/YoungJinJung/spotter/issues/24): measuring the interval between semantic opportunity, observable evidence, and intervention. It contains ten control trajectories from three retained experiment cohorts. Guidance and neutral continuation arms are excluded so the labels are not shaped by intervention content.

The packet is a labeling aid, not an outcome claim. Do not use the final task result as a label, and do not infer hidden reasoning. The source journals remain local; no transcript data is committed here.

## Cases

Open each source in chronological order. The source file is `<root>/spotter-home/sessions/<session_id>.jsonl`.

| Case | Cohort | Task | Session | Local root |
| --- | --- | --- | --- | --- |
| L01 | #365 high-F2P | `django__django-14011` | `01a06b83-34e5-79c3-a9c3-6af023a14c88` | `~/.spotter-experiments/issue-365-high-f2p-v1-20260904` |
| L02 | #365 high-F2P | `pylint-dev__pylint-4551` | `01a06b8a-7e0e-7fe2-93de-71e84c9ea957` | `~/.spotter-experiments/issue-365-high-f2p-v1-20260904` |
| L03 | #365 high-F2P | `django__django-16560` | `01a06b8e-a083-7380-822f-dca077134600` | `~/.spotter-experiments/issue-365-high-f2p-v1-20260904` |
| L04 | #362 independent | `pallets__flask-5014` | `01a06b00-6545-7f83-9e34-ae0a5c0b3f3f` | `~/.spotter-experiments/issue-362-independent-v1-20260904` |
| L05 | #362 independent | `psf__requests-2931` | `01a06b01-8034-7640-b465-f7bce58d6182` | `~/.spotter-experiments/issue-362-independent-v1-20260904` |
| L06 | #362 independent | `pytest-dev__pytest-10356` | `01a06b05-885c-7fd3-bb51-f451b77597ab` | `~/.spotter-experiments/issue-362-independent-v1-20260904` |
| L07 | #362 independent | `pylint-dev__pylint-8898` | `01a06b08-01be-7f03-8a7a-ca40ba29dd82` | `~/.spotter-experiments/issue-362-independent-v1-20260904` |
| L08 | #362 independent | `sphinx-doc__sphinx-7590` | `01a06b10-0294-78f2-bd2f-3292082781b0` | `~/.spotter-experiments/issue-362-independent-v1-20260904` |
| L09 | #362 independent | `sympy__sympy-13878` | `01a06b12-c17e-7d92-96de-af5db290ca51` | `~/.spotter-experiments/issue-362-independent-v1-20260904` |
| L10 | #42/v6 source | `pydata__xarray-6992` | `01a06a49-d733-7b43-9d07-e60540da3138` | `~/.spotter-experiments/issue-42-external-v3-20260904` |

## Review rules

For each case, record only what the journal supports:

1. Read the user goal and trajectory sequentially.
2. Mark the earliest and latest step where a **semantic opportunity** is defensible (`semantic_earliest`, `semantic_latest`).
3. Mark the earliest and latest step where the opportunity is **observable** from durable evidence (`observable_earliest`, `observable_latest`).
4. List the evidence step(s) that justify the observable interval, including repeated flags.
5. Record detector fired as `yes`, `no`, or `unknown` only when the journal makes it explicit.
6. Record wall-clock timestamps only when present; otherwise write `unknown`.
7. Use `unclear` rather than forcing an exact step. Do not infer hidden reasoning or treat the final task result as a label.

Hook-era journals may support step-level labels but not wall-clock timing. That is a valid missing value.

## Worksheet

| Case | Semantic earliest/latest | Observable earliest/latest | Evidence step(s) | Detector fired? | Wall-clock available? | Note |
| --- | --- | --- | --- | --- | --- | --- |
| L01 |  |  |  |  |  |  |
| L02 |  |  |  |  |  |  |
| L03 |  |  |  |  |  |  |
| L04 |  |  |  |  |  |  |
| L05 |  |  |  |  |  |  |
| L06 |  |  |  |  |  |  |
| L07 |  |  |  |  |  |  |
| L08 |  |  |  |  |  |  |
| L09 |  |  |  |  |  |  |
| L10 |  |  |  |  |  |  |

## Handoff

Return the completed worksheet. A second rater should independently double-label a small pilot subset before computing agreement or detection-delay metrics. After review, persist only supported labels with the existing `spotter label-opportunity` command; leave ambiguous fields explicitly unknown/unclear rather than inventing numeric steps.

For a summary view, set `SPOTTER_HOME` to the cohort's local `spotter-home` and run:

```bash
spotter analyze --session <SESSION_ID>
```

After persisting the reviewed windows, use the same cohort's `SPOTTER_HOME` and `CODEX_HOME` to run:

```bash
spotter fork-coverage --session <SESSION_ID>
```

Read `opportunity_window_points` for each rater's proposal coverage inside the observable interval.
Stale annotations have no eligible points; an interval without a proposal stays uncovered even if a
later proposal is forkable. `opportunity_windows_forkable` counts windows with at least one exact
point against `opportunity_window_annotations_current`, not independent opportunities or fully
covered intervals. Keep rater agreement separate. Zero annotations means measurement is pending,
not zero successful interventions. Legacy `labeled_opportunity_*` fields describe verdict labels and
are not the timing-window denominator.

## Retained-artifact preflight — 2026-11-16

All ten sources were inspected with the current `fork-coverage` implementation, using temporary
Spotter homes linked to the retained journals/labels and each cohort's original Codex home. No
continuations, scorers, annotations, or forks were created. These are current artifact-availability
counts, not a new outcome experiment or a historical environment-parity measurement.

| Case | Proposals | Exact | Missing context | Unsafe/unknown external state | Timing annotations |
| --- | ---: | ---: | ---: | ---: | ---: |
| L01 | 12 | 1 | 0 | 11 | 0 |
| L02 | 12 | 0 | 1 | 11 | 0 |
| L03 | 22 | 1 | 0 | 21 | 0 |
| L04 | 9 | 1 | 0 | 8 | 0 |
| L05 | 10 | 1 | 0 | 9 | 0 |
| L06 | 20 | 1 | 0 | 19 | 0 |
| L07 | 13 | 4 | 0 | 9 | 0 |
| L08 | 21 | 1 | 0 | 20 | 0 |
| L09 | 21 | 3 | 0 | 18 | 0 |
| L10 | 12 | 1 | 0 | 11 | 0 |
| **Total** | **152** | **14** | **1** | **137** | **0** |

No points were classified as missing repository state or observation gaps. The external-state
bucket includes unknown effects; it does not prove that 137 external writes occurred. L02 has no
currently exact branch, so do not run a continuation there without resolving context availability.
All ten cases still lack timing annotations. The 14/152 proposal coverage is therefore **not**
coverage of labeled intervention opportunities. The human worksheet and #42 qualification remain
pending.

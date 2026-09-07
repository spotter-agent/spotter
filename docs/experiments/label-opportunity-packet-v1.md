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

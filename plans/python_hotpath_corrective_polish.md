# Python Hot-Path Corrective Polish Plan

## Context

The Python hot-path dispatch and compression optimization has landed on `main` in commit `eedb869d33f0446cdb49be5f7e9423b228aa3888`. The implementation appears to cover the intended Phases 1–5: dispatch spans, single-pass safe-mode compression observation, lazy/path-level copy-on-write, segmentation/analyzer reductions, precomputed request metadata, narrowed selection lock, and routing trace writes outside the lock.

This corrective polish pass is not a new optimization wave. It is a closure pass to remove ambiguity, correct likely metric skew, verify observability semantics, and make the implementation safe for future maintenance.

## Goals

1. Correct dispatch span accounting so lock wait and lock-held metrics are accurate and not double-sampled.
2. Update stale safe-compression documentation/comments to match the new copy-on-write behavior.
3. Tighten safe-mode observation semantics so dashboard/operator metrics do not silently regress from the old observe analyzer.
4. Validate `_copy_with_replacements()` correctness for dict/list paths, shared prefixes, multiple replacements, and no-op identity behavior.
5. Add focused runtime/performance verification around the formerly slow safe-compression dispatch path.
6. Add dashboard/API checks for the new `dispatch_spans` payload and safe-mode observation fields.

## Non-goals

Do not reintroduce the separate observe analyzer call in safe mode.

Do not remove fail-closed stable-prefix content-hash verification.

Do not add Rust/PyO3/native acceleration in this pass.

Do not change route scoring to consume cache/compression/synthetic-cache fields.

Do not coalesce or weaken request/reservation/attempt persistence. Those writes remain correctness-critical.

Do not make routing trace writes required for dispatch success. Trace persistence should stay best-effort observability.

## Issue 1 — Correct selection lock span accounting

### Problem

`RequestCoordinator._select_and_persist_attempt()` now records real lock wait and locked durations after the `async with self._select_lock:` block. However, inside the lock it also opens placeholder `_maybe_span()` contexts for `SPAN_SELECTION_LOCK_WAIT` and `SPAN_SELECTION_LOCKED`.

The `SPAN_SELECTION_LOCK_WAIT` placeholder appears to record a near-zero sample in addition to the manually recorded real wait. If the recorder aggregates both, every request may contribute two lock-wait samples: one near-zero and one real. That would depress p50/p95 and make contention look lower than it is.

`SPAN_SELECTION_LOCKED` is also wrapped around the whole locked block and then manually recorded afterward. If both record, lock-held time may be double-sampled with slightly different boundaries.

### Plan

Remove placeholder `_maybe_span()` blocks for `SPAN_SELECTION_LOCK_WAIT` and `SPAN_SELECTION_LOCKED` from inside `_select_and_persist_attempt()`.

Use explicit timing only:

- `lock_wait_started_ns = time.perf_counter_ns()` immediately before acquiring `_select_lock`.
- `lock_acquired_ns = time.perf_counter_ns()` immediately after acquiring it.
- `lock_released_ns = time.perf_counter_ns()` immediately after the `async with` exits.
- Record exactly one sample each:
  - `SPAN_SELECTION_LOCK_WAIT = lock_acquired_ns - lock_wait_started_ns`
  - `SPAN_SELECTION_LOCKED = lock_released_ns - lock_acquired_ns`

Keep child spans such as `SPAN_CIRCUIT_PROBE`, `SPAN_ACCOUNT_LOOKUP`, `SPAN_DB_WRITE_REQUEST`, `SPAN_DB_WRITE_RESERVATION`, `SPAN_DB_WRITE_ATTEMPT`, and `SPAN_RUNTIME_PUBLICATION` inside the lock as they measure subregions of locked work.

Add a unit test around a fake or real `DispatchSpanRecorder` that executes one selection and asserts exactly one sample is present for `selection_lock_wait_ms` and exactly one sample is present for `selection_locked_ms`. If the recorder API does not expose counts, expose sample count in test-only snapshot output or test through a dedicated recorder stub.

Add a contention test with two concurrent selections where one is intentionally delayed under the lock, then verify the second request's lock-wait span is non-zero and not suppressed by near-zero placeholder samples.

### Acceptance criteria

- `selection_lock_wait_ms` and `selection_locked_ms` each record exactly one sample per selection attempt.
- The runtime API still exposes p50/p95/max for both spans.
- Existing child spans remain intact.
- No dispatch behavior changes.

## Issue 2 — Refresh stale safe-compression documentation and comments

### Problem

`src/eggpool/transcoder/compression/apply.py` still documents the previous implementation model: deep-copy before mutation and always deep-copy. The implementation now does discovery first, returns the original payload on no-op, and applies path-level copy-on-write only when replacements exist.

Stale comments are dangerous here because the safety model changed from whole-payload copy to selective structural copying. Future maintainers could reintroduce deep-copy or misread no-op identity as accidental.

### Plan

Update the module docstring in `apply.py`:

- Replace “applies transforms in-place on a deep-copied payload” with “discovers planned replacements, then applies them through path-level copy-on-write only when a mutation is needed.”
- Replace “deep-copies before any mutation” with “never mutates the input payload; no-op requests return the original payload, applied requests copy only mutated paths and their ancestors.”
- Keep the fail-closed stable-prefix hash language.
- Keep deterministic transform and latency-bound language.

Update the `apply_safe_compression()` docstring:

- Remove “always deep-copies.”
- State explicitly that no-op returns the original payload object and applied compression returns a copy-on-write payload.
- State that the public contract is “input is never mutated,” not “output is always a deep copy.”

Search for stale text in docs and architecture notes:

```bash
rg -n "deep-cop|always deep|in-place|copy-on-write|safe compression" src docs architecture README.md AGENTS.md
```

Update docs that explain safe mode:

- `docs/cache-compression.md`
- `docs/cache-compression-troubleshooting.md` if present
- `architecture/README.md`
- `.opencode/skills/architecture/SKILL.md` if it mirrors architecture notes

### Acceptance criteria

- No doc/comment claims safe compression always deep-copies.
- Documentation distinguishes “input never mutated” from “deep-copy always made.”
- Docs explicitly mention no-op path returns original payload and applied path uses copy-on-write.
- Behavior remains unchanged.

## Issue 3 — Tighten safe-mode observation semantics

### Problem

Safe-mode observation is now derived from `CompressionResult` via `SafeModeObservation`. That is correct architecturally, but the current summary may under-report opportunity metrics:

- No-op safe-mode runs return `candidate_count = 0`, `eligible_candidate_count = 0`, and `suppressed_candidate_count = 0` even when `reason_code_counts` contains threshold/suppression reasons.
- Applied safe-mode runs set `candidate_count = transform_count`, which is applied-transform count rather than candidate count.
- The dashboard may use candidate/opportunity fields to communicate whether compression found opportunities but skipped them due to thresholds or policy.

The plan should not reintroduce a full analyzer pass. It should improve the applier-derived counts as much as possible from the single pass.

### Plan

During safe-mode discovery in `_apply_safe_compression_impl()`, track these counters independently from applied transform count:

- `candidate_count`: number of segment/transform pairs that reached transform execution with non-empty text and produced a transform result or a threshold decision. If a more conservative definition is preferred, document it clearly.
- `eligible_candidate_count`: number of candidates that passed policy filters and produced positive token savings before min-threshold checks.
- `suppressed_candidate_count`: number of candidates suppressed by policy, placement, cache boundary, disabled transform, empty segment, latency budget, min candidate tokens, or min savings tokens.
- `applied_transform_count`: current `transform_count`.

Add these fields either to `CompressionResult` or a nested `candidate_summary` dataclass:

```python
candidate_count: int
eligible_candidate_count: int
suppressed_candidate_count: int
applied_transform_count: int
```

If adding fields to `CompressionResult`, preserve backward-compatible JSON summaries by adding fields rather than renaming existing ones.

Update `build_safe_mode_observation()` so:

- `candidate_count` reflects candidates considered by the safe applier.
- `eligible_candidate_count` reflects candidates that were eligible before final apply.
- `suppressed_candidate_count` reflects policy/threshold/latency suppression count.
- `transform_counts` remains applied transform counts by reason.
- `source = "safe_apply"` stays present in `to_summary_json()`.

Add docs clarifying that safe-mode observation counts are applier-derived, not a full observe analyzer trace, and intentionally omit raw candidate detail for content privacy and performance.

### Tests

Add safe-mode observation tests for:

- Empty/no text: candidate count remains zero or suppressed count reflects empty segment, depending on chosen definition; expected behavior must be explicit.
- Candidate below `min_candidate_tokens`: suppressed count increments, applied count remains zero.
- Candidate below `min_savings_tokens`: suppressed count increments, applied count remains zero.
- Disabled transform: suppressed reason increments.
- Cache-protected segment: suppressed reason increments.
- Applied transform: candidate count and applied transform count increment.
- Stable-prefix fail-closed: candidate counts remain visible, `failed_fallback = True`, applied result false.

Add dashboard/API regression tests verifying safe-mode rows/cards do not show “0 opportunities” when the safe applier saw threshold-suppressed candidates.

### Acceptance criteria

- Safe mode remains single-pass.
- Dashboard candidate/opportunity metrics remain meaningful.
- Existing persisted summaries parse correctly.
- No raw prompt/tool content is persisted.

## Issue 4 — Verify and harden path-level copy-on-write

### Problem

`_copy_with_replacements()` is now safety-critical. It must not mutate the original payload, must correctly handle dict/list paths, must support multiple replacements with shared prefixes, and must not corrupt unrelated branches.

The implementation should receive direct unit coverage beyond end-to-end compression tests.

### Plan

Add focused tests for `_copy_with_replacements()` in a compression apply test module.

Test cases:

1. No replacements:
   - returns the original object by identity.
   - original payload unchanged.

2. Single dict path:
   - replacement at `("messages", 0, "content")` changes copied result.
   - original payload content unchanged.
   - unrelated sibling branches preserve equality.

3. Single list path:
   - replacement inside `messages[1].content[0].text` works.
   - original list and nested dict remain unchanged.

4. Multiple replacements with shared prefix:
   - two replacements under the same message copy the shared prefix once logically.
   - both replacements appear in result.
   - sibling messages remain unchanged.

5. Multiple replacements across separate branches:
   - both branches mutate in result.
   - unchanged branches remain equal and, where safe to assert, identical by object identity.

6. Duplicate replacement path:
   - explicitly define behavior. Prefer last-wins only if documented; otherwise raise or coalesce before calling helper.
   - Add a test for the chosen behavior.

7. Invalid path:
   - helper should fail closed at the caller boundary if an impossible path appears.
   - Either return original via caught exception in `apply_safe_compression()` or skip invalid planned replacement before apply.

8. Root-level path:
   - if root-level string replacement is unsupported, assert helper ignores/rejects it clearly.

Also add one integration test where two transforms chain on the same segment and verify the final replacement reflects the chained text. The current discovery loop updates `current_text = new_text`; the final planned replacement list may contain multiple replacements for the same `content_path`, and `_copy_with_replacements()` currently collapses by path. That can be correct if the last planned replacement is the fully chained final text. Pin this behavior with a test.

### Acceptance criteria

- No test can mutate the original payload object.
- Shared-prefix replacement works for nested dict/list structures.
- Duplicate-path/chained-transform behavior is explicit and pinned.
- Invalid-path behavior fails closed.

## Issue 5 — Runtime verification of dispatch-span and compression gains

### Problem

The code now contains the intended optimizations, but we need an empirical closure check for the original symptom: dispatch overhead near one second with compression enabled.

Commit metadata claims a large test suite passes, but visible GitHub workflow/status metadata was not available through the connector. A local or CI performance baseline should be committed as a deterministic regression guard where feasible.

### Plan

Add or update performance tests under `tests/perf/` for the specific workloads from the original plan:

1. Compression disabled, small native OpenAI request.
2. Observe mode, small native OpenAI request.
3. Safe mode, small request with no transform opportunity.
4. Safe mode, large prior conversation with stable prefix and short volatile suffix, no transform opportunity.
5. Safe mode, large volatile log/tool output where transform applies.
6. Safe mode, threshold-suppressed transform candidate.
7. Transcoded request with safe compression disabled.
8. Transcoded request with safe compression enabled if supported by current path.

For each workload, assert relative invariants rather than absolute hardware timing:

- Safe no-op does not call `copy.deepcopy`.
- Safe mode does not call `analyze_compression`.
- Compression disabled does not record compression spans.
- Observe mode records `compression_analyze_ms` but not `compression_apply_ms`.
- Safe mode records `compression_apply_ms` but not `compression_analyze_ms`.
- Selection lock wait/held spans record exactly once per selection.
- Routing trace write spans are absent when mode is `off` or unsampled.

For optional local benchmark output, add a script or test helper that prints p50/p95 for:

```bash
uv run pytest -m performance -q
```

Avoid strict millisecond thresholds unless there is an existing stable perf harness. If thresholds are added, use generous budgets and skip/xfail on overloaded CI when appropriate.

### Acceptance criteria

- There is a repeatable way to compare old/new hot-path behavior.
- The test suite catches accidental reintroduction of duplicate safe-mode analyze or unconditional deep-copy.
- Dispatch-span payload can be trusted for live diagnosis.

## Issue 6 — Dashboard/API polish for `dispatch_spans`

### Problem

`dispatch_spans` is now exposed under `/api/stats/runtime`. The dashboard/runtime UI and docs must make this usable without adding clutter or confusing absent spans with zero-duration spans.

### Plan

Audit `/api/stats/runtime` output shape and dashboard rendering.

Ensure each span entry exposes:

- `p50_ms`
- `p95_ms`
- `max_ms`
- `sample_count`

If sample count is not exposed, add it. It is important for distinguishing a stable p95 from one or two samples.

In the runtime dashboard, show a compact “Dispatch spans” section or include the most actionable spans in an existing performance/dispatch card:

- `coordinator_pre_upstream_ms`
- `segmentation_ms`
- `compression_analyze_ms`
- `compression_apply_ms`
- `selection_lock_wait_ms`
- `selection_locked_ms`
- `routing_trace_write_ms`

Use absent/empty state copy such as “not observed in recent window” rather than `0 ms` when a span is absent. This matters because compression disabled should not show `compression_apply_ms = 0`; it should show no apply span.

Add docs explaining common interpretations:

- High `segmentation_ms`: segmenter/hash/token-estimation pressure.
- High `compression_analyze_ms`: observe-mode transform scan cost.
- High `compression_apply_ms`: safe-mode transform/apply cost.
- High `selection_lock_wait_ms`: concurrency contention.
- High `selection_locked_ms`: DB/account/runtime publication pressure.
- High `routing_trace_write_ms`: trace mode too heavy for current device.

### Tests

Add dashboard/API tests for:

- Runtime stats include `dispatch_spans` with sample counts.
- Missing spans render as absent/not observed, not zero.
- Safe mode shows apply span and no analyze span.
- Observe mode shows analyze span and no apply span.
- Trace write span absent when routing trace mode is `off`.

### Acceptance criteria

- Operators can identify the current dispatch bottleneck from `/api/stats/runtime` or the runtime page.
- Dashboard does not imply disabled paths took zero milliseconds.
- Sample count is visible for p50/p95 interpretation.

## Issue 7 — Final verification checklist

Run focused checks first:

```bash
uv run pytest -m request_path -v
uv run pytest -m dashboard -v
uv run pytest -m performance -v
uv run pytest tests/test_*compression* -q
uv run pytest tests/test_*segmentation* -q
uv run pytest tests/test_*routing* -q
uv run pytest tests/test_*coordinator* -q
```

Then run full quality gates:

```bash
uv run pytest
uv run ruff check src tests scripts
uv run ruff format --check src tests scripts
uv run pyright src scripts
```

Manual smoke check on a local server:

1. Start with compression disabled. Send a small request. Confirm no compression spans.
2. Start with observe mode. Send a request. Confirm `compression_analyze_ms` only.
3. Start with safe mode and no transform opportunity. Confirm `compression_apply_ms`, no analyzer, no deep-copy if test instrumentation is available.
4. Start with safe mode and large log/tool-output payload. Confirm transform applies, stable-prefix hash preserved, and upstream body changes only in volatile suffix.
5. Set `[routing.trace].mode = "off"`. Confirm no trace write span.
6. Set `[routing.trace].mode = "sampled"` with `sample_rate = 1.0`. Confirm trace write span appears.
7. Drive two concurrent requests and confirm `selection_lock_wait_ms` is meaningful and sample count increments once per attempt.

## Handoff notes

Prioritize Issue 1 first. Bad lock-span metrics would undermine diagnosis of every later concurrency claim.

Prioritize Issue 2 next because stale documentation around copying can cause future behavioral regressions.

Then handle Issue 3 and Issue 4 together if the same tests exercise `CompressionResult`, `SafeModeObservation`, and `_copy_with_replacements()`.

Finish with Issue 5 and Issue 6 to make the optimization measurable and operator-visible.

This pass should end with a short architecture/docs update that says the hot-path implementation is not just landed but validated: safe mode is single-pass, no-op safe compression does not copy, span metrics are one-sample-per-attempt, and runtime dashboard span interpretation is pinned by tests.

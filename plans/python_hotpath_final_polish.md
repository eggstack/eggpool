# Python Hot-Path Final Polish Plan

## Context

The Python hot-path dispatch and compression optimization has now landed in two implementation waves:

- `eedb869d33f0446cdb49be5f7e9423b228aa3888`: implements the main Phases 1–5 hot-path work.
- `0980fa97bf112fa19be25f5a400d3b67a4487ab7`: implements the corrective polish pass covering lock-span accounting, safe-mode candidate counts, copy-on-write hardening, dashboard dispatch-span rendering, and regression guards.

A follow-up review shows the repo is in good shape overall, but there are still a few narrow closure items before this line of work should be considered fully done:

1. One stale sentence remains in `src/eggpool/transcoder/compression/apply.py` claiming transforms apply in-place on a deep-copied payload.
2. Existing tests include useful helper/recorder-level guards, but true request-path coverage should prove safe mode does not call `analyze_compression()` under a real segmentation/apply path.
3. GitHub status/workflow metadata was not visible for the latest commits through the connector, so the validation story should be made explicit in repo docs or CI artifacts.
4. Runtime dashboard span behavior should be pinned end-to-end where feasible, not only through renderer/recorder unit tests.

This plan is deliberately small. Do not reopen the broader optimization design unless a test exposes a real behavior regression.

## Goals

- Remove the last stale deep-copy wording from safe-compression docs/comments.
- Add end-to-end request-path tests for safe-mode apply-only behavior.
- Add end-to-end request-path tests for observe-mode analyze-only behavior.
- Verify `dispatch_spans` behavior through the runtime API/dashboard path with realistic recorded spans.
- Make local/CI verification commands and expected signals explicit for handoff.
- Avoid functional changes unless required to make tests pass.

## Non-goals

Do not add Rust, PyO3, native extensions, or tokenizer rewrites.

Do not reintroduce the observe analyzer into safe mode.

Do not weaken stable-prefix content-hash fail-closed behavior.

Do not change compression thresholds, routing policy, quota policy, or request mutation defaults.

Do not add strict millisecond performance thresholds that will be flaky on CI or low-power devices.

Do not move correctness-critical request/reservation/attempt writes out of the locked section.

## Phase 1 — Remove stale safe-compression wording

### Problem

The `apply.py` module docstring now contains both the corrected copy-on-write design and one contradictory stale sentence near the top:

> applies deterministic transforms in-place on a deep-copied payload

That sentence is wrong after the corrective polish pass. Safe-mode compression now discovers planned replacements, returns the original payload by identity on no-op, and uses path-level copy-on-write for applied replacements.

### Plan

Update `src/eggpool/transcoder/compression/apply.py` module docstring so the introductory paragraph matches the corrected design:

Replace stale wording with language such as:

> walks volatile-suffix segments, identifies eligible compressible candidates, discovers planned deterministic replacements, and applies them through path-level copy-on-write only when at least one mutation is needed.

Also search the repo for any remaining stale phrasing:

```bash
rg -n "deep-copied payload|deep copy|deep-copy|always deep|in-place on a deep|copy-on-write" \
  src docs architecture README.md AGENTS.md .opencode/skills
```

Keep legitimate mentions of historical behavior only if explicitly marked historical. For current behavior, use these invariants consistently:

- Input payload is never mutated.
- No-op safe compression returns the original payload object.
- Applied safe compression returns a path-level copy-on-write payload.
- Stable-prefix content hash is still verified after applying replacements.
- This is not a full deep copy.

### Tests / verification

No behavior test is required for the docstring-only edit, but run formatting/lint to catch accidental syntax errors.

```bash
uv run ruff check src/eggpool/transcoder/compression/apply.py
uv run pyright src/eggpool/transcoder/compression/apply.py
```

### Acceptance criteria

- No current-behavior docs say safe compression applies in-place on a deep-copied payload.
- The module docstring and `apply_safe_compression()` docstring describe the same copy-on-write contract.
- No behavior change.

## Phase 2 — Add true request-path coverage for safe-mode apply-only behavior

### Problem

The existing corrective-polish tests pin important invariants at the helper/recorder level. They verify that `apply_safe_compression()` itself does not call `analyze_compression()` and that recorder contracts behave correctly. However, the higher-risk integration point is `handle_proxy_request()`: it resolves policy, decides segmentation, branches observe vs safe, calls the applier, builds `ProxyRequestContext`, and sends the context to `RequestCoordinator.execute()`.

A future refactor could accidentally reintroduce `analyze_compression()` in `proxy_request.py` before safe apply while leaving helper-level tests green.

### Plan

Add a focused request-path test module, preferably one of:

- `tests/unit/test_proxy_request_hotpath_modes.py`
- or extend an existing proxy request / compression request-path test file if one already exists.

Use the existing FastAPI/app/test-client/request-coordinator fixture style used by the repo. Avoid real upstream calls. The test should stub or fake `RequestCoordinator.execute()` so the request path runs through segmentation/compression/context construction and then stops.

Create a request payload that definitely produces a segmentation result and gives the safe applier work to inspect. Two useful variants:

1. Safe mode no-op payload: segmentation runs, safe applier runs, no transform applies.
2. Safe mode applied payload: volatile suffix contains repeated lines, logs, or another deterministic transform opportunity.

Patch/spies:

- Patch `eggpool.transcoder.compression.analyze_compression` or the exact imported call site used by `proxy_request.py` so any call raises `AssertionError("analyze_compression must not run in safe mode")`.
- Patch/wrap `eggpool.transcoder.compression.apply.apply_safe_compression` to assert it is called exactly once.
- Capture the `ProxyRequestContext` passed to `coordinator.execute()` and assert:
  - `context.compression_result is not None`.
  - `context.compression_observation is not None`.
  - `context.compression_observation.to_summary_json()` contains `"source": "safe_apply"`.
  - `context.segmentation is not None`.
  - `context.segmentation_not_collected is False`.
  - `context.estimated_reservation_tokens is not None`.
  - `context.thinking_requirement is not None` for native protocols, even when no thinking is requested.

Also verify dispatch spans if the app state exposes a recorder:

- `compression_apply_ms` present.
- `compression_analyze_ms` absent.
- `segmentation_ms` present.

### Acceptance criteria

- A real request through `handle_proxy_request()` in safe mode fails if `analyze_compression()` is called.
- Safe mode calls the applier exactly once.
- Safe-derived observation reaches `ProxyRequestContext`.
- Safe request path records apply-only compression spans.

## Phase 3 — Add true request-path coverage for observe-mode analyze-only behavior

### Problem

The inverse mode contract should be pinned end-to-end: observe mode should analyze but never apply. Helper/recorder tests are not enough because the branch lives in `proxy_request.py`.

### Plan

Add a request-path test using `mode = "observe"` and `enabled = true`.

Patch/spies:

- Patch/wrap `eggpool.transcoder.compression.analyze_compression` to assert it is called exactly once.
- Patch `eggpool.transcoder.compression.apply.apply_safe_compression` so any call raises `AssertionError("apply_safe_compression must not run in observe mode")`.
- Capture `ProxyRequestContext` passed to coordinator.

Assertions:

- `context.compression_observation is not None` if analyzer returns an observation for the chosen payload, or explicitly assert analyzer was invoked and `context.compression_observation` matches its return value.
- `context.compression_result is None`.
- `context.segmentation is not None`.
- `compression_analyze_ms` present in dispatch spans.
- `compression_apply_ms` absent.

### Acceptance criteria

- Observe mode cannot accidentally call safe apply.
- Observe mode records analyze-only spans.
- Observe-mode behavior remains observational and does not mutate upstream payload.

## Phase 4 — Add disabled-compression request-path coverage

### Problem

The optimized disabled path should remain cheap: no segmentation unless another consumer requires it, no analyzer, no applier, no compression spans. Current helper tests only assert an empty recorder has no spans; a real request-path test should pin the disabled branch.

### Plan

Add a request-path test with compression disabled and synthetic cache disabled.

Patch/spies:

- Patch `segment_request` so any call raises, unless the current app fixture has cache observability enabled and legitimately requires segmentation. Prefer configuring the app/test config so segmentation is not needed.
- Patch `analyze_compression` so any call raises.
- Patch `apply_safe_compression` so any call raises.
- Capture `ProxyRequestContext`.

Assertions:

- `context.segmentation is None`.
- `context.segmentation_not_collected is True`.
- `context.compression_observation is None`.
- `context.compression_result is None`.
- `compression_analyze_ms` absent.
- `compression_apply_ms` absent.
- `segmentation_ms` absent unless another explicit feature is enabled in the test config.

### Acceptance criteria

- Disabled compression path remains cheap and does not accidentally segment.
- Disabled path does not record misleading zero-duration compression spans.

## Phase 5 — Runtime API/dashboard dispatch-span integration check

### Problem

`dispatch_spans` is now rendered in the runtime dashboard and returned by `/api/stats/runtime`. The corrective-polish commit says absent spans render as “not observed in recent window” and sample counts are shown. This should be pinned through the API/rendering path with realistic sample data.

### Plan

Add or extend tests in `tests/unit/test_runtime_dispatch_spans_dashboard.py`.

Test API shape:

1. Create a `DispatchSpanRecorder`.
2. Record samples for:
   - `coordinator_pre_upstream_ms`
   - `segmentation_ms`
   - `compression_apply_ms`
   - `selection_lock_wait_ms`
   - `selection_locked_ms`
3. Do not record `compression_analyze_ms`.
4. Invoke the runtime stats snapshot/render path used by `/api/stats/runtime`.
5. Assert each recorded span includes:
   - span key
   - `sample_count`
   - `p50_ms`
   - `p95_ms`
   - `max_ms`
   - optionally `avg_ms` if already part of the contract
6. Assert absent spans are omitted from JSON or represented consistently as absent, not as zero-valued samples.

Test dashboard rendering:

- Render the runtime page with a dispatch-span payload that has apply but not analyze.
- Assert the Dispatch spans panel is present.
- Assert sample count text is present.
- Assert the missing analyze span renders “not observed in recent window” or the agreed empty-state text.
- Assert no missing span is rendered as `0.0 ms` unless a real zero sample was recorded.

### Acceptance criteria

- Runtime API span shape is stable.
- Dashboard empty-state behavior is pinned.
- Operators can distinguish absent spans from zero-duration spans.

## Phase 6 — Verification and status visibility

### Problem

The latest commits claim local tests passed, but GitHub status/workflow metadata was not visible through connector checks. This may be normal for the repo, but the handoff should leave unambiguous commands and expected outcomes.

### Plan

Add a short verification note either in the final implementation commit message or an existing dev/architecture doc, not necessarily a new permanent doc if that would create noise. Preferred locations:

- `AGENTS.md` focused verification section.
- `architecture/README.md` hot-path section.
- `plans/python_hotpath_final_polish.md` can remain the handoff source if no docs change is desired.

After implementing the tests, run:

```bash
uv run pytest tests/unit/test_hotpath_corrective_polish.py -v
uv run pytest tests/unit/test_runtime_dispatch_spans_dashboard.py -v
uv run pytest tests/unit/test_proxy_request_hotpath_modes.py -v
uv run pytest -m request_path -v
uv run pytest -m dashboard -v
uv run pytest -m performance -v
uv run pytest
uv run ruff check src tests scripts
uv run ruff format --check src tests scripts
uv run pyright src scripts
```

If the repo does not use GitHub Actions, note that explicitly in the handoff/commit message. If it does, ensure the relevant workflow triggers on the final polish commit and check status before closure.

### Acceptance criteria

- The final polish work has clear local verification commands.
- If CI exists, there is visible CI status for the final commit or a clear note explaining why not.
- The hot-path behavior is validated through request-path, dashboard, and helper-level tests.

## Suggested implementation order

1. Fix stale `apply.py` docstring sentence first.
2. Add the safe-mode end-to-end request-path test.
3. Add observe-mode and disabled-mode request-path tests.
4. Tighten runtime API/dashboard dispatch-span tests.
5. Run focused hot-path/dashboard suites.
6. Run full quality gates.
7. Update CHANGELOG or architecture notes only if behavior-facing details changed beyond tests/docs.

## Final closure checklist

Before closing this line of work, verify:

- No current docs say safe compression uses full deep copy.
- Safe mode through `handle_proxy_request()` calls apply once and analyze zero times.
- Observe mode through `handle_proxy_request()` calls analyze once and apply zero times.
- Disabled compression does not segment or emit compression spans unless another explicit feature requires segmentation.
- `compression_apply_ms` and `compression_analyze_ms` remain sparse and mode-specific.
- `selection_lock_wait_ms` and `selection_locked_ms` record exactly one sample per selection attempt.
- Runtime dashboard shows sample counts and absent-span empty states.
- All new tests are deterministic and avoid strict wall-clock thresholds.
- Full test, lint, format, and type-check gates pass.

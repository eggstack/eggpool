# Python Hot-Path Dispatch and Compression Optimization Plan

## Context

EggPool dispatch overhead is approaching roughly one second when request shaping / compression is enabled. The goal of this pass is to reduce dispatch latency, CPU usage, and transient memory pressure without removing request-shaping capability, changing routing semantics, weakening fail-closed compression safety, or introducing a Rust extension.

This plan covers the first five Python-only optimization items:

1. Add fine-grained hot-path instrumentation.
2. Remove redundant compression work in safe mode.
3. Make safe compression avoid full deep-copy unless mutation is actually needed.
4. Reduce segmentation allocation, serialization, and token-estimation overhead.
5. Shorten the serialized selection path and reduce routing-trace write pressure.

Rust / PyO3 acceleration is intentionally deferred. The implementation should first determine how much latency can be recovered inside the existing Python architecture.

## Non-goals

Do not remove compression observe mode, safe mode, cache-boundary preservation, stable-prefix fail-closed verification, synthetic cache controls, model/context limit checks, thinking-capability routing, quota-fair routing, request finalization, usage accounting, or dashboard observability.

Do not make compression affect route scoring. Request shaping, cache reporting, compression metrics, and synthetic-cache metrics must remain observational with respect to `QuotaFairScorer` and route eligibility unless an existing explicit policy already applies.

Do not add Rust, C extensions, native build steps, or optional binary wheels in this pass.

Do not silently change shipped request mutation defaults. Safe compression should still require the existing configuration mode and policy gates.

## Current hot-path shape

The request path in `src/eggpool/api/proxy_request.py` decodes the request body, validates the model and stream flag, performs context-limit checks, may run transcode preflight, resolves compression policy, conditionally segments the decoded payload, runs compression observation when compression is enabled, applies safe compression when mode is `safe`, constructs `ProxyRequestContext`, and then calls `RequestCoordinator.execute()`.

The current compression path can run both `analyze_compression()` and `apply_safe_compression()` for the same request. In safe mode this means segmentation is followed by an observe-style analyzer pass and then a mutating applier pass. The applier separately walks the same segments and re-runs transform eligibility and transform logic against actual string leaves.

`src/eggpool/transcoder/compression/apply.py` currently deep-copies the full decoded payload before it knows whether any transform will survive thresholds and actually mutate. This is safe but expensive for large chat histories, long tool output, and large tool schemas.

`src/eggpool/transcoder/segmentation.py` builds many small immutable segment objects, performs repeated token estimation, repeatedly serializes subtrees through `_serialize_for_hash()`, and then re-iterates over the segment tuple several times to compute counts, byte totals, token totals, shape descriptors, stable-prefix descriptors, and hashes.

`src/eggpool/request/coordinator.py` currently holds `_select_lock` across thinking classification, JSON parsing of `context.original_body`, token-reservation estimation, routing-plan construction, account selection, several SQLite writes, and optional routing-decision trace construction / persistence. The implementation already keeps upstream I/O outside the lock, but the pre-dispatch lock still serializes a large amount of work.

## Success criteria

A successful pass should produce measurable reductions in dispatch overhead with compression enabled on realistic low-power hardware. Concrete acceptance thresholds should be set after baseline measurement, but the intended target is:

- At least 30 percent reduction in p50 dispatch overhead for safe compression requests with no actual transform opportunity.
- At least 40 percent reduction in transient peak allocation for safe compression requests where no transform applies.
- No statistically significant increase in p95 dispatch overhead when compression is disabled.
- No behavior regression in request bodies emitted upstream for compression disabled, observe mode, safe mode no-op, safe mode applied, transcoded requests, synthetic cache dry-run, and synthetic cache apply mode.
- No loss of persisted aggregate observability for dashboard cards.
- Existing tests pass, and new performance regression tests cover the main no-op and applied-compression paths.

## Phase 1 — Fine-grained dispatch instrumentation

### Objective

Make dispatch overhead legible before changing the code path. The existing overhead recorder should expose enough timing fields to identify whether time is dominated by JSON decode, context checks, preflight transcode, compression policy resolution, segmentation, compression analysis, compression application, selection lock wait, locked selection work, DB writes, routing trace construction/write, upstream connection wait, or finalizer work.

### Code touchpoints

- `src/eggpool/runtime_dispatch.py`
- `src/eggpool/api/proxy_request.py`
- `src/eggpool/request/coordinator.py`
- `src/eggpool/request/finalizer.py`
- `src/eggpool/request/attempt_finalizer.py`
- `src/eggpool/dashboard` runtime/cache stats consumers as needed
- `src/eggpool/api/stats.py` or the relevant stats registration modules
- Tests under `tests/` using the existing `performance`, `request_path`, and `dashboard` markers

### Implementation details

Add a lightweight per-request timing accumulator. Prefer integer nanoseconds from `time.perf_counter_ns()` internally and convert to milliseconds only at reporting boundaries. Avoid dataclass-heavy nested records in the hot path; a small slots dataclass or fixed-key dict is acceptable, but it should not allocate large nested structures per request.

Add explicit timing spans around these regions in `handle_proxy_request()`:

- `auth_ms` if available without perturbing auth behavior.
- `body_read_ms` for `read_body_limited()`.
- `json_parse_ms` for `json.loads(body)`.
- `model_parse_ms` for provider-suffix parsing / known-provider lookup if non-trivial.
- `context_limit_ms` for original body limit enforcement.
- `transcode_preflight_ms` for `_prepare_transcode_preflight()` and translated context-limit checks.
- `compression_policy_ms` for `resolve_compression_policy()`.
- `segmentation_ms` for `segment_request()`.
- `compression_analyze_ms` for `analyze_compression()`.
- `compression_apply_ms` for `apply_safe_compression()`.
- `context_build_ms` for `ProxyRequestContext` construction and model rewrite.
- `coordinator_execute_pre_upstream_ms` once coordinator exposes it.

Add explicit timing spans around these regions in `RequestCoordinator._select_and_persist_attempt()`:

- `selection_lock_wait_ms`: time from immediately before attempting `_select_lock` to lock acquisition.
- `selection_locked_ms`: total time while `_select_lock` is held.
- `thinking_classification_ms`.
- `reservation_estimate_ms`.
- `routing_plan_ms`.
- `circuit_probe_ms`.
- `account_lookup_ms`.
- `db_create_request_ms`.
- `db_create_reservation_ms`.
- `db_create_attempt_ms`.
- `routing_trace_build_ms`.
- `routing_trace_write_ms`.
- `runtime_publication_ms` if there is a separate publication step.

Preserve existing `DispatchOverheadRecorder` semantics. Add new optional fields in a backwards-compatible way so older dashboard code or persisted rows do not break. Prefer sparse fields: if a phase does not run, report `None` or omit the field depending on current stats conventions.

Add an operator-facing aggregation endpoint or extend the existing runtime endpoint so the dashboard can show p50/p95/max for the new spans over a short rolling window. If there is already a runtime dispatch recorder with bucket support, extend that rather than introducing a second metrics stack.

### Tests

Add a unit test verifying disabled compression records no compression spans or zero/none spans without forcing segmentation.

Add a request-path test that exercises observe compression and asserts `segmentation_ms` and `compression_analyze_ms` are present while `compression_apply_ms` is absent or zero.

Add a request-path test that exercises safe compression and asserts `compression_apply_ms` is present.

Add a dashboard/API test verifying the new timing payload is accepted when fields are absent, partially present, and fully present.

### Validation commands

```bash
pytest -m request_path
pytest -m dashboard
pytest -m performance
pytest tests -q
ruff check src tests
pyright
```

If the full performance marker is too slow for local iteration, add a focused benchmark-like regression test that can run quickly in CI and documents expected relative relationships rather than absolute hardware-specific timings.

### Rollback criteria

Rollback this phase if timing collection adds more than low single-digit milliseconds to p50 dispatch overhead when compression is disabled, or if it creates high-cardinality persisted data that materially increases SQLite write pressure.

## Phase 2 — Remove duplicate safe-mode compression work

### Objective

In safe mode, avoid running both the observe analyzer and the mutating applier as independent full passes. Safe mode should still expose aggregate observability, but it should derive that observability from the applier pass when possible.

### Code touchpoints

- `src/eggpool/api/proxy_request.py`
- `src/eggpool/transcoder/compression/analyzer.py`
- `src/eggpool/transcoder/compression/apply.py`
- `src/eggpool/transcoder/compression/__init__.py`
- `src/eggpool/request/finalizer.py`
- Dashboard/cache stats endpoints reading compression observation/result summaries
- Tests for compression observe/safe mode

### Implementation details

Introduce a single-pass safe-mode return shape that can satisfy the fields currently needed by `FinalizationData.compression_observation` and `FinalizationData.compression_result`.

Path A: Add an `observation_summary` or `candidate_summary` field to `CompressionResult`. The applier computes aggregate candidate counts, eligible/suppressed counts, savings totals, reason-code counts, transform counts, warnings, and latency as it walks segments. `handle_proxy_request()` sets `compression_observation` to a lightweight adapter built from the `CompressionResult` rather than calling `analyze_compression()` separately.

Path B: Add `apply_safe_compression(..., collect_observation=True)` returning a composite result such as `SafeCompressionRun(result=..., observation=...)`. This is cleaner if the current finalizer strongly expects the `CompressionObservation` interface.

Prefer Path B only if it avoids awkward duck typing and does not create a large object graph. Prefer Path A if finalization can be updated to read a smaller aggregate summary directly.

In `handle_proxy_request()`, change the control flow:

- If compression disabled: do nothing.
- If compression enabled and mode is `observe`: run `analyze_compression()` exactly as today.
- If compression enabled and mode is `safe`: do not call `analyze_compression()` before apply. Call the safe applier once and derive observation-equivalent aggregate fields from its result.

Keep all existing safe-mode fail-closed rules. Stable-prefix content hash verification must remain exact. Native cache boundary preservation must remain unchanged. If the applier throws, the request must still fall back to the original payload.

Ensure the dashboard can distinguish true observe mode from safe mode. The existing observation mode field currently uses `mode="observe"`; for safe-derived observation, either use `mode="safe"` in a new summary shape or preserve the old field while adding `source="safe_apply"`. Do not mislabel safe-mode derived metrics as an observe-only analyzer run if that would confuse operator interpretation.

### Tests

Add a regression test that monkeypatches or spies on `analyze_compression()` and verifies it is not called when mode is `safe`.

Add safe-mode tests for:

- No eligible candidate: no mutation, original payload forwarded, observation aggregate present.
- Candidate below `min_candidate_tokens`: no mutation, demotion reason counted.
- Candidate below `min_savings_tokens`: no mutation, demotion reason counted.
- Candidate applies: transformed payload forwarded, savings counted, stable-prefix hash preserved.
- Stable-prefix mismatch simulation: original payload forwarded, fallback warning counted.

Add API/dashboard tests verifying safe-mode compression stats still populate cache/compression dashboard cards.

### Validation commands

```bash
pytest -m request_path
pytest -m dashboard
pytest -m performance
pytest tests/test_*compression* -q
ruff check src tests
pyright
```

### Rollback criteria

Rollback if safe-mode dashboard observability disappears, if finalizer columns become inconsistent between observe and safe mode, or if safe compression mutates a payload that the old implementation would have left unchanged.

## Phase 3 — Lazy and path-level copy for safe compression

### Objective

Avoid `copy.deepcopy(payload)` on requests where no transform will apply, and reduce memory pressure when only one or a few volatile string leaves are mutated.

### Code touchpoints

- `src/eggpool/transcoder/compression/apply.py`
- `src/eggpool/transcoder/segmentation.py` path-resolution helpers if sharing is useful
- Tests for safe compression mutation/no-op/fail-closed behavior

### Implementation details

Split `apply_safe_compression()` into two internal stages:

1. Discovery stage: walk eligible segments and transforms against the original payload, collect planned mutations as `(content_path, old_text_digest, new_text, orig_tokens, comp_tokens, reason_code, segment_id)` records. Do not mutate and do not deep-copy during this stage.
2. Apply stage: only if the plan is non-empty, create a mutated payload and apply the planned replacements.

In the first iteration, a full `copy.deepcopy(payload)` only after a non-empty plan is acceptable. That alone removes the large no-op cost.

In the second iteration, replace full deep copy with path-level copy-on-write:

- Implement a helper such as `_copy_with_replacements(payload, replacements)`.
- It should copy only dictionaries/lists on the paths to mutated leaves.
- It should preserve unchanged subtrees by reference.
- It must handle multiple replacements sharing path prefixes without duplicating those prefixes repeatedly.
- It must reject conflicting replacements to the same path unless the code explicitly orders transform chaining.

Be careful about transform chaining. The current implementation mutates the copied payload after each successful transform, so later transforms for the same segment can see the previously transformed text. That behavior can produce multi-transform output. To minimize behavioral risk, preserve existing chaining semantics initially:

- Discovery should simulate chained transforms on a local `current_text` for each segment.
- The final replacement for a path should be the result after all eligible transforms have been applied in the existing transform order.
- Token totals and reason counts should accumulate exactly as today.

Keep stable-prefix content hash verification after applying planned mutations. If the hash changes and `compress_static_prefix` is false, return the original payload with the same fallback semantics as today.

Avoid resolving text from a deep-copied payload during discovery. Use `_collect_text(payload, segment.content_path)` against the original payload, then operate on local strings.

### Tests

Add an identity/no-op test: when no transform applies, the result should be `applied=False`, `transform_count=0`, and `transformed_payload` should be the original payload object or at least should not require deep-copy. If object identity is too strict for public contract, assert via monkeypatch that `copy.deepcopy` is not called.

Add an applied-transform test that verifies unchanged stable-prefix and semi-stable subtrees are preserved when using path-level copy-on-write. If identity preservation is not guaranteed, verify at least equality and stable-prefix content hash preservation.

Add a multi-transform ordering test on the same segment to ensure behavior remains either exactly current or intentionally documented.

Add a conflicting-path test for nested replacements if any future segmenter could emit overlapping paths.

Add a fallback test that forces post-hash mismatch and verifies original payload is returned.

### Validation commands

```bash
pytest tests/test_*compression* -q
pytest -m request_path
pytest -m performance
ruff check src tests
pyright
```

### Rollback criteria

Rollback if transformed output differs from the previous implementation for the same payload/segmentation/policy except where an intentional and documented bug fix is covered by tests. Rollback if copy-on-write introduces aliasing that mutates the original decoded payload.

## Phase 4 — Reduce segmentation allocation, serialization, and token-estimation overhead

### Objective

Keep segmentation capability intact while reducing repeated serialization, synthetic string allocation, repeated token estimation, and multi-pass aggregation.

### Code touchpoints

- `src/eggpool/transcoder/segmentation.py`
- `src/eggpool/transcoder/compression/analyzer.py`
- `src/eggpool/request/limits.py`
- `src/eggpool/api/proxy_request.py`
- Tests for segmentation, context limits, compression analyzer, cache stability, synthetic cache controls

### Implementation details

#### 4.1 Single-pass aggregation in `_build_result()`

Replace repeated generator passes over `segment_tuple` with one accumulator loop. During this loop compute:

- `segment_count_by_kind`
- `stable_prefix_bytes`
- `semi_stable_bytes`
- `volatile_bytes`
- `stable_prefix_estimated_tokens`
- `semi_stable_estimated_tokens`
- `volatile_estimated_tokens`
- `cache_control_present`
- stable-prefix descriptor inputs
- volatile source set

Build the shape descriptor and stable-prefix descriptor from accumulator data rather than re-walking segments several times.

Preserve exact hash output unless intentionally changing only non-contract structural hash details. Because dashboard grouping may depend on hashes, hash stability should be treated as a compatibility contract. Add tests with golden hashes for representative OpenAI and Anthropic payloads before refactoring, then keep them stable.

#### 4.2 Avoid expensive `_serialize_for_hash()` for byte length where direct length is enough

For string leaves, compute byte length from `len(value.encode("utf-8"))` or, if current byte length intentionally includes JSON quotes/escaping, document and preserve that behavior. If preserving JSON-encoded byte length is required, add a helper that fast-paths ASCII strings and only falls back to JSON serialization for escaping-sensitive content.

For tool schemas and arbitrary non-string blocks, keep canonical serialization where needed, but do not serialize the same object more than once in a segment construction branch. Store local `serialized = _serialize_for_hash(obj)` when both byte length and hashing/debugging require it.

#### 4.3 Remove synthetic representative text allocation in analyzer

`_analyze_segment_for_transforms()` currently calls `_segment_text(segment)` when no text hint is present. `_segment_text()` can allocate a large string proportional to estimated tokens. Replace this with a source-aware structural path:

- Add `_segment_tokens(segment, text_hint)` and use it directly for fallback detectors.
- Update detectors so when `text_hint == ""`, they do not need a fake string. They should use `segment.source`, `segment.byte_length`, and `segment.estimated_tokens` directly.
- Only call line-splitting, regex matching, JSON parsing, and `_cheap_tokens(text_hint)` when a real non-empty `text_hint` exists.

Preserve the existing structural fallback estimates for command/tool output repeated lines, log compaction, search compaction, base64/blob detection, JSON minify structural fallback, and stack trace fallback.

#### 4.4 Share token estimates from the decoded request path

The request path currently computes context-limit token estimates and later the coordinator computes reservation estimates from raw bytes. Do not collapse these estimates if they intentionally use different semantics. Instead, carry them explicitly:

- Add fields to `ProxyRequestContext`, for example `estimated_context_input_tokens`, `estimated_reservation_tokens`, and `thinking_requirement`.
- Compute `estimated_reservation_tokens = estimate_reservation_tokens(body)` once in `handle_proxy_request()` after body read.
- Let `_select_and_persist_attempt()` use the precomputed value rather than recomputing from `context.original_body`.
- If context-limit check computes decoded payload estimate, consider exposing it from `check_context_limits()` or a sibling helper so it is not recomputed for translated preflight checks unless the body actually differs.

Keep quota reservation semantics unchanged: reservation tokens should continue using the bounded reservation estimate unless a separate deliberate change is planned.

### Tests

Add golden segmentation tests for:

- OpenAI system/developer/tool schema/prior messages/latest user/tool result.
- Anthropic top-level system, cache_control, tools, tool_result content string, tool_result nested text list, thinking blocks.
- Empty request and parse-failure result.
- Stable-prefix structural hash and request-shape hash stability.

Add analyzer tests verifying no large representative text is allocated. This can be tested by monkeypatching `_segment_text` if retained, or by constructing a huge `estimated_tokens` segment and asserting runtime/allocation behavior indirectly.

Add request-path tests verifying the coordinator uses precomputed reservation tokens and does not parse original body again for thinking classification when a precomputed thinking requirement exists.

### Validation commands

```bash
pytest tests/test_*segmentation* -q
pytest tests/test_*compression* -q
pytest -m request_path
pytest -m dashboard
pytest -m performance
ruff check src tests
pyright
```

### Rollback criteria

Rollback if segmentation hashes change without an explicit migration note and dashboard compatibility update. Rollback if compression candidate classification changes for representative payloads except where tests document a correction.

## Phase 5 — Shorten selection lock and reduce routing-trace write pressure

### Objective

Reduce serialized pre-upstream dispatch time under concurrency by moving pure computation out of `_select_lock` and reducing default routing trace cost on low-power installations.

### Code touchpoints

- `src/eggpool/request/coordinator.py`
- `src/eggpool/routing/router.py`
- `src/eggpool/models/config.py`
- `config.example.toml`
- `docs/` routing/runtime/cache docs as needed
- `src/eggpool/db/repositories.py` or routing decision repository implementation
- Dashboard runtime stats for routing trace mode and dispatch spans

### Implementation details

#### 5.1 Precompute request metadata outside `_select_lock`

Before acquiring `_select_lock`, compute or read from context:

- thinking requirement
- capability policy dict
- estimated reservation tokens
- exclude account set
- static provider/model/protocol fields needed by the routing plan

Move JSON parsing of `context.original_body` out of the lock. Prefer not to parse it in the coordinator at all; `handle_proxy_request()` already has decoded `payload`. Add `thinking_requirement` to `ProxyRequestContext` and set it before coordinator execution.

#### 5.2 Evaluate whether routing-plan construction can move outside `_select_lock`

Inspect `Router.build_routing_plan()` carefully. If it only reads registry/catalog/usage-window state and does not mutate reservations or active counters, move it outside the lock. If it reads mutable state that must be consistent with reservation creation, split it:

- Outside lock: compute eligibility candidates and static capability filtering.
- Inside lock: refresh active/quota-sensitive state, apply final scoring/fairness if necessary, probe circuit breakers, and persist selected attempt.

Do not introduce races where two concurrent requests can over-select the same account beyond existing active-request guardrails.

#### 5.3 Keep only mutation and durable publication inside `_select_lock`

The locked region should ideally contain:

- final candidate selection from an already prepared plan if safe
- circuit-breaker probe / health slot acquisition
- credential availability check if credentials can change concurrently
- account ID lookup if not cached, though this should be warmed or moved out where safe
- request/reservation/attempt creation
- runtime active publication

If account ID lookups hit SQLite on first use, add a startup or post-config-load warming pass for account IDs. Keep lazy fallback for correctness.

#### 5.4 Routing trace pressure reduction

Change low-power default routing trace behavior so full traces are not written for every attempt unless explicitly requested. Options, in preferred order:

1. Keep config default as `sampled` with a conservative sample rate, such as 0.05 or 0.10.
2. Keep current default in existing installs but change `config.example.toml` and onboarding defaults to sampled.
3. If backwards compatibility requires `all`, add a documented `[routing.trace]` low-power profile and dashboard warning.

This should not remove the feature. Operators must still be able to set `mode = "all"` and include score components.

Move `score_components` construction outside the locked region if possible. If it depends only on the chosen plan and selected candidate, it can be built after the durable attempt is created. The persisted trace can be written after releasing `_select_lock` if trace persistence is not required for reservation correctness. If trace write failure currently should not fail dispatch, preserve that behavior or explicitly make it best-effort with debug logging.

#### 5.5 DB write coalescing boundaries

Do not coalesce request/reservation/attempt writes unless the transactional invariants are re-proven. These rows are part of correctness, crash recovery, reservation release, and finalization. The safe optimization is to move non-correctness trace writes out of the critical section, not to weaken durable request accounting.

### Tests

Add concurrency tests that dispatch multiple requests concurrently and assert:

- selected accounts remain valid
- reservations are created exactly once per attempt
- active request counters are published/released correctly
- no request remains pending after success/error
- retry behavior still excludes attempted accounts

Add tests for routing trace modes:

- `off`: no trace rows written
- `sampled` with sample rate 0: no rows written
- `sampled` with sample rate 1: rows written
- `all`: rows written
- include score components false: trace row persists without expensive component JSON

Add a regression test that measures or asserts lock timing fields are populated when instrumentation is enabled.

### Validation commands

```bash
pytest -m request_path
pytest -m performance
pytest tests/test_*routing* -q
pytest tests/test_*coordinator* -q
pytest tests/test_*dashboard* -q
ruff check src tests
pyright
```

### Rollback criteria

Rollback if concurrent dispatch can double-book an account in a way the previous active/reservation guard prevented, if routing fairness changes unintentionally, if retry exclusion breaks, or if trace writes become required for request success.

## Suggested implementation order

Implement Phase 1 first and merge it independently. Do not start optimization refactors until baseline traces can show where time is going.

Then implement Phase 2 and Phase 3 together only if the patch remains reviewable. If the diff becomes large, land Phase 2 first, verify safe mode no longer calls the analyzer, then land lazy-copy as a second PR.

Implement Phase 4 after the compression path is simplified. Segmentation hash stability tests should be added before the refactor so any hash drift is caught immediately.

Implement Phase 5 last because it changes concurrency-sensitive coordinator behavior. The instrumentation from Phase 1 should show whether lock wait/locked time are a meaningful part of the one-second overhead before deeper surgery.

## Performance test matrix

Use representative payload classes:

1. Small native OpenAI request, compression disabled.
2. Small native OpenAI request, compression observe enabled.
3. Small native OpenAI request, compression safe enabled, no transform.
4. Large prior chat history with stable system/tool schema and short latest user turn, safe enabled, no transform.
5. Large tool output / command log in volatile suffix, safe enabled, transform applies.
6. Large base64/blob-like volatile suffix, safe enabled, transform applies or is rejected by thresholds depending on policy.
7. OpenAI-to-Anthropic transcoded request with thinking disabled.
8. OpenAI-to-Anthropic transcoded request with thinking controls present.
9. Synthetic cache dry-run enabled.
10. Synthetic cache apply mode enabled under policy.

For each class, record:

- dispatch overhead p50/p95/max
- request-shaping p50/p95 split by segmentation/analyze/apply
- selection lock wait p50/p95
- selection locked p50/p95
- SQLite writes per request
- transient allocation if available through `tracemalloc` in a focused benchmark
- upstream body equality or expected transformed-body diff

## Documentation updates

Update docs after implementation, not before:

- `docs/cache-compression.md`: describe that safe mode uses a single apply-derived observation pass.
- `docs/cache-compression-troubleshooting.md`: add timing fields and how to interpret high segmentation/analyze/apply time.
- `docs/deployment.md`: document low-power routing trace defaults and how to enable full traces temporarily.
- `config.example.toml`: prefer sampled/off routing trace defaults if changed.
- Dashboard labels: distinguish observe analyzer latency from safe apply latency.

## Final verification checklist

Before marking this line of work complete:

- Compression disabled path does not segment unless another enabled consumer requires segmentation.
- Observe mode still segments and analyzes without mutating the request.
- Safe mode does not call the observe analyzer as a separate pass.
- Safe no-op requests do not deep-copy the full payload.
- Safe applied requests preserve stable-prefix content hash or fail closed.
- Segmentation hash outputs remain stable for golden payloads.
- Routing trace defaults are documented and do not surprise existing operators.
- `_select_lock` timing decreases or is at least measurable under load.
- Dashboard cache/runtime pages remain readable and do not require expanded advanced panels to see the key latency signals.
- All new config behavior is backwards-compatible with existing config files.
- Full test suite, ruff, and pyright pass.

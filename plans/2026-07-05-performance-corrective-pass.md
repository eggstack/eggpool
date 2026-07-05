# EggPool Performance Corrective Pass Plan

## Purpose

This plan is a focused corrective pass after the initial performance-optimization implementation landed. The current repo now contains substantial pieces of the optimization roadmap: perf fixtures, prepared transcode reuse, conditional segmentation, single-pass routing plans, routing trace modes, metrics buffering, and dashboard/cache work. The next pass should tighten correctness and close partial implementations before any additional performance work or dashboard expansion.

The objective is not to broaden the feature set. The objective is to make the existing performance changes safe, predictable, test-covered, and operationally understandable.

## Current state summary

`main` is ahead of the original performance plan and includes these relevant changes:

- New `tests/perf/` fixtures and baseline/regression tests.
- New `eggpool.transcoder.prepared.PreparedTranscode` support.
- Coordinator support for reusing prepared transcode output when feature/protocol state matches and thinking-specific recompute is not required.
- New `eggpool.transcoder.segmentation_guard.should_segment_request()` predicate.
- `ProxyRequestContext.segmentation_not_collected` support for distinguishing skipped segmentation from an empty request.
- New `RoutingPlan` and `Router.build_routing_plan()`.
- Coordinator use of `build_routing_plan()` in the main selection path.
- New `routing.trace` config with `all`, `errors`, `sampled`, and `off` modes.
- Large dashboard/cache-page changes interleaved with the performance work.

The implementation is directionally correct, but several areas need correction before this line of work should be considered closed.

## Non-negotiable invariants for this pass

- No change may alter provider/account eligibility except through existing health/config/capability/quota policy semantics.
- No change may make compression, cache, synthetic-cache, segmentation, or dashboard metrics influence routing score.
- No change may let compression run without the segmentation inputs it requires.
- No change may turn skipped observability into false zero values.
- No change may route provider-suffixed model requests to a different provider.
- No change may remove core request/reservation/attempt/finalization durability.
- No change may weaken thinking/reasoning capability rejection behavior.
- No change may collapse distinct states: disabled, not-collected, empty, error, sampled, and off must remain distinguishable where surfaced.

## Corrective item 1: Resolve compression policy before segmentation gating

### Problem

`handle_proxy_request()` currently decides whether to run segmentation before resolving the per-request compression policy. The guard reads the global compression config from app state, then policy resolution happens afterward. This can skip segmentation when global compression is disabled but a scoped `[[compression.policies]]` override would enable observe/safe compression for the current request.

That is a correctness bug because compression analysis and safe compression require segmentation. The current ordering can cause enabled per-request compression policy to silently do nothing or record misleading defaults.

### Required implementation

1. Move pre-route compression policy resolution above segmentation gating.
2. Build the `CompressionPolicyContext` before calling `should_segment_request()`.
3. Compute `resolved_compression_policy` and `effective_compression_policy` before deciding `_segmentation_needed`.
4. Feed the guard from `effective_compression_policy`, not the raw global config.
5. Preserve current fail-closed semantics: if policy resolution raises, fall back to the global compression config exactly as current code intends.
6. Ensure synthetic-cache enabled state still participates in the guard.
7. Ensure cache observability, if supported by config, is explicitly wired rather than hardcoded to `False` unless intentionally unsupported.
8. Set `segmentation_not_collected=True` on `ProxyRequestContext` whenever segmentation is intentionally skipped.
9. Ensure finalizer/dashboard paths persist/display `not_collected`, not `empty_request`, for skipped segmentation.

### Required tests

Add or update tests covering:

- Global compression disabled, scoped policy enables `observe`: segmentation must run.
- Global compression disabled, scoped policy enables `safe`: segmentation must run and safe compression can see segments.
- Global compression enabled but scoped policy disables compression: segmentation may be skipped if no other consumer needs it.
- Compression policy resolver failure falls back to global behavior without blocking the request.
- With all consumers disabled, segmentation is skipped and persisted/displayed as `not_collected`.
- `empty_request` remains reserved for cases where segmentation actually ran and found no meaningful segments.

### Acceptance criteria

- Every compression path that requires segmentation receives segmentation.
- Skipped segmentation is represented explicitly as `not_collected`.
- The guard’s behavior is driven by the same effective policy the analyzer/applier will use.

## Corrective item 2: Eliminate remaining duplicate JSON encoding in transcode preflight

### Problem

Prepared transcode reuse landed, but `handle_proxy_request()` still encodes the translated preflight payload twice with `json.dumps(...).encode()`: once for context-limit checking and once for `PreparedTranscode.from_preflight_result()`. It also bypasses `encode_json_body()`, which means internal serialization is still inconsistent with the rest of the request path.

`_tool_token_padding()` also serializes each tool with default `json.dumps(tool)`, leaving avoidable allocation and non-compact sizing behavior on large tool schemas.

### Required implementation

1. Encode the translated payload exactly once after preflight succeeds.
2. Use `encode_json_body(preflight.translated_payload)` for the prepared transcode body.
3. For context-limit padding, avoid mutating/reallocating the dispatch body stored in `PreparedTranscode`.
   - Use a separate local `limit_check_body` variable.
   - If padding is needed, append padding only to `limit_check_body`.
4. Pass the compact encoded body into `PreparedTranscode.from_preflight_result()`.
5. Update `_tool_token_padding()` to use compact separators or a helper that matches `encode_json_body()` semantics.
6. Add a regression assertion that the prepared body is exactly the body reused by the coordinator for the common transcode path.
7. Add a test that padding bytes used for context-limit estimation are not included in the actual upstream dispatch body.

### Required tests

- Prepared body is encoded once and reused by coordinator.
- Context-limit padding does not leak into upstream dispatch body.
- OpenAI-to-Anthropic and Anthropic-to-OpenAI upstream JSON remains semantically identical to pre-correction behavior.
- Warning lists remain identical and are not duplicated.
- Large tool-schema request exercises `_tool_token_padding()` without changing routing or provider payload semantics.

### Acceptance criteria

- No duplicate translated-body encoding in `handle_proxy_request()`.
- Prepared dispatch body uses the same compact encoder as the rest of EggPool.
- Padding is only a context-limit estimation artifact.

## Corrective item 3: Tighten prepared transcode warning and thinking behavior

### Problem

The prepared transcode reuse branch extends `context.transcode_context.loss_warnings` with preflight warnings. The fallback branch recomputes warnings. This is the correct rough model, but it needs explicit tests around duplicated warnings and thinking controls because thinking-bearing requests intentionally bypass reuse when provider-specific budget resolution may matter.

### Required implementation

1. Add explicit counters or debug fields for:
   - `prepared_transcode_available`
   - `prepared_transcode_reused`
   - `prepared_transcode_recompute_reason`
2. Define stable recompute reasons:
   - `no_prepared_result`
   - `protocol_or_features_mismatch`
   - `thinking_controls_present`
   - `transcoder_missing`
3. Ensure warnings are appended exactly once on the reuse path.
4. Ensure the fallback path does not accidentally parse an already-translated body when provider-specific thinking recompute should start from the original client intent.
5. Ensure `_extract_original_thinking_budget_inputs()` still reads original client body and is not affected by prepared body reuse.

### Required tests

- Prepared transcode with non-thinking request reuses body and appends warnings once.
- Prepared transcode with thinking controls falls back to recompute.
- Provider-specific thinking budget override still applies after fallback recompute.
- Loss-policy rejection still happens before durable request/reservation/attempt creation.
- Debug/counter fields report reuse vs recompute accurately.

### Acceptance criteria

- Prepared transcode reuse is observably active in the common safe case.
- Thinking-capability semantics remain identical to pre-optimization behavior.
- Warning duplication is prevented by tests.

## Corrective item 4: Remove or formally justify the routing-plan fallback selection path

### Problem

The coordinator now uses `Router.build_routing_plan()` for the main selection path, but `_select_and_persist_attempt()` still contains a fallback branch that calls `select_account()` when `selected_state is None and not ranked_candidates`.

If `RoutingPlan` is authoritative, this fallback is either unreachable or a legacy bypass that reintroduces the old second selection path in edge cases. Both possibilities need correction:

- If unreachable, remove it and cover the intended behavior with tests.
- If reachable by design, document exactly when and why, and prove it does not change fairness, provider filtering, transcode eligibility, capability policy, or missing-account recovery semantics.

### Required implementation

1. Audit `build_routing_plan()` return semantics:
   - `eligible_names=[]`, `ranked_candidates=[]`: no eligible accounts.
   - `eligible_names!=[]`, `ranked_candidates=[]`: scoring/fairness produced no ranked candidates despite eligible states. Decide whether this can be valid.
2. Prefer removing the fallback to `select_account()` and handling `ranked_candidates=[]` as a deterministic error path.
3. If removal is unsafe, add a named helper such as `_select_fallback_after_empty_plan()` with explicit comments and tests.
4. Ensure `last_fairness_decision` / `last_fairness_band_names` compatibility is either preserved or replaced by plan-local metadata everywhere the dashboard reads it.
5. Confirm missing-account recovery is triggered no more than once per attempt.

### Required tests

- Equal-priority equal-quota accounts still rotate under `round_robin`.
- `random` mode remains restricted to the fairness band.
- `off` mode remains score ordered.
- Provider-suffixed requests cannot select a candidate from a different provider.
- Thinking-required requests with no eligible provider reject as before.
- Empty plan with enabled registry states returns the same 502/503 class as before, as appropriate.
- Missing-account recovery scheduling does not multiply after the single-pass plan.

### Acceptance criteria

- There is one authoritative selection path per attempt.
- Any fallback is explicit, tested, and behaviorally equivalent.
- The previous all-traffic-to-one-account regression class remains covered.

## Corrective item 5: Complete or rename `routing.trace.mode = "errors"`

### Problem

`RoutingTraceConfig` exposes `mode = "errors"`, but the current selection-time implementation comments that `errors` behaves like `all` because outcome is not known at selection time. This is safe but misleading: it does not reduce write pressure and does not match operator expectations.

### Required implementation options

Choose one of the following.

Option A: Complete `errors` mode now.

1. Do not persist successful routing traces at selection time when mode is `errors`.
2. Retain enough in-memory trace context on `ProxyRequestContext` or `SelectedAttempt` to write a trace during failure finalization.
3. Persist traces for:
   - Capability rejection.
   - No eligible account.
   - Upstream exhausted.
   - Retryable upstream failure attempt.
   - Non-retryable upstream error.
   - Non-2xx upstream response class.
   - Selection/persistence failure where a request row exists.
4. Ensure failure trace write cannot break finalization.
5. Ensure successful requests write no trace in `errors` mode.

Option B: Rename/defer.

1. Remove `errors` from the accepted config values until implemented, or rename it to `all_until_deferred_errors` only if maintainers explicitly want an internal/development mode.
2. Update docs and config examples to avoid promising a write-pressure reduction that does not happen.

Option A is preferred, but Option B is acceptable if the pass must stay small.

### Required tests

If Option A:

- `all` writes traces for successful requests.
- `off` writes no traces.
- `sampled` writes successful traces deterministically by sample rate.
- `errors` writes no trace for successful requests.
- `errors` writes traces for retryable failure, final exhaustion, capability rejection, and non-retryable upstream error.
- Dashboard/API handles absent successful traces as intentional.

If Option B:

- Config parser rejects `errors`, or docs/config no longer advertise it.
- Existing configs using `all`, `sampled`, and `off` work.

### Acceptance criteria

- The config surface truthfully matches runtime behavior.
- Operators can reason about write-pressure modes without reading coordinator internals.

## Corrective item 6: Separate request-path validation from dashboard/cache-page churn

### Problem

The same commit span includes large dashboard/cache-page changes and request-path performance changes. The dashboard may be improving, but interleaving it with hot-path routing/transcoding work increases review risk and makes regressions harder to isolate.

### Required implementation

1. Create or update test groups/markers so maintainers can run:
   - Request-path correctness suite.
   - Performance baseline suite.
   - Dashboard/cache-page suite.
   - Full suite.
2. Add a short developer note in the plan or docs explaining which tests validate each surface.
3. Ensure request-path tests do not rely on dashboard render internals.
4. Ensure dashboard tests handle intentionally missing/sampled routing traces and `segmentation_status = 'not_collected'`.
5. Ensure cache-page dashboard changes do not reintroduce data-plane queries on the primary DB connection when `worker_threads > 1` is configured.

### Required tests

- A request-path-only test command passes without dashboard assertions.
- Dashboard/cache tests pass with routing trace mode `off`, `sampled`, and `all`.
- Dashboard/cache tests display skipped segmentation as not-collected rather than zero/empty.
- Runtime metrics page does not assume advanced cache metrics are always present.

### Acceptance criteria

- Reviewers can validate data-plane changes independently from dashboard rendering changes.
- Dashboard accurately reflects optional/sampled observability data.

## Corrective item 7: Add CI/status visibility or a documented local verification command

### Problem

The GitHub status API currently shows no commit statuses for `main` through the connector. If the repo intentionally lacks CI, the handoff should include exact local verification commands. If CI exists elsewhere, the status path should be made visible.

### Required implementation

1. Document the minimum local verification command set in `AGENTS.md`, `README.md`, or a developer doc:
   - `pytest` baseline command.
   - perf marker command.
   - dashboard/cache test command.
   - type checking command.
   - lint command.
2. If GitHub Actions is intended, add or repair workflow status reporting for at least lint/type/unit tests.
3. Keep slow perf timing opt-in; do not block normal PRs on noisy wall-clock thresholds.
4. Ensure deterministic behavioral snapshots are test assertions; timing output can remain diagnostic.

### Acceptance criteria

- A handoff implementer can validate the corrective pass without guessing commands.
- If CI is configured, GitHub exposes status/check results for `main` or PR commits.

## Suggested execution order

1. Fix compression policy resolution ordering before segmentation gating.
2. Fix duplicate translated-body encoding and context-limit padding separation.
3. Add prepared-transcode warning/recompute tests.
4. Remove or justify the routing fallback path.
5. Complete or retract `routing.trace.mode = "errors"`.
6. Split/mark request-path vs dashboard validation.
7. Document verification commands or add visible CI.

Do not broaden the optimization surface until items 1 through 5 are closed.

## Verification matrix

Run or add tests for the following before closing this pass.

| Area | Required verification |
| --- | --- |
| Segmentation gating | Resolved policy controls segmentation; skipped state persists as `not_collected` |
| Compression | Observe/safe policies cannot run without required segmentation |
| Prepared transcode | Common non-thinking transcode reuses prepared body; thinking recomputes safely |
| JSON encoding | Prepared body encoded once; padding never reaches upstream dispatch |
| Routing plan | One authoritative selection path; fairness/provider/capability behavior unchanged |
| Trace modes | `all`, `sampled`, `off`, and `errors` semantics match documentation/config |
| Dashboard | Missing/sampled trace and not-collected segmentation are displayed honestly |
| Accounting | Request/reservation/attempt/finalization rows unchanged for core paths |
| Recovery | Stale request finalizer and crash recovery remain independent of trace rows |
| Provider safety | Provider-suffixed model IDs cannot cross provider boundaries |

## Rollback guidance

- If segmentation-order changes cause regressions, temporarily force segmentation on whenever compression config or policy state is ambiguous.
- If prepared transcode reuse mismatches any golden body or warning fixture, disable reuse and keep preflight validation while debugging.
- If single-pass routing diverges from old account selection in a non-understood way, keep `build_routing_plan()` behind a compatibility flag until the divergence is explained.
- If `errors` trace mode cannot be completed safely, remove or de-advertise it rather than leaving a misleading mode.
- If dashboard changes obscure request-path regressions, split the PRs and validate data-plane fixes first.

## Definition of done

This corrective pass is complete when:

- Segmentation decisions are based on resolved effective policy.
- Prepared transcode body generation has one compact encoding path.
- Thinking-bearing transcode requests preserve provider-specific budget behavior.
- Routing selection has one authoritative path or a documented/tested fallback.
- Routing trace config semantics match implementation.
- Dashboard/API surfaces optional observability accurately.
- Request-path and dashboard validation can be run independently.
- Local verification commands or CI statuses are available for handoff reviewers.

# EggPool Performance Optimization Safety Plan

## Purpose

This plan scopes a correctness-preserving performance pass for EggPool. The goal is to reduce avoidable request-path CPU, allocation, lock contention, database write pressure, and log I/O while preserving all routing, provider capability, protocol transcoding, cache-safety, accounting, and dashboard behavior unless a change is explicitly gated by configuration.

This is not a rewrite plan. Each optimization must land behind tests and observable invariants. If an optimization cannot prove it preserves current behavior, it should remain disabled or be split into a narrower change.

## Current repo observations motivating the work

The current codebase already contains several performance-conscious choices:

- `eggpool.cli` keeps a stdlib-only fast-path dispatcher for `croncheck` and `ensure-running`, avoiding full application imports for frequent lightweight invocations.
- Lifespan setup reuses shared HTTP clients for provider/catalog/update-check network paths.
- The database layer supports a separate read-only stats/dashboard connection when `database.worker_threads > 1`.
- Metrics rollups are coalesced in memory before database flush.
- DNS cache and provider-aware HTTPX client pools are already present.

The remaining obvious optimization opportunities are mostly hot-path duplication and excessive always-on observability work:

- Transcoding can run once during preflight and then again in `RequestCoordinator.execute()` for dispatch.
- Canonical request segmentation runs for every request even when compression/synthetic-cache consumers are disabled.
- Routing eligibility is computed once for names and again for ranked failover candidates in the selection path.
- Detailed routing decision traces are persisted on every attempt, even when the operator may only need them for errors or sampled diagnostics.
- SQLite writes for request/reservation/attempt/finalization are serialized through the primary aiosqlite connection; this is correct but can become the local bottleneck under high concurrency.
- Simple request/response middleware uses Starlette `BaseHTTPMiddleware` even though direct ASGI middleware can do the same work with less overhead.
- Routine request and transcode metadata logs run at INFO on the hot path.
- Missing-account recovery scanning runs synchronously from routing candidate construction.

## Non-negotiable invariants

Every implementation phase must preserve these invariants unless a config option explicitly opts into different behavior.

### Routing invariants

- Routing remains quota/load/fairness based, never cost-optimized unless an existing config says otherwise.
- Cache, compression, request-shaping, synthetic-cache, and advisory tuning metrics must not feed `QuotaFairScorer` inputs.
- Healthy configured accounts must not be silently de-pooled by catalog uncertainty.
- Account/model suppression must remain driven by config, credentials, health/circuit state, upstream quota/rate-limit/auth signals, or explicit capability policy.
- Same-tier fairness must continue to rotate effectively tied accounts in the configured fairness scope.
- Provider-suffixed model routing must not leak requests or credentials to the wrong provider.

### Capability invariants

- Thinking/reasoning capability routing must preserve `supported`, `unsupported`, `unknown`, and mixed-provider behavior.
- Unknown capability metadata must not be upgraded to supported by an optimization.
- Provider-specific capability overrides must still take precedence over generic/global metadata.
- Strict capability rejection must still happen before upstream dispatch.
- Lossy transcode rejection under `loss_policy = "reject"` must still happen before durable dispatch where it does today.

### Protocol and payload invariants

- Native OpenAI and native Anthropic requests must preserve upstream payload semantics.
- OpenAI-to-Anthropic and Anthropic-to-OpenAI transcoding must preserve current field mapping, warning generation, and error behavior.
- SSE streaming event order and finalization behavior must not change.
- Unknown upstream response headers must continue to pass through except for configured redaction.
- Provider-bound model rewrite behavior must remain identical.
- JSON serialization changes may produce byte differences only where JSON-normalized semantic equivalence is proven and tests explicitly allow it.

### Cache/compression invariants

- Native provider cache annotations must be preserved byte-for-byte where currently preserved.
- Stable-prefix protection must remain fail-closed.
- Safe compression must only mutate eligible volatile suffix leaves.
- Synthetic cache controls must remain disabled by default and dry-run-first when enabled.
- Context-limit checks must still happen before compression can make an over-limit request appear valid.

### Accounting and durability invariants

- Core request, reservation, attempt, usage, and finalization rows must remain durable enough for accounting, crash recovery, stale-request finalization, and quota reservation cleanup.
- Optimizations may buffer lossy analytics but must not buffer core accounting without a stronger durability design.
- Cost provenance and exactness semantics must not change.
- Stale pending requests and leaked reservations must still be recoverable after process restart.

## Phase 0: Baseline and regression harness

### Goal

Create an executable safety net before changing the hot path. This phase should make performance and behavioral drift visible.

### Implementation tasks

1. Add a benchmark/regression harness under `tests/perf/`, `tests/regression/`, or a similar existing test layout.
2. Use local fake upstreams or `respx` fixtures so tests do not require real provider credentials.
3. Cover these request classes:
   - Native OpenAI non-streaming chat completion.
   - Native OpenAI streaming chat completion.
   - Native Anthropic non-streaming messages request.
   - Native Anthropic streaming messages request.
   - OpenAI client request routed to Anthropic-only upstream via transcoder.
   - Anthropic client request routed to OpenAI-only upstream via transcoder.
   - Provider-suffixed model ID.
   - Unsuffixed collapsed/union model ID when applicable.
   - Same-provider equal-priority multi-account routing.
   - Retryable pre-body upstream failure followed by successful failover.
   - Non-retryable auth failure.
   - Quota/rate-limit cooldown handling.
   - Thinking/reasoning request with supported, unsupported, unknown, and mixed-provider capability states.
   - Large request with tools, long messages, and cache-control-like blocks.
4. Capture these timing metrics, preferably via existing runtime dispatch/DB contention surfaces where possible:
   - Total request wall time.
   - Dispatch overhead before upstream connection.
   - Body parse/encode count if instrumentable.
   - Transcode preflight latency.
   - Dispatch transcode latency.
   - Segmentation latency.
   - Compression analysis/apply latency when enabled.
   - Routing eligibility/scoring latency.
   - Database transaction count per request.
   - Database cumulative/max lock wait deltas.
   - Finalization latency.
5. Capture behavioral snapshots:
   - Selected account name and provider.
   - Attempt count.
   - Upstream URL and auth-header shape, with secrets redacted.
   - JSON-normalized upstream request body.
   - Response status, selected headers, and response body.
   - SSE event sequence for streaming fixtures.
   - Request/reservation/attempt/finalization rows.
   - Routing decision row when enabled.
   - Usage/cost rows and rollup deltas.
   - Capability warning/rejection counters.
6. Add a small CLI or pytest marker for the benchmark subset, for example `pytest -m perf_baseline`, without making slow perf tests mandatory in every PR.

### Acceptance criteria

- A maintainer can run a deterministic regression suite locally without provider credentials.
- Baseline metrics are emitted in a readable JSON or table form.
- Existing behavior is captured before optimization patches land.
- CI can run the functional/golden portion; slow performance timing may remain opt-in.

### Regression risks

- Test harness may overfit current implementation internals. Avoid asserting private timing values as correctness. Use timing as diagnostic output, not pass/fail, except for gross regressions in dedicated perf jobs.

## Phase 1: Reuse transcode preflight work

### Goal

Avoid translating and encoding the same request twice when preflight validation has already produced a translated provider-bound payload.

### Implementation tasks

1. Introduce a small dataclass such as `PreparedTranscode` with fields:
   - `client_protocol`
   - `upstream_protocol`
   - `translated_payload`
   - `translated_body`
   - `warnings`
   - `tool_token_padding`
   - `features_fingerprint` or enough metadata to prove the prepared result is still valid
   - `loss_policy_used`
2. Modify `_prepare_transcode_preflight()` to return `PreparedTranscode` or extend `TranscodePreflightResult` with the already encoded compact body.
3. Attach the prepared result to `ProxyRequestContext`.
4. In `RequestCoordinator.execute()`, before rerunning `select_transcoder().encode_request()`, check whether a prepared translation is present and valid for the selected upstream protocol.
5. Reuse the prepared translated payload/body and warning list when valid.
6. Preserve provider-specific thinking-budget resolution:
   - If selected provider/account capability data requires a different budget mapping than the preflight result, recompute only the affected thinking fields if possible.
   - If narrow recompute is not feasible in the first pass, explicitly fall back to full current transcode for that case and record a debug counter such as `prepared_transcode_reused=false reason=provider_budget_override`.
7. Ensure loss warnings are not duplicated when preflight warnings are reused.
8. Ensure `loss_policy = "reject"` behavior remains pre-dispatch.
9. Add counters or debug fields:
   - `prepared_transcode_available`
   - `prepared_transcode_reused`
   - `prepared_transcode_recompute_reason`

### Required tests

- JSON-normalized upstream body equivalence for OpenAI-to-Anthropic fixtures.
- JSON-normalized upstream body equivalence for Anthropic-to-OpenAI fixtures.
- Warning list equivalence and no duplicated warnings.
- `loss_policy = "reject"` still rejects before request/reservation/attempt creation.
- Provider-specific thinking budget override still applies.
- Native protocol requests do not allocate prepared transcode state.
- Large tool-schema request shows one transcode pass in instrumentation.

### Acceptance criteria

- Transcoded requests perform one full transcode pass in the common case.
- Existing tests and new golden fixtures pass.
- Any cases that still require full recompute are explicit and observable.

### Rollback plan

Keep the old coordinator transcode path intact behind a feature flag or fallback branch until fixtures prove equivalence. If a mismatch is detected, disable reuse and keep preflight validation only.

## Phase 2: Conditional request segmentation

### Goal

Avoid walking/hash-estimating every request body when no enabled feature consumes segmentation output.

### Implementation tasks

1. Add a central predicate, for example `should_segment_request(app_state, endpoint, request_headers, payload_metadata) -> bool`.
2. The predicate should return true when any active consumer needs segmentation:
   - Compression observe mode.
   - Compression safe mode.
   - Synthetic cache controls dry-run or apply mode.
   - Explicit request-shaping/cache observability mode that promises segmentation metrics.
   - Tests or debug config requesting always-on segmentation.
3. Decide compatibility default carefully:
   - If current public behavior promises segmentation/cache observability by default, preserve default behavior and add a documented lower-overhead profile.
   - If maintainers accept a default performance profile, make segmentation conditional but ensure dashboard/API fields clearly report `not_collected` rather than misleading zeros.
4. When segmentation is skipped, ensure finalizers store neutral/explicit status values such as `segmentation_status = 'not_collected'` rather than `empty_request`.
5. Ensure compression/synthetic-cache paths cannot accidentally run without segmentation when they require it.
6. Add runtime metrics for `segmentation_skipped_reason` and `segmentation_latency_ms`.

### Required tests

- With all consumers disabled, request routes and finalizes correctly with segmentation skipped.
- With compression observe enabled, segmentation runs.
- With compression safe enabled, segmentation runs and safe compression still applies only to volatile suffixes.
- With synthetic cache dry-run enabled, segmentation runs post-route where required.
- Dashboard/API stats distinguish disabled/not-collected from zero activity.
- Routing decisions, selected account, upstream body, usage, and cost are unchanged when segmentation is skipped.

### Acceptance criteria

- Segmentation no longer runs on minimal native proxy traffic when no consumer needs it, unless compatibility mode keeps it on.
- No request-shaping feature silently loses required inputs.
- Observability endpoints remain honest about missing data.

### Rollback plan

Keep an `always_segment_requests` or equivalent compatibility knob during rollout. If dashboard or cache reporting semantics regress, default the knob back to current behavior.

## Phase 3: Single-pass routing plan

### Goal

Compute eligibility, ranked failover candidates, fairness metadata, and recovery hints once per attempt.

### Implementation tasks

1. Introduce a dataclass such as `RoutingPlan` containing:
   - `eligible_names`
   - `ranked_candidates`
   - `fairness_decision`
   - `fairness_band_names`
   - `exclusions`
   - `capability_rejection_status`
   - `missing_account_recovery_hints`
2. Add a router method such as `build_routing_plan(...)` that internally performs what `get_eligible_account_names()` and `select_accounts_for_failover()` currently split across two calls.
3. Preserve the existing public methods by delegating to `build_routing_plan()` where appropriate, so external callers/tests keep working.
4. Update `RequestCoordinator._select_and_persist_attempt()` to consume one `RoutingPlan`.
5. Ensure `missing_account_recovery_callback` scheduling remains rate-limited and does not run more often than before.
6. Move heavier missing-account scans into the plan builder only once per attempt. In a later substep, consider moving them to a background task if the synchronous scan still shows up in profiling.
7. Preserve `last_fairness_decision` and `last_fairness_band_names` compatibility until dashboard/API code migrates to plan-local metadata.

### Required tests

- Golden selected account order for equal-priority equal-quota accounts.
- Round-robin fairness still rotates same-tier tied accounts.
- `fairness_mode = random` remains random only within the same fairness band.
- `fairness_mode = off` preserves score-ordered behavior.
- Provider filter, protocol filter, transcode eligibility, local quota mode, health/circuit state, and thinking capability policy produce the same eligibility sets as before.
- Retry-after-first-failure excludes attempted accounts and chooses the same next candidate as before.
- Missing-account recovery is triggered no more often than before for the same provider/model/account state.

### Acceptance criteria

- Request selection path performs one eligibility construction per attempt.
- Selection behavior is identical under deterministic fixtures.
- The recent class of regression where all traffic sticks to one account is covered by tests.

### Rollback plan

Keep old `get_eligible_account_names()` plus `select_accounts_for_failover()` path behind a temporary compatibility flag while validating fairness/account-distribution fixtures.

## Phase 4: Configurable observability write pressure

### Goal

Reduce per-request diagnostic database writes without weakening core accounting and recovery semantics.

### Implementation tasks

1. Add config under a suitable section, for example:

   ```toml
   [observability.routing_trace]
   mode = "all"       # all | errors | sampled | off
   sample_rate = 0.05
   include_score_components = true
   ```

2. Keep `mode = "all"` as the initial compatibility default unless maintainers explicitly choose a production/SBC default.
3. Define semantics:
   - `all`: current behavior.
   - `errors`: persist routing trace only for failed requests, retry exhaustion, capability rejection, or non-2xx upstream response classes.
   - `sampled`: persist successful traces at `sample_rate` plus all errors.
   - `off`: do not persist routing traces except possibly critical safety/audit events.
4. Avoid changing core request/reservation/attempt/finalization writes in this phase.
5. Consider buffering only diagnostic trace writes after the core path is stable. If buffering lands, make it lossy and explicitly non-accounting.
6. Update dashboard/API code to display disabled/sampled trace status clearly.
7. Add runtime counters for trace writes skipped by mode, sampled writes, error-forced writes, and write failures.

### Required tests

- In `all` mode, current routing decision rows are still persisted.
- In `errors` mode, successful requests skip trace rows but failures retain diagnostic rows.
- In `sampled` mode, sample behavior is deterministic under seeded tests.
- In `off` mode, request accounting and finalization still work.
- Dashboard/API handles missing trace rows without treating them as missing requests.
- Stale-request finalizer and crash recovery do not depend on routing trace rows.

### Acceptance criteria

- Operators can reduce diagnostic write volume without compromising billing/accounting/recovery.
- Existing behavior is available through `all` mode.
- Docs explain the difference between core accounting and diagnostic routing traces.

### Rollback plan

Default mode can remain or revert to `all`. Because this phase is config-gated, disabling the new modes should restore current write behavior.

## Phase 5: Low-risk hot-path cleanup

### Goal

Clean up smaller sources of per-request overhead after larger behavior-sensitive changes are protected by tests.

### Implementation tasks

1. Replace simple `BaseHTTPMiddleware` classes with direct ASGI middleware:
   - Body-limit middleware checks `Content-Length` and emits the same protocol-specific 413 JSON body.
   - Header-redaction middleware removes configured response headers after downstream response creation without interfering with streaming.
2. Make routine request logs configurable:
   - Keep access logs under the server/access-log setting.
   - Move routine `Proxying ...` logs to DEBUG or sampled INFO.
   - Keep warnings/errors at current severity.
   - Keep transcode loss warnings at INFO/WARNING only when warnings exist; routine transcode metadata can move to DEBUG.
3. Standardize JSON body generation:
   - Use `encode_json_body()` for internally generated upstream request bodies.
   - Use compact separators for preflight translated-body generation.
   - Avoid repeated `json.dumps()` over large tool schemas where a byte-length heuristic or cached compact serialization is sufficient.
4. Move missing-account recovery scans further off the hot path if Phase 3 metrics still show measurable cost:
   - Option A: plan-local cheap hint only, background supervisor performs scan.
   - Option B: provider/model/account recovery candidate cache with TTL.
5. Add a performance profile in config/docs:
   - Small/SBC profile: stats read connection enabled when dashboard is used, sampled routing trace, lower log volume, conditional segmentation.
   - Debug profile: always-on segmentation, full routing traces, verbose logs.

### Required tests

- Middleware returns identical status/body/header behavior for oversized OpenAI and Anthropic requests.
- Streaming responses still stream; middleware does not buffer the body.
- Header redaction still applies to non-streaming and streaming responses.
- Log-level changes do not hide warnings/errors.
- Compact JSON serialization is JSON-semantically equivalent for provider payloads.
- Missing-account recovery still fires under catalog-stale/missing-account regression fixtures.

### Acceptance criteria

- Per-request middleware overhead is reduced without protocol-visible behavior changes.
- Operators can reduce hot-path log I/O.
- JSON encoding is centralized and easier to audit.

### Rollback plan

ASGI middleware can be reverted to existing `BaseHTTPMiddleware` classes if streaming edge cases appear. Logging and recovery changes should be config-gated.

## Verification matrix for every phase

Each phase should run or update the following matrix before merge.

| Area | Required checks |
| --- | --- |
| Native OpenAI | Non-streaming and streaming request/response parity |
| Native Anthropic | Non-streaming and streaming request/response parity |
| Transcoding | OpenAI->Anthropic and Anthropic->OpenAI body/warning/error parity |
| Streaming | SSE ordering, final usage extraction, disconnect finalization |
| Routing | Provider filter, model suffix filter, same-tier fairness, retry failover |
| Capability | Supported/unsupported/unknown/mixed thinking behavior |
| Cache safety | Stable-prefix hash, native cache annotation preservation, synthetic dry-run |
| Compression | Observe and safe modes; no stable-prefix mutation |
| Accounting | Request/reservation/attempt/finalization rows; usage/cost provenance |
| Recovery | Crash recovery, stale-request finalizer, reservation release |
| Dashboard/API | Missing/sampled observability data displayed honestly |
| Config compatibility | Existing config keeps existing behavior unless explicitly changed |

## Suggested implementation order

1. Land Phase 0 first. Do not optimize before the harness exists.
2. Land Phase 1 next because duplicate transcoding is the highest-value CPU/allocation reduction and can be proven with body/warning golden fixtures.
3. Land Phase 2 after Phase 1 because segmentation interacts with compression/cache observability and should be isolated.
4. Land Phase 3 after routing golden tests are mature. This phase has the highest behavioral risk.
5. Land Phase 4 after the core request path is stable because write-pressure controls affect dashboard/debug expectations.
6. Land Phase 5 last as mechanical cleanup once the behavior-sensitive surfaces are covered.

## Explicit non-goals

- Do not change provider pricing/cost semantics.
- Do not make routing cost-based.
- Do not let compression/cache/synthetic-cache metrics influence routing score.
- Do not replace durable core accounting writes with lossy queues.
- Do not change default provider/account eligibility semantics around catalog uncertainty.
- Do not remove existing debug visibility without a compatibility mode.
- Do not introduce provider-specific behavior that can leak credentials or payloads across providers.

## Handoff checklist

For each PR in this line of work, include:

- Which phase it implements.
- Which invariants it touches.
- Before/after perf harness output.
- Golden fixture diffs, especially upstream body and selected account.
- New config defaults and compatibility behavior.
- Rollback switch or fallback path.
- Any dashboard/API representation changes.

A PR should not merge if it only improves timing but changes routing distribution, capability rejection semantics, provider-bound payloads, cache-prefix safety, or accounting rows outside an explicit and documented config-gated behavior change.

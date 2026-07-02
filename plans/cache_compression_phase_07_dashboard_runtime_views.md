# Phase 7 Plan: Dashboard, Runtime Views, and Operator Diagnostics

Date: 2026-07-02

Parent roadmap: `plans/cache_preserving_deterministic_compression_roadmap.md`

Depends on:

- Phase 1 cache/token observability
- Phase 2 canonical request segmentation
- Phase 3 transcoder cache stability
- Phase 4 observe-mode compression accounting
- Phase 5 safe suffix compression
- Phase 6 policy controls and safety rails

## Summary

Phases 1-6 produce the data operators need: cache counters, segmentation summaries, transcoder cache-boundary preservation/loss, observe-mode compression opportunities, safe-mode compression outcomes, exact stable-prefix content hashes, structural prefix/request-shape hashes, and resolved compression policies.

Phase 7 makes that data operationally usable in the dashboard and runtime APIs. The goal is not cosmetic UI. The goal is to let an operator answer these questions quickly:

- Are providers reporting cache counters, or are values unknown?
- Are stable prefixes being preserved across transcoding and compression?
- How often does observe mode find compression opportunities?
- How often does safe mode actually compress?
- Which transforms are doing useful work?
- Which policies are active?
- Are fail-closed fallbacks occurring?
- Is compression latency bounded and SBC-safe?
- Are cache/compression metrics reporting-only and not influencing routing?

The dashboard must remain safe for public/private dashboard modes: no raw prompts, no raw tool output, no request bodies, no auth headers.

## Non-goals

- Do not expose raw prompts, tool outputs, system messages, or provider responses.
- Do not add semantic inspection or search over request content.
- Do not add routing optimization based on cache/compression metrics.
- Do not implement provider cache-control synthesis; that is Phase 9.
- Do not add expensive per-request aggregation in hot paths.
- Do not require dashboard access for core routing/proxy behavior.

## Data inventory

The dashboard should unify these existing concepts:

### Cache/token observability

- `cache_counter_status`: `reported`, `not_reported`, `unknown_format`.
- `cached_input_tokens`.
- `cache_read_input_tokens`.
- `cache_creation_input_tokens`.
- `cache_write_input_tokens`.
- `input_tokens_reported`, `output_tokens_reported`, `total_tokens_reported`.
- Raw usage JSON status, not raw body display.

### Segmentation

- `segmentation_status`.
- stable/semi-stable/volatile bytes and token estimates.
- `request_shape_hash`.
- structural `stable_prefix_hash`.
- exact stable-prefix content hash where persisted or available in compression summary.
- compressible candidate count.
- protected segment count.

### Transcoder cache stability

- cache-control boundary counts by status: preserved, relocated, dropped unsupported, feature-disabled, invalid, provider-extension dropped.
- provider-visible stable-prefix hash if currently exposed.
- transcoded true/false.
- loss-policy reject/warn counts.

### Compression observe/apply

- `compression_status`.
- `compression_mode`: observe, safe.
- candidate count.
- estimated original/compressed/savings tokens.
- actual original/compressed/savings tokens in safe mode.
- transform counts by reason.
- warning/fallback counts.
- compression latency.
- resolved policy name/source from Phase 6.

## API design

Prefer a small set of focused endpoints rather than one giant dashboard endpoint.

Suggested endpoints:

```text
GET /api/stats/cache-observability
GET /api/stats/canonical-request-segmentation
GET /api/stats/cache-stability
GET /api/stats/compression-observability
GET /api/stats/compression-runtime
GET /api/stats/compression-policies
```

If some endpoints already exist, extend them rather than duplicating. Keep response schemas stable and documented.

### `/api/stats/compression-runtime`

Suggested response:

```json
{
  "window": {"seconds": 3600, "request_count": 1234},
  "mode_counts": {"disabled": 800, "observe": 300, "safe": 134},
  "applied_count": 120,
  "failed_fallback_count": 2,
  "candidate_count": 415,
  "estimated_savings_tokens": 123456,
  "actual_savings_tokens": 65432,
  "latency_ms": {"avg": 3.2, "p50": 1.1, "p95": 12.8, "max": 24.0},
  "transforms": {
    "repeated_line_run": {"applied": 80, "tokens_saved": 30000},
    "log_compaction": {"applied": 20, "tokens_saved": 20000}
  },
  "warnings": {"stable_prefix_hash_mismatch": 2},
  "cache_safety": {
    "stable_prefix_preserved": 132,
    "stable_prefix_mismatch": 2
  }
}
```

### `/api/stats/compression-policies`

Suggested response:

```json
{
  "policy_counts": [
    {
      "policy_name": "global",
      "policy_source": "global",
      "requests": 900,
      "mode_counts": {"observe": 900},
      "applied": 0,
      "failed_fallback": 0,
      "candidate_count": 120
    },
    {
      "policy_name": "opencode-safe",
      "policy_source": "policy:opencode-safe",
      "requests": 300,
      "mode_counts": {"safe": 300},
      "applied": 110,
      "failed_fallback": 1,
      "candidate_count": 150
    }
  ]
}
```

## Dashboard layout

Add or refine the following cards/sections.

### 1. Cache coverage card

Purpose: answer whether providers are surfacing cache counters.

Display:

- Requests with reported cache counters.
- Requests with no cache counters reported.
- Unknown/parse-failure usage formats.
- Known-only cache hit ratio.
- Cached input tokens by provider/model.

Important: never silently treat unknown cache counters as zero. Show unknown separately.

### 2. Segmentation health card

Purpose: answer whether requests are segmentable and whether stable/volatile regions look sane.

Display:

- Segmented/empty/parse-failure counts.
- Avg stable/semi/volatile token estimates.
- Compressible candidate count distribution.
- Protected segment count distribution.
- Top request-shape hashes by count.

Hashes should be short-displayed, e.g. first 8-12 chars, with full value only in JSON/API if already persisted.

### 3. Cache stability card

Purpose: answer whether transcoding is preserving cache-relevant boundaries.

Display:

- Transcoded request count.
- Cache-control preserved/dropped counts.
- Loss-policy rejects.
- Provider-extension drops.
- Stable-prefix provider-visible hash churn by provider/model if available.

### 4. Compression opportunity card

Purpose: answer what observe mode sees.

Display:

- Candidate requests.
- Estimated token savings.
- Transform candidate breakdown.
- Suppression reasons: placement, protected cache boundary, static prefix, below threshold, disabled transform.

### 5. Safe compression outcome card

Purpose: answer what safe mode actually did.

Display:

- Applied count.
- Actual token savings.
- Fallback count.
- Stable-prefix preservation count.
- Transform application breakdown.
- Compression latency avg/p95/max.

### 6. Policy card

Purpose: answer which resolved policies are active.

Display:

- Request count by policy.
- Mode distribution by policy.
- Applied/fallback counts by policy.
- Warnings by policy.

### 7. Routing separation notice

Add a static diagnostic note or card:

> Compression/cache metrics are reporting-only and are not used by same-provider account scoring.

Optionally show the actual active route scorer fields if the runtime stats endpoint already exposes them.

## Implementation tasks

### 1. Confirm existing stats service boundaries

Inspect existing stats modules and dashboard API routes. Reuse the existing style for:

- time-window filters,
- provider/model grouping,
- auth gating,
- JSON response shape,
- dashboard templates/static JS.

Do not create an unrelated stats subsystem.

### 2. Add repository queries

Add efficient aggregate queries over `requests`.

Required query patterns:

- Window-limited rollups by `started_at`.
- Group by provider/model/protocol/policy/mode/status.
- Sum token counters and savings counters.
- Count warning/fallback statuses.

Indexes already added in earlier migrations should be used where possible. If dashboard queries require new indexes, add them only after checking query plans.

Candidate indexes if needed:

```sql
CREATE INDEX IF NOT EXISTS idx_requests_compression_policy_started
    ON requests(compression_policy_name, started_at);

CREATE INDEX IF NOT EXISTS idx_requests_compression_mode_started
    ON requests(compression_mode, started_at);
```

Avoid indexing JSON blobs unless necessary.

### 3. Add stats service methods

Add functions such as:

- `get_cache_observability(...)`
- `get_segmentation_observability(...)`
- `get_cache_stability(...)`
- `get_compression_runtime(...)`
- `get_compression_policy_stats(...)`

Ensure each function returns typed dict/dataclass responses consistent with current API style.

### 4. Add API routes

Wire the stats service into API routes under existing auth gates.

Requirements:

- All endpoints return JSON.
- All endpoints are dashboard-auth-gated consistently with other runtime/stat endpoints.
- Bad window parameters return 400 rather than server errors.
- Missing DB columns from older migrations should produce a clear upgrade-needed error or safe empty data, depending on existing migration policy.

### 5. Update dashboard UI

Extend the runtime/stats dashboard with cards. Keep it simple and readable.

Avoid heavy client-side frameworks if the current dashboard is plain HTML/JS.

Each card should have:

- headline metric,
- small sub-metrics,
- last refreshed timestamp,
- empty-state text.

### 6. Add docs

Update README/architecture docs:

- Document endpoints.
- Define known-only cache hit ratio.
- Explain exact content hash vs structural hash.
- Explain why raw request content is never shown.
- Explain how to diagnose no-op compression: policy disabled, below threshold, non-resolving path, transform disabled, latency budget, fail-closed.

## Test plan

### Unit tests: stats aggregation

Use synthetic DB rows or repository-level fixtures.

- Reported/not_reported/unknown cache statuses counted separately.
- Known-only cache ratio excludes unknown/not-reported denominators unless intentionally specified.
- Compression observed candidates aggregate by transform reason.
- Safe compression applied/fallback counts aggregate correctly.
- Compression latency p50/p95/max computed correctly.
- Policy stats group by policy name/source.
- Empty DB/window returns zeros, not crashes.

### API tests

- Each endpoint returns 200 with auth.
- Each endpoint rejects unauthenticated access if runtime stats are auth-gated.
- Invalid time window returns 400.
- Empty data shape is stable.
- JSON contains no raw request body fields.

### Dashboard tests

If the repo has snapshot or template tests:

- Cards render with empty data.
- Cards render with non-empty data.
- No raw prompt strings appear.

If no dashboard test harness exists, add lightweight route-render tests for the server-rendered page or static asset references.

### Performance tests

- Stats queries over a moderately large synthetic request table stay bounded.
- No dashboard endpoint scans JSON blobs in hot loops.
- Compression runtime stats do not allocate large per-row structures unnecessarily.

## Acceptance criteria

- Operators can see cache-counter coverage by provider/model without confusing missing with zero.
- Operators can see segmentation coverage and stable/volatile token estimates.
- Operators can see cache-control preservation/loss across transcoding.
- Operators can see observe-mode compression opportunity and safe-mode compression outcomes.
- Operators can see fallback/stable-prefix mismatch warnings.
- Operators can see compression latency and confirm it stays within SBC-safe budgets.
- Operators can see resolved policy rollups once Phase 6 policy metadata exists.
- No raw prompts/tool outputs/request bodies are exposed.
- Routing remains unaffected by all dashboard metrics.
- Tests pass.

## Rollback notes

Dashboard/API additions should be safe to disable by hiding links/cards or leaving endpoints unused. Do not remove nullable DB columns during rollback. If a query causes performance issues, gate the specific card behind a config flag while keeping request handling unchanged.
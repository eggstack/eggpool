# Phase 1 Plan: Cache and Token Observability

Date: 2026-07-01

Parent roadmap: `plans/cache_preserving_deterministic_compression_roadmap.md`

## Goal

Add cache/token observability for provider responses without changing request bodies, routing decisions, or provider selection. This phase establishes the measurement foundation required for cache-preserving deterministic compression.

EggPool should be able to answer:

- How many input tokens did the provider report?
- How many output tokens did the provider report?
- How many input tokens were served from provider cache, when exposed?
- How many input tokens were written/created into cache, when exposed?
- Which provider/account/model/protocol path produced those counters?
- Which providers do not expose cache counters?
- Are cache hit ratios materially different across accounts without allowing that fact to skew routing?

## Non-goals

- Do not compress or mutate any request.
- Do not add learned compression or semantic cache behavior.
- Do not alter account routing or route score based on cache counters.
- Do not synthesize provider cache controls.
- Do not require all providers to expose cache fields.

## Current-state assumptions

EggPool already tracks requests, tokens, latency, errors, and estimated costs in SQLite. It also supports OpenAI-compatible and Anthropic-compatible request paths, protocol transcoding, and multi-provider/account routing. This phase extends the accounting model rather than replacing it.

## Implementation tasks

### 1. Inventory current usage parsing

Find all code paths that parse provider usage from non-streaming and streaming responses. Expected areas include:

- OpenAI Chat Completions response handling.
- Anthropic Messages response handling.
- Transcoded usage/cost preservation.
- Streaming final usage events, if currently captured.
- Error/partial-response paths where usage is absent.
- SQLite request/usage recording.
- Dashboard/stats aggregation.

Document the current internal usage structure before changing it.

### 2. Add normalized cache usage model

Introduce a provider-neutral internal structure for usage counters. The exact module location should follow the existing stats/models layout.

Suggested fields:

```python
class NormalizedUsage:
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    cached_input_tokens: int | None
    cache_read_input_tokens: int | None
    cache_creation_input_tokens: int | None
    cache_write_input_tokens: int | None
    reasoning_tokens: int | None
    raw_usage: dict[str, Any] | None
    cache_counter_status: Literal["reported", "not_reported", "unknown_format"]
```

Notes:

- Keep `None` distinct from `0`. `None` means provider did not report or EggPool could not parse the field.
- Preserve raw provider usage for debugging if the current database/logging policy allows it.
- If a provider reports cache creation and cache read separately, keep both.
- If a provider only reports a generic cached input counter, fill `cached_input_tokens` and leave read/write-specific fields null.

### 3. Implement provider-specific extraction helpers

Add explicit extraction helpers for known usage shapes.

OpenAI-style likely fields to handle:

- `usage.prompt_tokens`
- `usage.completion_tokens`
- `usage.total_tokens`
- `usage.prompt_tokens_details.cached_tokens`
- reasoning token detail fields if already tracked.

Anthropic-style likely fields to handle:

- `usage.input_tokens`
- `usage.output_tokens`
- `usage.cache_creation_input_tokens`
- `usage.cache_read_input_tokens`

Generic fallback:

- Preserve current token parsing behavior.
- Mark cache counters as `not_reported` or `unknown_format`.
- Never fail a request because cache counters cannot be parsed.

### 4. Extend request/account stats storage

Add a SQLite migration that preserves existing data and supports nullable cache fields.

Suggested new columns on the request/usage table, adjusted for the actual schema:

- `input_tokens_reported`
- `output_tokens_reported`
- `total_tokens_reported`
- `cached_input_tokens_reported`
- `cache_read_input_tokens_reported`
- `cache_creation_input_tokens_reported`
- `cache_write_input_tokens_reported`
- `cache_counter_status`
- `request_shape_hash` nullable placeholder for later phases
- `stable_prefix_hash` nullable placeholder for later phases
- `transcoded` boolean/int if not already stored
- `client_protocol` and `upstream_protocol` if not already queryable

Migration constraints:

- Existing databases must migrate cleanly.
- Existing dashboard/stats queries must keep working.
- Unknown fields should default to null, not zero.

### 5. Preserve routing behavior

Audit the route scoring and account fairness path after adding metrics. Cache counters must not enter account eligibility, route score, or fairness rotor behavior in this phase.

Add a targeted test if practical:

- Two same-provider accounts with different synthetic cached token ratios remain routed according to existing quota/fairness rules.
- Metrics are recorded after the fact but do not influence the selected account.

### 6. Add stats API support

Extend stats responses to expose cache fields in a minimal, backward-compatible way.

Suggested aggregate fields:

- `requests_total`
- `input_tokens_total`
- `output_tokens_total`
- `cached_input_tokens_total`
- `cache_read_input_tokens_total`
- `cache_creation_input_tokens_total`
- `cache_hit_ratio_known_only`
- `cache_counter_reported_requests`
- `cache_counter_unknown_requests`

Aggregation levels:

- global recent window
- provider
- account
- model
- provider/account/model if existing stats API supports this shape

Avoid claiming a zero cache hit ratio when providers do not expose counters. Display "unknown" or provide an explicit known/unknown denominator.

### 7. Add dashboard display

Add a small cache observability section before any compression UI exists.

Minimum dashboard additions:

- Cached input tokens reported.
- Cache hit ratio over recent requests where counters are known.
- Cache counter coverage: number/percentage of requests with parseable cache fields.
- Breakdown by provider/account/model.

Do not add optimization recommendations yet. This phase should display facts, not prescribe routing or compression changes.

### 8. Logging and diagnostics

Add structured debug logs for usage normalization failures. Logs should include provider/model/protocol and a compact reason, not full prompt bodies.

Examples:

- `cache_usage_not_reported`
- `cache_usage_unknown_shape`
- `usage_parse_missing_final_stream_event`
- `usage_parse_preserved_raw_only`

### 9. Tests

Required tests:

- OpenAI-style usage with `prompt_tokens_details.cached_tokens` normalizes correctly.
- OpenAI-style usage without cached fields returns `cache_counter_status = "not_reported"`.
- Anthropic-style usage with `cache_creation_input_tokens` and `cache_read_input_tokens` normalizes correctly.
- Generic usage without cache fields remains backward compatible.
- Missing usage does not fail request accounting.
- SQLite migration succeeds from a pre-change schema fixture if such fixtures exist.
- Dashboard/stats aggregation treats null as unknown, not zero.
- Routing test confirms cache counters do not influence same-provider account selection.

## Acceptance criteria

- Existing tests pass.
- A request with provider-reported cache counters persists those counters.
- A request without cache counters persists nulls and an explicit status.
- Stats APIs can report cache hit ratios using only known counter requests.
- Dashboard exposes cache observability without enabling compression.
- Route scoring/fairness behavior is unchanged.

## Manual verification

1. Start EggPool with a test config containing at least two same-provider accounts.
2. Send repeated requests with a stable long prefix to a provider that reports cache counters.
3. Confirm cache counters appear in request stats and dashboard.
4. Confirm account distribution remains consistent with existing fairness/quota behavior.
5. Send requests to a provider that does not report cache counters.
6. Confirm the dashboard shows unknown/missing cache coverage rather than zero cache hit.

## Rollback notes

This phase should be low risk because it is observational. If issues occur, disable dashboard display of cache fields while keeping nullable database columns. The migration should not require destructive rollback.

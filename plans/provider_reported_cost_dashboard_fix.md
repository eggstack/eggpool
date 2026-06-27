# Provider-Reported Cost Dashboard Fix Plan

## Context

The dashboard currently displays a very large `Total cost` value, for example `$3,321.66`, while OpenCode Go's own reporting indicates current API spend is closer to `$12`. The likely cause is that EggPool's dashboard sums the persisted `requests.cost_microdollars` field, but that field is populated by the local cost-estimation/fallback pipeline rather than by an authoritative provider-reported cost.

The current data path is:

1. `render_overview()` reads `summary["total_cost_microdollars"]` and formats it as `Total cost`.
2. `StatsService.get_summary()` / `fetch_summary()` aggregate `SUM(requests.cost_microdollars)`.
3. `RequestFinalizer.finalize()` computes `cost_microdollars` from token usage through `CostCalculator.calculate_cost()`.
4. When pricing is unavailable or the derived cost is zero, the finalizer can fall back to `selected.estimated_microdollars` and may floor estimated cost to at least the reservation estimate.
5. `QuotaEstimator.estimate_cost()` may produce large conservative reservation estimates for unknown models or models without reliable pricing.

That conflates three different values:

- authoritative provider-reported actual API cost;
- locally derived cost from known provider/model token rates;
- conservative local reservation estimate used for routing and quota pressure.

The dashboard should show actual spend first, not reservation pressure. The estimator should remain conservative for routing, but it should not inflate the primary financial metric.

## Goals

- Prefer OpenCode Go/provider-reported request cost when the upstream response exposes it.
- Preserve local derived cost as a fallback when provider-reported cost is unavailable but pricing is trusted.
- Preserve reservation estimates for routing, failover scoring, and active-reservation visibility, but do not let reservation estimates masquerade as actual spend.
- Make the dashboard explicit about cost source/exactness so the operator can distinguish `provider_reported`, `derived`, `partial`, `estimated`, and `unknown` spend.
- Provide a migration path that does not break existing databases.
- Add targeted tests that reproduce the `$3,321.66` versus `~$12` class of discrepancy.

## Non-goals

- Do not remove quota estimation or reservations. They are still useful for routing fairness and avoiding overuse.
- Do not require OpenCode Go to be the only provider with provider-reported cost support. Implement this generically enough that future providers can feed the same field.
- Do not trust ambiguous arbitrary response fields as cost unless the parser can identify the unit or provider contract clearly.
- Do not rewrite the whole stats subsystem.

## Proposed Data Model

Add columns to `requests`:

```sql
ALTER TABLE requests ADD COLUMN provider_cost_microdollars INTEGER;
ALTER TABLE requests ADD COLUMN provider_cost_source TEXT;
ALTER TABLE requests ADD COLUMN local_cost_microdollars INTEGER;
ALTER TABLE requests ADD COLUMN local_cost_exactness TEXT;
```

Recommended semantics:

- `provider_cost_microdollars`: authoritative upstream/provider-reported cost, if present.
- `provider_cost_source`: short source label such as `opencode_go:usage.cost_usd`, `opencode_go:usage.billing.cost_usd`, or `openai_compatible:usage.cost_microdollars`.
- `local_cost_microdollars`: EggPool-derived cost from pricing snapshots or fallback estimate.
- `local_cost_exactness`: exactness of the local calculation before provider override.
- Existing `cost_microdollars`: canonical displayed/accounted actual cost. After this fix, it should be assigned by precedence:
  1. `provider_cost_microdollars`, exactness `provider_reported` or `exact`;
  2. trusted local derived/partial cost;
  3. local estimated cost only when no better value exists;
  4. zero with `unknown` when no usage/cost can be established.

This keeps existing queries working while adding auditability.

Alternative lower-risk schema:

If the maintainer wants a smaller migration, add only:

```sql
ALTER TABLE requests ADD COLUMN provider_cost_microdollars INTEGER;
ALTER TABLE requests ADD COLUMN provider_cost_source TEXT;
```

and keep using `cost_microdollars` as the canonical cost. The fuller schema is preferred because it lets the dashboard and diagnostics explain discrepancies.

## Exactness Taxonomy

Current code already uses `exact`, `derived`, `partial`, `estimated`, and `unknown` counts. Extend the accepted vocabulary to include `provider_reported`.

Suggested precedence order for display trust:

1. `provider_reported`: upstream explicitly reported billed/request cost.
2. `exact`: exact cost from provider-specific response metadata, if ever added separately.
3. `derived`: calculated from trusted provider-specific price snapshot and usage.
4. `partial`: some categories priced by trusted rates, other categories filled by category fallback.
5. `estimated`: local heuristic/reservation-style estimate.
6. `unknown`: no meaningful cost.

If changing the vocabulary broadly is too invasive, map provider-reported cost to existing `exact`, but retain `provider_cost_source` so the distinction is still available.

## Implementation Plan

### Phase 1: Add Provider Cost Parsing

Create a small utility module, for example `src/eggpool/proxy/cost_reporting.py`, responsible only for parsing provider-reported cost from response payloads.

Expose a simple dataclass:

```python
@dataclass(frozen=True)
class ProviderReportedCost:
    microdollars: int
    source: str
```

Expose parsing helpers:

```python
def extract_provider_reported_cost(
    data: dict[str, Any],
    *,
    provider_id: str | None,
    protocol: str,
) -> ProviderReportedCost | None:
    ...
```

The parser should inspect likely OpenAI-compatible locations, with explicit unit handling. Good initial field candidates:

- `usage.cost_microdollars`
- `usage.cost_micros`
- `usage.total_cost_microdollars`
- `usage.total_cost_micros`
- `usage.cost_usd`
- `usage.total_cost_usd`
- `usage.billing.cost_usd`
- `usage.billing.total_cost_usd`
- `billing.cost_usd`
- `billing.total_cost_usd`

For OpenCode Go specifically, add provider-aware aliases once the exact observed response shape is confirmed. The code should be tolerant but not gullible: only dollar fields with `_usd`/`usd` naming should be converted from dollars, and only microdollar/micro/micros fields should be treated as microdollars. Avoid treating a bare `usage.cost` as dollars unless the provider-specific OpenCode Go contract confirms it.

Add robust numeric coercion:

- Accept integers, floats, Decimals, and numeric strings.
- Reject booleans.
- Reject negative, NaN, and infinity.
- Convert dollars to microdollars with `round(dollars * 1_000_000)`.
- Return `None` for absent/unparseable values without failing request finalization.

### Phase 2: Extend Usage Result Objects

Update `src/eggpool/proxy/usage.py`:

```python
@dataclass
class StreamUsageResult:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    reasoning_tokens: int = 0
    thinking_characters: int = 0
    reported_cost_microdollars: int | None = None
    reported_cost_source: str | None = None
    is_complete: bool = False
```

Update `IncrementalSSEObserver._merge_usage()` so reported costs merge safely. Recommended rule:

- If incoming usage has `reported_cost_microdollars`, replace the accumulated value and source with the incoming value.
- If multiple chunks report cost, use the last complete usage/cost event, matching how final stream usage usually supersedes intermediate deltas.
- Do not sum reported cost across chunks unless provider documentation proves per-chunk incremental cost semantics.

Update `OpenAIStreamUsageExtractor.extract()` and `AnthropicStreamUsageExtractor.extract()` to optionally call the provider-cost parser. These extractor classes currently know only protocol, not provider. Either:

- add optional `provider_id` to `IncrementalSSEObserver(protocol, provider_id=None)` and pass it into extractor constructors; or
- let the parser run provider-agnostic based on explicit field units only.

Preferred: pass `provider_id` through so OpenCode Go-specific shapes can be supported without overbroad parsing.

### Phase 3: Extract Provider Cost for Non-Streaming Responses

Update `RequestCoordinator._extract_non_stream_usage()`:

- Add `provider_id: str | None = None` parameter.
- After loading the response JSON object, call `extract_provider_reported_cost(data_dict, provider_id=provider_id, protocol=protocol)`.
- Populate `StreamUsageResult.reported_cost_microdollars` and `.reported_cost_source`.

Update the call site in `_execute_non_streaming()`:

```python
usage = self._extract_non_stream_usage(context.protocol, body, provider_id=selected.provider_id)
```

Update streaming construction:

```python
observer = IncrementalSSEObserver(context.protocol, provider_id=selected.provider_id)
```

Then ensure all finalizer calls pass the provider-reported cost fields from `usage` / `usage_result` into `FinalizationData`.

### Phase 4: Extend FinalizationData and Cost Precedence

Update `src/eggpool/request/finalizer.py`:

```python
@dataclass
class FinalizationData:
    ...
    provider_cost_microdollars: int | None = None
    provider_cost_source: str | None = None
```

In `RequestFinalizer.finalize()`:

1. Compute local cost exactly as today and store it in local variables:
   - `local_cost_microdollars`
   - `local_exactness`
2. If `data.provider_cost_microdollars is not None`, set canonical:
   - `cost_microdollars = data.provider_cost_microdollars`
   - `exactness = "provider_reported"` or `"exact"`
3. Else use local cost as today, but revise reservation fallback behavior:
   - Keep local `estimated` fallback if no cost can be calculated and billable work likely occurred.
   - Do not floor provider-reported or derived actual cost to reservation estimate.
   - Strongly consider removing this block for canonical persisted cost:
     ```python
     if exactness == "estimated" and cost_microdollars < selected.estimated_microdollars:
         cost_microdollars = selected.estimated_microdollars
     ```
   - If routing still needs conservative accounting, record reservation pressure elsewhere rather than inflating `requests.cost_microdollars`.
4. Pass provider/local cost audit fields to the repository update method.

Recommended finalizer logic sketch:

```python
local_cost_microdollars = 0
local_exactness = "unknown"

if calculator and has_usage:
    local_cost_microdollars, local_exactness = await calculator.calculate_cost(...)

if data.provider_cost_microdollars is not None:
    cost_microdollars = data.provider_cost_microdollars
    exactness = "provider_reported"
elif local_cost_microdollars > 0 or local_exactness in {"derived", "partial"}:
    cost_microdollars = local_cost_microdollars
    exactness = local_exactness
elif may_have_billable_work:
    cost_microdollars = local_cost_microdollars or selected.estimated_microdollars
    exactness = "estimated"
else:
    cost_microdollars = 0
    exactness = "unknown"
```

Important: if retaining a reservation fallback for cases with no usage, keep it visibly marked as `estimated` and do not describe it as actual provider spend.

### Phase 5: Repository and Migration Changes

Update `RequestRepository.finalize_if_pending()` signature to accept:

```python
provider_cost_microdollars: int | None = None
provider_cost_source: str | None = None
local_cost_microdollars: int | None = None
local_cost_exactness: str | None = None
```

Update the `UPDATE requests SET ...` statement to write the new columns.

Add a database migration under `src/eggpool/db/migrations.py` or the repo's existing migration structure. The migration must be idempotent with the current migration runner style.

Migration details:

- Add nullable columns only; do not rewrite historical data by default.
- Existing historical `cost_microdollars` remains as-is.
- Optionally backfill `local_cost_microdollars = cost_microdollars` and `local_cost_exactness = exactness` for existing rows to make diagnostics easier.
- Do not fabricate `provider_cost_microdollars` for historical rows.

### Phase 6: Stats and Dashboard Updates

Update summary/account/model/timeseries queries to count `provider_reported` exactness.

Minimal changes:

- Add `provider_reported_count` to `fetch_summary()`, `fetch_account_stats()`, and `fetch_model_stats()`.
- Include `provider_reported_count` in `_empty_summary()` and rollup summary equivalents.
- Update dashboard exactness displays so provider-reported rows are visible.

Better dashboard wording:

- Rename tooltip for `Total cost` from generic “current cost exactness pipeline” to: “Total canonical request cost in the selected period. Provider-reported costs are preferred; locally derived or estimated costs are used only when provider cost is unavailable.”
- If a significant fraction of cost is `estimated`, show the existing pricing warning banner.
- Extend the pricing warning to include dollar contribution by exactness class, not only request-count fraction. Request-count fraction can understate the issue when a few huge estimated requests dominate spend.

Add a small diagnostic panel or CLI command if appropriate:

```sql
SELECT
  exactness,
  COUNT(*) AS requests,
  SUM(cost_microdollars) AS cost_microdollars,
  SUM(provider_cost_microdollars) AS provider_cost_microdollars,
  SUM(local_cost_microdollars) AS local_cost_microdollars
FROM requests
WHERE started_at >= ? AND started_at < ?
GROUP BY exactness
ORDER BY cost_microdollars DESC;
```

This directly answers: “How much of my displayed total is provider-reported versus estimated?”

### Phase 7: Rollup Handling

The rollup code currently aggregates `cost_microdollars`. That can remain correct if `cost_microdollars` is canonical actual cost after the finalizer change.

However, for transparency, consider adding rollup fields later:

- `provider_cost_microdollars`
- `local_cost_microdollars`
- `provider_reported_count`
- `estimated_count`

For this fix, it is acceptable to keep rollups unchanged if tests show canonical `cost_microdollars` is correct. If rollups already include exactness fields, extend them now to avoid losing source breakdown on long windows.

### Phase 8: Tests

Add focused unit tests for cost parsing:

- Parses `usage.cost_microdollars: 12000000` as 12 dollars.
- Parses `usage.cost_usd: 12.0` as 12 dollars.
- Parses string dollar values like `"12.34"`.
- Rejects `usage.cost: 12` unless provider-specific support is explicitly implemented.
- Rejects negative, boolean, NaN, infinity.
- Preserves source path.

Add usage extraction tests:

- Non-streaming OpenAI-compatible response with usage tokens and `usage.cost_usd` produces `StreamUsageResult.reported_cost_microdollars`.
- Streaming final usage chunk with cost produces reported cost.
- Streaming multiple chunks use last reported complete cost, not sum of intermediate costs.

Add finalizer tests:

- Provider-reported cost overrides larger local estimate/reservation.
- Provider-reported cost overrides lower local estimate.
- Derived local cost is used when provider cost is absent.
- Reservation fallback remains marked `estimated` and does not override provider cost.
- `provider_cost_microdollars`, `provider_cost_source`, `local_cost_microdollars`, and `local_cost_exactness` are persisted.

Add dashboard/stats tests:

- `fetch_summary()` total cost sums canonical `cost_microdollars`.
- Exactness counts include provider-reported rows.
- A synthetic dataset with one request having provider cost `$12` and reservation `$3,321.66` displays `$12`, not `$3,321.66`.

### Phase 9: Operational Diagnostics for Existing Users

Add documentation or a one-off troubleshooting command/query to help users determine whether their current database has inflated historical costs.

Suggested CLI output:

```text
Cost source summary, last 30d:
provider_reported: $12.03 across 42 requests
estimated: $3,309.63 across 4 requests
partial: $0.00 across 0 requests
derived: $0.00 across 0 requests
unknown: $0.00 across 12 requests
```

If implementing a CLI command is too much for this pass, add a docs section with SQL snippets:

```sql
SELECT
  exactness,
  COUNT(*) AS n,
  ROUND(SUM(cost_microdollars) / 1000000.0, 2) AS dollars,
  ROUND(AVG(cost_microdollars) / 1000000.0, 4) AS avg_dollars
FROM requests
GROUP BY exactness
ORDER BY SUM(cost_microdollars) DESC;
```

```sql
SELECT
  id,
  started_at,
  provider_id,
  model_id,
  input_tokens,
  output_tokens,
  reserved_microdollars / 1000000.0 AS reserved_dollars,
  cost_microdollars / 1000000.0 AS cost_dollars,
  exactness
FROM requests
ORDER BY cost_microdollars DESC
LIMIT 25;
```

## Backward Compatibility

- Existing dashboards continue to work because `cost_microdollars` remains present and canonical.
- Existing rows retain their prior cost values. The migration should not attempt to infer provider-reported cost historically.
- Existing exactness queries need to tolerate the new `provider_reported` exactness value.
- Old databases without new columns should migrate cleanly at startup.

## Risk Areas

### Ambiguous cost units

The largest implementation risk is treating a provider field as dollars when it is actually cents, tokens, credits, or some other unit. Avoid parsing ambiguous bare `cost` fields except under provider-specific, tested response contracts.

### Streaming cost semantics

Some providers may emit cumulative usage/cost only in the final chunk. Others may emit incremental deltas. Default to final-value semantics and only sum if provider docs prove deltas.

### Rollup source visibility

If rollups aggregate only canonical cost, long-window dashboard totals will be correct, but exactness/source breakdown may be incomplete. This is acceptable for the first fix if documented, but should be closed later.

### Historical inflated values

This fix prevents new inflated records. It does not automatically repair old records unless there is enough durable provider-reported cost metadata in old responses, which EggPool likely did not persist. If historical correction is needed, provide an explicit operator command that either recalculates from external OpenCode Go reports or marks historical estimated rows as non-authoritative.

## Acceptance Criteria

- A request whose upstream response reports `usage.cost_usd = 12.0` persists `cost_microdollars = 12_000_000`, regardless of a much larger reservation estimate.
- A request with provider-reported cost records `provider_cost_microdollars` and `provider_cost_source`.
- Local calculated cost is still stored in `local_cost_microdollars` / `local_cost_exactness` when available.
- Dashboard `Total cost` uses canonical actual cost and no longer displays reservation-floored estimates when provider-reported cost exists.
- Exactness/source counts show provider-reported requests distinctly or map them to exact with a visible provider source.
- Existing tests pass.
- New parser, finalizer, and dashboard regression tests pass.

## Suggested File Touch List

- `src/eggpool/proxy/cost_reporting.py` — new parser module.
- `src/eggpool/proxy/usage.py` — extend `StreamUsageResult` and extractors.
- `src/eggpool/proxy/sse_observer.py` — merge provider-reported cost and pass provider ID.
- `src/eggpool/request/coordinator.py` — pass provider ID into usage extraction and finalizer data.
- `src/eggpool/request/finalizer.py` — canonical cost precedence and audit field handling.
- `src/eggpool/db/repositories.py` — persist provider/local cost fields.
- `src/eggpool/db/migrations.py` — add nullable request cost audit columns.
- `src/eggpool/stats/queries.py` — include provider-reported exactness/source counts.
- `src/eggpool/stats/service.py` — include new summary fields and rollup fallback handling.
- `src/eggpool/dashboard/render.py` — update `Total cost` tooltip/exactness display/warnings.
- `tests/` — parser, usage extraction, finalizer, migration, and dashboard summary regressions.

## Recommended Implementation Order

1. Add migration and repository fields without changing behavior.
2. Add parser and unit tests.
3. Extend usage result and coordinator extraction paths.
4. Extend finalizer data and cost precedence.
5. Add finalizer regression tests showing provider cost overrides reservation estimate.
6. Update stats exactness counts and dashboard wording.
7. Add operational diagnostic SQL/docs or CLI helper.
8. Run full test suite and manually exercise a live OpenCode Go response.

## Manual Verification Scenario

After implementation, run one known OpenCode Go-backed request that previously inflated cost. Then query:

```sql
SELECT
  id,
  provider_id,
  model_id,
  provider_cost_microdollars / 1000000.0 AS provider_cost_dollars,
  local_cost_microdollars / 1000000.0 AS local_cost_dollars,
  reserved_microdollars / 1000000.0 AS reserved_dollars,
  cost_microdollars / 1000000.0 AS canonical_cost_dollars,
  exactness,
  provider_cost_source,
  local_cost_exactness
FROM requests
ORDER BY id DESC
LIMIT 5;
```

Expected result: `canonical_cost_dollars` equals `provider_cost_dollars` when provider cost is present, while `reserved_dollars` may remain much higher. The dashboard should match the sum of `canonical_cost_dollars`, not the sum of reservation estimates.

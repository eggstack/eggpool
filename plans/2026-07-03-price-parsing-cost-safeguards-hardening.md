# Price Parsing and Cost Safeguard Hardening Plan

Date: 2026-07-03
Status: handoff plan
Scope: provider-neutral pricing parser hardening, request-cost safeguard tightening, tests, observability, and historical repair tooling.

## Problem statement

EggPool currently supports multiple pricing sources: operator overrides, provider metadata, provider pricing endpoints, static catalogs, and external catalogs. These sources are not unit-consistent. A bare numeric price may represent any of the following:

- dollars per token
- dollars per 1K tokens
- dollars per 1M tokens
- already-normalized microdollars per 1M tokens

The dangerous failure mode is unit inflation: treating a provider-native `$0.20 / 1M` style value as `$0.20 / token` inflates derived spend by 1,000,000x. The dashboard then appears to show impossible spend, such as a small number of requests accumulating tens of thousands of dollars.

The current resolver already contains partial sibling-aware logic for nested `pricing.prompt` / `pricing.completion` style fields, but this is not yet applied consistently across every pricing field shape. In particular, cache-read/cache-write fields in nested `pricing` dictionaries still default bare values to per-token semantics, and some top-level aliases bypass the same unit-inference layer. Separately, the request finalizer currently accepts any positive local calculator result as canonical cost, even when the calculator marked that result as `estimated` after detecting an implausible cost-per-token.

This plan hardens the system without adding provider-specific branches for MiniMax or any other provider. The core requirement is that every pricing source flows through a generic unit-aware parser, every local price snapshot is trust-gated before use, and no suspicious locally derived cost can become canonical dashboard spend.

## Goals

1. Make pricing parsing provider-neutral, cluster-aware, and unit-explicit.
2. Avoid catastrophic overestimation from ambiguous bare numeric price fields.
3. Preserve compatibility with OpenRouter-style per-token metadata and Anthropic/provider-native per-million metadata.
4. Treat cache price fields with the same unit-inference discipline as input/output prices.
5. Ensure request finalization never persists an inflated local `estimated` cost as canonical spend.
6. Reject or downgrade implausible snapshot rates before they influence request accounting.
7. Add tests that reproduce the MiniMax-shaped failure class without provider-specific logic.
8. Add repair/audit tooling for already-persisted inflated rows.

## Non-goals

- Do not special-case `provider_id == "minimax"`.
- Do not remove provider-reported cost precedence; explicit provider billing fields should still win.
- Do not silently rewrite historical request rows without an audit path or dry-run mode.
- Do not replace all pricing with static hardcoded model prices.
- Do not make the dashboard hide cost rows without fixing the persisted accounting semantics.

## Current seams to inspect

Primary files:

- `src/eggpool/catalog/pricing_resolver.py`
  - Current structured metadata resolver.
  - Has partial sibling-aware logic for nested `pricing.prompt`, `pricing.input`, `pricing.completion`, and `pricing.output`.
  - Nested cache pricing still uses `default_unit="token"`, which is unsafe for provider-native per-million payloads.

- `src/eggpool/catalog/pricing.py`
  - Defines `parse_price_per_1k`, `parse_microdollars_per_million`, `PriceSnapshot`, `PriceRepository`, and `CostCalculator`.
  - Contains trust ceilings such as `_MAX_TRUSTED_RATE_PER_MILLION_MICRODOLLARS` and `_MAX_TRUSTED_COST_PER_TOKEN_MICRODOLLARS`.
  - `CostCalculator.calculate_cost()` can downgrade a suspicious derived result to `estimated`, but currently still returns the inflated/clamped microdollar amount.

- `src/eggpool/request/finalizer.py`
  - Determines canonical `requests.cost_microdollars`.
  - Currently accepts any positive `local_cost_microdollars`, even if `local_cost_exactness == "estimated"`.

- `tests/unit/test_pricing_resolver.py`
  - Existing table-like tests for OpenRouter-style, provider-native, cache, and override pricing.
  - Should be expanded for cache inheritance, ambiguous top-level aliases, explicit units, and rejection behavior.

Likely additional files:

- `tests/unit/test_pricing.py` or equivalent cost-calculator tests.
- `tests/unit/test_request_finalizer*.py` or equivalent finalizer tests.
- CLI command modules under `src/eggpool/cli*` for optional historical repair tooling.
- DB repository modules for request-row repair/audit support.

## Phase 1: Immediate blast-radius limiter in request finalization

### Intent

Prevent suspicious locally derived cost from becoming canonical dashboard spend even if the pricing parser still misclassifies some future provider payload.

### Required change

In `RequestFinalizer.finalize()`, only accept local calculator output as canonical when `local_cost_exactness` is one of:

- `derived`
- `partial`
- `exact`

Do not accept a positive local cost solely because it is positive. If the calculator returns `(large_positive_value, "estimated")`, canonical cost must fall back to `selected.estimated_microdollars` when billable work is plausible.

Current unsafe shape:

```python
elif local_cost_microdollars is not None and (
    local_cost_microdollars > 0
    or local_cost_exactness in {"derived", "partial", "exact"}
):
    cost_microdollars = local_cost_microdollars
    exactness = local_cost_exactness or "derived"
```

Target behavior:

```python
trusted_local_exactness = {"derived", "partial", "exact"}

if data.provider_cost_microdollars is not None:
    cost_microdollars = data.provider_cost_microdollars
    exactness = "provider_reported"
elif (
    local_cost_microdollars is not None
    and local_cost_exactness in trusted_local_exactness
):
    cost_microdollars = local_cost_microdollars
    exactness = local_cost_exactness
elif may_have_billable_work:
    cost_microdollars = selected.estimated_microdollars
    exactness = "estimated"
    if local_cost_microdollars is None:
        local_cost_microdollars = selected.estimated_microdollars
    if local_cost_exactness is None:
        local_cost_exactness = "estimated"
else:
    cost_microdollars = 0
    exactness = "unknown"
```

### Acceptance criteria

- A local calculator result of `(250_000_000, "estimated")` does not persist `$250` as canonical request cost.
- Provider-reported cost still overrides local derived and estimated values.
- Local `(1234, "derived")`, `(1234, "partial")`, and `(1234, "exact")` results still persist as canonical.
- The reservation estimate floor remains limited to `exactness == "estimated"` and does not inflate trusted local or provider-reported costs.
- Existing request finalization tests pass.

## Phase 2: Introduce provider-neutral price candidate extraction

### Intent

Stop parsing each price field in isolation. Instead, gather a cluster of related raw price candidates and infer units using the whole local context.

### New internal structures

Add internal dataclasses/enums to `pricing_resolver.py` or a small helper module such as `src/eggpool/catalog/price_units.py`:

```python
PriceCategory = Literal["input", "output", "cache_read", "cache_write"]
PriceUnit = Literal[
    "dollars_per_token",
    "dollars_per_1k",
    "dollars_per_million",
    "microdollars_per_million",
]
UnitEvidence = Literal[
    "operator_override",
    "explicit_suffix",
    "field_name",
    "sibling_consensus",
    "numeric_scale",
    "safe_default",
]

@dataclass(frozen=True)
class RawPriceCandidate:
    category: PriceCategory
    raw_value: object
    path: tuple[str, ...]
    field_name: str
    explicit_unit: PriceUnit | None
    field_unit_hint: PriceUnit | None
    numeric_value: Decimal | None

@dataclass(frozen=True)
class ResolvedPriceCategory:
    category: PriceCategory
    microdollars_per_million: int | None
    legacy_price_per_1k: float | None
    unit: PriceUnit | None
    evidence: UnitEvidence | None
    confidence: str
    path: tuple[str, ...]
```

The exact names may vary, but the concepts should be preserved.

### Candidate extraction rules

Collect all known pricing aliases into one local cluster:

Input aliases:

- `pricing.prompt`
- `pricing.input`
- `input_price_per_1k`
- `prompt_price_per_1k`
- `input_usd_per_million`
- `prompt_usd_per_million`
- `input_per_million_microdollars`
- `prompt_per_million_microdollars`
- bare top-level `prompt` only if no more explicit field is present

Output aliases:

- `pricing.completion`
- `pricing.output`
- `output_price_per_1k`
- `completion_price_per_1k`
- `output_usd_per_million`
- `completion_usd_per_million`
- `output_per_million_microdollars`
- `completion_per_million_microdollars`
- bare top-level `completion` only if no more explicit field is present

Cache read aliases:

- `pricing.input_cache_read`
- `pricing.cache_read`
- `pricing.prompt_cache_read`
- `cache_read_per_million_microdollars`
- `input_cache_read_per_million_microdollars`
- `cache_read_usd_per_million`
- `input_cache_read_usd_per_million`
- `cache_read_input_token_cost`

Cache write aliases:

- `pricing.input_cache_write`
- `pricing.cache_write`
- `pricing.prompt_cache_write`
- `cache_write_per_million_microdollars`
- `input_cache_write_per_million_microdollars`
- `cache_write_usd_per_million`
- `input_cache_write_usd_per_million`
- `cache_creation_input_token_cost`

Operator overrides remain authoritative and should bypass heuristic inference, but they should still be normalized into the same result structure for audit consistency.

### Acceptance criteria

- All current resolver tests still pass after migrating call sites to the candidate model.
- The resolver can explain which raw path produced each category.
- The resolver no longer has separate isolated cache parsing logic that hardcodes `default_unit="token"` for every nested cache field.

## Phase 3: Cluster-aware unit inference

### Intent

Infer units using explicit suffixes, field-name hints, sibling consensus, numeric scale, and conservative defaults. The inference must be general, not provider-specific.

### Precedence order

1. Operator override unit, if the operator provided the normalized field.
2. Explicit unit suffix in the raw string, e.g. `/token`, `per token`, `/1k`, `per 1K`, `/1M`, `per million`.
3. Unambiguous field-name unit hint:
   - `*_per_1k` -> dollars per 1K.
   - `*_per_million_microdollars` -> microdollars per million.
   - `*_usd_per_million`, `*_dollars_per_million` -> dollars per million.
   - `*_input_token_cost`, `*_token_cost`, `*_per_token` -> dollars per token.
4. Sibling/cluster consensus:
   - If multiple candidates in the same pricing cluster have explicit or field-name units, use the majority compatible regime for otherwise ambiguous siblings.
   - Nested `pricing` dictionary values should be treated as a shared regime unless a particular field explicitly overrides its own unit.
5. Numeric scale:
   - Values below a tight per-token ceiling, e.g. `< 0.001`, are likely dollars per token when the broader cluster also supports that interpretation.
   - Human-scale values, e.g. `0.01`, `0.2`, `1.1`, `15`, should not be treated as dollars per token without explicit token evidence.
6. Safe default:
   - Ambiguous bare values default to dollars per million, not dollars per token.
   - This intentionally fails toward underestimation instead of catastrophic overestimation.

### Important cache behavior

Cache fields inside a nested `pricing` dictionary must inherit the same inferred unit regime as input/output siblings unless their own field name or value explicitly proves otherwise.

Example:

```python
{
    "pricing": {
        "input": 0.2,
        "output": 1.1,
        "cache_read": 0.02,
        "cache_write": 0.2,
    }
}
```

Expected:

- input = `$0.20 / 1M`
- output = `$1.10 / 1M`
- cache_read = `$0.02 / 1M`
- cache_write = `$0.20 / 1M`

Not expected:

- cache_read = `$0.02 / token`
- cache_write = `$0.20 / token`

### OpenRouter compatibility

OpenRouter-style values must continue to resolve correctly:

```python
{
    "pricing": {
        "prompt": "0.000000105",
        "completion": "0.00000028",
        "input_cache_read": "0.000000021",
        "input_cache_write": "0.000000105",
    }
}
```

Expected:

- prompt = `$0.105 / 1M`
- completion = `$0.28 / 1M`
- cache_read = `$0.021 / 1M`
- cache_write = `$0.105 / 1M`

### Acceptance criteria

- MiniMax-shaped provider-native bare values resolve as dollars per million.
- OpenRouter-shaped small decimal values resolve as dollars per token.
- Explicit suffixes override numeric scale.
- Field-name units override cluster consensus.
- Ambiguous single bare values default to dollars per million and receive low-confidence/safe-default provenance.
- Cache fields inherit the cluster regime rather than defaulting to token units.

## Phase 4: Snapshot trust gates before persistence

### Intent

Do not persist implausible local price snapshots as normal trusted pricing.

### Required changes

Add a snapshot validation step between `ResolvedPricing` creation and `PriceRepository.insert_snapshot()`.

Validation should normalize every category into canonical microdollars per million, then check:

- category rate is finite and non-negative
- category rate does not exceed `_MAX_TRUSTED_RATE_PER_MILLION_MICRODOLLARS`
- category rate does not exceed any stricter category-specific ceiling, if introduced later
- at least one trusted category remains before inserting a snapshot

If a category is implausible:

- discard that category from the snapshot
- log a warning with model id, provider id, category, raw path, raw value, inferred unit, evidence, and normalized rate
- mark resolver output as `low_confidence` or `rejected_category` if metadata fields are added

If all categories are implausible:

- skip snapshot insertion
- invalidate no existing good snapshot
- emit a structured warning

### Acceptance criteria

- A malformed metadata payload cannot insert a `$200,000 / 1M` local rate as trusted pricing.
- Existing good snapshots are not replaced by all-rejected bad snapshots.
- CostCalculator sees only trusted or bounded fallback rates.
- Logs contain enough information to identify which raw field caused rejection.

## Phase 5: CostCalculator fail-closed behavior for suspicious local costs

### Intent

Ensure local cost calculation returns a bounded estimate, not an inflated suspicious cost, whenever it detects a runaway unit bug.

### Required changes

Review `CostCalculator.calculate_cost()` behavior when:

- `_rate_is_implausible(...)` trips
- implicit request cost per token exceeds `_MAX_TRUSTED_COST_PER_TOKEN_MICRODOLLARS`
- trusted arithmetic produces a clamped near-`MAX_REQUEST_COST_MICRODOLLARS` value with normal token counts

When the implicit cost-per-token sanity check trips, return the generic `_estimate_cost(...)` result with `"estimated"`, not the inflated `cost_microdollars` with `"estimated"`.

Target behavior:

```python
if implicit_cost_per_token > _MAX_TRUSTED_COST_PER_TOKEN_MICRODOLLARS:
    logger.warning(...)
    return self._estimate_cost(input_tokens, output_tokens), "estimated"
```

This duplicates the finalizer blast-radius limiter by making the calculator itself fail closed.

### Acceptance criteria

- A suspicious derived request cost is replaced with a bounded generic estimate before returning.
- Finalizer hardening still exists and protects against future calculator regressions.
- Partial pricing with legitimate expensive output models still returns `partial` when below trust ceilings.

## Phase 6: Request accounting and dashboard provenance

### Intent

Make the UI and DB distinguish actual provider spend, trusted local derived spend, partial local spend, and safe estimates.

### Required changes

Review existing request columns before adding migrations. Prefer using existing fields if they already exist:

- `exactness`
- `provider_cost_microdollars`
- `provider_cost_source`
- `local_cost_microdollars`
- `local_cost_exactness`

If necessary, add narrowly scoped columns:

- `cost_guardrail_status` nullable text, e.g. `ok`, `estimated_due_to_implausible_rate`, `estimated_due_to_implausible_request_cost`, `provider_reported`
- `cost_guardrail_reason` nullable compact text

Dashboard should surface cost exactness clearly enough that estimated costs are not confused with provider-billed spend.

### Acceptance criteria

- A request whose local derived result was rejected shows canonical cost as estimated, not derived/provider-reported.
- Provider-reported rows remain distinguishable from estimated rows.
- Dashboard aggregate totals do not silently mix provider-reported and estimated costs without an exactness breakdown somewhere in the usage/cost view.

## Phase 7: Historical repair command

### Intent

Allow operators to fix already-persisted inflated cost rows after the parser/finalizer hardening lands.

### CLI shape

Add a dry-run-first command, exact module placement following current CLI conventions:

```bash
eggpool stats repair-costs --provider minimax --dry-run
eggpool stats repair-costs --provider minimax --since 2026-07-03 --dry-run
eggpool stats repair-costs --provider minimax --since 2026-07-03 --apply
```

Provider filter should be generic and accept any provider/account id substring or exact provider id depending on existing stats conventions.

### Candidate row selection

Repair should target rows where:

- provider/account matches the filter
- `provider_cost_microdollars IS NULL`
- canonical `cost_microdollars` is suspicious, for example:
  - equals or approaches `MAX_REQUEST_COST_MICRODOLLARS`
  - or implies cost per token above `_MAX_TRUSTED_COST_PER_TOKEN_MICRODOLLARS`
  - or has `exactness == "estimated"` with canonical cost far above the reservation estimate

Do not rewrite rows with explicit provider-reported cost unless the operator passes a separate explicit override flag. That override flag can be deferred.

### Repair behavior

Dry-run output should show:

- row count scanned
- row count considered suspicious
- old total cost
- proposed repaired total cost
- delta
- top 20 largest row-level changes
- provider/account/model breakdown

Apply mode should:

- write a repair audit row or append to a compact audit table if one exists
- update canonical `cost_microdollars`
- set exactness to `estimated_repaired` or an equivalent existing enum-compatible label
- preserve old value in audit metadata

If adding a new exactness value is risky for dashboards, use `estimated` as canonical exactness and record repaired status in audit metadata.

### Acceptance criteria

- Dry-run performs no writes.
- Apply mode is idempotent.
- Provider-reported cost rows are skipped by default.
- The command can correct the observed `$250/request` class of rows.
- Unit tests cover row selection and proposed replacement math.

## Phase 8: Test matrix

### Resolver tests

Expand `tests/unit/test_pricing_resolver.py` or equivalent with table-driven cases:

1. Provider-native bare per-million input/output:

```python
{"pricing": {"input": 0.2, "output": 1.1}}
```

Expected: `$0.20 / 1M`, `$1.10 / 1M`.

2. Provider-native bare per-million with cache:

```python
{"pricing": {"input": 0.2, "output": 1.1, "cache_read": 0.02, "cache_write": 0.2}}
```

Expected: all four categories interpreted as dollars per million.

3. OpenRouter-style per-token with cache:

```python
{"pricing": {"prompt": "0.000000105", "completion": "0.00000028", "input_cache_read": "0.000000021"}}
```

Expected: per-token interpretation converted to microdollars per million.

4. Explicit suffix mixed payload:

```python
{"pricing": {"prompt": "0.2 / 1M", "completion": "0.0000011 / token"}}
```

Expected: suffixes win per field.

5. Field-name units:

```python
{"input_price_per_1k": 0.0002, "output_usd_per_million": 1.1}
```

Expected: field-name hints win.

6. Ambiguous single bare value:

```python
{"pricing": {"input": 0.2}}
```

Expected: safe-default per-million, low-confidence/safe-default evidence.

7. Implausible category rejected:

```python
{"pricing": {"input": 200000, "output": 900000}}
```

Expected: rejected categories or no snapshot.

8. Boolean, NaN, negative, empty string, and nonnumeric strings:

Expected: ignored with warnings, never crashing request handling.

### Calculator tests

Add tests where a fake `PriceRepository` returns snapshots with:

- trusted rates -> returns `derived`
- one missing category -> returns `partial`
- all missing categories -> returns bounded `estimated`
- implausible snapshot rate -> returns bounded `estimated`
- plausible rates but impossible request-level cost per token -> returns bounded `estimated`, not clamped inflated cost

### Finalizer tests

Add tests with fake selected request/account objects and fake calculator output:

- provider-reported cost wins
- local derived cost persists
- local partial cost persists
- local exact cost persists if that label is used
- local estimated positive cost does not persist as canonical
- local estimated positive cost falls back to selected reservation estimate
- zero/no-usage terminal paths still produce `unknown` or zero as before

### Repair tests

If repair command is included in this pass:

- dry-run has no writes
- suspicious rows are selected
- provider-reported rows are skipped
- apply is idempotent
- audit metadata captures old and new values

## Phase 9: Observability and operator diagnostics

### Structured logs

Add warning logs for:

- ambiguous bare unit defaulted to per-million
- pricing cluster with mixed unit evidence
- implausible category rate rejected
- suspicious request-level local cost replaced with estimate
- finalizer ignored positive local estimated cost

Logs should include:

- provider id
- model id
- category
- raw path
- raw value where safe to log
- inferred unit
- evidence
- normalized microdollars per million
- trust ceiling

### Optional debug command

Consider a read-only diagnostic command:

```bash
eggpool models explain-pricing --provider <provider> --model <model>
```

Output:

- latest snapshot
- source/source_detail/source_confidence
- per-category normalized rate
- unit evidence if stored or reconstructable
- whether each rate passes trust gates

This can be deferred if the repair command provides enough diagnostics.

## Phase 10: Documentation

Update operator docs to explain:

- exactness labels: `provider_reported`, `derived`, `partial`, `estimated`, `unknown`
- why estimated cost is not the same as billed cost
- how EggPool interprets ambiguous provider pricing metadata
- how to run `repair-costs --dry-run`
- how to set explicit operator overrides when provider metadata is unreliable

Mention that ambiguous bare values default to per-million as a safety policy, and provider-reported cost remains the strongest authority.

## Implementation order

1. Patch finalizer cost precedence so positive local `estimated` values cannot become canonical.
2. Patch `CostCalculator` to return bounded estimates when request-level cost-per-token guardrail trips.
3. Refactor resolver internals to collect candidate clusters and infer units consistently.
4. Make nested cache fields inherit the pricing-cluster unit regime.
5. Add snapshot trust gates before persistence.
6. Expand resolver/calculator/finalizer tests.
7. Add repair dry-run/apply command if time permits; otherwise create a follow-up plan and at minimum document the SQL used to identify suspect rows.
8. Update docs and dashboard exactness labeling as needed.

## Suggested verification commands

Run the narrow test set first:

```bash
python -m pytest tests/unit/test_pricing_resolver.py -q
python -m pytest tests/unit/test_pricing.py -q
python -m pytest tests/unit/test_request_finalizer.py -q
```

Then run the broader suite used by the repo:

```bash
python -m pytest
```

If the project uses `uv` in CI, mirror CI locally:

```bash
uv run pytest
```

## Manual validation SQL

Before repair, operators can inspect suspect rows with:

```sql
SELECT
  r.id,
  a.provider_id,
  r.model_id,
  r.input_tokens,
  r.output_tokens,
  r.cache_read_tokens,
  r.cache_write_tokens,
  r.cost_microdollars / 1000000.0 AS cost_usd,
  r.exactness,
  r.provider_cost_microdollars,
  r.provider_cost_source
FROM requests r
JOIN accounts a ON a.id = r.account_id
WHERE a.provider_id LIKE '%minimax%'
ORDER BY r.cost_microdollars DESC
LIMIT 25;
```

A likely inflated-row signature is:

- `cost_usd` near the per-request cap
- `exactness = 'estimated'` or locally derived without provider cost
- `provider_cost_microdollars IS NULL`
- normal token counts that cannot justify the apparent request cost

## Done criteria

This hardening effort is complete when:

- No provider-neutral ambiguous metadata fixture can produce runaway dashboard spend.
- Nested cache price parsing no longer defaults to per-token in a way that overrides surrounding per-million evidence.
- The finalizer cannot persist positive local `estimated` values as canonical cost.
- The calculator fails closed to bounded estimates on implausible request-level derived costs.
- Implausible snapshot rates are rejected before persistence.
- Tests cover provider-native per-million, OpenRouter per-token, mixed explicit suffixes, field-name unit hints, cache inheritance, implausible rates, and finalizer precedence.
- Operators have a dry-run path to identify and repair historical inflated rows.
- Dashboard totals are no longer vulnerable to `$250/request` accumulation from local unit misclassification.

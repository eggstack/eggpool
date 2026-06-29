# Runtime Memory Footprint Plan

## Context

EggPool's request hot path is fast and observability-rich, but several in-memory data structures grow monotonically with workload cardinality rather than remaining bounded by workload throughput. Each growth axis can be reproduced by the existing load/integration tests:

- `tests/integration/test_load.py::test_memory_stability_under_repeated_requests` (50 sequential requests, asserts DB row count only).
- `tests/integration/test_load.py::test_file_descriptor_stability` (uses `resource.getrlimit` to assert no FD leak).
- `tests/integration/test_load.py::test_concurrent_mixed_protocols` (10 concurrent streaming requests, mixed OpenAI/Anthropic).

These tests do not currently assert RSS or per-object memory growth, but they exercise every allocation site we care about: catalog cache, quota estimator, SSE observer, request coordinator, outbound HTTP manager. The plan adds a `tracemalloc`-based regression test alongside the existing load suite so future regressions are caught before they ship.

The plan's primary operational goal is bounded steady-state RSS on long-running deployments, especially Raspberry Pi / SBC nodes. The plan must not regress any existing capability (routing, scoring, transcoding, streaming, audit trail, persistence) or measurable request latency.

## Goals

- Bound every growth axis in `QuotaEstimator`, `ModelCatalogCache`, `CatalogResolverPipeline.TTLCache`, `OutboundClientManager`, `AccountRuntimeState`, and `HealthManager` that is currently unbounded.
- Eliminate the per-call `set().copy()` allocation on `ModelCatalogCache.get_supporting_accounts(...)` (and its sibling `get_supporting_accounts_for_model`).
- Add a `tracemalloc` baseline test that locks in the post-change footprint.
- Document every operator-facing knob in `docs/` and the bundled `config.example.toml` / `_share/config.example.toml`.

## Non-goals

- Do not change the request lifecycle, scoring algorithm, or streaming pipeline.
- Do not change on-disk schemas or audit-trail columns.
- Do not switch any structure to a third-party cache (no `cachetools`, no Redis, no LRU dependency).
- Do not modify the TTL windows for the DNS cache, dashboard cache, or metrics buffer — those are already bounded and outside the scope of this plan.
- Do not introduce per-request allocations for any of the proposed changes.

## Findings to address

### 1. `QuotaEstimator` EWMA tables grow without bound

`src/eggpool/quota/estimation.py` lines 276-282 keep two monotonic dicts:

```text
account_model_ewma: dict[str, dict[str, EWMAEstimate]]
global_model_ewma: dict[str, EWMAEstimate]
```

`record_usage` (lines 322-336) inserts without ever evicting. With a large fleet of distinct `(account, model)` pairs and long uptimes, both dicts accumulate tens of thousands of `EWMAEstimate` instances. The persisted `quota_windows` table provides the canonical record; the in-memory tables are derived from it and can be safely recomputed on miss.

### 2. `QuotaWindow.observations` deque is pruned only on writes

`src/eggpool/quota/estimation.py` lines 36-38 declares `observations: deque[tuple[float, int, int]]`, and `_prune_old_observations` (lines 47-57) only runs inside `add_observation`. Every persisted snapshot path goes through `record_usage_and_snapshot` → `record_usage` → `QuotaWindow.record_usage` → `add_observation`, so the deque is correctly pruned on every write. **Verified no-op.** Document this as a confirmed invariant.

### 3. `CatalogResolverPipeline.TTLCache` holds unbounded `raw` payloads

`src/eggpool/catalog/catalog_resolvers.py` lines 117-164 holds the entire parsed OpenRouter catalog in memory for `ttl_seconds` (default 86_400). Each `CatalogEntry.raw` keeps the upstream JSON object (lines 244-278), and `to_resolved_pricing` (lines 280-303) only reads structured fields. OpenRouter ships ~250+ model entries with rich payloads — the `raw` blobs dominate the cache's footprint.

### 4. `ModelCatalogCache` stores both `_models` and `_provider_models`

`src/eggpool/catalog/cache.py` lines 109-111 declare both:

```text
self._models: dict[str, dict[str, Any]]                  # global, model_id -> info
self._provider_models: dict[tuple[str, str], dict[str, Any]]  # per-provider, (model_id, provider_id) -> info
```

`update_from_account` (lines 168-203) writes both dicts on every refresh. `_provider_models[(model_id, provider_id)]` is a copy of the same dict object that was just constructed, and `_models[model_id]` holds the first-seen-wins variant. Both store the full metadata blob (`display_name`, `protocol`, `protocol_source`, `capabilities`, `source_metadata`, `discovered_limits`, `effective_limits`, `first_seen_at`, `last_seen_at`).

### 5. `OutboundClientManager` per-host counters grow without bound

`src/eggpool/providers/outbound.py` lines 101-102 and lines 184-198 grow `_per_host_requests: dict[str, int]` and `_per_host_errors: dict[str, int]` for every distinct host. In practice the set is small (OpenRouter, provider hosts, update check host), but a leaked hostname string or unbounded DNS resolver traffic can inflate these dicts.

### 6. `AccountRuntimeState.model_availability` grows without bound

`src/eggpool/accounts/state.py` lines 58-61 declare `model_availability: dict[str, bool]`. `record_failure` does not write to this map; the map is populated by per-model health events. The map is only cleared in `reset_health` (line 175), so it persists stale per-model disable state across long uptimes.

### 7. `HealthManager.AccountHealth.disabled_models` grows without bound

`src/eggpool/health/health_manager.py` line 111 declares `disabled_models: dict[str, float | None]`. New entries are written on `mark_model_disabled` (line 353) and removed on `unmark_model_disabled` (line 362). The `:443` in line 443 also lists it. There is no periodic prune; the map persists until explicit unmark or `enable_account`.

### 8. `ModelCatalogCache.get_supporting_accounts(...)` allocates per call

`src/eggpool/catalog/cache.py` line 621-623 returns `self._account_support.get(model_id, set()).copy()`. Every routing decision allocates a fresh `set`. The sibling `get_supporting_accounts_for_model` (line 820-822) does the same. Callers (`routing/router.py:381`, `:385`; `catalog/cache.py:587`, `:606`, `:636`, `:657`; `catalog/service.py:786`) only iterate or check membership — they never mutate.

## Phase 1: Add `tracemalloc` baseline test (test-first)

Add an integration test `tests/integration/test_memory.py` (or extend `tests/integration/test_load.py` if a separate file is not preferred) that:

1. Snapshots `tracemalloc` allocations after one warm-up request (always included in the snapshot to amortise cold-start allocations).
2. Runs 100 identical requests, then snapshots again after `gc.collect()`.
3. Asserts the top-N allocation delta is dominated by `request` short-lived objects (transient per-request work) and does not include the persistent structures listed in §1-§7.
4. Runs the same test with streaming enabled (`stream: true`).
5. Asserts `len(cache._account_support) <= known_cardinality` and `len(router._quota_estimator.account_model_ewma) <= EWMA_HARD_CAP` after 1000 requests.

Suggested test surface (under `tests/integration/`):

```python
async def test_tracemalloc_baseline_under_repeated_requests(...):
    """Persistent data structures do not grow with request count."""
```

The test should be marked `pytest.mark.slow` so it can be skipped in PR CI but runs in nightly. Document this in `tests/integration/test_memory.py`.

## Phase 2: Bound `QuotaEstimator` EWMA tables

**File**: `src/eggpool/quota/estimation.py`.

- Replace `account_model_ewma: dict[str, dict[str, EWMAEstimate]]` with an LRU-bounded structure keyed by `f"{account_name}|{model_id}"`. Cap = `EWMA_HARD_CAP = 4096`. Eviction removes the entry.
- Replace `global_model_ewma: dict[str, EWMAEstimate]` with an LRU-bounded structure keyed by `model_id`. Cap = `GLOBAL_EWMA_HARD_CAP = 1024`.
- On LRU miss in `estimate_cost` (lines 365-429), call a new helper `_seed_ewma_from_window(account_name, model_id)` that reads the persisted `QuotaWindow` via the existing `UsageWindowRepository` (already wired via `set_usage_window_repo`) and computes a fresh EWMA from those observations. Persisted snapshot already provides `cost_5h/cost_7d/cost_30d`; compute `cost_per_token = cost_5h / max(tokens_5h, 1)` from the snapshot.
- Expose both caps as `QuotaEstimator` constructor parameters with the hardcoded defaults above; do not surface them as config knobs (per the user's "default hardcoded" preference).

**Capability diff**: None. EWMA values remain identical when entries fit in the cap. When an entry is evicted, the next `estimate_cost` call recomputes from the persisted window — the resulting value is at most one `cost_per_token` recomputation older than the live value.

**Perf diff**: Slightly faster on insert (LRU is O(1)), slightly slower on miss (one `QuotaWindow` scan). Net neutral; profiled in Phase 8.

## Phase 3: Document `QuotaWindow.observations` invariant

Add a docstring section to `src/eggpool/quota/estimation.py::QuotaWindow`:

```text
Invariant: every persisted snapshot path goes through `add_observation`,
which calls `_prune_old_observations`. The deque is therefore bounded by
the number of observations within the window_seconds window. Any future
write path that bypasses `add_observation` MUST call
`_prune_old_observations` explicitly.
```

**Capability diff**: None (documentation only).

## Phase 4: Bound `CatalogResolverPipeline.TTLCache`

**File**: `src/eggpool/catalog/catalog_resolvers.py`.

- Add `max_entries: int = 4096` parameter to `TTLCache.__init__` (lines 126-130).
- Replace `self._data: dict[str, CatalogEntry]` with `collections.OrderedDict[str, CatalogEntry]`. On `store()` (line 155-157), evict from the head until `len(self._data) <= self._max_entries`. The TTL semantics are preserved — eviction only happens at fetch time, which already follows the existing pattern.
- Strip `entry.raw` after `_parse_catalog` returns, before storing in the cache:

  ```text
  for entry in entries.values():
      entry.raw = {}
  ```

  This is safe because `to_resolved_pricing` only reads structured fields.
- Wire `max_entries` through `CatalogConfig` → `PricingCatalogEntry` config model.

**Capability diff**: None. Stripping `entry.raw` after parse is a one-way operation; no downstream caller reads `entry.raw`.

**Perf diff**: Slightly faster (smaller dicts, fewer attributes).

### Phase 4a: Expose `max_entries` as a config knob

**Files**: `src/eggpool/models/config.py`, `src/eggpool/catalog/catalog_resolvers.py`, `src/eggpool/catalog/service.py`.

- Add `max_entries: int = Field(default=4096, gt=0)` to `PricingCatalogEntry` (line 159-174 of `models/config.py`).
- Pass `max_entries` through `CatalogConfig` to `TTLCache`.
- Document in `docs/transcoding.md` (Pricing section), `src/eggpool/_share/config.example.toml`, and `config.example.toml`. Add the default with a comment explaining the rationale.

Example TOML addition:

```toml
[pricing.catalogs.openrouter]
enabled = true
priority = 100
ttl_seconds = 86400
# max_entries = 4096  # bound the in-memory catalog cache; oldest entries evict first
```

**Capability diff**: None.

**Perf diff**: None.

## Phase 5: De-dup `ModelCatalogCache._models` and `_provider_models`

**File**: `src/eggpool/catalog/cache.py`.

- Keep `_models` as the canonical storage (model_id → metadata).
- Replace `_provider_models` with a derived view: keep `_provider_models: dict[tuple[str, str], dict[str, Any]]` but populate it lazily only when `get_model_for_provider` (or its callers) needs a per-provider override.
- Concretely: introduce `def _ensure_provider_entry(self, key, default_info)` that copies `_models[model_id]`, applies the provider-specific override from the live refresh, and stores the result in `_provider_models[key]` on first read. Writes from `update_from_account` only populate `_provider_models` when the per-provider metadata actually differs from the global metadata (e.g. `protocol`, `display_name`, `capabilities`, `source_metadata`, `discovered_limits`, `effective_limits`).
- Audit all callers of `_provider_models`: `cache.py:165, 187, 615, 712, 832, 841, 902`; `routing/router.py:806`; `service.py:799`. None mutate the value.

**Capability diff**: None — every existing read path returns the same dict shape.

**Perf diff**: Slightly faster on refresh (one dict write per model where per-provider metadata equals global; two where it differs).

## Phase 6: Cap `OutboundClientManager` per-host counters

**File**: `src/eggpool/providers/outbound.py`.

- Add `MAX_TRACKED_HOSTS = 256` module constant.
- In `record_request` (lines 184-198), when `host is not None` and `len(self._per_host_requests) >= MAX_TRACKED_HOSTS` and `host not in self._per_host_requests`, evict the host with the smallest total counter (`requests + errors`). Use a heap-of-two dicts or simpler `min(...)` lookup. Add an `evictions_total` counter and surface it in `snapshot()` (lines 220-230).

**Capability diff**: None. The counters are best-effort diagnostics.

**Perf diff**: Negligible — `record_request` runs once per outbound HTTP response.

## Phase 7: Prune `AccountRuntimeState.model_availability` and `HealthManager.disabled_models` at sync

**Files**: `src/eggpool/accounts/state.py`, `src/eggpool/health/health_manager.py`.

- In `AccountRegistry.sync_accounts` (call site to be confirmed by `grep -n "sync_accounts" src/`), iterate every account's `AccountRuntimeState.model_availability` and remove entries whose `model_id` is not in the current provider model set.
- In `HealthManager` (the periodic health sync / supervisor task), drop `disabled_models` entries whose `model_id` is no longer advertised by any provider.

Both prunes must be no-ops when the structures are already in sync — only `dict.pop(...)` per stale entry.

**Capability diff**: None — pruned entries are by definition stale.

**Perf diff**: Negligible (runs once per account sync, not per request).

## Phase 8: Replace `set().copy()` with `frozenset` in `get_supporting_accounts`

**File**: `src/eggpool/catalog/cache.py`.

- Change `_account_support: dict[str, set[str]]` to `_account_support: dict[str, frozenset[str]]`.
- Change `get_supporting_accounts` (line 621-623) and `get_supporting_accounts_for_model` (line 820-822) to return `self._account_support.get(model_id, frozenset())` — no copy.
- Change `add_account_support` (line 806-810) to construct a new `frozenset` (frozensets are immutable; replacement is mandatory on add). Replace `set().add` with `frozenset({account_name}) | existing`.
- Audit all other writers of `_account_support`: `cache.py:201-203` in `update_from_account`. Replace the same way.

Audit confirmed (Phase 8.5):

- `cache.py:589` `if account_name not in supporting` — frozenset supports `__contains__` with O(1) cost.
- `cache.py:606` `supporting &= account_names` — frozenset supports `__iand__` returning a new frozenset.
- `cache.py:636, 657` iterate over it — frozenset is iterable.
- `service.py:792` `acct_row["name"] in supporting_accounts` — membership.
- `router.py:381` `state.name not in ...` — membership.
- `router.py:385` iterates over it.

All eight callers are read-only; frozenset is a strict superset of their needs.

**Capability diff**: None. Callers that previously mutated the returned set would now raise `AttributeError`; verified no caller mutates.

**Perf diff**: Eliminates one O(n) `set.copy()` per routing decision. Net faster.

## Phase 9: Update docs

**Files**: `docs/transcoding.md`, `src/eggpool/_share/config.example.toml`, `config.example.toml`.

- `docs/transcoding.md` — add a "Pricing" subsection explaining `max_entries`, its default of 4096, and the eviction order (LRU on fetch).
- `config.example.toml` files — add the commented-out `max_entries = 4096` line to both `openrouter` and `opencode_zen` sections.

## Phase 10: Validation

Run the full pre-commit sequence:

```bash
uv run ruff format --check src/ tests/ scripts/
uv run ruff check src/ tests/ scripts/
uv run pyright src/ scripts/
uv run pytest
```

Specifically re-run:

```bash
uv run pytest tests/integration/test_load.py -v
uv run pytest tests/unit/test_quota_audit.py -v
uv run pytest tests/unit/test_finalizer_reservation_regression.py -v
uv run pytest tests/unit/test_sse_observer.py -v
uv run pytest tests/unit/test_dashboard.py -v
uv run pytest tests/unit/test_runtime_metrics.py -v
```

The new memory test (Phase 1) is the regression gate; existing tests prove no capability reversal.

## Suggested implementation order

1. Phase 1 — add `tracemalloc` baseline test.
2. Phase 8 — switch to `frozenset` (smallest blast radius, immediately frees per-call allocation).
3. Phase 6 — cap `OutboundClientManager` per-host counters.
4. Phase 7 — prune `model_availability` and `disabled_models` at sync.
5. Phase 4 + 4a — bound `TTLCache` and add `max_entries` config.
6. Phase 5 — de-dup `_models` / `_provider_models`.
7. Phase 2 — LRU-cap the EWMA tables.
8. Phase 3 — documentation invariant for `QuotaWindow`.
9. Phase 9 — docs and config example updates.
10. Phase 10 — full validation.

## Acceptance criteria

- `tracemalloc` baseline test passes against the post-change code.
- All existing tests pass with no skips.
- `len(QuotaEstimator.account_model_ewma)` and `len(QuotaEstimator.global_model_ewma)` are bounded by their hardcoded caps after 10_000 requests.
- `len(OutboundClientManager._per_host_requests)` is bounded by `MAX_TRACKED_HOSTS`.
- `CatalogResolverPipeline.TTLCache._data` is bounded by `max_entries` (per-config).
- `ModelCatalogCache.get_supporting_accounts(...)` allocates zero new objects per call (verified by a profiling test).
- `_models` and `_provider_models` together occupy at most ~1.5× the post-change size of `_models` alone (de-dup effect).
- Docs and config examples are updated.
- No `pytest.mark.xfail` is added.

## Notes for implementers

- The `frozenset` switch (Phase 8) is the highest-confidence change: every caller is read-only, the change is mechanically simple, and the per-call allocation reduction is observable in any routing-heavy test. Do this first to build momentum.
- The `tracemalloc` test (Phase 1) must use `gc.collect()` before each snapshot; otherwise `requests`-shaped short-lived objects will dominate the diff and mask the persistent structures.
- The EWMA recomputation on LRU miss (Phase 2) is a real implementation cost — it touches the persisted `QuotaWindow`. Verify that `UsageWindowRepository` is available on `QuotaEstimator._usage_window_repo` at the call site before relying on it; otherwise fall back to "recompute from `QuotaEstimator.accounts[...].daily_window.observations`" (the in-memory deque).
- The `_provider_models` de-dup (Phase 5) is the riskiest change. Run the full provider routing and transcoding suite (`tests/integration/test_provider_routing_e2e.py`, `tests/contract/test_transcoder_contract.py`) after applying it.
- The `max_entries` knob is exposed per-catalog rather than globally because each `TTLCache` instance is independent; a global knob would require threading it through every catalog resolver.
- Avoid introducing `cachetools.LRUCache` or `functools.lru_cache` — both would add a dependency and change the surface area more than necessary. `collections.OrderedDict.move_to_end` is stdlib and sufficient.
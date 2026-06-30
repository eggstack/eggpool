# Model Info Phase 1: Foundation, Schema, Config, and Provider-Native Observations

## Objective

Create the durable and typed foundation for Eggpool model information without introducing any new external network dependency. This phase should establish the sidecar schema, configuration model, typed records, repository, service skeleton, provider-native observation source, deterministic summary generation, and CLI inspection hooks.

At the end of this phase, Eggpool should be able to create model-info rows for every model already discovered by the existing catalog. The metadata may be sparse, but it should be explicitly represented, persisted, queryable, and ready for the background scheduler in phase 2.

## Design constraints

Do not alter routing eligibility.

Do not fetch external benchmark/catalog APIs yet.

Do not overload `models.source_metadata` or `provider_model_metadata.source_metadata` with model-info fields.

Do not add fuzzy model matching.

Do not make model-info availability part of `/readyz`.

Do not block chat/completion requests on model-info work.

Keep all raw advisory data isolated from request/usage tables.

## Current repo touchpoints

`src/eggpool/models/config.py` contains `ModelsConfig`, `PricingConfig`, and other Pydantic config sections. Add a new `ModelInfoConfig` and include it in `AppConfig`.

`src/eggpool/db/schema/` contains numbered SQLite migrations. Add a new migration for model-info sidecar tables.

`src/eggpool/catalog/service.py` and `src/eggpool/catalog/cache.py` expose enough in-memory model data to create provider-native observations.

`src/eggpool/cli_full.py` contains the full Click CLI and should receive minimal `modelinfo` commands after the service/repository exists.

## Schema plan

Add a new migration file using the next schema number. The exact number should follow the current highest migration in `src/eggpool/db/schema/`.

Create `model_info_canonical`:

```sql
CREATE TABLE IF NOT EXISTS model_info_canonical (
    model_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    summary TEXT,
    detail_json TEXT NOT NULL DEFAULT '{}',
    provenance_json TEXT NOT NULL DEFAULT '{}',
    conflicts_json TEXT NOT NULL DEFAULT '{}',
    sparse INTEGER NOT NULL DEFAULT 0,
    first_seen_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_refreshed_at TIMESTAMP,
    next_refresh_at TIMESTAMP,
    FOREIGN KEY (model_id) REFERENCES models(model_id) ON DELETE CASCADE
);
```

Create `model_info_observations`:

```sql
CREATE TABLE IF NOT EXISTS model_info_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    model_id TEXT,
    provider_id TEXT,
    source TEXT NOT NULL,
    source_model_id TEXT NOT NULL,
    observed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMP,
    confidence REAL NOT NULL DEFAULT 0.5,
    raw_hash TEXT NOT NULL,
    normalized_json TEXT NOT NULL DEFAULT '{}',
    raw_json TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (model_id) REFERENCES models(model_id) ON DELETE CASCADE,
    UNIQUE(source, source_model_id, raw_hash)
);
```

Create `model_info_aliases`:

```sql
CREATE TABLE IF NOT EXISTS model_info_aliases (
    model_id TEXT NOT NULL,
    provider_id TEXT,
    alias TEXT NOT NULL,
    source TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 0.5,
    active INTEGER NOT NULL DEFAULT 1,
    first_seen_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (provider_id, alias, source),
    FOREIGN KEY (model_id) REFERENCES models(model_id) ON DELETE CASCADE
);
```

Create `model_info_source_health`:

```sql
CREATE TABLE IF NOT EXISTS model_info_source_health (
    source TEXT PRIMARY KEY,
    enabled INTEGER NOT NULL DEFAULT 1,
    last_success_at TIMESTAMP,
    last_error_at TIMESTAMP,
    last_error_class TEXT,
    last_error_message TEXT,
    cooldown_until TIMESTAMP
);
```

Add indexes:

```sql
CREATE INDEX IF NOT EXISTS idx_model_info_canonical_status
    ON model_info_canonical(status);

CREATE INDEX IF NOT EXISTS idx_model_info_canonical_next_refresh
    ON model_info_canonical(next_refresh_at);

CREATE INDEX IF NOT EXISTS idx_model_info_observations_model_source
    ON model_info_observations(model_id, source);

CREATE INDEX IF NOT EXISTS idx_model_info_observations_source_model
    ON model_info_observations(source, source_model_id);

CREATE INDEX IF NOT EXISTS idx_model_info_aliases_alias
    ON model_info_aliases(alias);
```

Use `ON DELETE CASCADE` for phase 1. Preserve withdrawn model metadata through tombstones only if a later phase needs historical model-info pages.

## Config plan

Add these Pydantic models to `src/eggpool/models/config.py`:

```python
class ModelInfoSourceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    priority: int = Field(default=100, ge=0)
    ttl_seconds: int = Field(default=86_400, gt=0)
    base_url: str | None = None
    api_key: str | None = None
    api_key_env: str | None = None
    max_entries: int = Field(default=4096, gt=0)
    options: dict[str, object] = Field(default_factory=dict[str, object])

    @property
    def resolved_api_key(self) -> str | None:
        if self.api_key:
            return self.api_key
        if self.api_key_env:
            return os.environ.get(self.api_key_env)
        return None
```

```python
class ModelInfoSourcesConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider_catalog: ModelInfoSourceConfig = Field(
        default_factory=lambda: ModelInfoSourceConfig(priority=0, ttl_seconds=300)
    )
    openrouter: ModelInfoSourceConfig = Field(default_factory=ModelInfoSourceConfig)
    artificial_analysis: ModelInfoSourceConfig = Field(
        default_factory=lambda: ModelInfoSourceConfig(enabled=False, priority=50)
    )
    huggingface: ModelInfoSourceConfig = Field(
        default_factory=lambda: ModelInfoSourceConfig(enabled=False, priority=200, ttl_seconds=604800)
    )
```

```python
class ModelInfoConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    startup_refresh: bool = True
    refresh_interval_s: int = Field(default=21_600, ge=0)
    known_ttl_s: int = Field(default=86_400, gt=0)
    partial_ttl_s: int = Field(default=43_200, gt=0)
    sparse_new_initial_ttl_s: int = Field(default=3_600, gt=0)
    sparse_new_later_ttl_s: int = Field(default=21_600, gt=0)
    sparse_new_accelerated_days: int = Field(default=7, ge=1)
    conflict_ttl_s: int = Field(default=7_200, gt=0)
    max_models_per_cycle: int = Field(default=50, ge=1, le=10_000)
    include_in_models_endpoint: bool = True
    store_raw_observations: bool = True
    sources: ModelInfoSourcesConfig = Field(default_factory=ModelInfoSourcesConfig)
```

Add `model_info: ModelInfoConfig = Field(default_factory=ModelInfoConfig)` to `AppConfig`.

Ensure default config generation and example config include a short `[model_info]` block only if existing config templates are maintained in the repo.

## Type definitions

Create `src/eggpool/model_info/types.py`.

Define status literals:

```python
ModelInfoStatus = Literal[
    "fresh",
    "partial",
    "sparse_new",
    "stale",
    "conflicting",
    "unmatched",
    "source_unavailable",
    "manual_override",
    "withdrawn",
]
```

Define `BenchmarkObservation` even if phase 1 does not populate it. This avoids later churn:

```python
@dataclass(frozen=True)
class BenchmarkObservation:
    benchmark_name: str
    score: float | None = None
    rank: int | None = None
    percentile: float | None = None
    version: str | None = None
    source: str = "unknown"
    observed_at: datetime | None = None
    notes: str | None = None
```

Define `SourceModelRecord`:

```python
@dataclass(frozen=True)
class SourceModelRecord:
    source: str
    source_model_id: str
    observed_at: datetime
    raw_hash: str
    raw_payload: dict[str, object]
    normalized: dict[str, object]
    aliases: tuple[str, ...] = ()
    provider_id: str | None = None
    model_id: str | None = None
    display_name: str | None = None
    family: str | None = None
    context_window: int | None = None
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None
    modalities: frozenset[str] = frozenset()
    supports_tools: bool | None = None
    supports_reasoning: bool | None = None
    input_price_per_1k: float | None = None
    output_price_per_1k: float | None = None
    benchmarks: tuple[BenchmarkObservation, ...] = ()
    release_date: date | None = None
    license: str | None = None
    confidence: float = 0.5
    sparse: bool = False
    notes: tuple[str, ...] = ()
```

Define canonical summary type:

```python
@dataclass(frozen=True)
class CanonicalModelInfo:
    model_id: str
    status: ModelInfoStatus
    summary: str | None
    sparse: bool
    detail: dict[str, object]
    provenance: dict[str, object]
    conflicts: dict[str, object]
    first_seen_at: datetime
    last_seen_at: datetime
    last_refreshed_at: datetime | None
    next_refresh_at: datetime | None
```

Keep these dataclasses conversion-friendly: repository methods should serialize dicts and dataclasses explicitly rather than relying on `asdict()` for all nested types.

## Source adapter foundation

Create `src/eggpool/model_info/sources/base.py`.

Define:

```python
class ModelInfoSource(Protocol):
    name: str

    @property
    def priority(self) -> int: ...

    async def fetch_all(self) -> list[SourceModelRecord]: ...

    async def fetch_one(self, model_id: str, *, provider_id: str | None = None) -> SourceModelRecord | None: ...
```

For phase 1, `fetch_all()` and `fetch_one()` may be no-op for non-provider sources. The key is to establish a stable interface.

Create `src/eggpool/model_info/sources/provider_catalog.py`.

This adapter should read existing in-memory `ModelCatalogCache` entries and convert them into `SourceModelRecord`s. It does not perform network I/O.

For each provider model entry, emit:

```text
source = "provider_catalog"
source_model_id = provider-scoped model id if available, otherwise model_id
model_id = base model id
provider_id = provider id
aliases = base model id, provider-scoped id, display name if safe
context/input/output = discovered/effective limits where available
modalities = derived from capabilities, e.g. text, vision
supports_tools = capabilities.supports_tools
raw_payload = provider model entry
normalized = compact normalized fields
confidence = 1.0 for provider-local facts
```

Provider-native observations should not claim public benchmark values.

## Repository plan

Create `src/eggpool/model_info/repository.py`.

Repository responsibilities:

`upsert_observation(record, model_id=None, provider_id=None)`: insert source observation by `(source, source_model_id, raw_hash)`.

`upsert_alias(model_id, provider_id, alias, source, confidence, active=True)`: maintain alias rows.

`upsert_canonical(info)`: write canonical status/detail/provenance/conflicts.

`get_canonical(model_id)`: return one canonical record.

`get_canonical_many(model_ids=None)`: return dict keyed by model ID.

`list_due(limit, now)`: list canonical rows where `next_refresh_at IS NULL OR next_refresh_at <= now`, ordered by status priority then next refresh time.

`record_source_success(source)` and `record_source_error(source, exc, cooldown_until=None)`.

`source_health_snapshot()`.

`delete_orphaned_model_info()` only if needed; otherwise rely on FK cascade.

Use the existing `Database` abstraction. Follow existing repository patterns for `fetch_all`, `execute_write`, `execute_many`, and transactions.

## Service skeleton

Create `src/eggpool/model_info/service.py`.

Phase 1 service responsibilities:

Initialize with config, db, catalog, and source list.

Load provider-native observations from catalog cache.

Create canonical rows for all catalog models.

Compute sparse status.

Generate deterministic summary.

Expose read methods for CLI and future API/dashboard.

Suggested class:

```python
class ModelInfoService:
    def __init__(self, config, db, catalog) -> None: ...

    async def load_cache(self) -> None: ...

    async def reconcile_catalog_snapshot(self, *, reason: str = "manual") -> dict[str, int]: ...

    async def refresh_provider_catalog_observations(self) -> dict[str, int]: ...

    async def get_summary(self, model_id: str) -> CanonicalModelInfo | None: ...

    async def get_summary_map(self, model_ids: Iterable[str] | None = None) -> dict[str, CanonicalModelInfo]: ...
```

The phase 1 reconciler can be intentionally simple:

A model is `sparse_new` when it has only provider-native observation and lacks at least two of: display name distinct from ID, effective context limit, tool/vision capability, pricing state, benchmark state, family/release metadata.

A model is `partial` when provider-native facts exist but benchmark/family metadata is absent.

A model is `fresh` only if enough provider-native fields exist to make the tooltip useful and no external sources are expected. In phase 1, most rows will be `partial` or `sparse_new`; this is acceptable.

## Deterministic summary generation

Create `src/eggpool/model_info/reconciliation.py` or keep private helpers in `service.py` for phase 1.

Do not use LLM-generated text. Generate summaries from fields:

Examples:

```text
"New model detected; metadata sparse. Callable via minimax. Context and benchmark details not yet verified."

"Callable via opencode_go. Local effective context limit: 200k tokens. Public benchmark metadata unavailable."

"Provider-discovered text/vision model with tool support. External benchmark metadata unavailable."
```

Rules:

If `sparse_new`, summary must explicitly say metadata is sparse.

If effective context limit exists, include it in compact scaled form.

If provider count is available, mention provider(s) only in detail JSON; avoid long summaries.

If conflicts exist, summary should lead with `Metadata conflict detected`.

## CLI commands

Add a `modelinfo` command group in `src/eggpool/cli_full.py` only after the repository/service is implemented.

Initial commands:

```text
eggpool modelinfo show <model-id>
eggpool modelinfo list [--status sparse_new|partial|fresh|conflicting]
eggpool modelinfo refresh [--provider-catalog-only]
```

Implementation approach:

Load `AppConfig` from config path.

Open `Database`.

Run migrations.

Construct minimal catalog service enough to load cached models. Avoid live provider refresh unless the command explicitly requests it in a later phase.

Construct `ModelInfoService` and call provider-catalog reconciliation.

Print concise JSON or table-like output. Do not add rich terminal dependencies.

## Tests

Add repository tests using in-memory SQLite or temp DB:

`test_model_info_migration_creates_tables`

`test_model_info_repository_upserts_canonical`

`test_model_info_repository_deduplicates_observations_by_hash`

`test_model_info_repository_lists_due_rows`

Add service tests:

`test_provider_catalog_source_emits_observations_from_cache`

`test_reconcile_catalog_snapshot_creates_sparse_rows`

`test_summary_mentions_sparse_for_new_sparse_model`

`test_manual_absence_of_external_sources_does_not_fail`

Add config tests:

`test_model_info_config_defaults`

`test_model_info_source_api_key_env_resolution`

`test_model_info_rejects_unknown_config_keys`

## Manual verification

Start Eggpool with existing config.

Run migrations.

Confirm existing `/v1/models` behavior is unchanged in phase 1.

Run `eggpool modelinfo refresh --provider-catalog-only`.

Run `eggpool modelinfo list` and confirm rows exist for catalog models.

Run `eggpool modelinfo show <model>` and confirm status, summary, provenance, and sparse flag render.

Restart Eggpool and confirm model-info rows persist.

## Acceptance criteria

A new `[model_info]` config section exists with safe defaults.

SQLite migrations add sidecar tables without altering routing-critical tables.

Provider-native catalog observations can be generated from existing catalog cache.

Canonical model-info rows are persisted for discovered models.

Sparse/partial status is computed deterministically.

CLI can list, show, and provider-refresh model-info records.

No external network source is required.

No routing behavior changes.

`/v1/models` output remains unchanged in this phase unless an explicit minimal debug field is intentionally added behind config, which is not recommended until phase 4.

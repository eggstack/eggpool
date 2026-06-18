# Multi-Provider Support

## Goal

Extend the aggregator to support multiple upstream providers (OpenCode Go, MiniMax,
GeneralCompute, etc.), each with its own base URL, account pool, supported protocols,
and model catalog. Load-balancing within each provider's account pool uses the same
quota-aware logic as today. Models are exposed to clients with provider-suffixed IDs
(e.g., `claude-sonnet-4/opencode-go`, `minimax-m2.7/minimax`).

## Provider Research Summary

| Provider | Base URL | Auth | Protocols | Model List Endpoint | Notes |
|----------|----------|------|-----------|-------------------|-------|
| OpenCode Go | `https://opencode.ai/zen/go/v1` | Bearer | openai, anthropic | `GET /models` | Current default |
| MiniMax | `https://api.minimaxi.com` | Bearer | openai, anthropic | `GET /v1/models` | Anthropic path: `/anthropic/v1/messages` |
| GeneralCompute | `https://api.generalcompute.com` | Bearer | openai | `POST /v1/models/list` | POST not GET for model list |

All providers use `Authorization: Bearer <key>`. No auth variation needed for MVP.

## Constraints

- Backward compatible: flat `[[accounts]]` configs auto-create default `opencode-go` provider
- Follow existing code conventions (`from __future__ import annotations`, type hints, Pydantic v2, aiosqlite)
- No external JS/CSS dependencies
- Must pass: `ruff format`, `ruff check`, `pyright`, `pytest`
- API keys never stored in SQLite (env var names only)

---

## Phase 1: Config Restructuring

### 1a. New `ProviderConfig` model

**File:** `src/go_aggregator/models/config.py`

```python
class ProviderConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str                                    # unique, e.g. "opencode-go"
    base_url: str                              # upstream base URL
    protocols: list[str] = ["openai"]          # supported protocols
    openai_path: str = "/chat/completions"     # path for OpenAI protocol
    anthropic_path: str = "/messages"          # path for Anthropic protocol
    models_method: str = "GET"                 # "GET" or "POST" for model list
    models_path: str = "/models"               # model list endpoint path
    connect_timeout_s: float = Field(default=5, gt=0)
    read_timeout_s: float = Field(default=300, gt=0)
    write_timeout_s: float = Field(default=30, gt=0)
    max_connections: int = Field(default=100, gt=0)
    max_keepalive: int = Field(default=20, gt=0)
    keepalive_timeout_s: float = Field(default=30, ge=0)
    accounts: list[AccountConfig] = []
```

### 1b. Restructure `AppConfig`

Replace `accounts: list[AccountConfig]` with `providers: dict[str, ProviderConfig]`.
Keep `upstream: UpstreamConfig` for backward compat (used as default provider).

```python
class AppConfig(BaseModel):
    # ... existing fields ...
    upstream: UpstreamConfig = UpstreamConfig()  # backward compat default
    providers: dict[str, ProviderConfig] = {}    # new provider-centric config
    accounts: list[AccountConfig] = []           # backward compat flat accounts
```

### 1c. Auto-migration of flat config

Add `_normalize_providers()` model validator that runs after validation:

```python
@model_validator(mode="after")
def _normalize_providers(self) -> AppConfig:
    """Convert flat accounts to default provider if no providers defined."""
    if not self.providers and self.accounts:
        self.providers = {
            "opencode-go": ProviderConfig(
                id="opencode-go",
                base_url=self.upstream.base_url,
                protocols=["openai", "anthropic"],
                openai_path="/chat/completions",
                anthropic_path="/messages",
                models_method="GET",
                models_path="/models",
                accounts=self.accounts,
            )
        }
        self.accounts = []  # clear flat accounts after normalization
    return self
```

### 1d. Config example

```toml
[providers.opencode-go]
base_url = "https://opencode.ai/zen/go/v1"
protocols = ["openai", "anthropic"]

[[providers.opencode-go.accounts]]
name = "personal"
api_key_env = "OPENCODE_GO_KEY_1"

[providers.minimax]
base_url = "https://api.minimaxi.com"
protocols = ["openai", "anthropic"]
anthropic_path = "/anthropic/v1/messages"
models_path = "/v1/models"

[[providers.minimax.accounts]]
name = "minimax-prod"
api_key_env = "MINIMAX_KEY_1"

[providers.generalcompute]
base_url = "https://api.generalcompute.com"
protocols = ["openai"]
models_method = "POST"
models_path = "/v1/models/list"

[[providers.generalcompute.accounts]]
name = "gc-primary"
api_key_env = "GC_API_KEY"
```

### 1e. Validation

- `AppConfig.validate_accounts()` checks for duplicate account names **across all providers**
- `AppConfig.validate_account_credentials()` iterates all provider accounts
- Provider IDs must be unique, non-empty, alphanumeric with hyphens

**Files changed:** `models/config.py`

---

## Phase 2: Data Model Changes

### 2a. Migration `0015_multi_provider.sql`

```sql
CREATE TABLE IF NOT EXISTS providers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider_id TEXT NOT NULL UNIQUE,
    base_url TEXT NOT NULL,
    protocols TEXT NOT NULL DEFAULT '["openai"]',
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

ALTER TABLE accounts ADD COLUMN provider_id TEXT NOT NULL DEFAULT 'opencode-go';
ALTER TABLE models ADD COLUMN provider_id TEXT NOT NULL DEFAULT 'update';
```

**Note:** The `ALTER TABLE models` default is a placeholder — existing models get `provider_id = 'opencode-go'` via the data migration step.

### 2b. Backfill existing data

```sql
UPDATE accounts SET provider_id = 'opencode-go' WHERE provider_id = 'update';
UPDATE models SET provider_id = 'opencode-go' WHERE provider_id = 'update';
UPDATE requests SET provider_id = 'opencode-go'
    WHERE provider_id IS NULL OR provider_id = '';
```

### 2c. Insert default provider

```sql
INSERT OR IGNORE INTO providers (provider_id, base_url, protocols)
VALUES ('opencode-go', 'https://opencode.ai/zen/go/v1', '["openai", "anthropic"]');
```

### 2d. New domain models

**File:** `src/go_aggregator/models/domain.py`

```python
class Provider(BaseModel):
    id: int
    provider_id: str
    base_url: str
    protocols: list[str]
    enabled: bool = True
    created_at: datetime
```

### 2e. Updated database models

**File:** `src/go_aggregator/models/database.py`

```python
class AccountRow(BaseModel):
    # ... existing fields ...
    provider_id: str = "opencode-go"

class ModelRow(BaseModel):
    # ... existing fields ...
    provider_id: str = "opencode-go"
```

### 2f. Repository updates

**File:** `src/go_aggregator/db/repositories.py`

- `AccountRepository.sync_from_config()` accepts `provider_id` per account
- New `ProviderRepository` class for CRUD on providers table
- `AccountRepository.get_id_by_name()` scoped to provider (or global if unique)

### 2g. Checksums

Update `src/go_aggregator/db/schema/checksums.json` with new migration hash.

**Files changed:** New migration, `checksums.json`, `models/database.py`, `models/domain.py`, `db/repositories.py`

---

## Phase 3: HTTP Client Pool

### 3a. New `ProviderClientPool` class

**New file:** `src/go_aggregator/providers/client_pool.py`

```python
class ProviderClientPool:
    """Manages per-provider HTTPX clients."""

    def __init__(self) -> None:
        self._clients: dict[str, httpx.AsyncClient] = {}

    def register(self, provider_id: str, client: httpx.AsyncClient) -> None:
        self._clients[provider_id] = client

    def get_client(self, provider_id: str) -> httpx.AsyncClient:
        client = self._clients.get(provider_id)
        if client is None:
            raise UpstreamError(f"No client for provider {provider_id!r}")
        return client

    async def close(self) -> None:
        for client in self._clients.values():
            await client.aclose()

    @classmethod
    def from_config(cls, providers: dict[str, ProviderConfig]) -> ProviderClientPool:
        pool = cls()
        for provider_id, cfg in providers.items():
            client = httpx.AsyncClient(
                base_url=cfg.base_url,
                timeout=httpx.Timeout(
                    connect=cfg.connect_timeout_s,
                    read=cfg.read_timeout_s,
                    write=cfg.write_timeout_s,
                    pool=cfg.connect_timeout_s,
                ),
                limits=httpx.Limits(
                    max_connections=cfg.max_connections,
                    max_keepalive_connections=cfg.max_keepalive,
                    keepalive_expiry=cfg.keepalive_timeout_s,
                ),
            )
            pool.register(provider_id, client)
        return pool
```

### 3b. Update `app.py`

Replace single `httpx.AsyncClient` with `ProviderClientPool`:

```python
# Old:
app.state.httpx_client = httpx.AsyncClient(base_url=config.upstream.base_url, ...)

# New:
from go_aggregator.providers.client_pool import ProviderClientPool
app.state.client_pool = ProviderClientPool.from_config(config.providers)
```

All components that previously received `httpx_client` now receive `client_pool` (or the specific client for their provider).

### 3c. Update `CatalogService` and `RequestCoordinator`

Both accept `ProviderClientPool` instead of a single `httpx.AsyncClient`.
They look up the correct client via `pool.get_client(provider_id)`.

**Files changed:** New `providers/client_pool.py`, `app.py`, `catalog/service.py`, `request/coordinator.py`

---

## Phase 4: Provider-Aware Account Registry

### 4a. Update `AccountRegistry`

**File:** `src/go_aggregator/accounts/registry.py`

```python
class AccountRegistry:
    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._states: dict[str, AccountRuntimeState] = {}
        self._api_keys: dict[str, str] = {}
        self._account_providers: dict[str, str] = {}  # account_name -> provider_id
        self._initialize()

    def _initialize(self) -> None:
        for provider_id, provider_cfg in self._config.providers.items():
            for acct_config in provider_cfg.accounts:
                api_key = os.environ.get(acct_config.api_key_env, "")
                # ... validation ...
                self._states[acct_config.name] = state
                self._api_keys[acct_config.name] = api_key
                self._account_providers[acct_config.name] = provider_id

    def get_provider_for_account(self, account_name: str) -> str | None:
        return self._account_providers.get(account_name)

    def get_accounts_for_provider(self, provider_id: str) -> list[AccountRuntimeState]:
        return [
            s for s in self._states.values()
            if self._account_providers.get(s.name) == provider_id
        ]
```

### 4b. Update `account_config_rows()`

Returns rows grouped by provider, each row includes `provider_id`.

### 4c. Update `AccountRepository.sync_from_config()`

Accepts `provider_id` per account row and persists it.

**Files changed:** `accounts/registry.py`, `db/repositories.py`

---

## Phase 5: Provider-Aware Catalog

This is the most complex phase. The catalog must track which providers support which models, generate provider-suffixed model IDs, and fetch from different endpoints per provider.

### 5a. Update `ModelCatalogCache`

**File:** `src/go_aggregator/catalog/cache.py`

Add provider-aware support tracking:

```python
class ModelCatalogCache:
    def __init__(self) -> None:
        self._models: dict[str, dict[str, Any]] = {}
        self._account_support: dict[str, set[str]] = {}      # existing
        self._account_providers: dict[str, str] = {}          # NEW: account_name -> provider_id
        self._account_last_refresh: dict[str, float] = {}
        self._last_refresh: float = 0.0
```

New methods:

```python
def set_account_provider(self, account_name: str, provider_id: str) -> None:
    """Record which provider an account belongs to."""

def get_supporting_providers(self, model_id: str) -> set[str]:
    """Get set of provider IDs that have accounts supporting this model."""

def get_provider_suffixed_models(
    self,
    expose_mode: str,
    eligible_account_names: set[str],
) -> list[dict[str, Any]]:
    """Get models with provider-suffixed IDs for client exposure.

    For each (model_id, provider_id) pair, generate a client-facing ID
    like 'minimax-m2.7/minimax'. If a model exists on only one provider,
    the suffix is still appended for consistency.
    """
```

Update `update_from_account()` to accept `provider_id`:

```python
def update_from_account(
    self,
    account_name: str,
    provider_id: str,
    models: list[dict[str, Any]],
) -> None:
    self._account_providers[account_name] = provider_id
    # ... existing logic ...
```

### 5b. Update `ModelCatalogCache.get_models_for_exposure()`

Rename to `_get_models_for_exposure_internal()` and have the public method call `get_provider_suffixed_models()` instead. The public API returns provider-suffixed model IDs.

### 5c. Generalize `fetch_models_for_account()`

**File:** `src/go_aggregator/catalog/fetcher.py`

```python
async def fetch_models_for_account(
    client: httpx.AsyncClient,
    api_key: str,
    account_name: str,
    provider_config: ProviderConfig,
) -> dict[str, Any]:
    """Fetch models using provider-specific endpoint configuration."""
    headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}

    if provider_config.models_method.upper() == "POST":
        response = await client.post(
            provider_config.models_path,
            headers=headers,
            json={},  # some POST endpoints need empty body
        )
    else:
        response = await client.get(provider_config.models_path, headers=headers)

    response.raise_for_status()
    return response.json()
```

### 5d. Update `CatalogService.refresh()`

**File:** `src/go_aggregator/catalog/service.py`

```python
async def refresh(self) -> None:
    async with self._refresh_lock:
        await self._load_cached_models()

        # Group accounts by provider
        provider_accounts: dict[str, list[tuple[str, str]]] = {}
        for state in self._registry.get_enabled_states():
            provider_id = self._registry.get_provider_for_account(state.name)
            if provider_id is None:
                continue
            api_key = self._registry.get_api_key(state.name)
            if not api_key:
                continue
            provider_accounts.setdefault(provider_id, []).append((state.name, api_key))

        # Fetch concurrently per provider
        tasks = []
        for provider_id, accounts in provider_accounts.items():
            provider_cfg = self._config.providers.get(provider_id)
            if provider_cfg is None:
                continue
            client = self._client_pool.get_client(provider_id)
            for account_name, api_key in accounts:
                tasks.append(
                    asyncio.create_task(
                        self._fetch_and_process_account(
                            account_name, api_key, provider_id, provider_cfg, client,
                        )
                    )
                )

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        await self._persist_catalog()
```

Update `_fetch_and_process_account()` to accept `provider_id`, `provider_cfg`, and `client`.

### 5e. Update `_load_cached_models()`

When loading from the `account_models` table, also load `provider_id` from the `accounts` table and set it on the cache.

### 5f. Update `_persist_catalog()`

When upserting `account_models`, include `provider_id` from the cache's `_account_providers` mapping.

**Files changed:** `catalog/cache.py`, `catalog/fetcher.py`, `catalog/service.py`

---

## Phase 6: Provider-Aware Routing

### 6a. Parse provider suffix from model ID

**New utility in `src/go_aggregator/routing/provider.py`:**

```python
def parse_model_provider(model_id: str) -> tuple[str, str | None]:
    """Parse 'model-id/provider-id' into (model_id, provider_id).

    If no '/', returns (model_id, None).
    """
    if "/" in model_id:
        parts = model_id.rsplit("/", 1)
        return parts[0], parts[1]
    return model_id, None

def format_model_provider(model_id: str, provider_id: str) -> str:
    """Format as 'model-id/provider-id'."""
    return f"{model_id}/{provider_id}"
```

### 6b. Update `get_eligible_accounts()`

**File:** `src/go_aggregator/routing/eligibility.py`

Add `provider_id` parameter:

```python
def get_eligible_accounts(
    all_states: list[AccountRuntimeState],
    model_id: str,
    catalog: ModelCatalogCache,
    health_manager: HealthManager | None = None,
    stale_after_s: float | None = None,
    provider_id: str | None = None,  # NEW
) -> list[AccountRuntimeState]:
```

When `provider_id` is specified, filter to only accounts belonging to that provider. The model support check uses the catalog's per-account support as before.

### 6c. Update `Router`

**File:** `src/go_aggregator/routing/router.py`

`select_account()` and `get_eligible_account_names()` accept `provider_id` parameter and pass it through to `get_eligible_accounts()`.

### 6d. Update `ProxyRequestContext`

**File:** `src/go_aggregator/request/coordinator.py`

Add `provider_id: str | None = None` to `ProxyRequestContext`. This is set after parsing the client's model ID.

### 6e. Update `SelectedAttempt`

Add `provider_id: str` field.

**Files changed:** New `routing/provider.py`, `routing/eligibility.py`, `routing/router.py`, `request/coordinator.py`

---

## Phase 7: Coordinator Dispatch

### 7a. Parse provider in endpoint handlers

**Files:** `api/chat_completions.py`, `api/messages.py`

After extracting `model_id` from the request body, parse the provider suffix:

```python
from go_aggregator.routing.provider import parse_model_provider

base_model_id, provider_id = parse_model_provider(model_id)
context = ProxyRequestContext(
    model_id=base_model_id,        # internal model ID (no suffix)
    provider_id=provider_id,        # requested provider (or None)
    # ...
)
```

### 7b. Update `_select_and_persist_attempt()`

Pass `provider_id` to `Router.select_account()`. Store `provider_id` in `SelectedAttempt`.

### 7c. Update `_execute_upstream()` and `_execute_streaming()`

Use provider-specific client and paths:

```python
# Get provider-specific client
client = self._client_pool.get_client(selected.provider_id)

# Get provider-specific upstream path
provider_cfg = self._config.providers[selected.provider_id]
if context.protocol == "anthropic":
    upstream_path = provider_cfg.anthropic_path
else:
    upstream_path = provider_cfg.openai_path

# Build request with provider-specific client
request = client.build_request("POST", upstream_path, headers=headers, content=body)
```

### 7d. Update `_get_upstream_path()`

Replace with provider-config-aware lookup. Remove the hardcoded path logic.

### 7e. Update error envelope generation

When generating protocol-compatible error envelopes after retries exhausted, use the provider's protocol support to determine the error format.

**Files changed:** `api/chat_completions.py`, `api/messages.py`, `request/coordinator.py`

---

## Phase 8: API & Dashboard

### 8a. `/v1/models` endpoint

**File:** `api/models.py`

Update to use `get_provider_suffixed_models()` from the catalog cache. Return model IDs like `minimax-m2.7/minimax`.

### 8b. Dashboard render updates

**File:** `src/go_aggregator/dashboard/render.py`

- `render_overview()`: Add provider dimension to account table (show provider name column)
- `render_models()`: Show provider-suffixed model IDs
- `render_account()`: Show provider for each account
- `render_requests()`: Show provider column in request table

### 8c. Dashboard route updates

**File:** `src/go_aggregator/dashboard/routes.py`

- Account queries scoped to provider when filtering
- Stats queries include provider dimension

### 8d. JSON API updates

**File:** `src/go_aggregator/api/stats.py`

- `fetch_summary` includes `total_providers` count
- `fetch_account_stats` includes `provider_id` per account
- Add provider filter to query endpoints

### 8e. Stats queries

**File:** `src/go_aggregator/stats/queries.py`

- Add `provider_id` to group-by clauses where appropriate
- `fetch_account_stats()` returns `provider_id` per row
- New `fetch_provider_stats()` for per-provider aggregation

**Files changed:** `api/models.py`, `dashboard/render.py`, `dashboard/routes.py`, `api/stats.py`, `stats/queries.py`, `stats/__init__.py`

---

## Phase 9: CLI Updates

### 9a. `models refresh` command

**File:** `src/go_aggregator/cli.py`

Update to create per-provider clients and iterate providers.

### 9b. `accounts status` command

Show provider for each account.

**Files changed:** `cli.py`

---

## Phase 10: Tests

### 10a. Unit tests

- `tests/unit/test_config.py`: Provider config parsing, flat-to-provider normalization, validation
- `tests/unit/test_providers.py`: `ProviderClientPool`, `parse_model_provider()`, `format_model_provider()`
- `tests/unit/test_catalog.py`: Provider-suffixed model IDs, per-provider catalog refresh
- `tests/unit/test_routing.py`: Provider-filtered eligibility, provider-aware selection
- `tests/unit/test_coordinator.py`: Provider-specific client dispatch, path resolution

### 10b. Integration tests

- `tests/integration/test_provider_routing.py`: End-to-end with mocked providers
- `tests/integration/test_catalog_refresh.py`: Multi-provider catalog refresh

### 10c. Update existing tests

- All existing tests that reference `config.accounts` or `httpx_client` need updating
- Nav link tests, dashboard render tests, stats query tests

**Files changed:** Multiple test files

---

## Phase 11: Pre-commit & Commit

- `uv run ruff format --check src/ tests/ scripts/`
- `uv run ruff check src/ tests/ scripts/`
- `uv run pyright src/ scripts/`
- `uv run pytest`
- Commit and push

---

## Implementation Order

| Order | Phase | Depends On | Risk |
|-------|-------|-----------|------|
| 1 | Phase 1: Config restructuring | — | Low |
| 2 | Phase 2: Data model | Phase 1 | Low |
| 3 | Phase 3: Client pool | Phase 1 | Low |
| 4 | Phase 4: Account registry | Phase 1, 2 | Low |
| 5 | Phase 5: Catalog | Phase 3, 4 | **High** — most complex, model ID generation |
| 6 | Phase 6: Routing | Phase 4, 5 | Medium |
| 7 | Phase 7: Coordinator | Phase 3, 5, 6 | Medium |
| 8 | Phase 8: API & Dashboard | Phase 5, 7 | Low |
| 9 | Phase 9: CLI | Phase 3, 4 | Low |
| 10 | Phase 10: Tests | All | Low |
| 11 | Phase 11: Pre-commit | All | Low |

## Estimated File Changes

**New files (3):**
- `src/go_aggregator/providers/__init__.py`
- `src/go_aggregator/providers/client_pool.py`
- `src/go_aggregator/routing/provider.py`

**New migration (1):**
- `src/go_aggregator/db/schema/0015_multi_provider.sql`

**Modified files (~20):**
- `src/go_aggregator/models/config.py`
- `src/go_aggregator/models/database.py`
- `src/go_aggregator/models/domain.py`
- `src/go_aggregator/db/repositories.py`
- `src/go_aggregator/db/schema/checksums.json`
- `src/go_aggregator/accounts/registry.py`
- `src/go_aggregator/catalog/cache.py`
- `src/go_aggregator/catalog/fetcher.py`
- `src/go_aggregator/catalog/service.py`
- `src/go_aggregator/routing/eligibility.py`
- `src/go_aggregator/routing/router.py`
- `src/go_aggregator/request/coordinator.py`
- `src/go_aggregator/api/chat_completions.py`
- `src/go_aggregator/api/messages.py`
- `src/go_aggregator/api/models.py`
- `src/go_aggregator/api/stats.py`
- `src/go_aggregator/dashboard/render.py`
- `src/go_aggregator/dashboard/routes.py`
- `src/go_aggregator/stats/queries.py`
- `src/go_aggregator/stats/__init__.py`
- `src/go_aggregator/app.py`
- `src/go_aggregator/cli.py`

## Key Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Config nesting | `providers.<id>.accounts` | Clean hierarchy, natural grouping |
| Backward compat | Auto-create default provider from flat config | Zero breaking changes |
| Client strategy | Per-provider `httpx.AsyncClient` | Different base URLs, connection pools, timeouts |
| Model ID format | `model-id/provider-id` (always suffixed) | Consistent, unambiguous |
| Auth | Bearer for all providers | All researched providers use Bearer |
| Protocol routing | Per-provider `protocols` list | Provider A supports both, B only openai |
| Model list endpoint | Configurable GET/POST + path | GeneralCompute uses POST |
| Cross-provider | Separate model pools per provider | User selects provider; no auto-cross-routing |
| Provider in DB | `provider_id` FK on accounts, models, requests | Clean relational model |
| Upstream paths | Per-provider config (`openai_path`, `anthropic_path`) | MiniMax anthropic path differs |

# Bugs and Remaining Issues

Review date: 2026-06-18

Checks run during review:

- `uv run ruff format --check src/ tests/ scripts/` - passed
- `uv run ruff check src/ tests/ scripts/` - passed
- `uv run pyright src/ scripts/` - passed
- `uv run pytest` - 1065 passed, 1 Starlette/FastAPI deprecation warning

## 1. Unsuffixed multi-provider requests can dispatch to the wrong upstream

Affected code:

- `src/eggpool/request/coordinator.py`
- `SelectedAttempt.provider_id` exists, but `_select_and_persist_attempt()` sets it
  from `context.provider_id or "opencode-go"` instead of from the selected
  account.
- `_execute_non_streaming()` and `_execute_streaming()` use
  `context.provider_id` for `_get_client()` and `_get_upstream_path()` instead
  of using the provider attached to the selected attempt.

Why this is a bug:

When a client requests an unsuffixed model ID in a multi-provider setup,
routing is allowed to select any eligible account. If the selected account is
not from `opencode-go`, dispatch still falls back to the default client/path or
fails with "No HTTP client available" when no `opencode-go` client exists. In
the fallback case, the selected account's upstream key can be sent to the wrong
provider base URL.

Intended fix:

- Resolve the selected account's provider during `_select_and_persist_attempt()`
  via `AccountRegistry.get_provider_for_account(account_name)` or the catalog's
  account-provider mapping.
- Store that resolved provider on `SelectedAttempt.provider_id`.
- Use `selected.provider_id` for `_get_client()` and `_get_upstream_path()` in
  both streaming and non-streaming dispatch paths.
- If an explicit provider suffix is requested but no client exists for that
  provider, fail closed instead of silently falling back to the default client.
- Add integration coverage with at least two providers where an unsuffixed model
  selects a non-default provider and the request is sent to that provider's
  base URL with that provider's configured path.

## 2. Provider-specific request paths are not wired into the application coordinator

Affected code:

- `src/eggpool/request/coordinator.py`
- `src/eggpool/app.py`

Why this is a bug:

`RequestCoordinator` accepts `config: AppConfig | None` and
`_get_upstream_path()` reads `ProviderConfig.openai_path` and
`ProviderConfig.anthropic_path` only when `self._config` is set. The application
constructs `RequestCoordinator` without passing `config`, so production data
plane requests always fall back to `/chat/completions` or `/messages` even when
a provider configured custom request paths.

Intended fix:

- Pass `config=config` when constructing `RequestCoordinator` in the FastAPI
  lifespan.
- Keep direct-test compatibility by retaining the optional constructor
  argument.
- Add an app-level integration test with a provider whose `openai_path` or
  `anthropic_path` is non-default, and assert the upstream mock receives the
  request at that configured path.

## 3. Catalog and pricing identity collapse providers that share the same model ID

Affected code:

- `src/eggpool/catalog/cache.py`
- `src/eggpool/catalog/service.py`
- `src/eggpool/catalog/pricing.py`
- `src/eggpool/db/schema/0001_initial.sql`
- `src/eggpool/db/schema/0015_multi_provider.sql`

Why this is a bug:

The multi-provider migration adds `models.provider_id`, but the model table
still uses `model_id` as the primary key, and `ModelCatalogCache` stores model
metadata in `_models[model_id]`. If two providers advertise the same model ID,
the later refresh overwrites protocol, display name, capabilities, source
metadata, and pricing for the earlier provider. Provider-suffixed exposure can
then show two IDs such as `foo/provider-a` and `foo/provider-b` while both share
one global protocol and one global price snapshot.

This breaks endpoint validation, routing, and cost accounting when the same
model ID has different protocol support, metadata, or prices across providers.

Intended fix:

- Make provider/model identity explicit throughout the catalog, for example by
  keying cache entries and database rows by `(provider_id, model_id)`.
- Update `models`, `account_models`, and `model_price_snapshots` schema and
  repository access so provider-specific metadata and prices cannot overwrite
  each other.
- Teach `get_model()`, exposure, routing eligibility, protocol validation, and
  cost lookup to use the selected/requested provider.
- Add migration tests and catalog tests where two providers expose the same
  `model_id` with different protocols and prices.

## 4. Half-open circuit breakers can get stuck after one successful probe

Affected code:

- `src/eggpool/health/circuit_breaker.py`
- `tests/unit/test_health.py`

Why this is a bug:

`CircuitBreaker.allow_request()` sets `_half_open_in_flight = True` when it
allows a half-open probe. `record_success()` increments `_success_count`, but it
only clears `_half_open_in_flight` when `_success_count >= success_threshold`.
With the default `success_threshold = 3`, the first successful probe leaves the
breaker in `HALF_OPEN` with `_half_open_in_flight = True`, so subsequent probes
are rejected forever and the breaker cannot reach the close threshold.

The existing close test calls `record_success()` twice directly, without going
through `allow_request()`, so it misses the stuck in-flight flag.

Intended fix:

- In `record_success()`, when state is `HALF_OPEN`, clear
  `_half_open_in_flight` after each successful probe. If the success threshold
  is reached, close the breaker as it does today; otherwise leave it half-open
  and allow the next probe.
- Add a regression test that opens the breaker, waits for recovery, calls
  `allow_request()`, records one success with `success_threshold > 1`, and then
  asserts that another `allow_request()` is permitted.

## 5. Configured providers are not persisted or kept in sync

Affected code:

- `src/eggpool/app.py`
- `src/eggpool/cli.py`
- `src/eggpool/db/repositories.py`
- `src/eggpool/db/schema/0015_multi_provider.sql`

Why this is a bug:

`ProviderRepository` exists, but neither application startup nor
`models refresh` uses it. Startup and CLI refresh sync accounts from config,
but the `providers` table remains whatever the migration inserted, usually only
`opencode-go`. Custom provider base URLs and protocol lists are therefore not
durably represented in SQLite.

This is a persistence drift issue today and will become a functional bug as
soon as operational tooling, dashboard views, or routing logic rely on the
`providers` table as the source of provider metadata.

Intended fix:

- Add a provider sync step in startup and `models refresh` before account sync.
- Upsert every configured provider's ID, base URL, protocol list, enabled state,
  and any path fields that need durable representation.
- Disable provider rows removed from config rather than leaving stale active
  rows.
- Add tests that a multi-provider config creates/upserts all provider rows and
  updates changed base URLs/protocol lists.

## 6. `ProviderConfig.protocols` is mostly ignored by catalog and routing

Affected code:

- `src/eggpool/models/config.py`
- `src/eggpool/catalog/service.py`
- `src/eggpool/catalog/normalizer.py`
- `src/eggpool/routing/eligibility.py`
- `src/eggpool/request/coordinator.py`

Why this is a bug:

Providers declare supported protocols, but catalog refresh calls
`normalize_models(raw_response)` without using the provider's protocol list,
and routing eligibility only checks account/model support from the catalog. A
provider configured for only OpenAI can still be considered for an Anthropic
request if model metadata or fallback protocol resolution says the model is
Anthropic, and `_get_upstream_path()` will return an Anthropic path even when
the provider did not advertise Anthropic support.

Intended fix:

- During catalog refresh, pass a provider protocol hint to normalization when a
  provider supports exactly one protocol.
- During eligibility and endpoint validation, require the selected provider to
  support the model protocol.
- Reject or mark unresolved models whose inferred protocol is not allowed by the
  provider unless an explicit model override says otherwise.
- Add tests for an OpenAI-only provider advertising a Claude-like model and for
  an Anthropic-only provider returning an OpenAI-shaped `/models` response.

## 7. Provider suffix parsing treats every slash as a provider delimiter

Affected code:

- `src/eggpool/catalog/cache.py`
- `src/eggpool/api/chat_completions.py`
- `src/eggpool/api/messages.py`
- `tests/unit/test_catalog.py`

Why this is a bug:

`parse_model_id()` splits on the last slash for all model IDs. That works for
the internal `model/provider` exposure format, but it misparses unsuffixed base
model IDs that legitimately contain slashes. For example, a base ID like
`vendor/model-name` is interpreted as base `vendor` with provider
`model-name`.

Intended fix:

- Parse a provider suffix only when the final path segment matches a configured
  provider ID or a provider known in the catalog.
- Otherwise treat the full string as the base model ID with no provider filter.
- Update tests to cover both provider-suffixed IDs and unsuffixed slash-bearing
  model IDs.

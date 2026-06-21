---
name: architecture
description: Architecture principles and design decisions for the EggPool project. Use when understanding the codebase structure, making design decisions, or reviewing architectural changes. Covers package boundaries, request lifecycle, and core invariants.
---

# Architecture Principles

See `architecture/README.md` for the full design overview.

## Core Principles

- Package boundaries must remain explicit
- Request proxying, routing, accounting, and dashboard concerns must not be combined in endpoint handlers
- Use Pydantic v2 for all data validation
- Use aiosqlite for all database operations

## Request Lifecycle

- All data-plane requests flow through `RequestCoordinator`
- Requests must be persisted before upstream dispatch
- Pre-body failures can retry; no retry after first downstream byte emitted
- Every retryable failed attempt must reach terminal state before the next attempt
- Each attempt reservation is released exactly once via `AttemptFinalizer`

## Database Invariants

- SQLite is the durable source of truth for quota windows (5h/7d/30d)
- SQLite transactions are serialized across concurrent tasks via a single connection lock + ContextVar
- All SQL operations on the shared connection are serialized; no task can execute SQL inside another task's transaction
- Every DML write must run inside `async with db.transaction():`; write helpers refuse to operate outside an owned transaction
- `Database.vacuum()` is the only sanctioned path for `VACUUM` in production code

## Concurrency

- Readiness probes use `probe_writable()` with owned transactions, never interfere with request lifecycle work
- Child tasks cannot inherit transaction ownership (both task identity and ContextVar depth must match)
- Reservation and active-count in-memory cleanup occur only when the database reservation actually transitions
- Exhausted retries cannot corrupt another request's in-memory state

## Quota and Routing

- Successful responses without terminal usage consume the reservation estimate
- Unknown model protocols are rejected before durable selection
- Quota-exhausted accounts recover after cooldown expiration via `_refresh_transient_state()`
- Pending active requests are excluded from expiry cleanup
- Cancelled nonzero-cost requests remain in usage windows
- Cache-only rate changes create snapshots; cache-only token usage invokes cost calculation

## Multi-Provider

- Provider-suffixed model IDs: `model-id/provider-id` format
- `ProviderClientPool` manages per-provider `httpx.AsyncClient` instances
- Per-provider upstream paths: `openai_path`, `anthropic_path`, `models_path`
- Legacy flat `[[accounts]]` auto-normalizes to default `opencode-go` provider
- `parse_model_id()` in `catalog/cache.py` handles suffix parsing

### Provider Contract Rendering

`src/eggpool/providers/contract.py` centralizes:
- `compose_provider_url()` — absolute URL composition
- `build_auth_headers()` — provider-aware auth header construction
- `build_static_headers()` — static provider headers from config
- `build_upstream_headers()` — combines auth + static headers

The coordinator calls `_build_upstream_headers()` which uses the provider
contract when available, falling back to legacy Bearer auth.

## Model Context Limits

- `ModelLimitOverrideConfig` provides reusable limit fields (context, input, output, enforcement)
- Global overrides via `[model_overrides.<model-id>]`, provider overrides via `[providers.<id>.model_overrides.<model-id>]`
- `ModelLimitResolver` resolves per-field with precedence: provider > global > upstream > unknown
- `conservative_limits()` merges provider limits for unsuffixed model exposure (minimum across providers)
- `eggpool configsetup opencode --json-only` generates OpenCode config with explicit model limits
- Effective limits are configuration-derived; no database migration needed for static overrides

## Health and Failure Classification

- Health systems use a normalized `FailureCategory` vocabulary shared by `HealthManager` and `AccountRuntimeState`
- `models.resolution_status` is set to `'resolved'` for all persisted models with resolved protocols

## Error Hierarchy

- `AggregatorError` — base for all aggregator errors
- `ConfigError` — invalid or missing configuration
- `DatabaseError` — database-related failures
- `UpstreamError` — base for upstream API errors (`status_code` attribute)
  - `TemporaryUpstreamError` — temporary upstream errors (502, 503, 504)
  - `TransientUpstreamError` — transient upstream errors (retries may succeed)
  - `AuthenticationError` — upstream rejects credentials
  - `QuotaExhaustedError` — upstream account quota exhausted
  - `RateLimitError` — upstream rate-limited (`retry_after` attribute)
  - `ModelUnavailableError` — model not available upstream
- `ProxyError` — general proxy/transport errors
- `ModelNotFoundError` — requested model does not exist (`model_id` attribute)
- `NoEligibleAccountError` — no account can serve the request (503)
- `CatalogUnavailableError` — model catalog not available (503)
- `AuthenticationUnavailableError` — upstream credentials cannot be loaded (503)
- `UpstreamExhaustedError` — all upstream attempts exhausted (502)
- `AccountSuspendedError` — account suspended (503)
- `RequestTooLargeError` — request body exceeds configured limit
- `ContextLimitExceededError` — estimated request context exceeds configured model limit
- Chain exceptions with `raise ... from err` or `raise ... from None`

## Security

- Local client credentials (`Authorization`, `X-Api-Key`, `Proxy-Authorization`) are stripped before upstream forwarding
- Only the selected account's bearer token is injected
- Persisted `error_detail` is fail-closed by default; the strengthened redactor (regex + JSON sanitization) only runs when `security.persist_redacted_error_detail = true`
- Optional persisted `error_detail` uses a strict diagnostic allowlist (`SAFE_JSON_KEYS`); arbitrary provider payload keys are dropped
- Never store API keys in SQLite
- Never log prompts, completions, or API keys
- Use constant-time comparison for API key verification

## Deployment

- The systemd unit intentionally omits `ExecReload`; all configuration changes require `sudo systemctl restart eggpool`
- The `scripts/check_database.py` checker opens the database read-only via `file:...?mode=ro` and refuses to mutate anything
- The `scripts/check_database.py` checker is fail-closed: it treats missing `_migrations`, empty `_migrations`, missing required tables/columns, and query errors as exit code 2 (configuration/schema error), not zero violations
- The `scripts/smoke_test.py` stream diagnostics use a rolling tail buffer to recognize SSE markers split across arbitrary transport chunks
- `scripts/verify_upstream_auth.py` is operator-only: it bypasses EggPool to confirm the configured key works directly upstream
- Pyright in CI covers `src/` AND `scripts/`; narrow type annotations with `cast` or `Any` rather than excluding a file

## CLI Commands

- `models refresh` synchronizes configured accounts via `AccountRepository.sync_from_config` before refreshing the catalog, so cached account/model relationships match normal application startup

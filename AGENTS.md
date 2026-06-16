# AGENTS.md

Development guidelines for the opencode-go-aggregator project.

## Code Style

- Python 3.12+ with `from __future__ import annotations` in all files
- Type hints on all function signatures and return types
- Ruff for linting (E, F, W, I, N, UP, B, A, SIM, TCH rules)
- Pyright in strict mode
- Line length: 88 characters
- Use `NoReturn` for functions that never return (e.g., `sys.exit`)

## Testing

- pytest with pytest-asyncio (strict mode)
- respx for HTTPX upstream mocking
- Tests in `tests/unit/`, `tests/integration/`, `tests/contract/`
- Run: `uv run pytest`
- All 596+ tests must pass before committing

## Pre-commit Checks

Run before every commit:

```bash
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
uv run pyright src/
uv run pytest
```

All must pass with zero errors.

## Architecture Principles

- Package boundaries must remain explicit
- Request proxying, routing, accounting, and dashboard concerns must not be combined in endpoint handlers
- Use Pydantic v2 for all data validation
- Use aiosqlite for all database operations
- Never store API keys in SQLite
- Never log prompts, completions, or API keys
- Use constant-time comparison for API key verification
- All data-plane requests flow through `RequestCoordinator`
- SQLite is the durable source of truth for quota windows (5h/7d/30d)
- Requests must be persisted before upstream dispatch
- Pre-body failures can retry; no retry after first downstream byte emitted
- Every retryable failed attempt must reach terminal state before the next attempt
- Each attempt reservation is released exactly once via AttemptFinalizer
- SQLite transactions are serialized across concurrent tasks via a single connection lock + ContextVar
- Readiness probes use `probe_writable()` with owned transactions, never interfere with request lifecycle work
- Successful responses without terminal usage consume the reservation estimate
- Unknown model protocols are rejected before durable selection
- All SQL operations on the shared connection are serialized; no task can execute SQL inside another task's transaction
- Child tasks cannot inherit transaction ownership (both task identity and ContextVar depth must match)
- Reservation and active-count in-memory cleanup occur only when the database reservation actually transitions
- Exhausted retries cannot corrupt another request's in-memory state
- Quota-exhausted accounts recover after cooldown expiration via `_refresh_transient_state()`
- Pending active requests are excluded from expiry cleanup
- Cancelled nonzero-cost requests remain in usage windows
- Cache-only rate changes create snapshots; cache-only token usage invokes cost calculation
- Health systems use a normalized `FailureCategory` vocabulary shared by `HealthManager` and `AccountRuntimeState`
- `models.resolution_status` is set to `'resolved'` for all persisted models with resolved protocols
- Every DML write must run inside `async with db.transaction():`; write helpers refuse to operate outside an owned transaction
- Local client credentials (`Authorization`, `X-Api-Key`, `Proxy-Authorization`) are stripped before upstream forwarding; only the selected account's bearer token is injected
- Persisted `error_detail` is fail-closed by default; the strengthened redactor (regex + JSON sanitization) only runs when `security.persist_redacted_error_detail = true`
- The systemd unit intentionally omits `ExecReload`; all configuration changes require `sudo systemctl restart gorouter`
- The `scripts/check_database.py` checker opens the database read-only via `file:...?mode=ro` and refuses to mutate anything
- `tests/integration/test_phase17_deployment_readiness_matrix.py` is the cross-cutting release-gate for the matrix in the Phase 17 plan

For detailed architecture documentation, see `architecture/` directory:
- `phase-0.md`: Repository and tooling foundation
- `phase-1.md`: Configuration, database, and application lifecycle
- `phase-2.md`: Account registry and model discovery
- `phase-3.md`: Non-streaming transparent proxy
- `phase-4.md`: Streaming proxy
- `phase-5.md`: Usage extraction and price accounting
- `phase-6.md`: Quota-aware routing and reservations
- `phase-7.md`: Retry, failover, and health management
- `phase-8.md`: Statistics API and dashboard
- `phase-9.md`: Deployment hardening
- `phase-10-integration-hardening.md`: Integration hardening and correct request lifecycle
- `phase-12-executable-correctness-pass.md`: Executable correctness pass
- `phase-13-attempt-transaction-hardening.md`: Attempt lifecycle and transaction hardening
- `phase-14-deployment-blockers-and-operational-hardening.md`: Deployment blockers and operational hardening
- `phase-15-concurrency-accounting-correctness.md`: Concurrency and accounting correctness
- `phase-17-deployment-readiness-corrections.md`: Deployment readiness corrections

## Import Organization

Follow ruff TCH rules:
- Move type-only imports into `TYPE_CHECKING` blocks
- Use `from __future__ import annotations` to enable forward references

## Error Handling

- Use the exception hierarchy in `errors.py`
- Config errors: `ConfigError`
- Database errors: `DatabaseError`
- Upstream errors: `UpstreamError` and subclasses
- Protocol errors: `ModelNotFoundError`, `NoEligibleAccountError`, `CatalogUnavailableError`, `AuthenticationUnavailableError`, `UpstreamExhaustedError`, `AccountSuspendedError`
- Chain exceptions with `raise ... from err` or `raise ... from None`

## Git Workflow

- Branch: `main`
- Commit messages: concise, imperative mood
- Never commit secrets, API keys, or `.env` files
- Run all checks before committing

## File Organization

- Source code: `src/go_aggregator/`
- Tests: `tests/` (mirrors src structure)
- Configuration: `config.example.toml`, `.env.example`
- Database schema: `src/go_aggregator/db/schema/`

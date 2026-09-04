# D003 Closure — Catalog Refresh, Normalization, and Persistence

Status: closed

Recommendation: closed; D004 is now the sole dependency-ready M5 implementation plan. D005 remains queued under the default serial handoff policy, and D006-D008 remain blocked by their stated predecessors.

Plan: [D003 — Catalog refresh, normalization, and persistence](../../implementation/routing-domain/003-catalog-refresh-normalization-and-persistence.md)

Implementation commit: [`c956e89`](https://github.com/eggstack/eggpool/commit/c956e89)

## Closure basis

D003 is implemented on the closed M4 transport and D002 cache/hydration boundary. The Rust `CatalogService` builds the configured provider models request, uses the account-aware `ProviderClientPool`, bounds and validates JSON responses, normalizes routing metadata, applies static seeds, isolates account outcomes, persists schema-54 state, and exposes exact reappearance/withdrawal event identities. No migration 55, inference path, retry policy, finalization logic, optional external polling loop, or second outbound HTTP stack was added.

## Requirement evidence

| D003 requirement | Evidence | Result |
|---|---|---|
| M4 models-endpoint contract | `rust/src/catalog/refresh.rs` uses `ProviderClientPool::get_client` and `ProviderHttpClient::send`; supports GET/POST, configured path/query/body, auth/static headers, `Accept`, status, latency, and model-count observations. | Pass |
| Bounded response handling | `ProviderBody::read_to_bytes` enforces a 10 MiB catalog bound before JSON parsing. | Pass |
| Validation and normalization | Object + `data`-array validation, invalid-row filtering, zero-valid-row rejection, OpenAI/Anthropic identity/display/capability/protocol metadata normalization. | Pass |
| Static models and disabled endpoints | Static rows seed support before I/O; `DISABLED` produces `skipped` without freshness advancement. | Pass |
| Non-destructive outcomes | Failed, skipped, empty, and partial results preserve prior support; authoritative withdrawal is policy-gated and deterministic. | Pass |
| Concurrency and isolation | Account fetches run in independent `JoinSet` tasks; immutable results are gathered before cache mutation; refreshes are serialized by a service lock. | Pass |
| One-account recovery | `CatalogService::refresh_one_account` validates account state and executes the same bounded fetch/apply/persist path. | Pass |
| Schema-54 persistence | Existing typed catalog repositories hydrate rows; one bounded transaction applies semantic model/provider/support/freshness/ping changes, preserves first-seen values, and reconciles withdrawn durable rows. | Pass |
| Per-account freshness | Only successful empty/partial/authoritative account fetches write `catalog_refresh_state`; failures and skipped accounts do not advance freshness. | Pass |
| Reappearance/withdrawal handoff | `CatalogModelEvent` carries provider, durable account ID/name, canonical model, upstream model, and protocol. | Pass |
| Optional enrichment boundary | No external enrichment is required for routing availability; deterministic metadata remains fixture/config driven. | Pass |
| Security and ownership | Secrets are used only to build request headers, are absent from observations/results/errors, and response bodies are bounded. | Pass |

## Verification

The following checks passed:

- `rtk cargo fmt --manifest-path rust/Cargo.toml`
- `rtk cargo check --manifest-path rust/Cargo.toml`
- `rtk cargo clippy --manifest-path rust/Cargo.toml --all-targets -- -D warnings`
- `rtk cargo test --manifest-path rust/Cargo.toml --quiet`
- `rtk uv run pytest tests/unit/test_routing_provider.py tests/unit/test_catalog_withdrawal_policy.py tests/unit/test_catalog.py -q --tb=short --maxfail=1` — 108 passed
- `rtk uv run pytest tests/integration/test_catalog_persistence_reconcile.py tests/integration/test_migration_compatibility.py -q --tb=short --maxfail=1` — 24 passed
- `rtk uv run pytest tests/integration/test_catalog_cache_reload.py tests/integration/test_catalog_unresolved_models.py tests/integration/test_provider_aware_catalog.py -q --tb=short --maxfail=1` — 15 passed
- `rtk uv run pytest tests/migration_rs -q --tb=short --maxfail=1` — 53 passed, 3 skipped
- `rtk uv run ruff format --check src/ tests/ scripts/`
- `rtk uv run ruff check src/ tests/ scripts/`
- `rtk uv run pyright src/ scripts/`
- `rtk uv run pytest tests/smoke/ -q --tb=short --maxfail=1` — 14 passed

The focused Rust fixture suite in `rust/tests/catalog_refresh.rs` covers disabled/static behavior, POST/query/auth/header construction without secret leakage, failed refresh preservation after hydration, and authoritative withdrawal/reappearance with durable-state updates. The Rust-wide test gate also includes the existing transport cancellation characterization.

## Planning follow-through

D003 is removed from the dependency-ready table and recorded as completed in `migration-rs/registry.md`. D004 is promoted to the sole dependency-ready plan and its plan header is marked `ready for handoff`. D005 remains queued, despite being architecturally independent, because the registry's default policy permits only one ready plan unless parallel handoff is explicitly authorized. D006 remains blocked on D004 plus D005; D007 remains blocked on D006; D008 remains blocked on D001-D007. No other future implementation plan is unblocked by D003 alone.

The closure is evidence-based against schema 54 and the current Rust interfaces. M8 still owns periodic scheduling and generic outbound lifecycle; D005 still owns quarantine state and consumes the event boundary later.

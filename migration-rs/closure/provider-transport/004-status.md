# T004 Closure — Provider/Account Client Pool and Lifecycle Boundary

Status: closed

Recommendation: closed; T005 is dependency-ready. M5 remains blocked on the
complete M4/T005 sequence.

Implementation commit: [`71ef03d`](https://github.com/eggstack/eggpool/commit/71ef03d)

Plan: [T004 — provider/account client pool and lifecycle boundary](../../implementation/provider-transport/004-provider-account-client-pool.md)

Contract: [provider transport contract](../../provider-transport-contract.md)

## Outcome

T004 adds an immutable, cloneable Rust `ProviderClientPool` built from one
`Config`. It creates exactly one direct Hyper/Rustls client per configured
provider and one dedicated Eggress-backed client for each configured account
with a resolved proxy. Direct accounts fall back to the provider client;
configured proxy accounts never fall back to direct transport. Proxy
resolution uses the existing Rust configuration boundary and preserves the
Python precedence rules, including inline, environment, named, and
`direct://` forms.

The pool exposes stable provider inventory, client lookup, and a serialized
snapshot matching the Python diagnostic shape. Pool build errors are typed,
carry only safe provider/account identities and stable transport categories,
and are all-or-nothing: partially built clients are dropped when a later
provider or proxy fails.

The migration-stage server now builds the pool after listener reservation,
database migration, and account synchronization, stores it in `AppState`, and
drops it with the server state after graceful shutdown. A pool-build failure
closes the database before returning and releases the already-bound listener
through normal ownership scope. No request dispatch, routing, retries,
credentials, or generation swapping was added.

## Requirement matrix

| Requirement | Evidence | Result |
|---|---|---|
| Empty config is supported | `provider_client_pool_builds_empty_and_provider_topologies` | Pass |
| One direct client per provider | same topology test; `ProviderClientPool::from_config` | Pass |
| Direct-account fallback | `provider_client_pool_uses_direct_fallback_and_dedicated_proxy_clients` | Pass |
| Dedicated proxied account clients | same topology test; `direct://` account case | Pass |
| Named/inline/environment proxy resolution | `Config::resolve_account_proxy_url`; existing T001 resolution matrix; pool construction uses it unchanged | Pass |
| Missing provider is local and typed | `provider_client_pool_fails_closed_for_missing_and_malformed_clients` | Pass |
| Malformed proxy fails closed without direct fallback | same failure test; `ProviderClientPoolError::AccountTransport` | Pass |
| Snapshot shape and stable ordering | exact JSON assertion in topology tests; `BTreeMap`/sorted account keys | Pass |
| Build-time construction, not request-time construction | immutable maps and unchanged snapshot after repeated lookup | Pass |
| Separate physical pools for identical proxy URIs | `proxied_accounts_keep_separate_pools_even_with_identical_proxy_uris`; two proxy/upstream connections observed | Pass |
| No upstream contact during construction | construction tests use non-routable example authorities and perform no network operation | Pass |
| Server state and failed-start cleanup | `failed_pool_build_after_bind_closes_database_and_releases_listener` | Pass |
| Secret-safe errors and snapshots | synthetic proxy marker assertions; snapshot contains only provider/account names | Pass |
| Dependency/resource constraint | `rust/Cargo.toml` and `Cargo.lock` unchanged; pool uses standard collections and existing T002/T003 clients | Pass |

## Differential and lifecycle evidence

Controlled Rust cases preserve the Python topology facts: one provider
default, dedicated clients only for resolved proxies, direct fallback, sorted
safe account inventory, and stable build counts. The identical-proxy isolation
case sends one request through each account client and observes two provider
connections, proving the accounts do not share one Hyper connection pool.

The server cleanup case forces a malformed account proxy after listener and
SQLite acquisition. It verifies the returned typed pool error contains no
synthetic secret marker, the port can immediately be rebound, and the
migrated database can be reopened and closed cleanly. Transport client
teardown remains drop-driven by the T002 Hyper client; there is no background
shutdown task or second lifecycle manager.

No live provider, external proxy, API key, or real proxy credential was used.

## Verification evidence

Commands completed successfully:

- `rtk cargo fmt --manifest-path rust/Cargo.toml -- --check`;
- `rtk cargo clippy --manifest-path rust/Cargo.toml --all-targets -- -D warnings`;
- `rtk cargo test --manifest-path rust/Cargo.toml --test provider_transport -- --test-threads=1` — 23 passed;
- `rtk cargo test --manifest-path rust/Cargo.toml -- --test-threads=1` — 46 passed;
- `rtk uv sync --frozen --extra ci`;
- `rtk uv run --extra proxy pytest tests/migration_rs -q --tb=short --maxfail=1` — 49 passed;
- `rtk uv run --extra proxy pytest tests/unit/test_provider_client_pool.py tests/unit/test_pproxy_transport.py tests/unit/test_config.py -q --tb=short --maxfail=1` — 139 passed;
- `rtk uv run ruff format --check src/ tests/ scripts/`;
- `rtk uv run ruff check src/ tests/ scripts/`;
- `rtk uv run pyright src/ scripts/` — 0 errors, 0 warnings, 0 informations;
- `rtk uv run pytest tests/smoke/ -q --tb=short --maxfail=1` — 14 passed;
- `rtk git diff --check`.

## Security, lifecycle, and known limitations

- Pool errors never retain resolved proxy URLs, proxy userinfo, environment
  values, provider API keys, or underlying Eggress error text.
- Snapshots contain only counts and provider/account names. Provider base URLs,
  credentials, proxy endpoints, and request data are absent.
- The pool intentionally has no Python-style in-place registration or displaced
  client registry. Whole-pool drop is the migration-compatible lifecycle
  boundary; generation replacement remains M8 work.
- Transport response-body cancellation and individual connection cleanup remain
  T002/T003 responsibilities. T004 qualifies aggregate pool ownership and
  shutdown only.
- Provider dispatch, account eligibility, health, quota, retry, finalization,
  and rehash are explicitly downstream.

Unresolved mandatory findings: none.

## Future-plan state

T004 is closed because the provider/account topology, proxy resolution,
account isolation, safe snapshot, all-or-nothing construction, and server
cleanup criteria pass. T005 is moved from queued/blocked to dependency-ready
because T001-T004 now have accepted closure records. M5 remains blocked on
T005/M4 closure, and no other future plan is safely unblocked by T004 alone.

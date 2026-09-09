# O007 Closure — Operator Inspection, Maintenance, and Metrics Flush

Status: closed

Implementation commit: [`94fddcc`](https://github.com/eggstack/eggpool/commit/94fddcc1561a98e116f5489046a2d6b75f989268)

Plan: [O007 — operator inspection, model/stats maintenance, and metrics flush](../../implementation/operations/007-operator-inspection-maintenance-and-metrics-flush.md)

## Acceptance summary

O007 is implemented in the Rust candidate and formally closed. Every owned
operator command now reaches a Rust operation service; the CLI remains an
adapter for configuration, lifecycle, and presentation. The real
`metrics_flush` capability is registered with the existing M8 process task
supervisor, and request finalization contributes only bounded scalar usage
facts to its coalescer. No Python fallback, second scheduler, external
metrics store, or Rust-only schema was added.

## Command/domain ownership and execution classification

| Command | Rust owner | Classification | Mutation/network |
|---|---|---|---|
| `accounts list` | config account projection | offline-safe | read-only; no network |
| `accounts status` | config account/status projection | offline-safe | read-only; no network |
| `accounts explain` | M5 `RoutingRouter::build_routing_plan` | local DB only | read-only; no claims, reservations, fairness mutation, selector, or network |
| `models refresh` | M5 `CatalogService::refresh` | may use external network | bounded provider refresh with per-account failure isolation |
| `modelinfo aliases/list/show` | canonical model-info tables | local DB only | read-only, bounded projections |
| `modelinfo refresh` | catalog service plus canonical model-info upsert | may use external network | bounded catalog refresh and canonical/provider-catalog reconciliation |
| `modelinfo repair` | canonical model-info backfill service | local DB only | bounded, idempotent detail repair |
| `stats transcoding` | request protocol aggregation | local DB only | read-only, validated period |
| `stats recompute-costs` | pricing snapshot resolver and cost operation | local DB only | dry-run by default; apply uses a transaction and skips provider-reported rows |
| `stats repair-costs` | cost repair operation | local DB only | dry-run by default; apply uses a transaction and writes repair audit rows |
| `stats explain-dashboard` | SQLite `EXPLAIN QUERY PLAN` projections | local DB only | read-only; current dashboard query families only |
| `dashboard public` | already closed by O004 | requires running server | not duplicated in O007 |
| `metrics_flush` | process-owned `MetricsWriteCoalescer` | process task | bounded additive `usage_rollups` upserts; no raw bodies or secrets |

Commands that can use providers reuse the existing provider client pool,
outbound stack, catalog service, and generation construction boundary. No
operator command starts a competing server runtime or recurring loop.

## Differential output matrix

| Python oracle surface | Rust projection | Parity decision |
|---|---|---|
| account list/status | deterministic provider/account rows with enabled, priority, weight, credential-presence, and environment-name fields | secret-safe semantic parity; Rust never prints credential values |
| account explain | eligible accounts, stable exclusion reason codes, optional scores and gate projection, catalog/health versions | observational routing-plan parity; no request claim or selector invocation |
| model refresh | model/new/withdrawn counts and account success/failure/skip counts | catalog-service semantic parity and failure isolation |
| model-info list/show/aliases | canonical status, sparse/detail/provenance/conflict/timestamp fields and source-filtered aliases | canonical schema parity; no-result/error behavior is explicit |
| model-info refresh/repair | provider-catalog reconciliation counts and bounded legacy-detail repair counts | provider-catalog-only refresh and idempotent bounded repair |
| transcoding stats | period, totals, native/transcoded counts, deterministic direction map, warning slot | JSON and human projection preserve O001 fields |
| cost recompute/repair | dry-run/apply summaries, limits/filters, current pricing, provider-reported exclusion, change/breakdown output, audit records | current schema semantics preserved; repair is idempotent |
| dashboard explain | period/bucket/group-by and six named query plans | read-only plans use the canonical dashboard query families |
| metrics coalescer | additive keyed rollups, immediate/buffered modes, bounded capacity, failure counters and shutdown flush | process-owned M8 capability, not a placeholder |

## Implementation evidence

### Operator services

- `rust/src/operations/operator.rs` owns all SQL, catalog refresh, routing
  explain, model-info, statistics, pricing, and repair behavior.
- Cost recompute and repair honor `--limit` caps, provider filters, `since`,
  provider-reported cost exclusion, persisted local cost preference, current
  and legacy price snapshot columns, dry-run behavior, and repair audit fields.
- Model-info reads and refreshes are capped at `MAX_OPERATOR_ROWS`; repair is
  capped by its explicit limit and skips malformed rows without aborting the
  rest of the batch.
- Account explanation uses `RoutingRequestFacts` and the read-only routing
  plan. It does not call selection, claim, reservation, fairness-advance, or
  semantic-router APIs.

### Metrics and lifecycle

- `MetricsWriteCoalescer` aggregates only bounded scalar terminal fields by
  the canonical rollup key and writes additive `usage_rollups` upserts in one
  SQLite transaction.
- Canonical UTC bucket strings are used, with configured bucket-size
  normalization. Distinct rollup rows and pending events are both bounded;
  failed writes are re-buffered only within those same limits and increment
  drop/failure diagnostics when capacity is unavailable.
- `ProcessRuntime::new_with_config` registers the singleton `metrics_flush`
  callback. `runtime_task_specs_for_config` continues to own enablement and
  interval staging, including immediate-mode disablement. The server performs
  one deadline-bounded final flush during shutdown.
- Finite and streaming terminal paths contribute the same redacted scalar
  usage shape; bodies, headers, API keys, route-session values, and arbitrary
  provider diagnostics never enter the metrics buffer.

## Schema, dependency, and security review

O007 uses existing migrations and tables: `models`, `model_info_canonical`,
`model_info_aliases`, `requests`, `model_price_snapshots`,
`request_cost_repairs`, and `usage_rollups`. No migration or schema fork was
introduced. No new crate dependency was added. The operation services use the
existing serialized SQLite connection, provider client pool, catalog service,
router, and M8 task supervisor.

CLI and diagnostics projections omit secrets and raw request/provider bodies.
Cost repair writes only bounded scalar audit fields. Metrics keys and
diagnostics are bounded and use no external datastore. Read-only explain and
EXPLAIN paths do not write durable state.

## Verification evidence

Commands actually run:

```text
rtk cargo fmt --manifest-path rust/Cargo.toml -- --check                         PASS
rtk cargo check --manifest-path rust/Cargo.toml                                  PASS
rtk cargo clippy --manifest-path rust/Cargo.toml --all-targets -- -D warnings    PASS
rtk cargo test --manifest-path rust/Cargo.toml --test operations_o007 -- --test-threads=1 PASS (3)
rtk cargo test --manifest-path rust/Cargo.toml --test operations_o006 -- --test-threads=1 PASS (4)
rtk cargo test --manifest-path rust/Cargo.toml --test runtime_lifecycle_r008 -- --test-threads=1 PASS (3)
rtk cargo test --manifest-path rust/Cargo.toml --test runtime_lifecycle_r012 -- --test-threads=1 PASS (10)
rtk cargo test --manifest-path rust/Cargo.toml --test runtime_lifecycle_r013 -- --test-threads=1 PASS (7)
rtk cargo test --manifest-path rust/Cargo.toml --test catalog_refresh -- --test-threads=1 PASS (4)
rtk cargo test --manifest-path rust/Cargo.toml --test routing_domain -- --test-threads=1 PASS (12)
rtk uv run pytest tests/unit/test_accounts.py tests/unit/test_model_info.py tests/unit/test_model_info_aliases.py tests/unit/test_model_info_refresh.py tests/unit/test_model_info_legacy_backfill.py tests/unit/test_cost_inflation_guards.py tests/unit/test_cost_repair.py tests/unit/test_cli_stats_transcoding.py tests/unit/test_dashboard_indexes.py tests/unit/test_metrics_buffer.py tests/unit/test_metrics_coalescer_invariants.py tests/unit/test_metrics_lifecycle.py -q --tb=short --maxfail=1 PASS (191)
rtk cargo run --manifest-path rust/Cargo.toml -- --config config.example.toml accounts list PASS
rtk git diff --check                                                            PASS
```

The multi-binary aggregate invocation was not used as closure evidence: on
this host, a parallel/combined link attempt hit an Apple clang linker
segmentation fault before tests ran. The affected Rust binaries passed when
run individually as recorded above; this is an environment/toolchain issue,
not a failing O007 assertion.

## Unresolved findings and downstream transition

No unresolved O007 correctness, security, data-loss, compatibility, resource,
or lifecycle finding remains. The dashboard public mutation/projection is
already covered by O004 and was not duplicated. Live-provider qualification,
deployment/install artifacts, update behavior, broad OS/SBC characterization,
and M9 aggregate qualification remain later-plan scope.

O007 is removed from the ready queue and recorded as closed. Per the accepted
plan, only O008 is promoted: its status is now `dependency-ready; O007 closure
accepted`. O009 remains queued behind O008 and O010 remains queued behind O009.
M10 remains blocked on accepted O010 closure; no later milestone is unblocked
by O007 alone.

# Dashboard Milestone 002 — Closure Status

Status: closed

Source implementation plan:

- `plans/implementation/dashboard/002-shared-shell-and-dashboard-api-restoration.md`

Source subsystem roadmap:

- `plans/subsystems/dashboard-roadmap.md#milestone-002--shared-shell-interaction-and-dashboard-json-contract-restoration`

Repository baseline reviewed: `286b70ab3aef6b3979a033f8ee5d8f72006ffa56`

Implementation commits:

- `da8183d` — restore the shared shell, bounded timeseries APIs, chart DOM hooks, and API documentation.
- `00c69f5` — qualify matched Overview and Timeseries browser interactions at desktop and mobile sizes.
- `1253d04` — match historical fallback behavior for unknown grouped dimensions, metrics, and 30-day buckets.

## 1. Executive finding

M002 is closed. Every dashboard page uses the same Rust layout; the strict
shared-shell projection matches the pinned Python oracle on all 14 routes in
both empty and populated states. The historical timeseries endpoints, their
auth policy, chart DOM hooks, and shared interactions are restored. Browser
qualification passed for both implementations at both viewport sizes. The
full parity report still has 41 cells assigned to later page/API milestones;
those known gaps do not block the shell and timeseries contract delivered here.

## 2. Requirement-to-evidence matrix

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| One canonical shell for all dashboard pages | `render_dashboard_layout`; paired parity report `/tmp/dashboard-m002-final-paired.md` and JSON | pass | Zero shared-shell mismatches across 28 route/state comparisons. |
| Historical shell details, nav, themes, periods, chart loading, and refresh rules | Strict DOM comparator and HTML contract in `rust/src/server/dashboard.rs` | pass | Includes exact theme order, `runtime`/`recent` link state, page-specific chart inclusion, and auto-refresh pages/intervals. |
| Bounded `/api/timeseries` and `/api/timeseries/grouped` API shape | `DashboardRepository::{timeseries_json,grouped_timeseries_json}` and oracle comparison | pass | Empty and populated projections matched; grouped results prefer usage rollups and have a bounded request-row fallback. Unknown dimensions and 30-day buckets use historical defaults; `metric` remains accepted with request-count ranking. |
| Public/private dashboard auth | Oracle matrix and `dashboard_timeseries_routes_preserve_auth_and_validate_periods` | pass | Both chart routes matched 200/401 behavior; invalid periods return 400. |
| Correct canvas/data hooks and valid shared markup | Rust renderer unit tests and full DOM projection | pass | `#timeseries-chart` is a canvas; grouped payload and controls are present. |
| Interaction, network, and console qualification | Eight paired CDP runs in `/tmp/dashboard-m002-final-paired.json` | pass | Python and Rust each passed Overview and Timeseries at desktop and mobile widths; no console errors, failed same-origin loads, or same-origin HTTP errors. |
| Static and theme asset identity | Full parity report | pass | All 54 static/theme assets matched the frozen oracle. |
| API/auth/content gaps outside M002 | Full parity report | deferred | Remaining gaps are assigned to M003–M006 below; no timeseries API or common-shell gap remains. |

## 3. Production implementation evidence

`rust/src/server/dashboard.rs` routes Overview through the shared layout,
restores the common historical shell, emits page-specific chart preload and
refresh behavior, fixes the Timeseries canvas/control hooks, and builds the
bounded grouped API response. `rust/src/server/mod.rs` registers both chart
routes. `rust/src/db/repositories.rs` owns the bounded rollup/raw projection
reads on the existing SQLite gate. `rust/tests/server_transport.rs` covers
dashboard auth, successful empty responses, and invalid-period rejection.
The qualification harness captures browser console/runtime exceptions,
same-origin load failures, HTTP errors, and matched desktop/mobile interaction
facts. CSS, JavaScript, chart, favicon, and theme asset bytes did not change.

## 4. Verification executed

### Commands run

```text
rtk cargo fmt --manifest-path rust/Cargo.toml --all -- --check
rtk cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
rtk cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
rtk cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
rtk cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
rtk cargo test --manifest-path rust/Cargo.toml --test server_transport -- --test-threads=1
rtk cargo test --manifest-path rust/Cargo.toml --workspace --lib timeseries_chart_contract_uses_a_canvas_even_without_rows -- --test-threads=1
rtk cargo test --manifest-path rust/Cargo.toml --workspace --lib overview_uses_the_shared_layout_and_keeps_valid_empty_account_markup -- --test-threads=1
rtk uv run ruff format --check scripts/ tests/tooling/
rtk uv run ruff check scripts/ tests/tooling/
rtk uv run pyright scripts/
rtk uv run pytest tests/tooling/ -q --tb=short --maxfail=1
rtk uv run pytest tests/tooling/test_dashboard_parity_projection.py -q --tb=short
rtk git diff --check
```

Historical-oracle qualification was run locally against the pinned detached
Python source, synthetic fixtures, and the current Rust binary:

```text
rtk proxy env EGGPOOL_DASHBOARD_CANDIDATE_SHA=1253d04dbb7115e461f5344ddf169f3f873b1ac5 /tmp/eggpool-dashboard-oracle-m002/.venv/bin/python scripts/qualification_dashboard_parity.py --skip-build --screenshots --output /tmp/dashboard-m002-closure.json --markdown /tmp/dashboard-m002-closure.md
```

### Results

- Rust formatting, default Clippy, no-default check, and no-default Clippy passed.
- The full default serial Rust suite passed after the final implementation commit: 804 tests across 65 suites in 275.97 seconds.
- The focused `server_transport` suite passed: 13 tests, including invalid-period, invalid-dimension fallback, metric compatibility, bucket fallback, and limit clamping; both dashboard renderer unit tests passed.
- Ruff format/check and Pyright passed. The full tooling suite passed: 146 passed, 1 skipped. The focused parity projection suite passed after the final browser-matrix change: 20 passed.
- Local oracle qualification against candidate `1253d04dbb7115e461f5344ddf169f3f873b1ac5` captured 28 screenshots, matched 54 static/theme assets, passed all 28 shared-shell route/state comparisons, and passed all eight desktop/mobile interaction runs for both implementations. Browser checks reported zero JavaScript exceptions, console errors, failed same-origin loads, or same-origin HTTP errors.
- The full report has 41 remaining parity cells: 6 Overview/Accounts/Models cells, 15 telemetry/routing/trace cells, 4 Runtime/Cache cells, and 16 historical stats/API/auth cells. None is a shared-shell or timeseries endpoint mismatch.
- No CI result is claimed; all listed results are local.

## 5. Invariant review

- Dashboard handlers remain bounded read-plane adapters. They use the existing `Database` serialization gate and do not move routing, inference, retries, or finalization into dashboard code.
- Public/private dashboard auth remains in the existing middleware. `/v1/*`, `/api/integrations/*`, and operational runtime/update/status routes retain their existing auth classification.
- Dynamic HTML remains escaped and JSON responses contain only aggregate bucket/series fields. No prompt, raw body, credential, cache key, or provider body is exposed.
- Static asset bytes are unchanged. Chart.js remains conditionally loaded according to page-owned hooks and oracle behavior.

## 6. Failure and recovery review

Invalid period/group/metric input receives bounded 4xx responses; dashboard read failure returns the existing redacted degraded response. The added handlers create no background tasks and do not change server shutdown, cancellation, or inference behavior. Row, bucket, group, and series limits are bounded. Browser checks exercised refresh failures/error reporting paths only for successful fixture requests; cancellation semantics remain owned by the existing Axum/EggServe lifecycle.

## 7. Migration and compatibility review

No schema, configuration, or migration change was added. The chart APIs are additive dashboard-gated reads; the existing summary API remains intact. Query periods are validated against the existing four-period set. The grouped route validates supported dimensions/metrics and clamps its series limit. Static assets and their hashes remain unchanged.

## 8. Security review

Both chart routes follow `[dashboard].public`; private mode requires the configured key. SQL values use bound parameters, query time/row bounds are explicit, and dynamic filters never become SQL identifiers. The fixed group/metric selector maps are allow-listed. Responses do not contain secrets or raw request content.

## 9. Documentation and operations

`architecture/deep-dive-dashboard.md` describes the two chart endpoints and their auth/bounds. `docs/api-reference.md` lists both paths and their query contract. The canonical oracle and qualification instructions remain in the M001 fixture and script documentation.

## 10. Unresolved findings

| Severity | Finding | Impact | Required action |
|---|---|---|---|
| medium | Overview, Accounts, Models, and Model Detail still have 6 parity cells | Page-owned structure/content remains reduced | M003 |
| medium | Telemetry, reliability, routing, and trace pages still have 15 parity cells | Page-owned structures/content remain reduced | M004 |
| medium | Runtime/Cache surfaces and historical stats endpoints account for 4 page cells and most of the 16 stats/API/auth cells | Operator telemetry surfaces remain incomplete | M005; reconcile the existing summary projection in M005/M006 |

These findings are pre-existing M001 parity gaps assigned to later capability
milestones; they are not regressions introduced by M002.

## 11. Roadmap disposition

Milestone closed; M003 and M004 are unblocked and ready. M004 may start because
its dependency on M003 is soft and ownership remains disjoint. M005 remains
blocked until M003/M004 view-model interfaces stabilize, and it cannot close
until both milestones close. M006 remains blocked on M003–M005.

## 12. Registry updates

`plans/registry.md`, this implementation plan, and
`plans/subsystems/dashboard-roadmap.md` now mark M002 closed. The closure audit
promotes M003 and M004 to ready, retains M005 as blocked on interface
stabilization/closure, and keeps M006 blocked on M003–M005. These updates are
included in the same commit as this closure record.

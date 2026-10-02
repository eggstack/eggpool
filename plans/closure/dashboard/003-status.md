# Dashboard Milestone 003 — Closure Status

Status: closed

Source implementation plan:

- `plans/implementation/dashboard/003-overview-account-model-parity.md`

Source subsystem roadmap:

- `plans/subsystems/dashboard-roadmap.md#milestone-003--overview-account-model-and-model-detail-parity`

Repository baseline reviewed: `9150fd3a1ed5a6c80df592619215d2bcc51e9be1`

Implementation commits:

- `87a53681` — activate dashboard M003 after M002 closure.
- `2df310eb` — restore account and model table density.
- `48727609` — project canonical model information on detail pages.
- `29c40a04` — use canonical model summaries in the catalog.
- `8e7dd7c7` — restore parity qualification in Rust-only checkouts.
- `bb3d34b3` — restore model and event overview panels.
- `827fc657` — refine model detail rendering.
- `afbdf5c1` — expose bounded model observations.
- `408e304a` — project pricing exactness in dashboard tables.
- `6cb82f55` — preserve account filters and enabled visibility.
- `f04f2f03` — apply account and model filters.
- `3bac668e` — restore overview health and activity card groups.
- `f162c310` — match overview metric and activity hierarchy.
- `ba0740d3` — restore the overview system health card class.
- `7913fea2` — restore populated overview health and warning panels.
- `ed243ea5` — restore overview metric tooltips.
- `9150fd3a` — complete bounded overview, account, model, and detail projections; document current authorities; align unavailable Model Detail markup with the oracle.
- `03b4988` — restore populated Model Detail metadata panels, safely project catalog observations, and add a fixed rich-metadata oracle fixture.
- `2ad7e09` — launch Chrome with the host's native architecture for deterministic dashboard captures on Apple Silicon.
- `8261e5a` — add paired desktop/mobile captures for populated model-info detail.

## 1. Executive finding

M003 is closed. Overview, Accounts, Models, and Model Detail now render the
historical operator surfaces from bounded dashboard/database projections and
current Rust catalog, model-info, pricing, and health owners. The strict
qualification compares both unavailable and fully populated canonical
Model-Info detail, including observations; both detail branches match the
Python oracle exactly. The remaining four M003 cells are traced to deliberate
current-source semantics or genuinely unavailable values. No schema
migration, provider probe, runtime fallback, or new authority was introduced.

## 2. Requirement-to-evidence matrix

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| Map page values to authoritative owners or explicit unknown values | `architecture/deep-dive-dashboard.md`; bounded reads in `rust/src/db/repositories.rs`; route projections in `rust/src/server/dashboard.rs` | pass | Usage, account/model/IP aggregates, reservations, operational events, pings, and token rollups use existing database owners. Catalog resolution and active-generation routing priority retain their current owners. |
| Overview empty/populated structure, metric groups, activity/glance panels, filters, chart hooks, and escaping | Full oracle report `/tmp/dashboard-m003-closure.json`; overview renderer tests | pass with accepted semantic differences | Empty/populated root each differ only in the unavailable request-shaping summary text. Timeseries/chart hooks and browser interactions pass. |
| Account enabled/disabled filtering, stable usage/cost/status table, and long/Unicode values | Full oracle report; account renderer and repository projection | pass with accepted semantic difference | Empty projection matches. The populated account budget-priority class differs because no current account-budget authority exists; Rust leaves it unknown instead of repeating the old renderer's `no` classification. |
| Model filters, routing priority, status/availability, exactness, pricing, and metadata presentation | Full oracle report; current-generation provider configuration and catalog projection | pass with accepted semantic difference | Routing priority comes from active provider configuration. Catalog `configured` resolution may differ from the retired renderer's broader `available` label. |
| Model Detail full/sparse/missing metadata and observations | `/tmp/dashboard-m003-final-screens.json`; `migration-rs/fixtures/dashboard/q012-model-info.sql`; dashboard unit tests; `operations_o007` | pass | Empty and populated full-metadata DOMs match the oracle. Compact provider-catalog observations are projected from canonical provenance and the current catalog; raw JSON and hashes are omitted. |
| Desktop/mobile visual review, including dense populated Model Detail | 32 matched captures under `/var/folders/2j/dlwhrpps66scv9bw8f7vdfg40000gq/T/eggpool-q012-screenshots-65502`; manifest SHA-256 `005718adc6d7e05eaafeea5d4098a25d5caa0594a3aaeb80b71b8f973d33d451` | pass | Python/Rust use the same fixture, Cyber Red theme, and 1440×900 / 390×844 viewports. The four rich-detail captures are individually hashed in the manifest. Responsive panel order and content density match; minor shared-shell control-border/vertical-offset differences remain for M006's visual disposition. |
| Existing authentication, assets, and browser interactions | `/tmp/dashboard-m003-final-screens.json` | pass | 32 captures, eight paired desktop/mobile interaction runs, and 54 static/theme assets; browser checks report no JavaScript exceptions, console errors, failed same-origin loads, or same-origin HTTP errors. |
| M003 and later milestone gap accounting | `/tmp/dashboard-m003-final-screens.json`, candidate `8261e5a` | pass | 38 total remaining strict-parity cells: 4 M003, 15 M004, 4 M005, and 15 shared stats/API cells. Both Model Detail comparisons are exact. The M003 cells are the empty/populated overview summary, account budget-priority class, and model availability label. |

## 3. Production implementation evidence

`rust/src/db/repositories.rs` adds bounded dashboard projections for account
latency, reservations, traffic, utilization, cost/exactness ratios, per-IP
aggregates, request counts in daily token activity, recent finalizer/recovery
events, and 180 days of token rollups. `rust/src/server/dashboard.rs` renders
the expanded Overview, Accounts, Models, and Model Detail surfaces, including
metric cards, health and activity panels, warnings, filters, model metadata,
availability and pricing exactness. Active provider configuration supplies
model routing priority; current catalog resolution supplies model status.
Unavailable facts stay unknown, and stale Python values are not synthesized.

The dashboard documentation now records these owners and the remaining
unavailable fields. The parity runner has a fixed secret-free model-info SQL
fixture, compares the full metadata route, captures its populated desktop and
mobile views, and launches universal Chrome natively on Apple Silicon. The
installer harness test timeout was raised from 60 to 120 seconds after the
unchanged 46-case harness was measured at about 57 seconds on this host.

## 4. Verification executed

### Commands run

```text
rtk cargo fmt --manifest-path rust/Cargo.toml --all -- --check
rtk cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
rtk cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
rtk cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
rtk cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
rtk cargo test --manifest-path rust/Cargo.toml --test server_transport -- --test-threads=1
rtk cargo test --manifest-path rust/Cargo.toml --test status_command -- --test-threads=1
rtk cargo test --manifest-path rust/Cargo.toml --test catalog_refresh -- --test-threads=1
rtk cargo test --manifest-path rust/Cargo.toml --test model_router -- --test-threads=1
rtk cargo test --manifest-path rust/Cargo.toml --lib server::dashboard::tests -- --test-threads=1
rtk cargo test --manifest-path rust/Cargo.toml --test operations_o007 -- --test-threads=1
rtk cargo test --manifest-path rust/Cargo.toml --test dashboard_parity -- --test-threads=1
rtk uv run --frozen --no-sync ruff format --check scripts/ tests/tooling/
rtk uv run --frozen --no-sync ruff check scripts/ tests/tooling/
rtk uv run --frozen --no-sync pyright scripts/
rtk uv run --frozen --no-sync pytest tests/tooling/ -q --tb=short --maxfail=1
rtk git diff --check
```

Pinned-oracle qualification against candidate `8261e5a`:

```text
rtk env EGGPOOL_DASHBOARD_ORACLE_ROOT=/tmp/eggpool-dashboard-oracle-m002 EGGPOOL_DASHBOARD_ORACLE_PYTHON=/tmp/eggpool-dashboard-oracle-m002/.venv/bin/python EGGPOOL_DASHBOARD_CANDIDATE_SHA=8261e5a uv run --frozen --no-sync python scripts/qualification_dashboard_parity.py --skip-build --screenshots --output /tmp/dashboard-m003-final-screens.json --markdown /tmp/dashboard-m003-final-screens.md
```

### Results

- Rust formatting, default Clippy, no-default check, and no-default Clippy passed on the final Rust implementation.
- The full serial Rust suite passed after the populated metadata implementation: 808 tests across 65 suites in 273.98 seconds. Dashboard unit tests passed (10), and `operations_o007` passed (7), including the provenance-gated provider-catalog projection and raw-data omission.
- `dashboard_parity` is not a Rust test target in this checkout. The attempted command reported that no such target exists. The current substitute is the M001 strict Python oracle runner, which compares complete page DOM and API projections, private authentication, assets, and browser behavior; focused Rust dashboard unit and `server_transport` coverage also passed.
- Ruff format/check and Pyright passed. The full tooling suite passed: 146 passed, 1 skipped in 49.84 seconds. Its installer harness test now allows 120 seconds; the same deterministic 46-case harness passed directly in about 57 seconds.
- `uv sync --frozen` completed and reported the workspace package set checked. This repository-tooling-only `pyproject.toml` has no `requires-python`, so uv warned that it defaulted to `>=3.12`; the command rewrote `uv.lock`, which was restored before commit. Subsequent tooling commands used `--frozen --no-sync`.
- Oracle qualification completed with candidate SHA `8261e5a`, 54 matching static/theme assets, and 38 remaining matrix differences assigned as 4 M003, 15 M004, 4 M005, and 15 stats/API/shared-surface cells. Both missing-info and full canonical Model Detail comparisons pass.
- The final screenshot qualification passed after the runner launched the arm64 Chrome binary slice on this Apple Silicon host: 32 captures, all eight desktop/mobile interaction runs, and clean browser checks. Four captures exercise the populated canonical metadata branch. The desktop and mobile Python/Rust detail pairs were visually reviewed; minor shared-shell control styling differences are carried into M006 disposition.
- No CI result is claimed; all results are local.

## 5. Invariant review

- Rendering remains observational. It reads existing database/config/catalog/model-info/health owners and does not mutate routing, quota, retry, finalization, health, or catalog state.
- Account/model health and availability are sourced from current health/catalog projections. Missing state stays unknown; model resolution is not inferred from request presence.
- Dashboard queries use the existing serialized SQLite owner and bounded time windows, result limits, and grouped projections. The catalog observation projection is enabled only by canonical provenance and reads the catalog's current model row; it neither writes observations nor invokes provider sources. No schema or persistence path was added.
- Shared dashboard authentication and URL behavior are unchanged. Rust HTML escaping remains in use for account, model, provider, and observation content. The fixture is secret-free and reports no secrets.
- Current `/v1/models`, integration profile, inference, and routing contracts are unchanged. Dashboard pages perform no provider network probes.

## 6. Failure and recovery review

New reads are synchronous bounded database projections through the existing gate; handlers create no background tasks or mutable shared renderer state. Optional model-info and health absence render explicit unavailable/unknown content. Database failures retain existing redacted dashboard behavior. No retries, reservations, finalizers, generation publication, or recovery state are altered. No cancellation or restart behavior changed.

## 7. Migration and compatibility review

No migration or configuration change was added. The new dashboard projections use existing request, account, reservation, operational-event, ping, and usage-rollup data. Existing URL/query behavior, dashboard auth, model API semantics, routing semantics, and frontend assets remain compatible. Historical Python-only request-shaping and account-budget status are not recreated without a current authority.

## 8. Security review

Dashboard HTML continues to escape dynamic text; observations expose only compact source/model/provider/time/confidence facts and omit raw JSON/hash payloads. Queries remain parameterized and result-bounded. Credentials, prompts, raw request/provider bodies, and cache keys are not persisted or rendered. Existing public/private auth behavior passed the full page/API qualification. No privilege or write capability was added.

## 9. Documentation and operations

`architecture/deep-dive-dashboard.md` records the bounded projections, current catalog/health/config owners, token calendar source, and explicit unavailable fields. The M001 runner remains the executable parity gate and now includes a sanitized populated-model-info fixture and native-architecture Chrome launch. Dashboard operational guidance and public API contracts did not change.

## 10. Unresolved findings

| Severity | Finding | Impact | Required action |
|---|---|---|---|
| low | Overview request-shaping text, account budget-priority classification, and the Python/Rust model-availability label retain four strict projection differences across empty/populated comparisons. | Current Rust truth cannot safely reproduce retired policy/state without inventing compression/cache or account-budget facts; catalog resolution intentionally differs from historical Python's broad availability label. | Keep explicit unknown/current resolution semantics. M006 must retain these in the final parity disposition; do not fabricate legacy values. |
| low | The populated Model Detail capture shows minor shared-shell control-border and top-offset differences although its detail panels and responsive stacking align. | Strict DOM equality does not encode all CSS paint details; this is not a model-data or M003 renderer mismatch. | Retain in M006's cross-route visual review and disposition. |
| medium | Telemetry/routing/reliability/trace pages retain 15 parity cells. | Those M004 surfaces are not yet restored. | M004. |
| medium | Runtime/cache pages retain 4 parity cells. | Those M005 surfaces are not yet restored. | M005 after M004 closes and view-model interfaces stabilize. |
| medium | Historical stats/API/shared surfaces retain 15 parity cells. | Runtime/cache-era endpoints and aggregate compatibility remain incomplete. | M005 implementation and M006 full API/auth reconciliation. |

No high- or critical-severity M003 defect remains.

## 11. Roadmap disposition

Milestone closed; M004 is eligible and remains ready because its dependency on
M003 was soft and the M003/M004 renderer ownership is disjoint. The requested
sequence is preserved by activating M004 after this closure commit. M005 stays
blocked until the M003/M004 view-model interfaces stabilize and both plans
close. M006 stays blocked on M003–M005.

## 12. Registry updates

This closure commit marks M003 closed in the source plan, registry, and
subsystem roadmap, adds this record, and records the unblock audit. M004 stays
ready because its M003 dependency is soft and its renderer ownership is
disjoint. M005 remains blocked until M003/M004 interfaces stabilize and both
milestones close; M006 remains blocked on M003–M005.

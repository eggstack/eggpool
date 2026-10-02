# Dashboard Corrective Pass 007 — Closure Status

Status: closed

Source implementation plan:

- `plans/implementation/dashboard/007-empty-recovery-summary-correction.md`

Source subsystem roadmap:

- `plans/subsystems/dashboard-roadmap.md#milestone-004--telemetry-routing-reliability-and-trace-parity`

Repository baseline reviewed: `36e3a93f8ec45e9571abba579cb4c46112bc8659`

Implementation commit:

- `b068315` — restore bounded Runtime/Cache telemetry projections, include zero-count operational events in summaries, and synchronize recovery-summary qualification on the query window.

## 1. Executive finding

The M004 zero-count recovery-summary finding is resolved. Rust now includes
every persisted operational event type in the grouped summary, matching the
Python query. The parity runner waits until both implementations' startup
recovery events are durable and strictly earlier than the second-precision
exclusive query upper bound, then requests Reliability first. The strict
Reliability DOM cells passed for empty and populated fixtures in two
consecutive post-commit runs. The frozen oracle and full-DOM comparator are
unchanged.

## 2. Requirement-to-evidence matrix

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| Include zero-count recovery rows using the Python summary semantics | `rust/src/db/repositories.rs`; populated/empty strict M001 reports | pass | Removed the positive-count filter; retained event type, count, timestamp, and scalar sums. |
| Make the initial Python summary cache fill deterministic | `scripts/qualification_dashboard_parity.py`; `tests/tooling/test_dashboard_parity_projection.py` | pass | Both isolated databases are polled for a persisted row whose `occurred_at < datetime('now')`; timeout fails qualification. |
| Preserve recent operational event visibility and recovery behavior | `rust/src/runtime_lifecycle/recovery.rs`; `runtime_lifecycle_r008` | pass | Recovery ownership and payload are unchanged. |
| Strict empty/populated Reliability parity | `/tmp/dashboard-m007-closure-1.json`, `/tmp/dashboard-m007-closure-2.json` | pass | Candidate `b068315`; both reports have zero M004 route mismatches and both Reliability state cells pass. |
| Preserve strict comparison and frozen oracle | M001 runner and oracle commit `c23a70961f4b7858fdb0264cfb27b7ea26a8a334` | pass | No oracle capture, normalization, or comparator changes. |

## 3. Production implementation evidence

`DashboardRepository::load` now groups all in-window `operational_events`
rows by event type, including zero-count startup `crash_recovery` events. The
qualification barrier observes both Python and Rust SQLite files and waits
for the startup row to be strictly inside the summary query's second-precision
time window. Reliability is requested before Overview can fill the Python
summary cache. No recovery lifecycle or persistence semantics changed.

## 4. Verification executed

### Commands run

```text
rtk cargo fmt --manifest-path rust/Cargo.toml --all -- --check
rtk cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
rtk cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
rtk cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
rtk cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
rtk cargo test --manifest-path rust/Cargo.toml --test runtime_lifecycle_r008 -- --test-threads=1
rtk cargo test --manifest-path rust/Cargo.toml --test server_transport -- --test-threads=1
rtk cargo test --manifest-path rust/Cargo.toml --lib server::dashboard::tests -- --test-threads=1
rtk uv run --frozen --no-sync ruff format --check scripts/ tests/tooling/
rtk uv run --frozen --no-sync ruff check scripts/ tests/tooling/
rtk uv run --frozen --no-sync pyright scripts/
rtk uv run --frozen --no-sync pytest tests/tooling/ -q --tb=short --maxfail=1
rtk git diff --check
```

The full oracle command was run twice after commit `b068315` with
`EGGPOOL_DASHBOARD_ORACLE_ROOT=/tmp/eggpool-dashboard-oracle-m002`,
`EGGPOOL_DASHBOARD_ORACLE_PYTHON=/tmp/eggpool-dashboard-oracle-m002/.venv/bin/python`,
and `EGGPOOL_DASHBOARD_CANDIDATE_SHA=b068315`:

```text
rtk uv run --frozen --no-sync python scripts/qualification_dashboard_parity.py --skip-build --output /tmp/dashboard-m007-closure-1.json --markdown /tmp/dashboard-m007-closure-1.md
rtk uv run --frozen --no-sync python scripts/qualification_dashboard_parity.py --skip-build --output /tmp/dashboard-m007-closure-2.json --markdown /tmp/dashboard-m007-closure-2.md
```

### Results

- Default Clippy and no-default check/Clippy passed.
- The full serial Rust workspace suite passed: 811 tests across 65 suites.
- Focused suites passed: `runtime_lifecycle_r008` (4), `server_transport` (13), and dashboard library tests (13).
- Ruff format/check and Pyright passed. The full tooling suite passed: 148 passed, 1 skipped.
- Both consecutive strict reports passed empty and populated Reliability DOM cells; the M004 telemetry/routing/trace mismatch group is zero. Other report differences remain owned by M003/M005 and are not part of this corrective pass.
- Results are local; no CI result is claimed.

## 5. Invariant review

- Startup recovery remains owned by `runtime_lifecycle::recovery`; the event contains only bounded scalar counts.
- Dashboard reads remain observational and use the existing serialized SQLite owner.
- The summary query adds no raw request content, identifiers, credentials, or cache keys.
- The strict full-DOM comparator and frozen oracle remain unchanged.

## 6. Failure and recovery review

The barrier has a bounded 15-second timeout, observes SQLite directly, and
fails the qualification if either process exits or its recovery row never
enters the current summary window. Production recovery, restart, and failure
semantics are unchanged.

## 7. Migration and compatibility review

No migration, schema, route, or event-payload change was made. Zero-count
events are now included in Rust summary rows to match the established Python
query semantics.

## 8. Security review

The barrier reads only event type and timestamp from isolated local test
databases. No secret-bearing or request-content fields are read or persisted.

## 9. Documentation and operations

The M004 closure disposition is updated additively in
`plans/closure/dashboard/004-follow-up-007.md`; its original closure record is
preserved. The dashboard roadmap and registry now record M004 as closed and
M005 as active.

## 10. Unresolved findings

| Severity | Finding | Impact | Required action |
|---|---|---|---|
| none | No M004 recovery-summary finding remains. | None. | None. |

## 11. Roadmap disposition

Corrective pass 007 is closed, and M004's named parity condition is resolved.
M005 remains active. M006 remains blocked solely on M005 closure; its M003 and
M004 hard dependencies are closed. No other dashboard plan became newly
eligible.

## 12. Registry updates

Applied in the closure commit: M004 and corrective pass 007 are marked closed;
M005 stays active; M006's blocker is narrowed to M005.

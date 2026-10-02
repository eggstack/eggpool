# Dashboard Milestone 004 — Closure Status

Status: closing

Source implementation plan:

- `plans/implementation/dashboard/004-telemetry-routing-trace-parity.md`

Source subsystem roadmap:

- `plans/subsystems/dashboard-roadmap.md#milestone-004--telemetry-routing-reliability-and-trace-parity`

Repository baseline reviewed: `4c74b48dbb7f3233d45670dce423527f3254f6ff`

Implementation commits:

- `c342803d` — restore bounded telemetry, routing, reliability, timeseries, and trace projections; add startup recovery event evidence and privacy guard tests.

## 1. Executive finding

Production work for the eight M004 pages is implemented, the existing dashboard
assets remain unchanged, and the documented source/bounds/privacy review is
complete. Matched browser checks and the M004 route matrix pass in the captured
run. One zero-count operational-summary panel varies against the frozen Python
oracle between independent qualification runs; closure disposition is pending
the M006 deterministic-state review.

## 2. Requirement-to-evidence matrix

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| Map page fields to authoritative sources and bound every query | `DashboardRepository` projections in `rust/src/db/repositories.rs`; architecture source/bounds matrix | pass | Request/event/ping pages cap at 100; routing selections 100; latency groups 400; timeseries 200; operational summaries 25 and recent rows 25; bandwidth 180 daily rows. |
| Latency, Events, Bandwidth, and Pings parity | `/tmp/dashboard-m004-capture-report.json`, strict M001 route projections | pass | Empty/populated route cells pass in the captured report; public/private checks pass. |
| Grouped/aggregate Timeseries, controls, and chart interactions | `/tmp/dashboard-m004-capture-report.json`; `/tmp/dashboard-m004-browser.json` | pass | Grouped JSON remains M002-owned; aggregate totals count input plus output tokens once; browser period/group/metric/filter hooks pass. |
| Reliability attempts and operational events | `runtime_lifecycle/recovery.rs`; `server/dashboard.rs`; `runtime_lifecycle_r008` startup recovery assertion | partial | Startup recovery event is persisted from its lifecycle owner. The zero-count summary row can differ from the Python service cache in isolated runs; see finding 1. |
| Routing diagnostics use persisted decisions without re-running policy | `DashboardRepository` routing projections and rendered routing distribution/selection tables | pass | No dashboard selection or retry policy is introduced. |
| Trace metadata stays outside the content boundary | `trace_renderer_does_not_emit_prohibited_error_content`; bounded request projection | pass | Synthetic prompt/body/tool/cache-key/auth sentinels are absent from the rendered trace. |
| Matched browser behavior has no console/resource failures | `/tmp/dashboard-m004-browser.json` | pass | 32 captures; eight desktop/mobile interaction checks passed; no JS exception, console error, failed same-origin load, or same-origin HTTP error. |
| Static asset bytes and schema migrations remain unchanged | qualification asset inventory; `git diff` | pass | No dashboard asset or migration changes. |

## 3. Production implementation evidence

`DashboardRepository` now supplies bounded latency percentiles, persisted
routing selection aggregates, extended timeseries token/byte observations,
and operational-event summary/recent projections. Timeseries rows are ordered
chronologically and the per-bucket table aggregates the source rows. Startup
recovery writes a metadata-only `crash_recovery` operational event in the
runtime lifecycle owner. The reliability renderer escapes and bounds event
details. The routing renderer consumes stored `routing_decisions` only. The
trace renderer excludes `error_message`, client IP, and request content.

The routing trace panel exposes configured mode/rate and leaves writer
counters unavailable when no authoritative runtime writer snapshot exists.
It does not synthesize accepted/written/dropped counts.

## 4. Verification executed

### Commands run

```bash
cargo test --manifest-path rust/Cargo.toml dashboard_repository_loads_empty_telemetry_views --lib -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml trace_renderer_does_not_emit_prohibited_error_content --lib -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test runtime_lifecycle_r008 startup_recovery_converges_multiple_bounded_passes_without_provider_work -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test server_transport -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test routing_domain -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test routing_claims -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test status_command -- --test-threads=1
uv run pytest tests/tooling/test_dashboard_parity_projection.py -q --tb=short
uv run python scripts/qualification_dashboard_parity.py --skip-build
uv run python scripts/qualification_dashboard_parity.py --skip-build --screenshots
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
git diff --check
```

### Results

- Focused Rust results: 1 repository projection test, 1 trace sentinel test,
  1 startup recovery audit test, 13 server transport tests, 12 routing-domain
  tests, 11 routing-claims tests, and 13 status-command tests passed.
- Tooling projection tests: 21 passed.
- Strict qualification candidate: `c342803d8d41255049f8111c3595527d7ef5ce5b`.
  The captured report passes all eight M004 routes in empty/populated/private
  states. The whole dashboard report still has 23 non-M004 mismatches owned by
  M003/M005, plus the intermittent zero-count reliability summary variance
  described below when the Python event-summary cache differs between runs.
- Matched browser qualification: 32 captures and 8 interaction checks passed.
  Artifacts are under `/tmp/dashboard-m004-screens` and the report is
  `/tmp/dashboard-m004-browser.json`.
- The implementation plan names a `dashboard_parity` Cargo test target, but
  no such file/target exists under `rust/tests/`. Its route contract is covered
  by the M001 Python qualification runner and `server_transport` instead.
- These are local results, not CI results. No schema migration or dependency
  change was made.

## 5. Invariant review

- Routing is read from persisted decision rows; selection policy remains in
  `rust/src/routing/` and is not reimplemented in the renderer.
- Retry totals use stored attempt classifications.
- Health/ping views remain observational and issue no provider probes.
- Page queries are bounded and run through the existing database repository
  gate; no browser/network work occurs while a DB operation is held.
- Timeseries JSON continues through the existing safe script serializer.
- Trace rows omit raw error messages, request bodies, prompts, tool arguments,
  client IPs, cache keys, credentials, and provider bodies.
- No background telemetry collector or schema migration was added.

## 6. Failure and recovery review

An optional trace source without a live writer snapshot renders `Unavailable`
or zero-capacity configured-off facts rather than fabricating writer counters.
Dashboard database failures retain the existing bounded degraded response.
Startup recovery records only scalar, secret-free counts in
`operational_events`; `runtime_lifecycle_r008` verifies the durable event and
multi-pass recovery counts. The startup audit insert follows the bounded
reconciliation passes, so a process crash between repair commit and audit
insert can lose that single audit summary; the repaired request state itself
remains committed and the next startup records its own pass.

## 7. Migration and compatibility review

No schema migration, configuration change, public API change, or asset change
was introduced. The existing dashboard auth boundary remains unchanged.
Timeseries and grouped-data endpoints retain M002 contracts.

## 8. Security review

Trace sentinel coverage passes. Recent event JSON is HTML-escaped and capped
at 200 characters. The page reads only dashboard-authorized rows and does not
expose request content or account credentials.

## 9. Documentation and operations

Updated `architecture/deep-dive-dashboard.md` with M004 data owners, bounds,
trace restrictions, startup recovery event ownership, and unavailable trace
writer telemetry behavior. The operator-facing API reference remains accurate:
these views are rendered in dashboard HTML and add no new API route.

## 10. Unresolved findings

| Severity | Finding | Impact | Required action |
|---|---|---|---|
| medium | The frozen Python dashboard sometimes serves an empty cached operational summary while the Rust projection sees the just-written zero-count `crash_recovery` event (or vice versa). The strict DOM cell can therefore compare a paragraph with a summary table across runs. | One bounded reliability summary panel can differ; the durable recent event and recovery facts are retained. No inference or recovery correctness impact. | M006 must reproduce the state with a pinned empty fixture, inspect the Python dashboard cache lifecycle, and record whether the stable contract is empty-on-zero or event-row-on-startup. Do not relax the full DOM comparator. Re-run the strict M004 route cells afterward. |

## 11. Roadmap disposition

Conditionally closed. The telemetry/runtime DTO boundaries are stable enough
for M005 implementation. M005 may proceed, but it cannot close until this
named M004 parity condition and its hard dependency on M004 are resolved.
M006 remains blocked on M003-M005 closure.

## 12. Registry updates

The roadmap and registry now place M004 in `closing`. M005 remains blocked
until M004's closing disposition is recorded; M006 remains hard-blocked on
M003-M005. The follow-up unblock audit will update M005 when M004 transitions
to its final disposition.

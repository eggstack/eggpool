# Dashboard Corrective Pass 007 — Deterministic Recovery Summary Qualification

Status: closed

Repository baseline: `36e3a93f8ec45e9571abba579cb4c46112bc8659`

Source roadmap:

- `plans/subsystems/dashboard-roadmap.md#milestone-004--telemetry-routing-reliability-and-trace-parity`

Corrects:

- `plans/implementation/dashboard/004-telemetry-routing-trace-parity.md`
- `plans/closure/dashboard/004-status.md` (finding 1)

Long-term requirements:

- `plans/000-long-term-specification.md`
- `plans/001-terminology-and-domain-model.md`
- `plans/003-planning-process.md`

Applicable ADRs: None required.

Primary class: invariant

## 1. Objective

Make the M004 reliability comparison deterministic across Python cache warm-up and second-precision time-window boundaries by synchronizing qualification on a persisted startup recovery event that is strictly before the query window's upper bound.

## 2. Why this corrective pass is ready

The M004 closure records one medium strict-DOM variance: the Python summary query returns every event type, including zero-count `crash_recovery`, while the Rust query filters zero-count groups. Both services also use a second-precision exclusive upper bound; an event inserted in the current second can appear in the unbounded recent-events panel but be absent from the summary. Qualification therefore waits until both recovery rows are durable and strictly earlier than the current query-window upper bound before requesting Reliability.

## 3. Current implementation evidence

- `rust/src/runtime_lifecycle/recovery.rs` persists `crash_recovery` after every successful reconciliation, including when all repair counts are zero.
- `rust/src/db/repositories.rs` filters zero-count summary groups, unlike the historical Python repository query.
- `scripts/qualification_dashboard_parity.py` must wait for startup recovery rows from both databases to fall strictly inside the summary query window before the first dashboard page read.
- `rust/tests/runtime_lifecycle_r008.rs` verifies event persistence when 501 requests are repaired.
- `scripts/qualification_dashboard_parity.py` compares the full reliability DOM against the immutable M001 oracle.

## 4. Invariants that must not regress

- Recovery repair passes and convergence behavior remain unchanged.
- Real interrupted requests, released reservations, or terminalized attempts retain the bounded scalar audit event.
- A persisted recovery event, including zero-count startup recovery, remains visible in both summary and recent-event panels.
- The strict DOM comparator and frozen oracle remain unchanged.
- No request content, credentials, or account identifiers enter the audit event.

## 5. Scope

### In scope

- Restore event-row semantics in the summary query.
- Add a bounded, observable startup barrier for both databases before the first dashboard request.
- Re-run strict reliability empty/populated qualification repeatedly.
- Add an additive closure follow-up and update dashboard registry/roadmap status.

### Explicitly out of scope

- Changing reconciliation limits, SQL repair semantics, reliability rendering, or M004 telemetry APIs.
- Relaxing or normalizing the oracle comparison.

## 6. Required production changes

Keep recovery-event ownership unchanged. Make the dashboard summary include every persisted event type, including zero-count recovery events, matching the Python repository query. In the qualification runner, poll both isolated databases for the startup `crash_recovery` row and require `occurred_at < datetime('now')` with a bounded timeout before issuing any page request, then request Reliability before Overview. This ensures each first summary read observes durable startup state inside its second-precision time window. A missing row or timeout fails qualification.

## 7. Ordered work packages

### Work package A — Synchronize the first cached summary read

Intent: ensure Python and Rust compare the same completed startup state.

Required changes:

- Remove the positive-count `HAVING` filter from the Rust summary projection.
- Add a SQLite-backed readiness barrier for each implementation's recovery event to precede the window upper bound, then request `/reliability` before other pages in `_run_pair`.

Acceptance evidence:

- Empty and populated runtimes retain their current scalar recovery event behavior.
- Each first summary read occurs only after that implementation's startup event is durable and strictly precedes the window upper bound.
- Strict M001 empty and populated `/reliability` projections pass on repeated runs.

## 8. Failure, restart, and contention semantics

Recovery failure and pass-limit behavior remain unchanged. The qualification barrier observes SQLite state with bounded polling and fails rather than continuing with an inconsistent oracle snapshot.

## 9. Compatibility and migration

No migration or recovery event semantics change. Dashboard summaries include persisted zero-count recovery events, as the historical Python query does.

## 10. Required tests

- Existing `runtime_lifecycle_r008` zero-count/nonzero recovery event assertions.
- M001 qualification runner with pinned oracle source, covering empty and populated reliability pages and public/private access on two consecutive runs.

## 11. Required verification commands

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo test --manifest-path rust/Cargo.toml --test runtime_lifecycle_r008 -- --test-threads=1
uv run --frozen --no-sync python scripts/qualification_dashboard_parity.py --skip-build
git diff --check
```

## 12. Documentation updates

- Add the corrected event meaning and test evidence to an additive M004 closure follow-up.
- Update the dashboard roadmap and registry to record the corrective pass and clear the M004 finding only after qualification passes.

## 13. Acceptance criteria

- Oracle summary caching begins after the persisted recovery event is observable.
- Python and Rust summary/recent-event views agree.
- Empty and populated reliability oracle cells pass without comparator changes.

## 14. Stop conditions

Stop if the oracle runtime produces no observable startup event, if parity requires changing the frozen oracle/comparator, or if any privacy-restricted data is required.

## 15. Closure evidence required

Record the production/test commit, exact test and oracle commands, repeated empty-route result, event payload review, and additive M004 closure disposition.

## 16. Handoff notes

Rust integration tests are serial. Use the pinned oracle checkout and Python environment documented in `plans/closure/dashboard/003-status.md`.

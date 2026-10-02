# Dashboard Corrective Pass 007 — Empty Startup Recovery Summary

Status: active

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

Make empty startup reconciliation produce the stable empty reliability projection expected by the frozen M001 oracle, while retaining durable audit events whenever startup actually repairs interrupted work.

## 2. Why this corrective pass is ready

The M004 closure records one medium strict-DOM variance: zero-count `crash_recovery` events race the Python dashboard's cached empty operational summary. The failure is reproducible from the event ownership decision and can be corrected without changing request recovery behavior.

## 3. Current implementation evidence

- `rust/src/runtime_lifecycle/recovery.rs` persists `crash_recovery` after every successful reconciliation, including when all repair counts are zero.
- `rust/src/db/repositories.rs` aggregates positive repair counts for the reliability summary.
- `rust/tests/runtime_lifecycle_r008.rs` verifies event persistence when 501 requests are repaired.
- `scripts/qualification_dashboard_parity.py` compares the full reliability DOM against the immutable M001 oracle.

## 4. Invariants that must not regress

- Recovery repair passes and convergence behavior remain unchanged.
- Real interrupted requests, released reservations, or terminalized attempts retain the bounded scalar audit event.
- Empty reconciliation does not fabricate a crash-recovery occurrence.
- The strict DOM comparator and frozen oracle remain unchanged.
- No request content, credentials, or account identifiers enter the audit event.

## 5. Scope

### In scope

- Omit the event when all repair counts are zero.
- Add an idle-start regression test and retain the existing non-empty recovery assertion.
- Re-run strict reliability empty/populated qualification.
- Add an additive closure follow-up and update dashboard registry/roadmap status.

### Explicitly out of scope

- Changing reconciliation limits, SQL repair semantics, reliability rendering, or M004 telemetry APIs.
- Relaxing or normalizing the oracle comparison.

## 6. Required production changes

In `runtime_lifecycle::recovery`, persist the event only if requests were interrupted, reservations released, or attempts terminalized. Continue to publish `StartupRecoveryReport` for a converged zero-change run. Add a synchronized test that runs recovery against an empty migrated database and verifies that the report converges while no `crash_recovery` row is written.

## 7. Ordered work packages

### Work package A — Suppress zero-change recovery event

Intent: align the operational-event meaning with actual recovery work.

Required changes:

- Guard the existing event transaction on nonzero repair counts.
- Keep the existing nonzero recovery event transaction and payload unchanged.
- Add an empty-startup test.

Acceptance evidence:

- Empty recovery converges and writes zero events.
- Non-empty recovery still writes exactly one event with the current scalar payload.
- Strict M001 empty and populated `/reliability` projections pass repeatedly.

## 8. Failure, restart, and contention semantics

Recovery failures and pass-limit behavior remain unchanged. A crash after successful zero-change reconciliation has no event to lose because no repair occurred. A crash after real repair retains the existing post-repair audit insert semantics.

## 9. Compatibility and migration

No migration or API shape change. `crash_recovery` becomes an event for actual repaired work rather than each process start.

## 10. Required tests

- `runtime_lifecycle_r008` empty and non-empty startup recovery cases.
- M001 qualification runner with pinned oracle source, covering empty and populated reliability pages and public/private access.

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

- Zero-change process startup does not create a crash-recovery event.
- Actual repair continues to create a durable bounded event.
- Empty and populated reliability oracle cells pass without comparator changes.

## 14. Stop conditions

Stop if stable parity would require oracle changes, if recovery semantics must change beyond audit-event creation, or if any privacy-restricted data is required.

## 15. Closure evidence required

Record the production/test commit, exact test and oracle commands, repeated empty-route result, event payload review, and additive M004 closure disposition.

## 16. Handoff notes

Rust integration tests are serial. Use the pinned oracle checkout and Python environment documented in `plans/closure/dashboard/003-status.md`.

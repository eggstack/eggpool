# Live Configuration Rehash Final Milestone D3 — Release Validation and Closure

## Status

Detailed handoff plan for the final release-validation segment of the live configuration rehash roadmap.

D3 does not add broad new architecture. It closes the roadmap after D1 request-policy expansion and D2 background/observability expansion by validating the complete supported `LIVE` inventory under realistic process, concurrency, failure, and resource-pressure conditions.

## Objective

Establish release-grade evidence that live rehash is safe, non-disruptive, fail-closed, observable, and operationally predictable across all supported field families, while preserving explicit restart boundaries for process-owned configuration.

## Release boundary

The feature may be declared complete only for fields explicitly listed in the versioned live inventory. All other fields remain `RESTART_REQUIRED` by default.

The release inventory should be generated or validated from the same policy source used by `compute_diff()`. Documentation must not maintain an independent, drift-prone list.

## Phase 1: Full field inventory audit

Produce a machine-checkable matrix for every `AppConfig` field containing:

- dotted path or path family;
- disposition (`LIVE`, `RESTART_REQUIRED`, `IGNORED`);
- owning runtime/process object;
- candidate construction or dynamic update path;
- retirement behavior;
- validation test;
- behavioral test;
- documentation anchor.

Requirements:

- every schema field appears exactly once;
- unknown fields fail closed;
- dynamic map children inherit only from explicitly approved parents;
- secret-bearing paths are tagged and redacted;
- no documentation-only live claims exist without policy entries and tests.

Add a CI test that compares the schema walk to the policy inventory.

## Phase 2: Canonical process-level acceptance suite

Create a compact release-defining suite separate from broad unit tests. It should run an actual EggPool server process, control socket, SQLite database, and deterministic mock providers.

Required scenarios:

1. Invalid TOML rejected locally; server never contacted.
2. Valid local preflight followed by server-side invalid config rejection.
3. Digest mismatch rejected with no generation change.
4. Provider/account live swap with an active stream.
5. Credential rotation observed on the next upstream request.
6. Routing change observed deterministically.
7. Transcoder policy change observed on new requests.
8. Compression/cache policy change observed without payload regression.
9. Background interval change converges without duplicate tasks.
10. Provider removal during an active stream preserves old request completion and excludes new traffic.
11. Mixed LIVE plus process-bound diff rejects the entire transaction.
12. Concurrent reload returns deterministic busy status.
13. Stale candidate publication conflict is rejected.
14. Old generation resources close after lease drain.
15. Retirement timeout closes resources according to documented policy.
16. `connect` and `logout` use live reload when available and never implicitly restart a healthy server when control transport is unavailable.
17. Same supervisor PID, worker PID, and listener throughout successful rehashes.
18. No pending requests, attempts, reservations, tasks, or client pools leak.

Each scenario must assert behavior, not merely command success or process survival.

## Phase 3: Failure-injection matrix

Add deterministic injection points around every pre-commit stage:

- config read;
- parse/schema validation;
- startup auth validation;
- credential validation;
- digest comparison;
- diff classification;
- persistence reconciliation;
- provider client construction;
- catalog/model state construction;
- task-spec construction;
- readiness checks;
- publication generation guard.

For each failure before publication, assert:

- active generation unchanged;
- active request behavior unchanged;
- candidate resources closed;
- persistence rolled back or remains idempotently safe;
- no new tasks scheduled;
- structured redacted operational event recorded;
- CLI receives stable nonzero exit code.

Add post-publication failure injection for:

- old supervisor stop failure;
- metrics flush failure;
- client-pool close failure;
- outbound-manager close failure;
- retirement timeout;
- operational-event write failure.

Post-publication cleanup failures must not roll back the active generation. They must remain visible in diagnostics and be retryable or bounded.

## Phase 4: Soak and resource-leak testing

Run repeated reload sequences covering at least:

- 100 no-op reloads;
- 50 alternating provider/routing generations;
- 50 request-policy generations;
- 25 background-schedule generations;
- overlapping long streams across multiple retired generations;
- forced retirement timeouts;
- mixed successful and rejected reloads.

Measure:

- open file descriptors;
- HTTP client/pool counts;
- task counts;
- thread counts;
- memory/RSS trend;
- retiring-generation count;
- SQLite connection and transaction state;
- pending request/reservation rows;
- control socket responsiveness;
- reload stage durations.

Define quantitative thresholds appropriate for Raspberry Pi/SBC deployment. Tests should tolerate allocator caching but reject monotonic unbounded growth.

## Phase 5: Performance regression gates

Live rehash infrastructure must not materially degrade the normal request path.

Measure before and after:

- dispatch overhead;
- TTFT;
- non-streaming request latency;
- streaming chunk cadence;
- lease acquisition/release cost;
- memory per active generation;
- reload preparation and publication duration;
- task transition duration.

Requirements:

- no config parsing or diff computation on normal requests;
- lease path remains constant-time;
- publication lock is never held during slow candidate construction;
- active traffic remains available during preparation;
- reload-specific diagnostics do not introduce synchronous database work on the hot path.

Add benchmark thresholds or regression comparisons rather than unbounded informational measurements.

## Phase 6: Security and secret-safety review

Audit:

- control socket path ownership and `0600` permissions;
- stale socket replacement behavior;
- symlink and non-socket path handling;
- peer/process-user assumptions;
- request-size and protocol-version bounds;
- malformed JSON handling;
- denial-of-service behavior for repeated reload attempts;
- config digest handling;
- all logs, events, CLI output, JSON output, reprs, and test failure messages for secret leakage.

Add tests using recognizable sentinel credentials and assert they never appear in captured output or persisted operational events.

The server must ignore client-supplied arbitrary config paths and always reload its startup-resolved path.

## Phase 7: Operator workflow validation

Validate deployment modes:

- foreground `eggpool serve --verbose`;
- detached daemon mode;
- systemd-managed deployment;
- per-user XDG state directory;
- production service user/state directory;
- stale PID and socket recovery;
- server not running;
- healthy server with inaccessible socket;
- explicit restart-required changes.

Document exact commands and expected exit codes for:

- successful live apply;
- no-op;
- retirement pending;
- validation rejection;
- restart-required rejection;
- busy reload;
- digest mismatch;
- control unavailable;
- preparation failure.

Consider adding a `rehash --dry-run` or `rehash --check` mode only if it reuses the exact server-side diff policy and does not create a divergent validation path.

## Phase 8: Documentation and code cleanup

Before closure:

- remove obsolete comments stating live reload is unsupported;
- remove legacy restart-alias compatibility paths unless required by public API guarantees;
- ensure startup and candidate construction share common builders and task specs;
- ensure plan/status documents accurately reflect completed and deferred field families;
- update README, deployment, architecture, command help, changelog, and troubleshooting;
- document explicit restart-required boundaries;
- document generation/lease/retirement semantics for maintainers;
- record how to add a new config field safely.

Add a maintainer checklist:

1. Add schema field.
2. Leave restart-required by default.
3. Identify owner and consumer.
4. Add candidate/update path.
5. Add disposition entry.
6. Add unit and behavioral E2E tests.
7. Add secret classification.
8. Update generated inventory/docs.

## Phase 9: Release gate

Required commands must pass from a clean checkout:

- Ruff formatting/check;
- Pyright strict checks;
- unit suite;
- integration suite;
- canonical rehash acceptance suite;
- failure-injection suite;
- soak/resource-leak suite;
- targeted performance benchmarks;
- packaging/install smoke test.

Record exact counts and durations in the closure commit or release checklist. Distinguish local results from hosted CI status.

## Final acceptance criteria

The live-rehash roadmap is complete when:

- the supported live inventory is explicit, complete, and machine-checked;
- every live field has a real behavioral test;
- all pre-publication failures preserve the old runtime;
- all post-publication cleanup failures remain observable without rollback;
- active streams survive generation changes;
- new requests consistently use the new generation;
- repeated reloads leak no unbounded resources;
- task schedules never duplicate;
- persistence identity and history remain stable;
- process-bound changes reject atomically;
- `connect`/`logout` never unexpectedly restart a healthy process;
- CLI and JSON contracts are stable;
- secrets never appear in diagnostics;
- normal request-path performance remains within defined regression thresholds;
- deployment and recovery workflows are documented and tested;
- the repository contains no contradictory live-reload documentation.

## Deferred follow-up

Anything not included in the final live inventory remains intentionally restart-required. Future expansion must use the same field-ownership, candidate-construction, behavioral-test, and release-gate process rather than weakening the fail-closed default.

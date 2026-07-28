# Integrated Hardening, Soak, Rollout, and Closure

Date: 2026-07-25
Status: completed — all workstreams implemented and verified, Plan 030 closure evidence at artifacts/plan-030-exact-head-evidence.md

Parent roadmap:

- `plans/022-upstream-error-isolation-and-hotpath-hardening-roadmap.md`

Depends on:

- `plans/023-error-isolation-reproducer-and-invariant-baseline.md`
- `plans/024-provider-bound-thinking-control-normalization.md`
- `plans/025-failure-effects-and-model-quarantine.md`
- `plans/026-process-owned-request-finalization.md`
- `plans/027-database-recovery-and-transaction-reconciliation.md`
- `plans/028-provider-payload-lifecycle-hotpath-consolidation.md`
- `plans/029-dispatch-writer-and-observability-bounds.md`

## Objective

Integrate and close the upstream-error-isolation and hot-path-hardening roadmap under realistic protocol, provider, concurrency, cancellation, database-fault, rehash, shutdown, and long-running workloads. Establish safe rollout defaults, remove temporary dual paths and feature flags when their exit criteria are met, document operator recovery, and commit exact-head evidence for the verified implementation tree.

This phase is not a substitute for incomplete phase work. Every dependent plan must satisfy its own acceptance criteria before this plan can be marked complete.

## Closure statement to prove

A provider-specific request validation error, including an unsupported MiniMax-M3 thinking level through OpenCode Go, is contained to that request. It cannot disable unrelated providers or models, leak runtime ownership, permanently invalidate process database availability, require a restart, require deleting the SQLite database, or produce increasing dispatch overhead over process lifetime.

## Scope

### In scope

- Cross-phase integration and ownership review.
- Full protocol/provider/error/cancellation matrix.
- Fault-injected database recovery matrix.
- Streaming and non-streaming concurrency.
- Rehash and shutdown interaction.
- Performance comparison against Plan 023 baseline.
- Short, standard, soak, and extended-soak gates.
- Feature-flag rollout and removal.
- Configuration migration and compatibility documentation.
- Operator diagnostics/runbook.
- Exact-head CI and evidence artifacts.

### Out of scope

- New provider capabilities unrelated to the roadmap.
- Routing-strategy redesign.
- Database-engine replacement.
- New compression algorithms.
- Production deployment requiring live provider credentials as the only closure evidence.
- Deferring correctness failures as “operational follow-up.”

## Workstream A — Cross-phase architecture audit

Before integration testing, inspect the merged implementation for split ownership or duplicated policy.

Required checks:

1. One authoritative provider-bound thinking adaptation function.
2. One authoritative failure-effects classifier.
3. One authoritative failure-effects application boundary.
4. One selected-attempt finalization supervisor.
5. One runtime ownership release abstraction.
6. One process-owned database recovery controller.
7. One decoded provider-bound request lifecycle.
8. One non-stream parsed response lifecycle.
9. One bounded dispatch-writer diagnostics implementation.
10. One request-coherent detailed span sampling decision.

Reject the integration if legacy and new paths can both apply health, quarantine, finalization, reservation release, connection recovery, or payload transformations for the same event.

Add source-level guard tests or structural assertions for high-risk duplicate paths.

## Workstream B — Canonical end-to-end scenarios

Create one integration harness that can configure multiple mock providers/accounts and execute exact request sequences.

Required providers:

- OpenCode Go-like OpenAI-compatible provider with MiniMax-M3 fixed or restricted thinking controls.
- MiniMax-native-like provider with its distinct accepted thinking contract.
- Anthropic-compatible provider.
- Generic OpenAI-compatible provider.
- A provider that emits controlled transport and malformed response failures.

Required canonical scenario:

1. Send MiniMax-M3 through OpenCode Go with unsupported thinking level.
2. Assert local adaptation/rejection or one allowed compatibility retry.
3. Assert protocol-appropriate client response.
4. Assert zero account/model/circuit/durable-backoff health effects.
5. Assert no pending request/attempt/reservation ownership after bounded finalization.
6. Immediately send an unrelated successful request.
7. Immediately send corrected MiniMax-M3 request through OpenCode Go.
8. Immediately send MiniMax-M3 request through MiniMax native provider.
9. Restart neither process nor database.
10. Repeat under streaming, non-streaming, cancellation, and induced finalization/database fault variants.

The harness must record structured state snapshots before and after every step.

## Workstream C — Full protocol and capability matrix

Cover:

- OpenAI client -> OpenAI upstream.
- Anthropic client -> Anthropic upstream.
- OpenAI client -> Anthropic upstream.
- Anthropic client -> OpenAI upstream.
- Provider-qualified and collapsed model IDs.
- Native and prepared-transcode paths.
- Streaming and non-streaming.
- Tools absent/present.
- Cache controls absent/present.
- Compression off/observe/safe.
- Synthetic cache off/dry-run/apply.

Thinking/control cases:

- Omitted.
- `low`.
- `med` alias.
- `medium`.
- `high`.
- provider-specific accepted alias.
- unsupported effort.
- unknown effort.
- explicit budget below minimum.
- explicit budget within range.
- explicit budget above maximum.
- fixed reasoning with client-selected effort.
- historical reasoning content without new control.
- strict reject policy.
- warn/drop policy.
- map-if-known policy.
- unknown-contract allow-with-warning policy.

For every case assert upstream request semantic JSON, client response semantic JSON or exact bytes, thinking trace, usage/cost, finalization state, and health effects.

## Workstream D — Full failure-effects matrix

Exercise at least:

- local malformed JSON;
- missing model;
- local context-limit rejection;
- local capability rejection;
- upstream unsupported thinking control;
- HTTP 400 generic validation;
- HTTP 401 auth;
- HTTP 402 quota;
- HTTP 403 auth-like;
- HTTP 403 quota-like;
- HTTP 403 ambiguous;
- HTTP 404 generic route;
- HTTP 404 runtime model-like;
- HTTP 404 authoritative catalog absence;
- HTTP 408;
- HTTP 409 generic;
- HTTP 409 quota-like;
- HTTP 422 generic;
- HTTP 422 quota-like;
- HTTP 429 with and without Retry-After;
- HTTP 500, 502, 503, and 504;
- connect timeout/error;
- pool timeout;
- read timeout/error;
- write timeout/error;
- remote protocol error;
- malformed success JSON;
- malformed error JSON;
- client cancellation before first byte;
- client cancellation midstream;
- upstream midstream failure;
- finalization failure;
- database failure.

For each observation, assert the exact immutable `FailureEffects`, applied state changes, durable backoff/quarantine record, retry behavior, and client response.

## Workstream E — Cancellation and ownership race matrix

Use deterministic synchronization barriers, not sleeps, for each point:

1. Before request persistence.
2. During selection persistence.
3. After durable selection commit before runtime publication.
4. After runtime claim before upstream send.
5. During provider-bound adaptation.
6. During upstream connect.
7. After headers before body.
8. Before non-retryable finalization registration.
9. During durable finalization.
10. After finalization commit before runtime release.
11. During runtime release.
12. During response rendering.
13. Midstream after one chunk.
14. During finalization retry.
15. During database recovery.
16. During rehash generation swap.
17. During shutdown drain.

Run each race repeatedly, minimum 100 iterations for critical cancellation points. Assert:

- exact terminal durable state;
- zero duplicate terminal transitions;
- zero double health/quarantine effects;
- no leaked active count, quota reservation, health probe, response, generation lease, task, or queue entry;
- subsequent request success.

## Workstream F — Database fault and recovery matrix

Execute the Plan 027 deterministic cases through the real request path, not only unit helpers.

Required integrated outcomes:

- Clean rollback leaves original connection usable.
- Rollback uncertainty invalidates and replaces connection.
- Commit ambiguity reconciles dispatch/finalization exactly.
- Readiness false during recovery and true only after probe/reconciliation.
- Concurrent requests join one recovery attempt.
- Background writers pause/resume without duplicates.
- Read-only dashboard behavior matches documented policy.
- Rehash does not create a second recovery controller.
- Shutdown during recovery leaves database and durable state consistent.
- Recovery exhaustion remains failed closed with actionable diagnostics.

Run a database consistency audit after every injected fault class.

## Workstream G — Model quarantine lifecycle matrix

Validate:

- First runtime model-like 404 creates bounded suspected state.
- Repeated equivalent evidence promotes according to threshold.
- Generic 404 does not contribute.
- Expiry restores routing.
- Exact-key success clears state.
- Provider catalog reappearance clears state.
- Alternate provider/account/protocol remains eligible throughout.
- Authoritative withdrawal becomes terminal only under configured policy.
- Legacy migrated model-unavailable rows hydrate according to Plan 025.
- Operator-disabled models remain disabled.
- Rehash preserves or reconstructs unexpired state without duplication.

Use simulated time or injected clocks; do not make tests wait for real TTLs.

## Workstream H — Performance comparison

Compare exact Plan 023 baseline profiles to the integrated tree.

Required metrics:

- Request/response JSON decode and encode counts.
- `local_pre_upstream_ms` p50/p95/p99.
- Coordinator dispatch overhead p50/p95/p99.
- Per-span p50/p95/p99.
- Selection-claim wait/hold.
- SQLite lock wait and transaction duration.
- Finalization completion latency.
- Dispatch-writer queue age, batch wait, transaction time, batch size.
- Throughput.
- RSS, tasks, threads, file descriptors.
- Pending rows/reservations/active counts/finalization jobs.

Profiles:

- Serial native pass-through.
- Serial native provider adaptation.
- 50 concurrent native streams.
- 8 concurrent transcoded streams.
- Mixed provider/control workload.
- 10% validation failures.
- 25% client cancellation.
- Dispatch writer disabled/enabled.
- Detailed spans at 0%, default, and 100%.
- File-backed SQLite with two worker connections.
- Minimum-footprint SBC-like configuration.

Statistical policy:

- Warm up before measurement.
- Record sample counts and environment.
- Use repeated runs and report distributions.
- Define noise threshold before declaring regression.
- Do not discard unfavorable runs without documented cause and rerun policy.

## Workstream I — Resource and long-running soak

Required modes:

### PR/short soak

- 15–30 minutes.
- Mixed native/transcoded, streaming/non-streaming workload.
- Validation failures and cancellations.
- At least one database recovery cycle.
- Rehash cycles.

### Standard soak

- At least 2 hours on file-backed SQLite.
- Stable workload after warm-up.
- Periodic provider contract errors.
- Periodic bounded model quarantine expiry/clear.
- Dispatch writer enabled.
- Default instrumentation sampling.

### Extended soak

- At least 8 hours or repository-established extended duration.
- Mixed concurrency and periodic fault injection.
- Repeated database replacement.
- Repeated rehash.
- Metrics/dashboard polling.
- Background maintenance, checkpoint, model-info refresh, backup policy as appropriate for test environment.

Plateau assertions compare early and late stable windows for:

- RSS;
- task/thread/file-descriptor count;
- recorder sample count;
- metric series/cardinality;
- finalization active registry/history;
- dispatch queue depth/oldest age;
- database lock-wait p95;
- local-pre-upstream p95;
- pending requests/attempts/reservations;
- active account counts and quota reservations.

A monotonic increase requires root-cause resolution or explicit bounded explanation; it may not be waived as normal uptime behavior.

## Workstream J — Rollout and feature-flag closure

Inventory every temporary feature flag introduced by Plans 024–029.

For each flag record:

- default at initial landing;
- telemetry/verification required to enable;
- target default;
- removal release/criterion;
- owner;
- fallback behavior.

Expected rollout:

1. Provider contract observe mode.
2. Explicit OpenCode Go MiniMax-M3 normalization enabled.
3. Typed failure effects shadow comparison, then authoritative switch.
4. Bounded model quarantine enabled.
5. Process-owned finalization enabled.
6. Database automatic recovery enabled.
7. Payload lifecycle consolidation enabled by completed migration, not permanent branch flag.
8. Revised dispatch writer remains opt-in until soak evidence, then default decision documented.
9. Detailed span production sampling default applied.

Remove flags whose comparison window is complete. Permanent dual paths are not closure.

## Workstream K — Configuration and migration validation

Test:

- Old configuration with no new fields.
- New provider-control policy fields.
- Model capability overrides.
- Database recovery settings.
- Dispatch writer metric/batching settings.
- Instrumentation sampling settings.
- Invalid values and contradictory combinations.
- Rehash changes classified correctly as live/restart-required under existing policy.
- Database migrations from representative prior schema versions.
- Legacy terminal model-unavailable data.

`eggpool check-config` must validate all new configuration before startup or rehash. Rehash must not partially apply new provider contracts or recovery settings.

## Workstream L — Operator diagnostics and runbook

Update documentation and deployment skill with:

- How provider-control adaptation decisions appear.
- How to inspect request-local versus provider-health errors.
- How model quarantine differs from terminal withdrawal.
- How to list/clear bounded quarantine.
- How finalization backlog is reported.
- How database recovery/readiness transitions appear.
- What to do for disk full, permissions, corruption, and migration failure.
- Why restart or database deletion is not expected for ordinary provider validation errors.
- How to capture a sanitized diagnostic bundle.
- Recommended production topology and sampling settings.

Add an automated diagnostic command if existing CLI patterns support it. It must redact secrets and content.

## Workstream M — CI partition and focused command

Add a dedicated CI job or extend a focused request-path hardening job with Plans 023–030 tests. Avoid one enormous serial job when suites can be partitioned safely.

Required command categories:

- capability/provider contract;
- failure effects/quarantine;
- finalization/cancellation;
- database recovery;
- payload equivalence/hot path;
- writer/observability;
- integrated scenario;
- performance;
- soak/audit.

Document commands in `AGENTS.md`. Do not remove existing Plan 019–021 or request-path coverage.

## Workstream N — Exact-head evidence

Create `artifacts/plan-030-exact-head-evidence.md` after all code/test changes are committed and verified.

Required contents:

- Full 40-character implementation commit SHA.
- Implementation tree SHA.
- Date/time and platform.
- Python 3.11 and 3.12 versions.
- Configuration profiles.
- Focused test results for Plans 023–030.
- Full standard non-slow suite result.
- Reload-control and existing request-path suite results.
- Performance comparison table against Plan 023.
- Short/standard/extended soak results.
- Database fault/recovery consistency results.
- Cancellation race repetition results.
- Resource plateau table.
- Ruff format/check, Pyright, and xfail/skip audit results.
- CI links/status where available.
- Explicit statement that no source/test changes occurred after verification.

If documentation-only changes follow verification, list them and prove the implementation tree is unchanged.

## Acceptance criteria

### Defect closure

- [ ] Unsupported MiniMax-M3 thinking control through OpenCode Go is contained to one request.
- [ ] The upstream never receives a contract-invalid control unless explicitly allowed by unknown-contract policy.
- [ ] No account, circuit, model, catalog, or durable backoff penalty occurs for the compatibility error.
- [ ] No runtime ownership leaks remain.
- [ ] Corrected and unrelated requests succeed immediately afterward.
- [ ] Fault variants recover without restart or database deletion.

### Cross-phase ownership

- [ ] There is one authoritative implementation for each of the ten architecture ownership points.
- [ ] Legacy duplicate health/finalization/recovery/payload paths are removed or unreachable with structural tests.
- [ ] Failure effects are applied once.
- [ ] Finalization is process-owned.
- [ ] Database recovery is process-owned and single-flight.
- [ ] Request and response payload lifecycle is single-parse/single-encode on common paths.
- [ ] Diagnostics are bounded.

### Functional matrix

- [ ] Full protocol/capability matrix passes.
- [ ] Full failure-effects matrix passes.
- [ ] All critical cancellation points pass at least 100 repeated iterations.
- [ ] Database fault matrix passes through real request paths.
- [ ] Model quarantine lifecycle passes with simulated time.
- [ ] Rehash and shutdown interactions pass.
- [ ] Existing routing, transcoding, usage, cost, cache, compression, dashboard, and reload suites remain green.

### Performance

- [ ] Plan 028 parse/encode count gates are satisfied.
- [ ] Native no-transform p50/p95 has no material regression beyond predefined noise threshold.
- [ ] Transcoded/multi-transform paths show the planned operation-count reduction.
- [ ] Finalization transaction p95 does not regress and expected lookup reduction is evidenced.
- [ ] Dispatch writer enabled profile has bounded queue age and demonstrated transaction reduction.
- [ ] Instrumentation overhead at default sampling is quantified and acceptable.
- [ ] No benchmark path disables required capability, accounting, or finalization behavior.

### Long-running stability

- [ ] PR, standard, and extended soak modes complete.
- [ ] RSS plateaus after warm-up.
- [ ] Task, thread, and file-descriptor counts plateau.
- [ ] Pending requests/attempts/reservations return to baseline.
- [ ] Active counts, quota reservations, health probes, and finalization jobs return to baseline.
- [ ] Recorder storage and metric cardinality remain within configured bounds.
- [ ] Snapshot latency does not increase with process age.
- [ ] Database lock wait and dispatch latency show no monotonic late-window increase.
- [ ] Repeated database recovery and rehash do not leak resources.

### Rollout and operations

- [ ] Every temporary flag has a documented removal criterion.
- [ ] Completed comparison flags are removed.
- [ ] Safe defaults are documented.
- [ ] Old configuration remains valid or receives a precise migration error.
- [ ] `check-config` validates all new settings.
- [ ] Operator runbook covers normal recovery and true database faults.
- [ ] Diagnostic outputs are sanitized.

### Exact-head verification

- [ ] Focused suites pass on Python 3.11 and 3.12.
- [ ] Full standard non-slow suite passes.
- [ ] Existing reload-control/request-path/performance/soak jobs pass.
- [ ] Ruff format and check pass.
- [ ] Pyright reports zero errors.
- [ ] Xfail/skip audit passes.
- [ ] Exact-head evidence references the verified commit and tree.
- [ ] No source/test change occurs after verification without rerunning affected gates.

## Roadmap closure procedure

After Plan 030 acceptance criteria pass:

1. Update Plans 023–030 to `Status: completed` with implementation/evidence references.
2. Update Plan 022 to `Status: completed` and link Plan 030 exact-head evidence.
3. Add focused commands to `AGENTS.md`.
4. Commit evidence after the implementation tree is fixed.
5. Verify the final documentation-only commit diff contains no source/test changes.
6. Do not declare closure with deferred correctness, recovery, leak, or unbounded-memory items.

## Explicit rejection conditions

Do not mark this plan complete if any of the following remain:

- A provider validation error can disable unrelated traffic.
- Recovery requires deleting the database.
- Database invalidation remains process-permanent.
- Any cancellation path can strand an active count, quota reservation, health probe, or pending reservation.
- A runtime-only first 404 can create indefinite model withdrawal by default.
- Common non-stream paths still parse the same body repeatedly without documented necessity.
- Dispatch-writer diagnostics remain unbounded.
- Snapshot cost increases with total historical batches.
- Long-running dispatch or lock latency rises monotonically after warm-up.
- Tests use sleeps or broad outcome alternatives where deterministic barriers/faults are required.
- Closure evidence is not tied to an exact committed tree.

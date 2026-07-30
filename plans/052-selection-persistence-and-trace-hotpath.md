# Plan 052 — Selection, Persistence, and Trace Hot-Path Reduction

Date: 2026-07-30
Status: implementation handoff
Parent roadmap: `plans/045-upstream-streaming-hardening-hotpath-roadmap.md`
Planning baseline: `216e615d75269cc1471a920ae81ece9ef2d21802`

## Objective

Reduce serialized pre-upstream work in account selection and dispatch persistence while preserving the durable-selection, circuit-breaker, quota, fairness, and crash-recovery invariants.

This phase targets two confirmed avoidable costs:

1. account-ID lookup may await SQLite while the global selection-claim lock is held on a cache miss;
2. every selection attempt scans enabled accounts to reconstruct quarantine exclusions solely for diagnostics, even when the routing trace will not be persisted.

It also measures remaining selection/persistence contributors and applies only low-risk optimizations supported by the data.

## Ownership boundary

Primary modules:

- `src/eggpool/request/coordinator.py`
- `src/eggpool/routing/router.py`
- account registry/runtime generation account identity hydration
- routing plan/trace result types
- routing trace guard/writer integration
- dispatch span/selection diagnostics
- focused contention/performance tests

Do not redesign routing scores, fairness semantics, quota policy, circuit-breaker thresholds, database schema, dispatch writer defaults, or request finalization.

## Correctness invariants

The optimized path must preserve:

- one account claim at a time where mutable circuit/active state requires serialization;
- circuit half-open probe acquisition is atomic with selected candidate identity;
- account API key/provider identity corresponds to the claimed account;
- durable request/reservation/attempt rows commit before runtime publication;
- post-commit publication failure performs compensation;
- no upstream I/O occurs while the selection lock is held;
- fairness and attempted-account exclusion remain deterministic under the configured policy;
- trace generation cannot fail dispatch;
- crash recovery can reconcile ambiguous persistence outcomes.

## Workstream A — Prehydrate immutable account identity

### Required state

At runtime generation construction, build an immutable lookup keyed by account name containing at least:

- durable account ID;
- provider ID;
- account name;
- credential availability metadata, without duplicating raw secret storage unnecessarily;
- static routing priority/weight fields if already useful to the router.

The account registry may remain authoritative for API key retrieval. The key requirement is that durable account ID and provider identity needed for selection are resolved before request-time claim locking.

### Request-time behavior

Inside `_selection_claim_lock`:

- look up immutable identity in memory;
- do not instantiate `AccountRepository`;
- do not await SQLite;
- if identity is unexpectedly missing, release any acquired probe and fail with a bounded internal/database consistency error;
- do not perform an opportunistic database read under the lock as fallback.

A recovery/reload path may rebuild the generation identity map transactionally.

### Tests

- generation build loads all enabled/configured account IDs;
- rehash with account addition/removal publishes a coherent new map;
- missing identity fails closed and releases probe ownership;
- no database method is called while the selection-claim lock is held;
- account credentials are not copied into diagnostics or persisted maps.

## Workstream B — Move quarantine exclusion ownership into routing plan

The router already evaluates eligibility. Extend its plan/result to return structured exclusions generated during eligibility evaluation, including quarantine where available.

Required result shape may include:

```python
RoutingPlan(
    eligible_names=...,
    ranked_candidates=...,
    exclusions=tuple[RoutingExclusion, ...],
    ...
)
```

The coordinator must not rescan every enabled account to reconstruct exclusions after the router has already filtered them.

Requirements:

- no duplicate quarantine lookup per account for trace construction;
- trace semantics remain truthful;
- exclusion details are built only to the configured diagnostic granularity;
- score components remain omitted unless requested/configured;
- plan construction remains bounded by the candidate set already necessary for routing.

## Workstream C — Decide trace sampling before expensive trace detail

The request-level deterministic trace sampling decision must be available before constructing optional trace details.

For unsampled/off requests:

- do not build score-components JSON;
- do not build quarantine/exclusion detail beyond what routing itself needs;
- do not construct a `RoutingTraceEvent`;
- do not call time/serialization helpers solely for trace output;
- do not scan accounts solely for diagnostics.

For sampled requests:

- preserve current selected/top score and exclusion correctness;
- obey queue/backpressure guardrails;
- trace submission remains nonblocking/best effort.

Sampling decision must remain stable per request ID and configuration.

## Workstream D — Measure selection critical sections

Use existing dispatch spans/selection diagnostics to record:

- plan build time;
- first claim-lock wait and hold;
- circuit probe time;
- account identity lookup time;
- persistence queue/wait/transaction/commit time;
- second publication-lock wait and hold;
- trace detail build and submission time;
- compensation time.

Ensure instrumentation itself is sampled/bounded where detailed. Do not add per-candidate unbounded metrics labels.

Capture baseline before changes under:

- one account, no contention;
- 8 accounts, no contention;
- 8 accounts with mixed quarantine/circuit state;
- concurrency 1, 5, 20;
- trace off, sampled 5%, all;
- direct persistence and dispatch writer enabled where the existing runtime supports it.

## Workstream E — Narrow follow-on optimizations

After Workstreams A–D, apply only optimizations with measured significance. Candidate examples:

- cache provider/account immutable lookup directly on ranked candidate state;
- avoid rebuilding attempted-account sets when empty or unchanged;
- avoid repeated provider lookup from catalog then registry when routing plan already resolved it;
- reuse precomputed trace top-score metadata from the plan;
- remove imports/object construction from the critical path where measurable;
- batch or streamline persistence only through the existing dispatch writer design, without changing its default.

Each optimization must have:

- a before/after span or operation-count result;
- a correctness test for the invariant it touches;
- no expansion into broad router refactoring.

## Contention and failure tests

### Lock tests

Use explicit barriers to hold persistence or database operations while concurrent selectors run. Prove:

- a stalled SQLite operation does not hold `_selection_claim_lock`;
- a second request can complete claim phase while the first waits on persistence, subject to circuit/active correctness;
- publication remains serialized only for the brief runtime-state update;
- cancellation during persistence releases/compensates the acquired health slot.

### Trace tests

- trace `off`: zero trace detail objects/events;
- sampled request not selected: zero optional trace detail construction;
- sampled request selected: exact expected exclusions including quarantine;
- queue full/backpressure: dispatch succeeds and trace is dropped with diagnostic;
- sampling decision stable for same request ID;
- no full account scan solely for trace building.

### Reload/recovery tests

- generation reload rebuilds account identity map before publication;
- aborted candidate closes resources and never publishes partial identities;
- database invalidation/recovery does not leave stale account IDs in active generation;
- account deletion/addition across rehash routes only according to active generation.

## Performance acceptance targets

Comparison targets are evaluated on the same harness/machine:

- zero database awaits under `_selection_claim_lock` is mandatory;
- claim-lock p95 hold time reduced materially on account-ID cold-cache characterization, target at least 50%;
- unsampled trace path performs zero full enabled-account diagnostic scan;
- trace-off/sampled dispatch p95 does not regress more than 5%;
- sampled trace detail remains within current bounded queue/memory behavior;
- concurrency 5/20 selection lock wait p95 improves or remains statistically equivalent after removing persistence/cache-miss work;
- routing decisions and fairness sequence remain identical for deterministic fixtures.

Do not promote machine-specific absolute latency numbers as Raspberry Pi guarantees.

## Acceptance criteria

- [ ] Account IDs/provider identities required for selection are prehydrated before request dispatch.
- [ ] No SQLite query or repository creation occurs while `_selection_claim_lock` is held.
- [ ] Unexpected missing identity releases the probe and fails closed.
- [ ] The router returns the exclusions needed for trace truthfulness.
- [ ] The coordinator no longer scans all enabled accounts solely to reconstruct quarantine exclusions.
- [ ] Deterministic sampling/off decision occurs before optional trace-detail construction.
- [ ] Unsampled/off requests construct no trace event or optional score-component payload.
- [ ] Persistence remains outside the claim lock and publication retains its brief correctness lock.
- [ ] Compensation still converges after post-commit publication failure/cancellation.
- [ ] Fairness, eligibility, attempted-account exclusion, and provider/account selection parity are preserved.
- [ ] Detailed instrumentation remains bounded and does not add high-cardinality labels.
- [ ] Baseline and after measurements cover account count, contention, trace modes, and concurrency.
- [ ] Native request dispatch p95 does not regress beyond the comparison gate.
- [ ] Focused contention tests use barriers rather than arbitrary sleeps.

## Explicit rejection conditions

Do not close Plan 052 if:

- a cache miss still falls back to SQLite under the claim lock;
- account identity hydration can publish partially during rehash;
- trace optimization removes exclusion truth rather than transferring ownership to the router;
- unsampled requests still build discarded trace details;
- routing/fairness behavior changes without explicit authorization;
- the dispatch writer is enabled by default without separate evidence/decision;
- performance claims measure private helpers instead of the real coordinator path;
- absolute SBC performance promises are made from shared CI hardware.

## Handoff record

Record:

- implementation commit SHA;
- account identity map ownership/lifecycle;
- proof of zero DB awaits under claim lock;
- routing-plan exclusion API;
- trace operation counts by mode;
- baseline/after span table for all required profiles;
- fairness/routing parity results;
- contention/cancellation test repetition counts;
- any remaining measured hotspot deferred with evidence.

## Definition of done

Plan 052 is complete when request-time selection uses prehydrated immutable account identity, the global claim lock contains no database I/O, trace details are built only for requests that can use them, quarantine exclusions remain truthful without duplicate scans, routing invariants are unchanged, and measured concurrent dispatch overhead improves or remains bounded on the real coordinator path.
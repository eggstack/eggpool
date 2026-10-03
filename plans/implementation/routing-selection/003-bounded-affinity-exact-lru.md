# Routing Selection M003 — Bounded Exact Affinity LRU

Status: closing

Repository baseline: `31e9a6aa`

Source roadmap:

- `plans/subsystems/routing-selection-roadmap.md#milestone-003--bounded-exact-affinity-lru`

Evidence prerequisite:

- `plans/closure/routing-selection/002-affinity-lru-qualification.md`

Long-term requirements:

- `plans/000-long-term-specification.md`
- `plans/001-terminology-and-domain-model.md`
- `plans/002-long-term-roadmap.md`
- `plans/003-planning-process.md`

Applicable ADRs:

- None. This is a private, dependency-free cache data-structure change preserving existing runtime ownership and behavior.

Primary class: polish

## 1. Objective

Replace the O(n) `VecDeque::retain` recency touch on `ModelRouterAffinity` cache hits with an exact, bounded O(1) LRU update. Preserve sticky alias decisions, TTL, exact eviction order, single-flight, cancellation recovery, stats, and public API shape.

## 2. Why this milestone is ready

Routing M001 is closed. The representative Pi 5 workload measured the current hit path at 1.35 μs p50 / 1.56 μs p95 for 64 entries, 8.13 μs / 9.80 μs for 512, and 77.78 μs / 92.06 μs at 4,096 entries (`plans/closure/routing-selection/002-affinity-lru-qualification.md`). The cost is material at the configured cap.

## 3. Current implementation evidence

- `rust/src/model_router.rs` owns `AffinityState`, a `HashMap<AffinityKey, AffinityDecision>`, and a `VecDeque<AffinityKey>` under one `std::sync::Mutex`.
- `lookup` calls `state.lru.retain(|item| item != key)` then appends a cloned key, scanning the entire list on every live cache hit while holding the mutex.
- Expiration and invalid-route removal also scan the recency list. Insert cleanup checks a bounded number of expired entries.
- `rust/tests/model_router.rs` covers TTL/LRU eviction, non-sticky bypass, single-flight, cancelled-leader recovery, invalid selections, and selector failures.

## 4. Invariants that must not regress

- Exact least-recently-used eviction order: every valid cache hit moves precisely that entry to MRU; misses do not change recency until stored.
- Expired and invalid-target entries are removed and counted exactly as today.
- Cache entries and free index slots remain bounded by `max_entries` (normally 4,096); no unbounded stale-node queue.
- Single-flight leaders/joins, cancellation recovery, failures, and stats retain current meanings.
- State remains process-owned and protected by its existing mutex; no new lock, async work, dependency, public API, schema, config, or protocol surface.
- Model selection still precedes provider/account routing and cannot pin an account or bypass health/quota.

## 5. Scope

### In scope

- Replace list storage with a slot-indexed doubly linked recency structure backed by bounded `Vec<Option<Node>>` storage and a reusable free-slot stack.
- Store the slot index with each map entry and maintain head/tail links for constant-time hit, arbitrary removal, MRU move, and LRU eviction.
- Keep keys in the recency nodes so eviction can remove the corresponding map entry; avoid cloning keys on hits.
- Preserve bounded expired-entry cleanup during store and direct expiry removal during lookup.
- Add exact-order, expiry, slot-reuse, capacity, cancellation, and single-flight regression coverage.
- Repeat the same Pi 5 release workload at 64/512/4096 live entries without adding a permanent benchmark framework.

### Explicitly out of scope

- Approximate LRU, sampling, segmented/clock eviction, sharding, concurrent maps, and dependencies.
- Changing affinity TTL, cache capacity, session identity, selector protocol, route selection, or routing locks.
- General mutex/lock architecture changes.

## 6. Required production changes

Likely owner: `rust/src/model_router.rs`.

Represent each cached map value as `{ decision, lru_slot }`. A recency node stores the `AffinityKey`, previous slot, and next slot. `AffinityState` owns the optional-node vector, reusable free-slot vector, LRU head/tail, and existing map/flights/stats. Implement small private helpers for append-MRU, unlink, move-to-MRU, and unlink-and-free. Mutate the map and list together under the existing mutex; maintain one node for every entry and no node for an absent entry.

The node vector may allocate up to the configured cache capacity and reuses removed slots. The list operations must not scan the node vector or recency list on hits, invalidation, replacement, or eviction.

## 7. Ordered work packages

### Work package A — Indexed exact recency structure

Intent: make common hit and removal operations independent of cache length.

Changes: add slot-bearing map entries, linked recency nodes, head/tail indices, and free-slot reuse; route lookup/store/expiry/eviction through these helpers.

Acceptance evidence: code inspection confirms no list scan on cache hit; the number of live recency nodes equals map entries; capacity and eviction remain exact.

### Work package B — Semantic and lifecycle regression coverage

Intent: prove order, expiry, bounds, and concurrency remain unchanged.

Changes: extend `rust/tests/model_router.rs` with cases for hit-updated eviction order, expired non-head entries, invalid-target removal, replacement of an existing key, repeated slot reuse at capacity, and existing single-flight/cancelled-leader behavior.

Acceptance evidence: all tests pass serially; stats and decisions match the current contract.

### Work package C — Pi 5 before/after qualification

Intent: verify the measured hot-path cost improves at realistic occupancy.

Changes: run the same temporary optimized workload from M002 against the new implementation, using the same Pi 5, toolchain, capacities, cache occupancy, 90/10 hit distribution, and 50,000 samples per size.

Acceptance evidence: report p50/p95/max at each size. At 4,096 entries p95 must improve at least 5× from the 92.056 μs baseline; at 64 entries p95 must not regress by more than 10% from 1.555 μs. These are qualification thresholds for this workload, not public request SLAs.

## 8. Failure, cancellation, restart, contention semantics

No new async tasks or locks. The existing mutex serializes cache/list changes atomically. A cancelled selector leader must still wake followers via `FlightGuard`; LRU slots are allocated only when a decision is stored. Failed/aborted selector results must not leave occupied slots. TTL cleanup and `max_entries` eviction must unlink the node before returning its slot for reuse.

## 9. Compatibility and migration

No persisted state, config, public API, dependency, or wire compatibility change. Cache contents are process-local and rebuilt after restart.

## 10. Required tests

```bash
cargo test --manifest-path rust/Cargo.toml --test model_router -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test routing_domain_d008 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- --test-threads=1
```

Run strict Clippy and formatting for default/no-default per the repository development workflow. Run both relevant model-router integration suites after implementation.

## 11. Documentation updates

- Update the routing roadmap current-state and M003 status.
- Add a closure record with semantic test evidence, before/after Pi 5 timings, bounded-memory review, and unblock audit.
- Do not add a permanent benchmark framework.

## 12. Acceptance criteria

- Exact LRU semantics and all existing affinity behavior are preserved.
- Cache-hit recency updates and arbitrary entry removals are O(1) expected time, with no `VecDeque::retain` or equivalent list scan on those paths.
- Node storage, free slots, map entries, and flights remain bounded by configured limits.
- Existing/new serial tests and full default/no-default workspace gates pass.
- Pi 5 measurements meet the 5× p95 improvement at 4,096 and <=10% p95 regression at 64.
- No public/API/config/dependency/protocol or production topology change.

## 13. Stop conditions

Stop if exact order, TTL, cancellation, single-flight, or stats cannot be preserved with bounded slot bookkeeping; if implementation adds a list scan to common hit or removal paths; if measurements miss thresholds; or if the design requires a new dependency, public surface, or changed lock ownership. Record a keep/revert disposition rather than weakening the gates.

## 14. Closure evidence required

Record the implementation commit, semantic tests, default/no-default/Clippy/fmt results, before/after Pi 5 measurements, capacity/free-slot bounds, compatibility/security review, and whether any other plan became ready.

## 15. Handoff notes

The Pi 5 results and exact M002 workload are in `plans/closure/routing-selection/002-affinity-lru-qualification.md`. Keep release measurements on the physical Pi/MMC host; do not substitute development-host timings.

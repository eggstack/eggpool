# Routing Selection M003 — Closure Status

Status: closed

Source implementation plan:

- `plans/implementation/routing-selection/003-bounded-affinity-exact-lru.md`

Source subsystem roadmap:

- `plans/subsystems/routing-selection-roadmap.md#milestone-003--bounded-exact-affinity-lru`

Repository baseline reviewed: `e26ba34f` (implementation start after M002 qualification)

Implementation and closure commits:

- `dc6ea713` — replace affinity LRU scan with indexed list; add Pi 5 qualification evidence
- `b0da0e6f` — begin formal closure
- Final closure/status reconciliation commit: recorded in Git history

## 1. Executive finding

M003 is complete. The process-owned affinity cache now uses an index-bearing map entry and a bounded slot-indexed doubly linked LRU list. Live cache hits move the existing node to MRU in expected O(1) time, without scanning or cloning the affinity key. Expiration, invalid-target removal, replacement, and eviction unlink and reuse slots under the existing mutex. Exact LRU semantics, TTL, single-flight, cancellation recovery, and stats remain intact.

On the same Raspberry Pi 5 and the same 90% hot-alias / 10% long-tail, 50,000-hit workload used for M002, 4,096-entry p95 fell from 92.056 μs to 1.019 μs (about 90.3×). The 64-entry p95 fell from 1.555 μs to 0.482 μs (about 3.2×). Both gates pass. No account/provider selection, lock ownership, public API, config, protocol, persistence, dependency, or routing behavior changed.

## 2. Requirement-to-evidence matrix

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| Exact hit-to-MRU order and eviction | `affinity_is_ttl_lru_bounded_and_sticky_false_bypasses_cache` | pass | A hit to the oldest entry changes the next eviction exactly as before. |
| Expiration and invalid selection behavior | `model_router` integration suite | pass | Expired entries are removed/count as expirations; invalid targets are removed without changing hit/miss semantics. |
| Single-flight and cancellation recovery | `concurrent_misses_single_flight_and_cancelled_leader_recovers`; full workspace suites | pass | Flight ownership and `FlightGuard` are unchanged; failed/cancelled selectors allocate no recency slot. |
| Repeated eviction and slot reuse | `affinity_reuses_slots_across_repeated_capacity_eviction` | pass | Thirty inserts through a capacity-three cache retain three entries and report exactly 27 evictions. |
| Bounded storage | source review of `AffinityState::{lru_nodes, free_lru_slots, entries, flights}` | pass | Nodes, free slots, and entries are bounded by `max_entries`; flights retain the existing `max_entries` admission bound. Removed nodes are reused, with no stale-node queue. |
| Pi 5 performance thresholds | physical release workload at 64/512/4096 live entries | pass | 4,096-entry p95 is about 90× better; 64-entry p95 also improves. |
| Workspace compatibility gates | full serial default/no-default workspace tests, strict Clippy for both feature sets, fmt | pass | 842 default tests and 843 no-default tests passed; one manually qualified dashboard test is ignored in each matrix. |

## 3. Production implementation evidence

`AffinityState` stores `CachedDecision { decision, lru_slot }` in its existing hash map. LRU nodes contain the affinity key and previous/next slot indices. `lru_head` and `lru_tail` identify endpoints, and removed slots go to `free_lru_slots` for reuse. `append_mru`, `unlink`, `move_to_mru`, and `remove_entry` update this state under the existing `std::sync::Mutex`.

`lookup` performs the existing key lookup, TTL/target checks, decision clone, and stats update. On a valid hit it moves the known slot to MRU without scanning the list. Expiry and invalid-target removal unlink by slot. Store-time cleanup retains its existing bounded 16-node scan; eviction removes the head and recycles its slot. There is no `VecDeque::retain` or equivalent cache-hit scan.

The cache remains process-local. There is no schema or migration, public shape, API, configuration, wire, dependency, or lock-topology change. Model selection remains ahead of provider/account selection and continues to respect health and quota routing.

## 4. Raspberry Pi 5 measurement evidence

Physical evidence and binary digest are committed at:

- `artifacts/qualification/routing-affinity-m003-pi5-2026-10-03/physical-attestation.md`
- `artifacts/qualification/routing-affinity-m003-pi5-2026-10-03/binary.sha256`

The test ran on Raspberry Pi 5 Model B Rev 1.0 (`rasp10`), aarch64 Ubuntu 24.04.4, Rust 1.98.1, with the project on ext4/MMC `/dev/mmcblk0p2`. It used 50,000 seeded cache hits at each capacity, with a deterministic 90% hot sticky alias and 10% uniformly distributed long-tail identities. Selector work was not included in timed hits. The temporary release integration workload was deleted after qualification; no permanent benchmark framework was added.

| Live entries | Baseline p50 | Baseline p95 | M003 p50 | M003 p95 | M003 maximum |
|---:|---:|---:|---:|---:|---:|
| 64 | 1.352 μs | 1.555 μs | 0.444 μs | 0.482 μs | 37.482 μs |
| 512 | 8.130 μs | 9.796 μs | 0.426 μs | 0.500 μs | 16.352 μs |
| 4,096 | 77.778 μs | 92.056 μs | 0.408 μs | 1.019 μs | 21.074 μs |

The M002 results are recorded in `plans/closure/routing-selection/002-affinity-lru-qualification.md`. The isolated cache timing is not an end-to-end request latency or throughput claim. Both comparisons were collected on this same physical Pi 5/MMC environment.

## 5. Verification executed

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
umask 022; CARGO_BUILD_JOBS=2 cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
umask 022; CARGO_BUILD_JOBS=2 cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
umask 022; CARGO_BUILD_JOBS=2 cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
umask 022; CARGO_BUILD_JOBS=2 cargo test --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- --test-threads=1
CARGO_BUILD_JOBS=2 cargo test --manifest-path rust/Cargo.toml --release --test m002_affinity_qualification -- --nocapture --test-threads=1
```

Both full workspace matrices completed all 66 test targets with zero failures: 842 passed/one ignored under default features and 843 passed/one ignored with no default features. Strict Clippy passed under both configurations, formatting and whitespace checks passed, and the temporary release qualification test passed at all three cache sizes.

The shell's inherited umask was `0002`; two unrelated update tests correctly rejected group-writable temporary executable fixtures under that setting. The repository matrices pass with standard restrictive `umask 022`, which creates the fixture files with the permissions those existing security assertions require. No source change was made for this environment-specific fixture assumption.

## 6. Compatibility and security review

Only process-local affinity bookkeeping and regression coverage changed. Cache decisions, route validation, TTL policy, stats meaning, session identity, selector execution, provider/account selection, and mutex ownership remain unchanged. No user content, credentials, or new persistent/diagnostic data were introduced. The old key is still retained in the bounded LRU node solely for eviction; no stale nodes remain after removal.

## 7. Unblock audit

M003 has no downstream blocked implementation dependency. The active implementation-plan inventory is now empty, and the registry dependency-ready and blocked-work tables contain no eligible work. Persistence remains an active subsystem roadmap because the foreground checkpoint tail is unresolved, but its dedicated-checkpointer M007 experiment was rejected on Pi 5 WAL progress/convergence evidence; no successor is currently registered. Deployment and packaging has no ready successor. Provider profile metadata C001 and the requested Runtime Efficiency M001–M003 line are closed. No additional existing plan can be unblocked or started from this closure; new work requires a newly scoped plan.

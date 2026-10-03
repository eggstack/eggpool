# Routing Selection M002 — Affinity LRU Cost Qualification

Status: closed

Source subsystem roadmap:

- `plans/subsystems/routing-selection-roadmap.md#milestone-002--semantic-affinity-exact-lru-cost-qualification-evidence-gated`

Repository baseline reviewed: `9de6def0`

## 1. Executive finding

The representative workload makes the current exact-LRU hit touch material at the 4,096-entry configured cap. The hit path in `rust/src/model_router.rs` calls `VecDeque::retain` while holding the affinity mutex, scanning the recency list on each cache hit. Measured p50 rose from 1.35 μs at 64 entries to 8.13 μs at 512 and 77.78 μs at 4,096; 4,096-entry p95 was 92.06 μs. The evidence supports a bounded exact indexed-list follow-up. No runtime code changed in M002.

## 2. Requirement-to-evidence matrix

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| Measure representative 64/512/4096 live entries | optimized temporary integration workload on the current Pi 5, 50,000 hits per capacity | pass | Each cache was seeded to exactly its tested live-entry count. |
| Exercise sticky alias repetition and long tail | deterministic 90% hot session / 10% uniformly distributed long-tail hits | pass | All measured accesses were cache hits; selector work was skipped. |
| Measure the current complete cache-hit call | `ModelRouterAffinity::resolve` on the existing compiled router and seeded session identities | pass | Timing includes async call overhead, identity map/hash work, mutex acquisition, decision clone, and current LRU touch. |
| Preserve source-under-test identity | `rust/src/model_router.rs` byte-compared equal to the pinned release-test worktree source | pass | Test built against the M001-complete implementation. |
| Decide whether an exact bounded redesign is justified | p50/p95 results below | pass | 4,096-entry costs justify an exact O(1) hit update; 64-entry costs remain small. |

## 3. Physical target and workload

- Board: Raspberry Pi 5 Model B Rev 1.0, Linux/aarch64, Ubuntu 24.04.4 LTS, four CPU cores.
- Project filesystem: ext4 on MMC `/dev/mmcblk0p2`.
- Toolchain: `rustc 1.98.1 (48a229cea 2026-09-01)`.
- Release-test source revision: `6f0cfd532e753a43c454b370453850c55679aab5`; the affinity module is byte-identical to the current baseline.
- Per size: fill cache to capacity with distinct live sticky-session identities; perform 50,000 hits, selecting the same hot alias for 90% and a deterministic uniform long-tail identity for 10%; collect per-hit `Instant` durations and compute p50/p95/max.
- The temporary test was run in the detached Pi worktree as `rust/tests/m002_affinity_qualification.rs`; it was not retained as a permanent benchmark framework, per roadmap scope.

## 4. Results

| Live entries | Measured hits | p50 | p95 | Maximum |
|---:|---:|---:|---:|---:|
| 64 | 50,000 | 1.352 μs | 1.555 μs | 32.556 μs |
| 512 | 50,000 | 8.130 μs | 9.796 μs | 30.222 μs |
| 4,096 | 50,000 | 77.778 μs | 92.056 μs | 419.705 μs |

At the 4,096-entry cap, 1,000 cache hits/s at the measured p50 consume about 77.8 ms of CPU time per second, approximately 7.8% of one core. This is a direct arithmetic inference from the per-hit measurement, not an application-wide throughput claim. The workload isolates cache lookup and does not include request parsing, provider selection, or network latency.

## 5. Verification executed

```bash
CARGO_BUILD_JOBS=2 cargo test --manifest-path rust/Cargo.toml --release --test m002_affinity_qualification -- --nocapture --test-threads=1
```

Result: 1 temporary release test passed; output reported 50,000 hits for each of the three cache sizes. Full output and test source were local to the temporary qualification worktree; the result table above retains the aggregate evidence needed for the bounded follow-up.

## 6. Invariant and compatibility review

The workload exercised only the process-owned affinity cache. No routing selection, provider/account choice, TTL policy, session identity, selector protocol, diagnostics, or public API changed. Any follow-up must retain exact LRU order, bounded capacity, expiry behavior, single-flight, cancellation recovery, and stats.

## 7. Unresolved findings

| Severity | Finding | Impact | Required action |
|---|---|---|---|
| medium | Exact LRU recency touches scale to 77.8 μs p50 and 92.1 μs p95 at 4,096 live entries. | Material serialized affinity-lock cost at the configured cap. | M002 authorizes a bounded exact O(1) follow-up plan; approximate eviction and unbounded stale-node queues remain out of scope. |

## 8. Roadmap disposition and unblock audit

M002 is closed as a measured qualification. The evidence makes a follow-up eligible, so routing-selection M003 is registered `ready` with an index-based exact LRU design. It has no dependency on Persistence M007; M007's rejection does not block it. No other blocked implementation plan depends on M002.

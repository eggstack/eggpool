# Routing affinity M003 Pi 5 qualification

- Hardware: Raspberry Pi 5 Model B Rev 1.0 (`rasp10`), aarch64, four CPU cores.
- OS: Ubuntu 24.04.4 LTS, Linux `6.8.0-1064-raspi`.
- Project filesystem: ext4 on MMC `/dev/mmcblk0p2`.
- Rust: `rustc 1.98.1 (48a229cea 2026-09-01)`.
- Source baseline: `e26ba34f387a42229653afdd828f0d47353c41db`.
- Test binary SHA-256: `d2a2d64e9ba6055442f1e428f3be3fb8c4eac903e216aac0d6d3bb98e2c4c203`.
- Method: temporary optimized integration test; 50,000 `ModelRouterAffinity::resolve` cache hits per size, fully seeded live cache, deterministic 90% hot sticky alias and 10% uniform long-tail identities. Per-call `Instant` timings; selector work was skipped on all measured hits.

| Live entries | Hits | p50 | p95 | Maximum |
|---:|---:|---:|---:|---:|
| 64 | 50,000 | 0.444 μs | 0.482 μs | 37.482 μs |
| 512 | 50,000 | 0.426 μs | 0.500 μs | 16.352 μs |
| 4,096 | 50,000 | 0.408 μs | 1.019 μs | 21.074 μs |

The 4,096-entry p95 improved about 90.3× against the M002 Pi 5 baseline of 92.056 μs. At 64 entries, p95 improved about 3.2× against 1.555 μs. This isolated cache-hit measurement is not an end-to-end request SLA. The temporary benchmark source was not retained as a framework.

Command:

```bash
CARGO_BUILD_JOBS=2 cargo test --manifest-path rust/Cargo.toml --release --test m002_affinity_qualification -- --nocapture --test-threads=1
```

Result: 1 test passed; 50,000 hits measured at each of the three capacities.

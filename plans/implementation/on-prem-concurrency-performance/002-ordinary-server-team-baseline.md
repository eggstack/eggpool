# On-Prem Concurrency Performance M002 — Ordinary-server small-team capacity baseline

Status: ready

Repository baseline: `88f58bbf2e9095cf46437945edf83e580270bdb6` (`main`, post-PR #7).

Source roadmap: `plans/subsystems/on-prem-concurrency-performance-roadmap.md#m002--ordinary-server-team-baseline`

Long-term requirements: `plans/000-long-term-specification.md#1-product-definition`, `#2-end-state-invariants-normative`, `plans/002-long-term-roadmap.md#phase-5--optional-on-prem-small-team-access-and-budgeting`.

Applicable ADR: `plans/adrs/ADR-0002-on-prem-user-identity-and-policy-boundaries.md` (accepted). Persistence/storage ADRs unchanged.

Primary class: polish

## 1. Objective

Measure credible on-prem small-team capacity on representative ordinary Linux server hardware with durable SSD/NVMe, explicitly separate named team membership from active simultaneous inference requests, and retain Pi 5 microSD as a low-end solo performance/durability regression signal. This is not a mandate to optimize Pi microSD tails or claim 50 concurrent generations.

## 2. Readiness and dependencies

Hard: none; on-prem PR #7 and Pi M001 attribution are integrated/conditionally closed. Operational: an explicitly provisioned ordinary Linux target with hardware/OS/storage details is mandatory for a server-capacity claim; otherwise close only a tooling/setup slice conditionally and record unmeasured cells. No external data/provider credentials.

## 3. Current evidence

`plans/closure/on-prem-user-access/012-status.md` measured 49 named Pi accounts, RSS around 20 MB and single-request p50 around 5 ms, with microSD contention and eight-way saturation. `plans/closure/on-prem-concurrency-performance/001-status.md` isolated recurrent microSD SQLite publication tails versus tmpfs; a separate exploratory NVMe run reported 1,000/1,000 completions at low latency. These are not representative ordinary-server capacity/SLA evidence. `scripts/qualification_sbc.py` already has principal, concurrency and optional budget load parameters and emits sanitized artifacts.

## 4. Invariants

Do not change production DB journal, worker/gate topology, admission, policy, provider transport, quota fairness or budget semantics from benchmarking alone. No real user secrets/prompts, no live external provider billing, no false 50-concurrent service claim, and no insecure performance shortcut (e.g. production tmpfs database).

## 5. Scope

In: repeatable ordinary-server synthetic qualification corpus, resource/latency metrics, safety and deployment guidance; optional small tooling adjustments. Out: runtime performance optimization, capacity promises without hardware, re-testing Pi M001 closure as a blocker, switching to external DB, distributed cluster, web load balancer.

## 6. Required production changes

None by default. Extend `scripts/qualification_sbc.py` or extract shared sanitized tooling if needed for ordinary-server fixtures; retain existing Pi fixture and predeclared metric meanings. Record build identity, physical hardware CPU/RAM/disk/fs/mount, container/VM status, server OS, actual storage persistence, WAL/NORMAL and database worker count. Freeze workload and gates prior to collecting results.

## 7. Ordered work packages

### A — Freeze workload and qualification design

Define at least 1,5,10,25,49 named accounts (metadata) separately from 1,2,4,8,16 concurrent in-flight requests, bounded batch sizes and run duration; further levels optional until safe. Use a synthetic upstream with configurable latency distribution, finite/streaming/translated surfaces, realistic payload sizes, per-user/global budget controls, mixed policy denies and token revocation. Include solo baseline on same host.

Acceptance: reproducible manifest, build hashes, no customer secrets, capacity terminology unambiguous.

### B — Paired durable server runs

On stable ordinary Linux SSD/NVMe test host, run three fresh-root repetitions per primary condition with WAL/NORMAL, one writer, no over-commit/benchmark artifacts. Record p50/p95/p99/max, throughput, requests/timeouts, stream TTFT, pre-upstream/dispatch/provider phases, CPU, RSS, SQLite gate, disk IO/pressure, WAL/checkpoints, and pending/reservation convergence. Include saturated load without causing host destabilization; report safety stop conditions.

Acceptance: accepted fixture JSON, accurate per-condition percentiles (enough samples to support p99), no orphaned active reservations, no data leakage.

### C — Interpret deployment envelope

Produce an ordinary-server deployment guide with honest throughput/concurrency ranges for the measured host/workload and sizing factors (provider latency, number of backends, SSD/CPU, login and rate/budget policy). Compare Pi microSD solo reference without rewriting old results. If an Eggpool-owned hot-path issue emerges, register a separate evidence-gated implementation plan rather than silently retuning SQLite or WAL.

Acceptance: explicit no-SLA language and reproducible raw-to-summary report guarded by tooling tests.

## 8. Failure, cancellation, restart and contention

Exercise shutdown/restart while users and budgets are active, retry and partial upstream errors, disk slowdowns and user disable. Count observed 429s separately for provider, identity and budget. Do not infer percentiles from a few samples; run on isolated target and abort pressure tests on sustained IO pressure/OOM. No batch success report can hide incomplete reservations.

## 9. Compatibility/migration

No storage migration or runtime API change expected. Keep Pi M001/MMC evidence immutable. Qualify the installed release candidate and its current migration state; do not claim capacity for unpublished binaries or different storage/media.

## 10. Tests

Extend `tests/tooling/test_qualification_sbc.py`, `tests/tooling/test_on_prem_mmc_tail_attribution.py` or add ordinary-server result validation tests; sanity-check concurrency/principal counts, p99 sample size, synthetic identities and secret absence. Re-run `rust/tests/on_prem_budgets.rs`, `rust/tests/on_prem_global_budgets.rs` and `rust/tests/on_prem_attribution.rs` if fixture logic changes.

## 11. Verification commands

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
uv run pytest tests/tooling/test_qualification_sbc.py tests/tooling/test_on_prem_mmc_tail_attribution.py -q
uv run ruff check scripts/ tests/tooling/
uv run pyright scripts/
git diff --check
```

## 12. Documentation

`docs/deployment.md`, `docs/raspberry-pi.md`, `architecture/deep-dive-observability.md`; report isolated ordinary-server results without rewriting Pi closure data or promising that every small team has the same workload.

## 13. Acceptance criteria

At least one ordinary-server/SSD target with three repeated measured conditions, explicit 1/5/10/25/49 named-user and concurrent-request separation, durable settlement/cancel/recovery evidence, honest measured envelope and solo low-end regression qualification.

## 14. Stop conditions

Stop and report conditional/unmeasured results if physical target unavailable, unsafe IO pressure, unreproducible provider fixture, insufficient p99 sample size, or architecture would need persistence-policy ADR. Do not invent a capacity number.

## 15. Closure evidence

Create `plans/closure/on-prem-concurrency-performance/002-status.md` with exact target/build/source, experiment matrix and sanitized artifacts, gate outcomes (including failures), measured latency/throughput/resource/settlement observations, security and content audit, conditionals and unblock audit.

## 16. Handoff

Performance evidence only, independent of M017–M023 code. Ordinary server is the likely team host; Pi microSD remains the canonical low-budget solo platform and a meaningful lightweight performance bound.

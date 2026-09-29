# Persistence Milestone 003 — Closure Status

Status: closed — rejected by paired Pi/MMC acceptance gates; implementation reverted

Source implementation plan:

- `plans/implementation/persistence/003-event-driven-checkpoint-coordination.md`

Source subsystem roadmap:

- `plans/subsystems/persistence-roadmap.md#milestone-003--event-driven-checkpoint-coordination`

Repository baseline reviewed: `abfdb18b62c437135fdb034f30f2e1630f25b8ae` (accepted M006 closure)

Implementation commits:

- `b5d145fb6468dcb787e07559e3d0adeb7c1da804` — attempted commit-derived wake, task-supervisor integration, successful-commit watermark, tests.
- `29bcbb4e` — reverted the M003 runtime implementation after physical acceptance failed.
- `abfdb18b` — accepted hard prerequisite, M006 SQLite NOOP and WAL-reset safety baseline.

## 1. Executive finding

M003 is closed with a rejected implementation outcome. The implementation passed Rust verification and successfully triggered maintenance during the measured batch. Across three paired Pi 5/MMC runs it removed the multi-second foreground publication COMMIT tail, but introduced 1.88–3.30 second finalization database-gate waits. Every candidate run failed the explicit requirement that improvement not transfer the tail into foreground gate wait. The implementation was reverted; production remains on the M006-fixed SQLite 3.53.2 timer-only baseline. No event-driven capability is claimed.

## 2. Requirement-to-evidence matrix

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| M006 observational NOOP and WAL-reset-fixed engine prerequisite | `plans/closure/persistence/006-status.md`; M006 closure SHA `abfdb18b`; SQLite 3.53.2 | pass | Exact baseline used for paired physical work. |
| Successful-commit-only signal, coalescing wake, and failure suppression | M003 DB unit coverage in attempted commit `b5d145fb`; DB tests | pass for attempted candidate | Ordinary/explicit successful COMMIT signaled after gate release; rollback, body failure, and deferred-FK COMMIT failure did not signal. Runtime change reverted. |
| Existing task ownership, event/timer/cancellation integration | `runtime_lifecycle_r008`; task supervisor implementation at `b5d145fb` | pass for attempted candidate | Existing single process-owned checkpoint task was retained; burst commit wake observed before 60-second fallback. Runtime change reverted. |
| Database/task/lifecycle, backup/recovery, publication/finalization, default and no-default parity | Full serial default and no-default workspace suites | pass for attempted candidate | Includes database compatibility, O006, R006/R008, publication/finalization, transport and lifecycle suites. Runtime change reverted. |
| Paired physical performance gates on Pi 5/ext4/MMC | Three baseline and three candidate Plan-239-style phase artifacts under `artifacts/qualification/m003/` | fail | Candidate publication COMMIT maxima fell to 349–376 µs, but finalization gate wait maxima were 1.88–3.30 s and total request maxima 1.89–3.31 s. |
| Candidate ordinary concurrency-4, durability and functional evidence | `candidate-ordinary-benchmark.json` and `baseline-ordinary-benchmark.json` | pass, descriptive | 32/32 concurrent requests succeeded; candidate had 0 functional failures and durable convergence, backup/recovery/restart/shutdown/re-hash checks passed. This does not override the sequential acceptance failure. |
| Retain safe production baseline | Revert commit `29bcbb4e`; source diff and `git status` | pass | M006 engine remains; no M003 runtime source change retained. |

## 3. Production implementation evidence

The attempted implementation added a separate successful-COMMIT sequence and a coalescing `Notify` to the shared Database state. Both implicit and explicit transaction APIs signaled only after successful COMMIT and after dropping the EggPool database gate. The existing checkpoint task gained an optional wake and selected between cancellation, wake, and fallback timer. Maintenance eligibility used the successful-commit sequence instead of the transaction-attempt count. No SQLite hook, second connection, unbounded queue, public config, schema migration, or new process task was introduced.

Those changes were reverted as required by work package F. The retained production state uses the M006 SQLite 3.53.2 engine, one connection/gate/worker, the existing process-owned timer-driven checkpoint task, the 256-frame soft threshold, 60-second timer, and SQLite's 1000-page automatic fallback.

## 4. Physical qualification and disposition

All six phase runs used the same physical Raspberry Pi 5 Model B Rev 1.0, Ubuntu 24.04.4 LTS, Linux/aarch64 kernel 6.8.0-1064-raspi, ext4, and non-rotational MMC storage. Effective settings were WAL, synchronous=NORMAL, page size 4096, automatic checkpoint 1000 pages, maintenance soft threshold 256, and fallback 60 seconds. The fixture was the low-wear steady-state SBC benchmark fixture. The direct loopback provider control remained stable (maximum 1 ms in the recorded baseline and candidate phase artifacts).

On-device release candidates:

| Candidate | Source commit | Build | SHA-256 |
|---|---|---:|---|
| M006 feature baseline | `abfdb18b62c437135fdb034f30f2e1630f25b8ae` | 373 s | `afb4486b511ff19147fe48e77a4f15e81e5d7b44b6b8713be317b780d33563a4` |
| M006 ordinary binary | `abfdb18b62c437135fdb034f30f2e1630f25b8ae` | 428 s | `4fd5654767b806435d2100c4c808447bfc8ad93233e7948b4d48e7e1dd19c9ba` |
| M003 feature candidate | `b5d145fb6468dcb787e07559e3d0adeb7c1da804` | 431 s | `1c5d681dcd00b87cb0760bc6a0aea59220461c81b469c06700f8eab3b410378a` |
| M003 ordinary binary | `b5d145fb6468dcb787e07559e3d0adeb7c1da804` | 389 s | `4c7d63ff6afbca7a8533c4904a8730d94d5d000b8a31f74947bc6cb644f2782e` |

The paired 60-request phase summaries are:

| Run | Variant | Batch ms | p95 / max total ms | Publication COMMIT p95 / max µs | Publication gate max µs | Finalization COMMIT p95 / max µs | Finalization gate max µs | Maintenance delta: below / checkpointed / failures / busy |
|---:|---|---:|---:|---:|---:|---:|---:|---|
| 1 | M006 baseline | 11,846 | 5 / 8,277 | 581 / 8,267,729 | 1 | 192 / 198,214 | 2 | 0 / 0 / 0 / 0 |
| 2 | M006 baseline | 5,839 | 5 / 3,327 | 457 / 3,308,555 | 1 | 183 / 39,723 | 2 | 0 / 0 / 0 / 0 |
| 3 | M006 baseline | 4,241 | 4 / 2,036 | 509 / 1,984,519 | 1 | 157 / 48,121 | 1 | 0 / 0 / 0 / 0 |
| 1 | M003 candidate | 7,268 | 177 / 2,933 | 353 / 376 | 1 | 4,594 / 5,314 | 2,924,193 | 108 / 12 / 0 / 0 |
| 2 | M003 candidate | 9,639 | 1,652 / 1,892 | 337 / 349 | 1 | 6,182 / 26,842 | 1,884,151 | 108 / 12 / 0 / 0 |
| 3 | M003 candidate | 6,761 | 243 / 3,312 | 334 / 355 | 8 | 26,985 / 57,214 | 3,304,519 | 108 / 12 / 0 / 0 |

The M006 baseline reproduced foreground publication COMMIT tails correlated with WAL checkpoint-sequence changes. The candidate made 12 maintenance checkpoints per measured batch with no checkpoint-maintenance failures and retained the configured 1000-page automatic fallback. However, in all three candidate runs, the maximum request correlated with checkpoint-sequence change and long post-provider/finalization wait; measured finalization gate wait was 1.88–3.30 seconds. Candidate maximum total latency exceeded the 500 ms gate in every run, and p95 exceeded 100 ms in runs 1–3. This is the exact tail-transfer failure prohibited by the plan. M003 is rejected; criteria were not weakened.

Ordinary benchmark artifacts are descriptive and use 10 benchmark samples with client concurrency 4. Candidate concurrency batch completed 32/32 requests, with p95 291 ms, max 293 ms, 33.017 requests/s, no failures, and durable state converged (`pending_requests=0`, `active_reservations=0`). Candidate functional IDs, re-hash, backup, recovery, restart, graceful shutdown, and resource stability passed. Candidate peak RSS was 18,419,712 bytes; M006 baseline peak RSS was 18,415,616 bytes. The candidate's one measured streaming response took 1,813 ms; the baseline ordinary benchmark also showed variable loopback streaming timing. These descriptive measurements do not meet or negate the failed sequential gate.

Artifacts:

- Baseline phase: `artifacts/qualification/m003/baseline-run-1.json`, `baseline-run-2.json`, `baseline-run-3.json`.
- Candidate phase: `artifacts/qualification/m003/candidate-run-1.json`, `candidate-run-2.json`, `candidate-run-3.json`.
- Ordinary comparison: `artifacts/qualification/m003/baseline-ordinary-benchmark.json`, `candidate-ordinary-benchmark.json`.

## 5. Verification executed

Local verification on the attempted candidate `b5d145fb`:

```text
cargo fmt --manifest-path rust/Cargo.toml --all -- --check                         passed
CARGO_BUILD_JOBS=1 cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1  passed
CARGO_BUILD_JOBS=1 cargo test --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- --test-threads=1  passed
CARGO_BUILD_JOBS=1 cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings  passed
CARGO_BUILD_JOBS=1 cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings  passed
cargo build --manifest-path rust/Cargo.toml --locked --release                         passed
cargo build --manifest-path rust/Cargo.toml --locked --release --features qualification-db-diagnostics  passed
```

Both full workspace suites were serial. The first default suite attempt hit a fixed-wait O002 timeout while compile I/O was high; the isolated test passed, and a cached full rerun passed. These are local results; no hosted CI result is claimed. The runtime implementation was reverted after these checks; documentation-only closure changes were checked with `git diff --check`.

Physical phase and ordinary qualification used `scripts/qualification_sbc.py` against release binaries built on the target. Exact invocations and captured metadata are in the JSON artifacts. The phase runs each used 60 sequential native finite requests plus the bounded direct-provider control; ordinary comparisons used concurrency 4.

## 6. Invariant, failure, and compatibility review

- Attempted candidate preserved one connection, one gate, one tokio-rusqlite worker, and one process-owned checkpoint task.
- No SQLite WAL hook, second connection, per-request task, unbounded queue, schema migration, or public API/config change was introduced.
- WAL/NORMAL, auto-checkpoint 1000, maintenance threshold 256, and 60-second fallback were unchanged.
- Successful COMMIT and gate release ordering was covered; rollback/body/failed-COMMIT paths remained silent.
- Backup, recovery, restart, shutdown, reload/re-hash, and functional checks passed on candidate ordinary qualification.
- Runtime changes are absent after revert. M006's NOOP and WAL-reset safety baseline remains in place.

## 7. Findings and follow-up

- **High — rejected performance behavior, not retained:** event-triggered checkpoint work can hold the shared DB gate long enough to impose repeated multi-second foreground finalization waits. The mechanism was reverted. This is a failed candidate result, not a current production defect.
- No unresolved correctness, data integrity, compatibility, or security defect was found in the retained M006 baseline.

The M003 approach is not eligible for promotion as production behavior. Any future checkpoint redesign must be registered as a new bounded plan and prove it does not move storage latency into foreground gate wait. This closure yields no automatically ready successor.

## 8. Registry update and unblock audit

- M006's hard engine dependency was closed at `abfdb18b`; M003 was promoted from blocked to ready in `a132a824`, then marked active in `aa3703f0`.
- M003 was implemented, qualified, rejected, and reverted. Its plan and roadmap now say closed with rejected disposition; this closure is the evidence record.
- The persistence roadmap has no other unstarted milestone whose hard prerequisites are newly satisfied by this closure. M001/M004/M005/M006 are already closed; M002 is closed. No later eligible persistence plan exists to promote.
- Provider-transport M002 remains blocked on its unrelated upstream Eggfetch API. Routing-selection M002 remains evidence-gated and is unaffected.
- A new checkpoint plan is not created or promoted here: the failed measurements establish a concrete design constraint, but do not establish a safe replacement mechanism.

## 9. Disposition

**Closed — rejected implementation.** The M003 runtime changes are reverted. The accepted M006 engine baseline remains the production state. There is no eligible successor to unblock; future checkpoint design requires a separately registered plan.

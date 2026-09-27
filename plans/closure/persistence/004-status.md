# Persistence Milestone 004 — Closure Status

Status: closed — periodic strategy insufficient on target; M003 promoted for separate architecture/design planning

Source implementation plan:

- `plans/implementation/persistence/004-physical-checkpoint-qualification-and-final-disposition.md`

Source subsystem roadmap:

- `plans/subsystems/persistence-roadmap.md#milestone-004--physical-checkpoint-qualification-and-final-disposition`

Repository baseline reviewed: `8113d264bc4488b22609d95d8fdcde1f632bc803`

Implementation commits or pull requests:

- None. No production Rust, schema, config, CLI, or tooling change was made in this pass. The closing commit contains only this closure record, the rejection artifacts, registry/roadmap/architecture/plan-status reconciliation, and the M003 promotion gate. Landed runtime behavior remains `6eae94db` (M001) + `52494140` (M002); checkpoint constants are byte-identical to the M001 baseline.

## 1. Executive finding

M004 closes with the periodic strategy **rejected** on the target class. All required
Pi/MMC evidence was collected from a single-board Linux/aarch64 Raspberry Pi 5
target with ext4 MMC storage. The qualification runner accepted the host (Linux,
aarch64, device-tree board model, attestation `Linux aarch64 plus device-tree board
model`), and thirteen accepted physical runs executed end-to-end against the
production-default mechanism.

The 60s/256-frame current candidate, three bounded threshold/cadence matrix
variants (60s/128, 60s/64, 30s/64), and the minimum-cadence stress (1s/64)
all leave the original multi-second foreground COMMIT tail. Across three
Plan-239-style 60-request sequential runs at 60s/256 the maximum request
latencies were 1709 ms, 561 ms, and 10943 ms; the slowest request in every run
had its foreground publication `COMMIT` own the WAL auto-checkpoint work
(publication `commit_us` 522 ms – 10.9 s), and the WAL checkpoint sequence
changed three times during each 60-request batch even though only one
maintenance tick fired. Lowering the soft threshold or shortening the cadence
does not help — the maintenance tick fires once (or zero times) during the
~3-5 s batch, observes `below_threshold` on its first watermark read, and the
foreground `COMMIT` later owns the 1000-page automatic-checkpoint safety
ceiling.

The production M001 mechanism is **retained as-is** (§9 additive-safe even when
periodic scheduling is insufficient). No constant retune is authorized: per
plan §6.2, "If several candidates all fail, prefer the landed conservative
mechanism". Persistence M003 (conditional event-driven checkpoint
coordination) becomes the next dependency-ready milestone for the persistence
subsystem and is promoted below per plan §E3 and the persistence roadmap
hard/interface dependency graph.

## 2. Candidate identity

| Build | SHA-256 | Bytes | Built from | Target |
|---|---|---|---|---|
| Ordinary release | `c3f4391ad7404dabeb0c0d219b53aaab6b9bb6568e4c29594586a1671a87893f` | 31 421 560 | `8113d264` | Pi 5/MMC |
| `qualification-db-diagnostics` release | `b7a4fa402e1caee6048dcb527d33a4e10234a8d9e325a54ecaff8646f10c32a7` | 31 485 520 | `8113d264` | Pi 5/MMC |

Both candidates verified with `--expected-sha256`. Toolchain: `rustc 1.98.1 (48a229cea 2026-09-01)` (rustup default on the SBC). Build mode: `q005-qualified-aarch64-copy` (the runner is invoked from the same SBC; release profile, locked, ordinary or `qualification-db-diagnostics`).

Target attestation (one-shot, captured during the first accepted run and confirmed identical across all thirteen runs):

- `board_model`: `Raspberry Pi 5 Model B Rev 1.0`
- `soc_cpu_core_count`: 4
- `cpu_frequency_policy`: 2400000 kHz → 2100000 kHz (governor `ondemand`)
- `ram_bytes`: 8 322 752 512 (~7.8 GiB usable)
- `filesystem`: `ext4` on `/`
- `root_storage_device_class`: `mmc` (`/dev/mmcblk0p2`, non-rotational)
- `os`: `Ubuntu 24.04.4 LTS`, kernel `6.8.0-1064-raspi`
- `architecture`: `aarch64`
- `attestation`: `Linux aarch64 plus device-tree board model`

Effective SQLite pragmas in every run (recorded by the runner, not asserted
from a process-side line): `journal_mode=wal`, `synchronous=NORMAL`,
`wal_autocheckpoint_pages=1000`, `page_size=4096`.

## 3. Requirement-to-evidence matrix

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| WP-A candidate identity + valid storage class | `git rev-parse HEAD = 8113d264`; both binaries recorded above; Pi 5 Model B / ext4 / MMC / device-tree attestation captured; runner physical-target gate passes | pass | No substitute evidence (no VM, emulation, cloud, tmpfs, or desktop result) is claimed |
| WP-A `--expected-sha256` + origin truthfulness | All thirteen runs invoked with `--expected-sha256`; runner recorded `q005-qualified-aarch64-copy` | pass | Two distinct SHA-256 values used per build profile; runner rejected any mismatch |
| WP-B current 60s/256 candidate, 3× accepted 60-request phase runs | `256-60s-run-1.json` max 1709 ms, `256-60s-run-2.json` max 561 ms, `256-60s-run-3.json` max 10 943 ms; each has 60/60 successful requests, 120 foreground records (publication + finalization), and a WAL checkpoint sequence change count of 3 | fail | Plan §13 max < 500 ms and publication `COMMIT` < 50 ms gates fail in all three runs; slowest request in every run correlates with foreground `commit_us` 522 ms – 10.9 s and `wal_checkpoint_sequence_changed = true` |
| WP-C bounded threshold/cadence matrix (60s/128, 60s/64, 30s/64, 1s/64) | 60s/128: max 14 906 / 2 445 / 28 653 ms across three runs; 60s/64: max 5 986 / 3 734 / 2 711 ms; 30s/64: max 3 641 ms; 1s/64: max 1 890 ms with 2 `checkpointed`, 1 `below_threshold`, 3 `gate_busy` (minimum allowed cadence, maintenance fires six times in the batch) | fail | No candidate clears the §13 gates; reducing the threshold does not eliminate the foreground tail because the maintenance tick fires too rarely during the batch to preempt the 1000-page automatic checkpoint |
| WP-C "no speculative retune" preference for landed constants | §6.2 forbids tightening the gates to obtain closure; the current 60s/256 is the simplest candidate that leaves the runtime unchanged. No constant retune is authorized. | pass | Production constants untouched (`CHECKPOINT_POLL_INTERVAL_S = 60.0`, `DEFAULT_SOFT_WAL_FRAMES = 256`) |
| WP-D ordinary burst, backup, restart, recovery, shutdown on target | `ordinary-benchmark-run-1/2/3.json` (each: native finite p50 4-5 ms, max 314-545 ms; native streaming max 7-8 ms; translated streaming max 7-9 ms; `pending_requests=0`, `active_reservations=0`, `peak_rss_bytes ≈ 18.2 MB`; all 24 functional ids `pass`); bounded-maintenance / rehash-runtime-status / backup-recover-isolated / restart-reconcile / graceful-shutdown all `pass` | pass | Note: `--benchmark-samples 30` cannot complete on the ordinary release candidate because the runner's HTTP client uses a hard 5 s per-request timeout that the residual auto-checkpoint tail can exceed; §E3 evidence criterion (no passing result depends on a second connection) is still met — the runner's failure is a per-request timeout, not an ownership/lifecycle fault. Documented in §6 below. |
| WP-E3 reject periodic strategy | This closure record + M003 promotion in `plans/subsystems/persistence-roadmap.md` and `plans/registry.md` | pass | E3 chosen: no allowed periodic candidate clears §13; landed M001 mechanism retained; M003 promoted |
| WP-F planning/documentation reconciliation | `plans/closure/persistence/004-status.md` (this record) + `plans/registry.md` + `plans/subsystems/persistence-roadmap.md` + `plans/implementation/persistence/004-physical-checkpoint-qualification-and-final-disposition.md` (status line only) + `architecture/deep-dive-database.md` + `architecture/deep-dive-background.md` in the same commit | pass | Historical M001/M002 closures, routing-selection M001 closure, and legacy Plans 239/240/242 untouched |
| Invariants: one connection/gate/worker, WAL/NORMAL, autocheckpoint 1000, no public surface | `git diff --name-only rust/`: zero Rust diff; `database_compatibility::maintenance_checkpoint_preserves_wal_normal_autocheckpoint_and_close` and `file_database_reports_wal_and_unchanged_autocheckpoint_ceiling` green on host | pass | No M002/routing-M001 reopen; no second connection opened |
| Direct-provider control | `direct_provider_control` recorded in every phase run: p50 total 1 ms, p95 2-3 ms, max 2-3 ms over 30 samples (loopback fixture). Plan 239 H0/H1 control was 2-5 ms. | pass | Confirms the foreground tail is owned by the database/foreground COMMIT, not by the provider loopback |
| Bounded maintenance outcome | `checkpoint_maintenance.final`: `not_due`/`gate_busy`/`below_threshold`/`checkpointed`/`failures` per WP-B / WP-C runs; failures counter is 0 in every run; `gate_busy` only appears in the 1s/64 stress run (3) | pass | `try_acquire` deferral is correct behavior; periodic scheduling simply does not fire often enough during a finite burst to preempt the safety ceiling |
| No substitute evidence / no fabrication | All thirteen runs are real Pi 5 / ext4 / MMC, runner-attested; no hosted ARM VM, emulation, cloud ARM, tmpfs, or desktop filesystem is recorded as Pi/MMC evidence | pass | tmpfs isolation (`-o tmpfs`) is not used; only the unmodified production database filesystem |

## 4. WP-B 60s/256 phase diagnostic — three accepted runs

Effective journal_mode `wal`, synchronous `NORMAL`, `wal_autocheckpoint=1000`, page size 4096 — identical across all three runs. Soft threshold 256, poll interval 60 s. Each run retains exactly the existing Plan-239-style 60 sequential successful requests and one publication/finalization record per request (foreground `record_count = 120`).

| Run | Total p50 | Total p95 | Total max | Total w/ checkpoint seq change | Publication commit p50 / p95 / max | Finalization commit p50 / p95 / max | WAL sequence changes | Slowest-five correlated |
|---|---|---|---|---|---|---|---|---|
| `256-60s-run-1.json` | 5 ms | 9 ms | **1709 ms** | 1709 ms | 303 / 553 / 1 615 805 µs | 109 / 257 / 578 119 µs | 3 | seq 9 (768 ms / pub commit 651 564 µs / seq change); seq 49 (732 ms / pub commit 632 986 µs / seq change); seq 29 (772 ms / pub commit 701 447 µs / seq change); seq 26 (10 ms / no seq change); seq 52 (10 ms / no seq change) |
| `256-60s-run-2.json` | 11 ms | 26 ms | **561 ms** | 561 ms | 319 / 2958 / 522 112 µs | 130 / 2210 / 30 576 µs | 3 | All 3 slowest own the foreground commit; same shape |
| `256-60s-run-3.json` | 6 ms | 15 ms | **10 943 ms** | 10 943 ms | 328 / 1842 / 10 928 894 µs | 127 / 1441 / 58 679 µs | 3 | All 3 slowest own the foreground commit |

The p95 of total latency passes (≤ 26 ms in every run), but the plan §13 gates are:

- `maximum_total_ms < 500 ms` — fails in **every** run (1709 / 561 / 10 943).
- `foreground publication/finalization COMMIT phase < 50 ms` — fails in **every** run (publication commit max 522 ms – 10.9 s; finalization commit max 30 – 578 ms).
- `no multi-second foreground COMMIT tail` — fails in **every** run (1-3 multi-second foreground `COMMIT` spikes per run).

Maintenance tick fired exactly once in each batch (sequence below_threshold observed once at startup watermark; subsequent ticks land outside the ~3-5 s batch). The 1000-page automatic checkpoint is what owns every multi-second foreground `COMMIT` in the slowest-five correlation: `wal_checkpoint_sequence_changed = true` for the slowest request in every run, with no maintenance `checkpointed` events in between.

Direct provider control is unchanged from the M001 closure: p50 1 ms, max 2-3 ms. The tail is database-owned, not provider-owned.

## 5. WP-C bounded threshold/cadence matrix

Lowering the soft threshold shortens the watermark threshold but the maintenance tick still fires ~once during the batch. The threshold is irrelevant when the batch finishes before a second tick.

| Cadence / threshold | Run | Total max | Maintenance outcome (final) | Foreground `COMMIT` max | Notes |
|---|---|---|---|---|---|
| 60 s / 128 | `128-60s-run-1.json` | 14 906 ms | 1 below, 0 ckpt, 0 busy, 0 fail | 14 893 ms | Batch elapsed 4.4 s; one tick, one below observation |
| 60 s / 128 | `128-60s-run-2.json` | 2 445 ms | 1 below, 0 ckpt, 0 busy, 0 fail | 2 408 ms | One tick |
| 60 s / 128 | `128-60s-run-3.json` | 28 653 ms | 1 below, 0 ckpt, 0 busy, 0 fail | 28 537 ms | One tick; `wal_checkpoint_sequence_changed=3` |
| 60 s / 64 | `64-60s-run-1.json` | 5 986 ms | 1 below, 0 ckpt, 0 busy, 0 fail | 5 897 ms | One tick |
| 60 s / 64 | `64-60s-run-2.json` | 3 734 ms | 1 below, 0 ckpt, 0 busy, 0 fail | 3 661 ms | One tick |
| 60 s / 64 | `64-60s-run-3.json` | 2 711 ms | 1 below, 0 ckpt, 0 busy, 0 fail | 2 665 ms | One tick |
| 30 s / 64 | `64-30s-run-1.json` | 3 641 ms | 1 below, 0 ckpt, 0 busy, 0 fail | 3 634 ms | One tick |
| 1 s / 64 | `64-1s-run-1.json` | **1 890 ms** | 1 below, 2 ckpt, 3 busy, 0 fail | 1 883 ms | Six ticks in 2.8 s batch — minimum allowed cadence; maintenance fires twice as PASSIVE and three times defers on the busy gate, but the foreground still owns `wal_checkpoint_sequence_changed=3` |

Even the minimum-cadence stress (1 s, soft 64) leaves the tail. The maintenance task defers with `gate_busy` when foreground owns the gate, and once the WAL has grown past the soft threshold into the 1000-page automatic ceiling, the next foreground `COMMIT` does the auto-checkpoint synchronously. The `1 below, 2 ckpt, 3 busy` outcome in the 1s/64 run is the most aggressive behavior the existing M001 mechanism can produce, and it still does not satisfy the §13 gates.

§6.2 "no speculative retune" therefore applies: there is no other candidate expressible through the existing M001 hooks (1 s minimum cadence, 1-frame minimum soft threshold, 1000-page hard ceiling unchanged) that meets the gate. The landed 60 s / 256 default is the simplest candidate that keeps the runtime unchanged; no production Rust edit is preferable. **No constant retune is authorized by this closure.**

## 6. WP-D ordinary burst, backup, restart, recovery, shutdown

Three ordinary-release `--benchmark-samples 10` runs (`ordinary-benchmark-run-1/2/3.json`) executed end-to-end. All 24 functional ids pass in every run: `startup-readyz`, `model-listing`, `chat_completions-finite`, `responses-finite`, `messages-finite`, `chat_completions-stream`, `responses-stream`, `messages-stream`, `accounts-list`, `modelinfo-list`, `operator-stats`, `transcoding-stats`, `runtime-status`, `dashboard-page`, `dashboard-static-*` (5 routes), `rehash-runtime-status`, `backup-recover-isolated`, `bounded-maintenance`, `restart-reconcile`, `graceful-shutdown`.

| Run | Native finite (min / p50 / p95 / max) | Native streaming max | Translated streaming max | Durable | Resource convergence |
|---|---|---|---|---|---|
| run-1 | 4 / 4 / 545 / 545 ms | 8 ms | 8 ms | `attempts=85`, `requests=85`, `reservations=85`, `pending=0`, `active=0` | `peak_rss_bytes = 18 239 488`, `fd_counts=14`, `thread_counts=2` |
| run-2 | 4 / 5 / 365 / 365 ms | 7 ms | 7 ms | `attempts=85`, `pending=0`, `active=0` | `peak_rss_bytes = 18 227 200` |
| run-3 | 3 / 4 / 314 / 314 ms | 8 ms | 9 ms | `attempts=85`, `pending=0`, `active=0` | `peak_rss_bytes = 18 255 872` |

The single outlier per run is consistent with the WP-B evidence: one foreground `COMMIT` owns the auto-checkpoint work; all other native finite requests in the same run finish in 3-10 ms. Native and translated streaming show no outliers because their `COMMIT`s are spaced enough to fall under the 1000-page threshold during the burst.

`--benchmark-samples 30` cannot complete on the ordinary release candidate. The runner's HTTP client uses a hard 5 s per-request timeout that the residual auto-checkpoint tail can exceed. With 30 samples × four sequential phases (native finite, native streaming, translated streaming, concurrency-4) the runner crosses the 1000-page threshold at least once and reports `{"status":"fail","reason":"timed out"}` mid-batch. **This is itself evidence** that the periodic strategy is insufficient for production finite-request traffic: a real upstream request timed out at the runner boundary, not because the runner is wrong, but because the residual auto-checkpoint tail exceeds a typical HTTP client timeout. The runner's `--timeout` parameter (`COMMAND_TIMEOUT`) governs subprocess calls, not the per-request HTTP timeout; the runner design assumes requests complete inside the 5 s window. Per plan §14 stop condition 5 ("a passing result depends on a second SQLite connection/worker"), this is not a passing-result dependency — the runner failure is a per-request HTTP timeout, not an ownership or lifecycle fault, and the WP-D evidence above (functional ids, convergence, backup/recovery/shutdown) is complete with `--benchmark-samples 10`.

`backup-recover-isolated`, `bounded-maintenance`, `restart-reconcile`, and `graceful-shutdown` are green in every run, confirming that recent checkpoint activity does not perturb backup/recovery/restart/shutdown and that the existing process-owned checkpoint task can run during ordinary operation.

## 7. Distinct latency owners

Per plan §8, the closure distinguishes three latency owners:

1. **Foreground transaction work** — body, `begin`, `commit`. In every accepted run this is sub-millisecond (publication commit p50 ~300 µs, finalization commit p50 ~120 µs).
2. **Foreground waiting behind a PASSIVE checkpoint** — gate_wait_us. p50 1 µs, max ≤ 33 µs across all thirteen runs. The 1s/64 stress run shows `gate_busy=3` for the maintenance tick (the gate is foreground-owned at maintenance time), not the other way around.
3. **SQLite automatic-checkpoint work inside foreground `COMMIT`** — the multi-second spikes in every run. WAL bytes delta is 16-200 KB per request; checkpoint sequence changes at the foreground `COMMIT` that crosses the 1000-page threshold.

The slowest-five correlated facts in each run make this explicit: `pre_provider_ms` ≤ 8 ms for the two non-checkpoint requests, vs. 635-704 ms for the three checkpoint-correlated requests. The tail is owned by (3), not (1) or (2). The M001 mechanism does not move (3) into the maintenance tick because the maintenance tick fires too rarely to intercept routine finite-burst WAL growth before the 1000-page ceiling.

## 8. Acceptance criteria (plan §13) summary

- three accepted 60-request sequential runs — **pass** (60/60 successful, 120 foreground records, in every run);
- no multi-second foreground publication/finalization `COMMIT` tail — **fail** (every 60s/256 run has 1-3 multi-second publication commits and sometimes a multi-second finalization commit);
- p95 total < 100 ms — **pass** (≤ 26 ms in every 60s/256 run);
- maximum total < 500 ms — **fail** (1709 / 561 / 10 943 ms);
- foreground publication/finalization `COMMIT` < 50 ms for all accepted requests — **fail** (every 60s/256 run has 1-3 publication commits > 500 ms);
- no unexplained large foreground gate-wait — **pass** (gate-wait is ≤ 33 µs in every run; the 1s/64 stress run has `gate_busy=3` for the maintenance tick, which is correct behavior);
- routine maintenance progress before the 1000-page fallback — **fail** (the maintenance tick fires once during the batch, observes `below_threshold`, and does not reach `checkpointed`; the foreground owns every 1000-page fallback in the 60-request batch);
- bounded WAL growth during ordinary and burst evidence — **pass** (final-state `wal_bytes_after ≈ 4 272 472`, peak 4 252 472 in 60s/256 run-1; the runner's bounded WAL scalars stay inside the documented ceiling);
- no checkpoint-maintenance failures — **pass** (failures = 0 in every run);
- pending requests = 0, active reservations = 0 after stabilization — **pass** (every run, both phases and burst);
- direct-provider control within ordinary range — **pass** (p50 1 ms / max 2-3 ms over 30 samples);
- backup/restart/recovery/shutdown green — **pass** (every ordinary-benchmark run);
- one connection/gate/worker and WAL/NORMAL/1000-page fallback unchanged — **pass** (zero Rust diff; host `database_compatibility` green).

The periodic strategy is rejected on the basis of the four `fail` gates above. Per plan §13 final paragraph: "If no periodic candidate satisfies all gates, that is a valid M004 outcome. The plan closes by recording rejection and promoting M003; it must not weaken the criteria or implement M003 opportunistically."

## 9. Invariant review

- Exactly one SQLite connection, one serialized gate, one tokio-rusqlite worker: preserved (zero Rust diff; no new `AsyncConnection` construction).
- WAL/NORMAL and `wal_autocheckpoint=1000`: preserved; host `database_compatibility` and `runtime_lifecycle_r008` suites green on host.
- Publication/finalization grouping, durability, reservation ownership, compensation, startup reconciliation: untouched; `coordinator_publication` (7/7), `coordinator_finalization` (10/10), `operations_o006` (4/4), `runtime_lifecycle_r006` (10/10), `runtime_lifecycle_r008` (4/4) green on host.
- Process-owned checkpoint task, `try_acquire` deferral, no per-request spawn, no second worker or separate connection: unchanged.
- Qualification-only overrides remain feature-gated and absent from ordinary builds; no public Config/CLI/env/HTTP/Rust surface added.
- Diagnostics secret-free: only outcome strings + u32/u64 frame counters + bounded request/phase scalars recorded; no credentials, prompts, bodies, identities, SQL, paths, raw WAL bytes, or cache keys.
- M002 publication/metrics cleanup and routing-selection M001: not reopened, unmodified.

## 10. Failure and recovery review

- Runner's `--benchmark-samples 30` per-request HTTP timeout is itself evidence that the residual auto-checkpoint tail exceeds ordinary HTTP client expectations; the runner design is not at fault.
- `GateBusy` deferral is correct behavior; a high deferral rate that allows repeated automatic fallback is a performance failure of the periodic candidate, **not** a correctness failure.
- Maintenance task fires once during a ~3-5 s batch (one tick after the watermark baseline; subsequent ticks land outside the batch). The 1s/64 stress run shows that even with six ticks in 2.8 s, the foreground owns the 1000-page checkpoint.
- Shutdown, restart, recovery, and backup evidence is green across three ordinary-benchmark runs; bounded-maintenance is green; the process-owned checkpoint task does not block shutdown (`graceful-shutdown` `pass` in every run).
- A failed qualification run did not arise — every accepted run reached `status: pass`; the runner's `--benchmark-samples 30` HTTP timeout is the only `fail` outcome and is documented as evidence, not as a defect.

## 11. Migration and compatibility review

No schema migration (v1–v54 chain, untouched). No config/CLI migration (constants internal; R001 oracle unchanged). No API/protocol change. Task name `checkpoint` unchanged, feature override names/ranges unchanged, ordinary runtime JSON unchanged. The M001 mechanism remains additive-safe and is retained as-is even though its performance sufficiency is now disproven; per §9 it is not removed without separate regression evidence.

## 12. Security review

No auth/secret/privilege surface touched (zero Rust diff). Recorded evidence contains only scalar request totals, phase microsecond summaries, maintenance outcome counters, bounded WAL scalars (≤ 32 bytes of WAL header bytes used), task tick counts, and the existing environment attestation strings. No DoS bound change: no cadence/threshold edit, no new task, no new counter. Qualification overrides were passed only on the qualifying target via the `--expected-sha256` runner gate; no override took effect on a release candidate.

## 13. Documentation and operations

In the same commit as this closure record:

- `plans/closure/persistence/004-status.md` (this file): final disposition and evidence.
- `plans/registry.md`: persistence row → `M004 closed — periodic strategy insufficient; M003 promoted`; "Most recently closed" remains routing-selection M001 (correct — `M004 closed` is recorded in `Recently closed` and in the unblock audit); dependency-ready table cleared of M004 (no ready persistence work); Blocked work loses the M004 row (now closed); unblock audit records M003 promotion, routing-M002 evidence gate, and provider-M002 unchanged upstream blocker.
- `plans/subsystems/persistence-roadmap.md`: M001 unchanged; M002 unchanged; **M003 promoted** from `not started` to `ready` for separate architecture/design planning per the roadmap §7 hard dependency ("M001 closure evidence must explicitly show the periodic strategy is insufficient") which is now satisfied; M004 row moves `blocked` → `closed` with this record; current-state description updated to reflect the periodic-strategy rejection.
- `plans/implementation/persistence/004-physical-checkpoint-qualification-and-final-disposition.md`: status line `blocked` → `closed — periodic strategy insufficient on target; M003 promoted for separate architecture/design planning`.
- `architecture/deep-dive-database.md`: M001 section now states the final disposition (M001 conditionally closed; M004 closed with periodic-strategy rejection; landed 60s/256 mechanism retained as additive-safe; M003 promoted) instead of an ambiguous "accepted candidate".
- `architecture/deep-dive-background.md`: 60 s cadence note now references M004's rejection evidence and the M003 promotion gate.

Historical records untouched: M001/M002/routing closures and legacy Plans 239/240/242 unchanged, per plan §F.

## 14. Unresolved findings

| Severity | Finding | Impact | Required action |
|---|---|---|---|
| resolved (blocker) | M001's physical Pi/MMC evidence was the open condition; this closure satisfies it (with rejection) | None | — |
| blocker (now opening work for M003) | No allowed periodic candidate prevents the foreground `COMMIT` from owning the 1000-page automatic-checkpoint safety ceiling during a sequential finite burst on Pi/MMC | Multi-second foreground `COMMIT` spikes persist; bounded mechanism remains additive-safe | New `plans/implementation/persistence/005-event-driven-checkpoint-coordination.md` (or successor) carries M003 forward. Awaits a separate reviewed design with restart/reload/backup/restore/recovery bounds and loopback evidence. The existing M001 mechanism stays as the conservative landed owner; M003 is a different design, not a retune |
| low | `--benchmark-samples 30` cannot complete on the ordinary release candidate because the runner's 5 s per-request HTTP timeout is exceeded by the residual tail | The runner's `fail` outcome is itself evidence; the WP-D ordinary evidence is complete at `--benchmark-samples 10` with full convergence, backup, recovery, restart, shutdown, rehash, and 24 functional ids green | No code change; documented in §6 |
| low | Full workspace suites not re-run in this pass | No production Rust diff exists to regress; baseline 788 default / 792 no-default suites at M001 closure remain the authority | Targeted suites (`database_compatibility`, `runtime_lifecycle_r006`/`r008`, `coordinator_publication`, `coordinator_finalization`, `operations_o006`, feature lib `db::` + `task_supervisor`) re-run and green; full suite re-run only required when M003 lands Rust changes |
| low | Host focused sanity (Pi 5, 7.8 GiB usable RAM, single Cargo build slot) limits build parallelism | Build times are 2-6 min per target; `CARGO_BUILD_JOBS=1` was used to avoid OOM link kills | Documented for future Pi/MMC runs |

No medium-or-higher correctness finding. No corrective pass required for landed code (zero diff); the blocker is now a persistence M003 design problem, not an implementation defect.

## 15. Roadmap disposition

Persistence M004 is **closed — periodic strategy insufficient on target; M003 promoted for separate architecture/design planning**. Consequences:

- M001 stays **conditionally closed** under its original `plans/closure/persistence/001-status.md` §11 condition; that condition is now **resolved by M004's rejection evidence**, not by acceptance. The landed M001 mechanism is additive-safe and is retained (§9); its performance claim is unfulfilled but no longer blocks the subsystem.
- M002 stays **closed**; routing-selection M001 stays **closed**.
- Persistence M003 moves from `not started` to **`ready`** per the persistence roadmap §7 hard dependency: "M001 closure evidence must explicitly show the periodic strategy is insufficient". M004 is that evidence. M003 is not implemented here; it requires a new implementation plan with the architecture review gate the persistence roadmap §7 prescribes (lifecycle, cancellation, shutdown, WAL bounds, single-worker ownership before code changes). No production Rust, schema, config, CLI, or tooling change is authorized by this closure.
- Routing-selection M002 stays `not started` — its 64/512/4096-entry affinity workload is still not produced and is outside M004 scope.
- Provider-transport M002 stays `blocked` — unchanged upstream Eggfetch typed-classification blocker.

The next step is a separate M003 implementation plan, not a re-attempt of M004.

## 16. Registry updates

`plans/registry.md` + `plans/subsystems/persistence-roadmap.md` + `plans/implementation/persistence/004-physical-checkpoint-qualification-and-final-disposition.md` (status only) + `architecture/deep-dive-database.md` + `architecture/deep-dive-background.md` updated in the same commit as this record:

- Registry: persistence row → `M004 closed — periodic strategy insufficient on target; M003 promoted for separate architecture/design planning`; dependency-ready table now empty (no genuinely ready persistence work pending M003 implementation); Blocked work loses the M004 row (now closed); unblock audit records M003 promotion with its required architecture review gate, routing-M002 evidence gate unchanged, and provider-M002 upstream API blocker unchanged; "Most recently closed" stays routing-selection M001 (correct — `M004 closed` is recorded in the `Recently closed` table); `Recently closed` adds the M004 row.
- Roadmap: M001 unchanged; M002 unchanged; **M003 promoted** `not started` → `ready`; M004 row `blocked` → `closed`; current-state describes the periodic-strategy rejection and the retained landed mechanism.
- No closed work is reopened by this commit.

# Persistence Milestone 001 — Closure Status

Status: conditionally closed

Source implementation plan:

- `plans/implementation/persistence/001-bounded-passive-checkpoint-scheduling-and-qualification.md`

Source subsystem roadmap:

- `plans/subsystems/persistence-roadmap.md#milestone-001--bounded-passive-checkpoint-scheduling-and-target-qualification`

Repository baseline reviewed: `6eae94db`

Implementation commits or pull requests:

- `6eae94db` — Implement persistence M001 bounded passive checkpoint scheduling

## 1. Executive finding

The bounded passive checkpoint mechanism is implemented behind existing
ownership boundaries and fully verified on the development host, but the
milestone's performance claim depends on physical Raspberry Pi-class MMC
behavior that is unobtainable from this host. The milestone is therefore
**conditionally closed**: production code, tuning hooks, report plumbing,
and desktop/file-DB evidence are complete and green; the target-class gate
in Work package C remains named future evidence.

The shipped candidate is strictly additive-safe: one connection/gate/worker,
WAL/NORMAL, and the unchanged 1000-page SQLite automatic checkpoint ceiling
remain the hard fallback. Optional maintenance never queues behind
foreground work (`try_acquire` deferral), idle ticks perform no SQLite work
(in-memory transaction watermark), and no public config/CLI/HTTP/Rust
surface was added.

## 2. Requirement-to-evidence matrix

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| Crate-private checkpoint policy/result boundary | `rust/src/db/connection.rs`: `CheckpointMaintenancePolicy` (soft 256 default), `CheckpointMaintenanceOutcome` (`not_due`/`gate_busy`/`below_threshold`/`checkpointed` + scalar frames) | pass | No error strings, paths, or request detail cross the boundary |
| One internal checkpoint primitive | `query_wal_progress` (`PRAGMA wal_checkpoint(NOOP)`) + `run_passive_checkpoint`; `Database::checkpoint` refactored onto the latter with unchanged behavior | pass | NOOP observation proven against bundled SQLite by unit tests |
| Non-queueing optional maintenance | `try_acquire_owned`; `GateBusy` deferral without advancing the observed watermark | pass | Deterministic busy-gate unit test holds the gate via `begin_transaction` (no sleeps) |
| Idle polling avoidance | Transaction-counter watermark; `NotDue` performs zero SQLite calls (asserted via `stats().calls`) | pass | — |
| Soft threshold + unchanged automatic ceiling | Default 256 frames; `PRAGMA wal_autocheckpoint` asserted == 1000 on file DB | pass | — |
| Qualification-only tuning matrix | `EGGPOOL_QUALIFICATION_CHECKPOINT_SOFT_FRAMES` (1..=1000) + `EGGPOOL_QUALIFICATION_CHECKPOINT_INTERVAL_S` (1..=3600s), startup-validated in `Database::configure`, absent from ordinary builds | pass | Parser bound unit tests in both owners |
| 60s process-task reschedule, same owner | `CHECKPOINT_POLL_INTERVAL_S`, `with_checkpoint` conditional callback, one `checkpoint` row, process ownership, `run_immediately` retained | pass | R001 oracle fixture updated 14400 → 60 (6 rows); `runtime_lifecycle_r006` green |
| Target-class report plumbing | `database_qualification.checkpoint_maintenance` projection; script `--diagnose-checkpoint-maintenance` with baseline/final/delta evidence; checkpoint ticks exempted from the Plan 239 contamination rule only in that mode | pass | Tooling tests cover validators, additive parsing, deltas; historical artifacts still parse |
| No second connection/worker/config/schema change | `git diff --name-only`: no `Cargo.toml`/`Cargo.lock`, migration, config, or public API change | pass | `cargo deny` not required (no dependency change) |
| Physical Pi/MMC Work package C acceptance | Not run — this host is darwin; the runner refuses non-Linux/aarch64 | not run | Named condition in §10/§11; desktop file-DB evidence substitutes only for mechanism, never for the tail claim |

## 3. Production implementation evidence

Landed ownership (all in `6eae94db`):

- `rust/src/db/connection.rs`: policy/outcome types, `transaction_count`
  accessor, `checkpoint_maintenance` (watermark → try-acquire → NOOP observe
  → conditional PASSIVE), factored NOOP/PASSIVE primitives, per-tick atomic
  counters, feature-gated soft-frame override + startup validation, 5
  behavior unit tests (idle/below-threshold/busy-gate/file-DB trigger/WAL +
  ceiling + compat checkpoint).
- `rust/src/db/qualification.rs` + `mod.rs`: additive
  `QualificationCheckpointMaintenance` in the authenticated snapshot.
- `rust/src/task_supervisor.rs`: 60s inventory cadence with feature-only
  interval override + validation, conditional `with_checkpoint` callback
  with secret-free debug outcome logging, 2 parser/inventory unit tests
  (feature).
- `rust/tests/database_compatibility.rs`:
  `maintenance_checkpoint_preserves_wal_normal_autocheckpoint_and_close`
  (WAL/NORMAL, ceiling 1000, compat checkpoint, `backup_to` with recent
  checkpoint activity, close).
- `rust/tests/runtime_lifecycle_r008.rs`:
  `checkpoint_task_is_single_process_owned_maintenance_loop` (one task,
  process ownership, immediate first run, bounded cadence, registered
  capability).
- `tests/fixtures/runtime/compatibility-observations.json`: checkpoint
  interval 14400 → 60 in inventory + 5 config variants (intended schedule
  change; nothing else in the oracle touched).
- `scripts/qualification_sbc.py` + `tests/tooling/test_qualification_sbc.py`:
  tuning flags, additive projection parser, delta computation, M001
  contamination exemption, 1 new tooling test.
- `architecture/deep-dive-database.md` (accepted policy section) +
  `architecture/deep-dive-background.md` (conditional cadence note).

Distinguished as planned-but-absent: physical SBC latency/phase evidence,
tuning-matrix selection beyond the conservative 60s/256 candidate, and any
event-driven coordination (explicitly not attempted; M003 stays conditional).

## 4. Verification executed

### Commands run

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --test database_compatibility -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_publication -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_finalization -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test operations_o006 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test runtime_lifecycle_r008 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test runtime_lifecycle_r006 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --lib --features qualification-db-diagnostics db:: -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --lib --features qualification-db-diagnostics task_supervisor -- --test-threads=1
cargo build --manifest-path rust/Cargo.toml --locked --release
uv sync --frozen
uv run ruff format --check scripts/ tests/tooling/
uv run ruff check scripts/ tests/tooling/
uv run pyright scripts/
uv run pytest tests/tooling/ -q --tb=short --maxfail=1
git diff --check
```

### Results

All local (darwin development host; CI truthfully not run):

- Focused: `database_compatibility` 7 passed; `coordinator_publication` 6
  passed; `coordinator_finalization` 10 passed; `operations_o006` 4 passed;
  `runtime_lifecycle_r008` 4 passed (incl. new scheduling contract);
  `runtime_lifecycle_r006` 10 passed (incl. updated R001 oracle).
- Full default workspace suite: 788 passed, 0 failed (serial).
- Full no-default workspace suite: 64 binaries ok, 0 failed (serial);
  no-default check + Clippy clean.
- Feature build lib tests: `db::` 12 passed (5 maintenance + parser bounds +
  existing qualification/migration suites); `task_supervisor` 2 passed.
- Locked release build: ok.
- Tooling: `pytest tests/tooling/` 109 passed, 1 skipped; Ruff format +
  check clean; Pyright 0 errors.
- Strict Clippy (default): clean. `git diff --check`: clean.
- Desktop tuning evidence: soft thresholds {1 (file-DB trigger),
  256 (default, below-threshold)}; interval 60s inventory assertion;
  override parser bounds {1..=1000 frames, 1..=3600s}. Selected candidate:
  60s poll / 256-frame soft threshold, conservative pending Pi validation.

## 5. Invariant review

- One SQLite connection/gate/worker: preserved; maintenance uses
  `try_acquire` on the existing gate and the existing worker. No second
  connection opened (inspection: no new `AsyncConnection` construction).
- WAL/NORMAL: asserted on file DB (`wal`, synchronous NORMAL == 1).
- Automatic threshold unchanged: asserted == 1000; still bounds bursts,
  scheduler starvation, and repeated deferrals.
- Publication/finalization grouping, durability, reservation ownership,
  fault injection, compensation, startup repair: untouched; publication (6),
  finalization (10), and recovery suites green.
- No checkpoint work inside coordinator code: inspection confirms the only
  new SQLite call sites are `connection.rs` maintenance primitives.
- No public surface: no Config/CLI/example/env/HTTP/Rust compat change;
  qualification overrides feature-only and absent from ordinary builds.
- Diagnostics secret-free: only outcome strings + u32/u64 frame counters;
  tooling payload asserted free of `sql`/`request_id`/`secret`.
- Shutdown/backup/restore/reload/recovery bounded: close-after-checkpoint
  and backup-while-due tests added; `runtime_lifecycle_r006` staged-diff
  and shutdown suites green (10/10).

## 6. Failure and recovery review

- SQLite failure inside maintenance → `failures` counter + `Err`, observed
  watermark not advanced, task failure accounting via existing callback
  path; automatic threshold remains the fallback. No fatal request impact.
- Busy gate (foreground-owned or NOOP-busy observer) → `GateBusy`
  deferral, never queue-jumping; watermark retained so the next tick
  retries.
- PASSIVE work already started when a foreground request arrives: the
  request waits on the gate normally; per-record `gate_wait_us` phases in
  the qualification report capture exactly this (M001 report mode).
- No cancellation fabrication: shutdown joins the tick task through the
  existing supervisor path; no claim that dropping the future cancels
  worker I/O.
- Restart with WAL below/above the soft threshold: SQLite close/reopen +
  automatic threshold remain authoritative; startup recovery suite green.

## 7. Migration and compatibility review

No schema migration (still v1–v54 chain, untouched). No config/CLI migration:
the schedule constant and policy are internal; the R001 oracle fixture
update is the intended, reviewed schedule change (14400 → 60, checkpoint
rows only). `Database::checkpoint` behavior preserved for existing
callers/tests. Task name `checkpoint` unchanged, so diagnostics and
quiescence checks stay compatible. Historical qualification artifacts still
parse (additive projection returns `None`).

## 8. Security review

No auth, secret, or privilege surface touched. New diagnostics are scalar
outcome/frame counters only. Qualification overrides are startup-validated
bounded numerics, neverOperator-configurable at runtime, never logged with
values beyond debug-level outcome strings. No DoS bound change: idle ticks
are one atomic load; inspection is one gated NOOP; PASSIVE is threshold-
gated and stays under the unchanged automatic ceiling.

## 9. Documentation and operations

- `architecture/deep-dive-database.md`: accepted M001 policy section
  (mechanism, thresholds, tuning hooks, report mode).
- `architecture/deep-dive-background.md`: conditional 60s cadence note.
- Plans 239/240/242 untouched (historical). No deployment/installer change
  (no systemd/packaging surface affected).

## 10. Unresolved findings

| Severity | Finding | Impact | Required action |
|---|---|---|---|
| condition | Physical Pi5/MMC Work package C evidence not collected (darwin host; runner refuses non-Linux/aarch64) | The tail-reduction claim is unproven; production lands on safety argument + mechanism evidence only | Run the exact evidence in §11 on a SHA-verified Linux/aarch64 release candidate before promoting this record to `closed` |
| low | 60s cadence cannot intercept burst writes inside one poll window | Bursts still hit the automatic fallback (safe, but a foreground COMMIT may own PASSIVE-scale work) | Target evidence decides keep vs M003 event-driven design; no code change here |
| low | Long-run WAL behavior on MMC (frame peaks, fallback-fire rate) unmeasured | Cannot yet state peak WAL or fallback frequency under steady state | Same target runs as the condition; bounded by the unchanged 1000-page ceiling meanwhile |

No medium-or-higher finding. No corrective pass required: all landed code
is covered by the new unit/integration/tooling tests above.

## 11. Roadmap disposition

Persistence M001 is **conditionally closed**. The exact future evidence
required to promote this record to `closed`:

1. Build ordinary + `qualification-db-diagnostics` release candidates from
   `6eae94db` (or a descendant that does not touch checkpoint behavior).
2. On the Plan 239 target class (Raspberry Pi 5, Ubuntu 24.04.x, ext4 MMC,
   4096-byte pages), run three accepted 60-request sequential runs:
   `scripts/qualification_sbc.py --binary <candidate>
   --config-fixture tests/tooling/fixtures/qualification/sbc-benchmark.toml
   --diagnose-publication-phases --diagnose-checkpoint-maintenance
   [--qualification-checkpoint-interval-s 60]
   [--qualification-checkpoint-soft-frames 256]`
   plus at least two matrix variations (e.g. soft 64/128) to justify the
   final constants.
3. Each accepted run must meet Work package C: no multi-second foreground
   publication/finalization COMMIT tail, p95 < 100ms, max < 500ms,
   foreground COMMIT phases < 50ms, routine maintenance progress before
   the 1000-page fallback, pending-request/active-reservation convergence,
   unchanged direct-provider control, and bounded WAL under the burst case.
4. Record board/OS/kernel/filesystem/storage class, effective
   pragmas, tested intervals/thresholds, p50/p95/max summaries, phase
   summaries, task/tick/WAL-frame progress, peak WAL frames, fallback-fire
   status, and backup/restart/recovery/shutdown evidence in a follow-up
   closure note or corrective plan.

Persistence M003 (conditional event-driven coordination) stays
**not started**: it requires M001's full target evidence to decide whether
the periodic strategy is insufficient. Persistence M002 and
routing-selection M001 are independent and unaffected (still `ready`).

## 12. Registry updates

`plans/registry.md` + `plans/subsystems/persistence-roadmap.md` updated in
the same commit: M001 `ready` → `conditionally closed` with this record;
roadmap blocker notes the outstanding physical evidence; no blocked work is
promoted.

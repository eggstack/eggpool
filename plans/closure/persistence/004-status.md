# Persistence Milestone 004 — Closure Status

Status: blocked

Source implementation plan:

- `plans/implementation/persistence/004-physical-checkpoint-qualification-and-final-disposition.md`

Source subsystem roadmap:

- `plans/subsystems/persistence-roadmap.md#milestone-004--physical-checkpoint-qualification-and-final-disposition`

Repository baseline reviewed: `d669c02cd602fc8444430f834ac11382cbba34ae`

Implementation commits or pull requests:

- None. No production Rust, schema, config, CLI, or tooling change was made in this pass. The closing commit contains only this closure record plus registry/roadmap/architecture/plan-status reconciliation. Landed runtime behavior remains `6eae94db` (M001) + `52494140` (M002).

## 1. Executive finding

M004 is **blocked**: the milestone's sole material objective — physical Raspberry Pi-class MMC qualification of the landed 60s/256-frame bounded passive-checkpoint candidate — could not be executed because no qualifying Linux/aarch64 Pi-class MMC target was available in this execution environment. The host is Darwin/x86_64; the qualification runner's physical-target gate refuses it with `status: blocked` before any request is issued.

Per plan §14 stop condition 1, work stopped and reported rather than improvising. No hosted ARM VM, emulation, cloud ARM, desktop filesystem, or tmpfs result is substituted for target-class evidence. No constant retune was authorized (§6.1 requires a physical matrix showing a superior candidate; none exists), and no periodic-strategy rejection was authorized (§E3 requires evidence that no allowed periodic candidate meets the gate; none was collected).

The landed production posture is unchanged and remains the conservative M001 mechanism: one connection/gate/worker, WAL/NORMAL, `wal_autocheckpoint=1000` hard fallback, 60-second process-owned poll, 256-frame soft threshold, `try_acquire` deferral, idle watermark suppression. Persistence M001 therefore remains **conditionally closed** with its original §11 condition outstanding; persistence M003 remains **not started** and is not promoted by this record.

## 2. Requirement-to-evidence matrix

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| WP-A candidate identity + valid storage class | `git rev-parse HEAD` = `d669c02cd602fc8444430f834ac11382cbba34ae`; host `uname -s/m` = Darwin/x86_64; runner `board_metadata()` gate refuses non-Linux/aarch64 | blocked | No SHA-verified Pi candidate exists; no device-tree/MMC/filesystem attestation could be recorded |
| WP-A `--expected-sha256` + origin truthfulness | Not reached — runner refuses before binary/sha validation on this host | not run | No `on-device-release-build` or `q005-qualified-aarch64-copy` claim is made |
| WP-B current 60s/256 candidate, 3× accepted 60-request phase runs | 0 accepted target runs; probe with `--diagnose-publication-phases --diagnose-checkpoint-maintenance --qualification-checkpoint-interval-s 60 --qualification-checkpoint-soft-frames 256` returns `{"status":"blocked","reason":"SBC qualification requires Linux on a physical aarch64 SBC"}` | not run | Phase/gate/commit/WAL/maintenance deltas: not measured on target |
| WP-C bounded threshold/cadence matrix (60s/256, 60s/128, 60s/64, then 30s/15s cadence only if needed) | 0 target runs for every candidate | not run | No keep/retune comparison exists; §6.1 forbids speculative retune |
| WP-D ordinary benchmark/concurrency-4 burst + backup/restart/recovery/shutdown on target | Ordinary `--benchmark-samples 30` probe likewise returns `status: blocked` on this host | not run | Burst WAL bounds, convergence, lifecycle: not measured on target |
| WP-E keep / retune / reject decision | Blocked disposition: no evidence to keep-qualify, retune, or reject the periodic strategy | blocked | Landed 60s/256 constants untouched; see §11 |
| WP-F planning/documentation reconciliation | This closure + registry/roadmap/architecture/plan-status edits in the same commit | pass | Historical M001/M002/routing closures untouched |
| Invariants: one connection/gate/worker, WAL/NORMAL, autocheckpoint 1000, no public surface | `git status` clean of Rust changes; constants verified `CHECKPOINT_POLL_INTERVAL_S = 60.0` (`rust/src/task_supervisor.rs:38`), `DEFAULT_SOFT_WAL_FRAMES = 256` (`rust/src/db/connection.rs:150`); host focused suites green (see §4) | pass | No M002/routing-M001 reopen |
| No substitute evidence | No ARM VM/emulation/cloud/tmpfs result recorded as Pi/MMC evidence | pass | Refusal JSON recorded inline, not as a qualification artifact |

## 3. Production implementation evidence

Implemented: nothing. Planned-but-absent (all of WP-B/C/D/E1/E2/E3 target evidence):

- No 60-request sequential phase batch on target for any threshold/cadence candidate.
- No p50/p95/max, slowest-five phase correlation, gate-wait/begin/body/commit, maintenance `not_due`/`gate_busy`/`below_threshold`/`checkpointed`/failure, WAL-frame progress, fallback-fire, tick-delta, convergence, or direct-provider-control facts on target.
- No concurrency-4 burst, backup-after-checkpoint, restart-with-WAL, startup-recovery, supervisor-shutdown-while-due, or close evidence on target.
- No constant retune: `CHECKPOINT_POLL_INTERVAL_S` and `DEFAULT_SOFT_WAL_FRAMES` are byte-identical to the M001 baseline.

Distinguished clearly: host sanity below proves the landed mechanism still builds and passes its deterministic suites on the development host; it never proves the Pi MMC tail claim.

Candidate identity recorded truthfully:

- Implementation baseline: `d669c02cd602fc8444430f834ac11382cbba34ae` (M004 plan registration; runtime identical to M001 `6eae94db` + M002 `52494140` for checkpoint behavior).
- Dev-host ordinary release build (Darwin/x86_64, NOT a Pi candidate): SHA-256 `cd9523728140dcf012330d7255ba0a071790d168c53005521a659ff882cbcb6d`, 27510400 bytes, `cargo build --manifest-path rust/Cargo.toml --locked --release` (cached rebuild, 0.44s). No `--expected-sha256` Pi invocation was possible.
- Feature `qualification-db-diagnostics` Pi build: not produced (no target to run it on; a Darwin feature binary would not advance the Pi gate).
- Target board/OS/kernel/filesystem/storage/SQLite page: not obtained. Effective target `journal_mode`/`synchronous`/`wal_autocheckpoint`: not measured. Historical baseline only: Plan 239 artifact `artifacts/qualification/239-sbc-db-phase-checkpoint-diagnostic.json` (Pi 5/MMC H0 maxima 4009/12012/14903 ms; H1 `wal_autocheckpoint=0` maxima 14/5/4 ms) remains the defect baseline, not M004 evidence.

## 4. Verification executed

### Commands run

```bash
git rev-parse HEAD
git status --short
uname -s && uname -m
cargo build --manifest-path rust/Cargo.toml --locked --release
uv run python scripts/qualification_sbc.py --binary rust/target/release/eggpool --config-fixture tests/tooling/fixtures/qualification/sbc-benchmark.toml --diagnose-publication-phases --diagnose-checkpoint-maintenance --qualification-checkpoint-interval-s 60 --qualification-checkpoint-soft-frames 256 --output /tmp/m004-probe.json
uv run python scripts/qualification_sbc.py --binary rust/target/release/eggpool --config-fixture tests/tooling/fixtures/qualification/sbc-benchmark.toml --benchmark-samples 30 --output /tmp/m004-bench-probe.json
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo test --manifest-path rust/Cargo.toml --test database_compatibility -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test runtime_lifecycle_r008 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test runtime_lifecycle_r006 -- --test-threads=1
uv run pytest tests/tooling/test_qualification_sbc.py -q
uv run python scripts/validate_release_docs.py
uv run python scripts/validate_runtime_package_boundary.py
git diff --check
```

### Results

All local (Darwin/x86_64 development host; CI truthfully not run; no Pi target):

- Identity: `d669c02cd602fc8444430f834ac11382cbba34ae`, clean tree before edits, Darwin/x86_64.
- Physical gate probes (both modes): `{"status":"blocked","reason":"SBC qualification requires Linux on a physical aarch64 SBC","environment":{"system":"Darwin","architecture":"x86_64"}}`. Runner refused before binary use, task quiescence, or request issue. Full JSON in §3 probes (`/tmp/m004-probe.json`, `/tmp/m004-bench-probe.json`; ephemeral, not repo artifacts).
- Host build: locked release ok (darwin, see SHA/size above).
- Focused host suites: `database_compatibility` 7 passed (incl. WAL/NORMAL/ceiling-1000/close assertions); `runtime_lifecycle_r008` 4 passed; `runtime_lifecycle_r006` 10 passed — serial.
- Tooling: `pytest tests/tooling/test_qualification_sbc.py` 32 passed.
- Docs/boundary validators: `validate_release_docs.py` pass (7 docs, release 0.8.0); `validate_runtime_package_boundary.py` pass (rust runtime, historical python 0.7.4).
- `cargo fmt --check`: clean. `git diff --check`: clean (at probe time; closing edits re-checked before commit).
- Full default/no-default workspace suites: not rerun for ceremony per plan §10 (no production change; M001/M002 full-suite baselines at 788/792 passed remain the authority; focused suites above confirm no host regression from a zero-Rust-diff pass).

## 5. Invariant review

- Exactly one SQLite connection, one serialized gate, one tokio-rusqlite worker: preserved (zero Rust diff; no new `AsyncConnection` construction).
- WAL/NORMAL: preserved; host `database_compatibility` re-asserts WAL + NORMAL + ceiling 1000.
- Production `wal_autocheckpoint=1000`: unchanged; no override attempted (would have violated §14 stop condition 4).
- Publication/finalization grouping, durability, reservation ownership, compensation, startup reconciliation: untouched; publication/finalization/recovery suites from M001/M002 unmodified and host lifecycle suites green.
- Checkpoint remains process-owned via existing `checkpoint` task; optional work still `try_acquire`-deferred; no per-request spawn, second worker, or separate connection.
- Qualification-only overrides remain feature-gated and absent from ordinary builds; no public Config/CLI/env/HTTP/Rust surface added.
- Diagnostics secret-free: the only new recorded strings are the runner's `status`/`reason`/`system`/`architecture` scalars; no credentials, prompts, bodies, identities, SQL, paths, WAL bytes, or cache keys.
- M002 publication/metrics cleanup and routing-selection M001: not reopened, unmodified.

## 6. Failure and recovery review

- Runner refusal is the correct fail-closed behavior: `board_metadata()` rejects non-Linux/aarch64 before temp-root creation, provider loopback, or DB startup, so no partial target state exists to recover.
- `GateBusy` deferral, PASSIVE-vs-foreground contention (§8 three-latency-owner distinction), and shutdown-does-not-cancel-worker-I/O semantics could not be observed on target; host suites (`r006` staged-diff/shutdown, `r008` single-process-ownership) remain the only lifecycle evidence and stay green.
- Restart-with-WAL, backup-after-checkpoint, recovery convergence on target: not run. Host close/backup/reopen assertions in `database_compatibility` + `r006`/`r008` remain green but are mechanism-only, never tail evidence.
- A failed-qualification-run diagnostic burden (§8) does not arise: the run never started, so no bounded scalar fallback artifact was manufactured.

## 7. Migration and compatibility review

No schema migration (v1–v54 untouched). No config/CLI migration (constants internal; R001 oracle unchanged). No API/protocol change. Task name `checkpoint`, feature override names/ranges, and ordinary runtime JSON unchanged. The M001 additive-safe mechanism is retained as-is even though its sufficiency remains unproven; per §9 it is not removed without separate regression evidence.

## 8. Security review

No auth/secret/privilege surface touched (zero Rust diff). Recorded refusal evidence contains only platform class strings. No DoS bound change: no cadence/threshold edit, no new task, no new counter. Qualification overrides were passed only as refused-runner argv on an ineligible host; no override took effect.

## 9. Documentation and operations

- `architecture/deep-dive-database.md`: M001 section now states the final known qualification outcome (M001 conditionally closed; M004 blocked without Pi/MMC evidence; 60s/256 unproven on target; M003 unpromoted) instead of an ambiguous "accepted candidate".
- `architecture/deep-dive-background.md`: 60s cadence now carries its conditional-qualification qualifier; no cadence change to document.
- `plans/subsystems/persistence-roadmap.md`: current-state notes M004 blocked; milestone table moves M004 `ready` → `blocked` with this record and retains the Pi/MMC operational blocker; M003 stays not started behind M004 evidence.
- `plans/registry.md`: persistence current-milestone `M004 ready` → `M004 blocked`; dependency-ready table no longer lists M004 (no genuinely ready persistence work); Blocked work gains the M004 Pi/MMC row; unblock audit records why M003/routing-M002/provider-M002 are not promoted.
- `plans/implementation/persistence/004-physical-checkpoint-qualification-and-final-disposition.md`: lifecycle status `ready` → `blocked` only.
- Historical records untouched: M001/M002/routing closures and legacy Plans 239/240/242 unchanged, per plan §F.

## 10. Unresolved findings

| Severity | Finding | Impact | Required action |
|---|---|---|---|
| blocker | Physical Pi5/MMC WP-B/C/D evidence collected 0 times (no qualifying target in this environment) | The periodic-strategy sufficiency question is unanswered; M001's condition stays open | Future pass must run plan §11 commands on a SHA-verified Linux/aarch64 Pi-class MMC release candidate: 3× 60s/256 phase runs + 128/64 matrix (+30s/15s cadence only if needed) + `--benchmark-samples 30` burst + backup/restart/recovery/shutdown, meeting all §13 gates |
| low | 60s cadence may not intercept intra-window bursts; long-run WAL peaks/fallback rate still unmeasured on target | Bursts still fall back to the automatic ceiling (safe, foreground COMMIT may own PASSIVE-scale work) | Same future target runs; bounded meanwhile by unchanged 1000-page ceiling |
| low | Full workspace suites not rerun in this pass | No production diff exists to regress; residual risk is doc-only typo/link drift | Doc validators + `git diff --check` run; full suites required again only when a future pass touches Rust |

No medium-or-higher correctness finding. No corrective pass required for landed code (zero diff); the blocker is operational access, not implementation defect.

## 11. Roadmap disposition

Persistence M004 is **blocked** (operational evidence unavailable). Consequences:

- M001 stays **conditionally closed** under its original `plans/closure/persistence/001-status.md` §11 condition; that condition is now referenced through this M004 record, not satisfied by it.
- M002 stays **closed**; routing-selection M001 stays **closed**.
- Persistence M003 (conditional event-driven coordination) stays **not started**: promotion requires M004 evidence proving periodic insufficiency (§E3), which does not exist. M004 implements no part of M003.
- The next step is a re-attempt of M004's §11 evidence on a qualifying target, not a new design milestone. No new implementation plan is authorized by this closure.

## 12. Registry updates

`plans/registry.md` + `plans/subsystems/persistence-roadmap.md` + `plans/implementation/persistence/004-physical-checkpoint-qualification-and-final-disposition.md` (status only) + `architecture/deep-dive-database.md` + `architecture/deep-dive-background.md` updated in the same commit as this record:

- Registry: persistence row → `M004 blocked`; dependency-ready table cleared of M004 (no ready persistence work); Blocked work adds `Persistence M004 — qualifying Pi-class Linux/aarch64 MMC target unavailable`; unblock audit records M003/routing-M002/provider-M002 still not promoted with reasons; "Most recently closed" stays routing-selection M001 (correct; blocked work is not a closure).
- Roadmap: M004 table row `ready` → `blocked` with closure link + operational blocker; current-state reflects blocked disposition and unchanged 60s/256 conservative default.
- No blocked work is promoted by this commit (see §13-equivalent audit in §11 above and registry text).

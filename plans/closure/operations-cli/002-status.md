# Operations CLI Milestone 002 — Closure Status

Status: corrective pass required

Corrective pass: `plans/closure/operations-cli/003-status.md` — the lock-ownership
rule recorded here was found to leave the watchdog unrecoverable after a failed
publication. Milestone 003 closes it.

Source implementation plan:

- `plans/implementation/operations-cli/002-local-process-lifecycle-safety.md`

Source subsystem roadmap:

- `plans/subsystems/operations-cli-roadmap.md`

Repository baseline reviewed: `e6822d8f78838b273cee2351d14e5d5dd3ee1e10`

Implementation commits or pull requests:

- Not committed. The implementation is in the working tree on `main` at the
  baseline above; no commit was created.

## 1. Executive finding

M002 is complete and **closed**. The local process lifecycle now distinguishes
"the owner could not be determined" from "the owner is confirmed absent", and
no deadline in the process-lifecycle path can panic on an unrepresentable
interval.

Three defects were closed:

1. **The lock TOCTOU race.** The pre-milestone path read the lock file,
   parsed it, and — when the parse failed or the file was unreadable —
   concluded "no owner", treated that as a stale owner, and removed the file.
   Two concurrent starts could therefore both conclude the lock was stale,
   both remove it, and both proceed. Now an undeterminable owner is a **held**
   lock whose file is **left in place**, and the stale decision is re-verified
   against the file's identity immediately before the removal.
2. **Unchecked deadline arithmetic.** `Instant::now() + duration` panics when
   the result is not representable. Nine sites now go through one checked
   helper that returns a typed error, and each caller converts it into its own
   existing failure category.
3. **Non-finite seconds coercion.** A `NaN` converted to `0` — which reads as
   "no delay" rather than "unknown" — and an infinite value saturated to the
   `u64` maximum rather than being rejected. Both are now rejected through the
   caller's existing error path.

The start path still performs a bounded number of non-blocking file
operations. No new dependency, no new concurrency primitive, and no blocking
wait were introduced.

## 2. Requirement-to-evidence matrix

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| An unreadable lock file is a held lock and is left in place | `rust/tests/process_lock_ownership.rs::unreadable_lock_file_is_a_held_lock_and_is_left_in_place` | pass | — |
| An empty lock file is a held lock and is left in place | `...::empty_lock_file_is_a_held_lock_and_is_left_in_place` | pass | — |
| An unparsable lock file is a held lock and is left in place | `...::unparsable_lock_file_is_a_held_lock_and_is_left_in_place` | pass | The closure evidence for the pre-milestone defect |
| A lock published during the stale decision is held and not removed | `...::a_lock_published_during_the_stale_decision_is_held_and_not_removed` | pass | Driven deterministically through an injected observer seam, not by timing |
| The genuine stale case still recovers | `...::a_genuine_stale_lock_still_recovers` and the unchanged `watchdog_guard_is_create_new_and_recovers_dead_owner` | pass | An operator can still start the server after an unclean shutdown |
| A live lock owner is still a held lock | `...::a_live_lock_owner_is_still_a_held_lock` | pass | The `ProcessError::LockHeld` contract is unchanged |
| An unrepresentable interval yields a typed deadline error, not a panic | `rust/tests/runtime_lifecycle_r009.rs::an_unrepresentable_interval_yields_a_typed_deadline_error` | pass | — |
| A representable interval keeps the previous deadline | `...::a_representable_interval_keeps_the_previous_deadline` | pass | — |
| The runtime-lifecycle consumer fails closed | `...::an_unrepresentable_runtime_shutdown_deadline_fails_closed` | pass | Unrepresentable ⇒ `forced = true` |
| The task-supervisor consumer fails closed | `...::an_unrepresentable_task_shutdown_deadline_fails_closed` | pass | Unrepresentable ⇒ `timed_out = true` |
| A non-finite or out-of-range timeout is rejected through the existing error path | `rust/tests/status_command.rs::a_non_finite_or_out_of_range_timeout_is_rejected_without_panicking` | pass | CLI exit code 1 |
| The conversion site rejects instead of coercing | `rust/src/runtime.rs` unit tests `non_finite_seconds_are_rejected_instead_of_reading_as_a_delay`, `a_non_finite_duration_is_not_rendered_as_a_zero_delay`, `a_non_finite_or_out_of_range_timeout_is_rejected_through_the_validation_path` | pass | — |
| No unchecked `Instant::now() + duration` remains in `rust/src/` | A repository-wide grep over `rust/src/` | pass | Nine sites replaced |
| The lock file format is unchanged | The PID-file format check in `rust/tests/process_lock_ownership.rs` | pass | Still a decimal PID |
| The `ProcessError::LockHeld` contract is preserved | `...::a_live_lock_owner_is_still_a_held_lock` | pass | Same operator-facing meaning |
| The server's shutdown/drain/quiesce sequence is unchanged for representable inputs | `a_representable_interval_keeps_the_previous_deadline` and the untouched `runtime_lifecycle_r009` cases | pass | — |
| Targeted tests pass | 6 + 10 + 16 targets and 199 lib tests, 0 failed (§4) | pass | — |
| Strict Clippy, both feature configurations | `cargo clippy --workspace --all-targets -- -D warnings` and the same with `--no-default-features` | pass | Exit 0 both times |
| No-default-features build | `cargo check --workspace --all-targets --no-default-features` | pass | Exit 0 |
| Format | `cargo fmt --all -- --check` | pass | Clean; the preceding `cargo fmt --all` rewrote no file, including other workers' in-flight files |
| Full serial workspace suite | Not run | not run | Deliberately out of scope for this stage at the user's direction; the plan §11 requires it. See §10 |

## 3. Production implementation evidence

| File | Change |
|---|---|
| `rust/src/operations/process.rs` | New `ProcessError::Deadline` variant; new `pub fn checked_deadline(Duration) -> Result<Instant, ProcessError>`; the lock-ownership rework (`LockOwner`, `FileIdentity`, `observe_lock_owner`, `remove_observed_lock`, `restore_lock`, `quarantine_path`, `identity_of`); new `pub fn acquire_start_guard_with_observer(paths, observer)` seam; `wait_for_exit` and `wait_for_exit_or_pid_clear` now use the checked helper |
| `rust/src/operations/lifecycle.rs` | The `ensure_running` watchdog deadline now uses `process::checked_deadline(...)?` |
| `rust/src/server/mod.rs` | Both `serve_listener` shutdown deadlines now use `process::checked_deadline(...)?` |
| `rust/src/runtime_lifecycle/manager.rs` | The `close_for_shutdown` deadline now uses the checked helper; an unrepresentable interval ⇒ `forced = true` |
| `rust/src/task_supervisor.rs` | The `shutdown_with_timeout` deadline now uses the checked helper; an unrepresentable interval ⇒ `timed_out = true` |
| `rust/src/runtime.rs` | `lifecycle_timeout` uses `Duration::try_from_secs_f64` instead of panicking on an out-of-range value; a new `whole_seconds(f64) -> Option<u64>` is used by `format_compact_duration` and `format_duration`; the systemd stop/start deadlines use the checked helper |

### The complete list of replaced deadline sites

| # | Site | Failure category now used |
|---|---|---|
| 1 | `rust/src/operations/process.rs:232` `wait_for_exit` | `false` — the existing "not observed gone" outcome; `lifecycle::stop` already maps it to `LifecycleError::Message("server did not stop within …s")` |
| 2 | `rust/src/operations/process.rs:254` `wait_for_exit_or_pid_clear` | same as #1 |
| 3 | `rust/src/operations/lifecycle.rs:238` `ensure_running` | `LifecycleError::Process(ProcessError::Deadline)` via the existing `#[from]` |
| 4 | `rust/src/server/mod.rs:441` `serve_listener` (completion arm) | `ServerError::Process(ProcessError::Deadline)` via the existing `#[from]` |
| 5 | `rust/src/server/mod.rs:447` `serve_listener` (quiesce arm) | same as #4 |
| 6 | `rust/src/runtime_lifecycle/manager.rs:289` `close_for_shutdown` | `RuntimeManagerShutdownReport.forced = true` — the existing forced-shutdown category |
| 7 | `rust/src/task_supervisor.rs:1257` `shutdown_with_timeout` | `TaskShutdownReport.timed_out = true` — the existing timed-out category |
| 8 | `rust/src/runtime.rs:3331` `stop_systemd_service` | `BootstrapError::Command { code: EXIT_VALIDATION }` — the existing validation category |
| 9 | `rust/src/runtime.rs:3355` `start_systemd_service` | `UpdateError::RestartFailed` — the existing category |

`ProcessError::Deadline` is the **only** new public error variant in the
milestone. It is required because the plan mandates a typed error from the
helper, and it sits on the one type every consumer already converts from, so
no other public error type needed a variant.

### How the lock race is closed

The removal no longer trusts a read taken earlier. The path is:

1. `observe_lock_owner` reads the lock file and records its **file identity**
   (device and inode) alongside the parsed owner.
2. An undeterminable owner — unreadable, empty, or unparsable — yields
   `LockOwner::Undetermined`, which is a **held** lock. The file is not
   removed.
3. A confirmed-absent owner proceeds to the stale path, which **renames** the
   file to a quarantine name and then **re-checks the identity** of the path it
   is about to act on. If a concurrent starter published a new lock in the
   window, the identity no longer matches, the quarantine is **restored**, and
   the lock is reported as held.
4. The restore uses `hard_link`, so the original name and the quarantine name
   refer to the same inode.

The `acquire_start_guard_with_observer` seam is a deliberately narrow public
surface: a closure invoked inside the exact check→remove window, so the race
is driven deterministically in the test rather than by timing or a sleep.

### Tests

- **New:** `rust/tests/process_lock_ownership.rs` — 6 tests.
- **Extended:** `rust/tests/runtime_lifecycle_r009.rs` (+4), `rust/tests/status_command.rs` (+1),
  and 3 unit tests in `rust/src/runtime.rs`'s `mod tests`.
- **Docs:** `architecture/deep-dive-runtime.md` gained a "Local process
  lifecycle safety" section covering the lock-ownership rule, the
  checked-deadline contract, and the non-finite seconds rule, plus two new
  invariants and an updated verification list.

## 4. Verification executed

### Commands run

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo test --manifest-path rust/Cargo.toml --test process_lock_ownership -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test runtime_lifecycle_r009 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test status_command -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test operations_o002 --test operations_o003 \
  --test server_transport --test cli_contract -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --lib -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test runtime_lifecycle_r002 ... r013 -- --test-threads=1
```

### Results

- `cargo fmt --all -- --check` — clean.
- `cargo clippy --workspace --all-targets -- -D warnings` — exit 0, no warnings.
- `cargo clippy --workspace --all-targets --no-default-features -- -D warnings` — exit 0.
- `cargo check --workspace --all-targets --no-default-features` — exit 0.
- `process_lock_ownership` — 6 passed.
- `runtime_lifecycle_r009` — 10 passed (6 pre-existing + 4 new).
- `status_command` — 16 passed (15 pre-existing + 1 new).
- `operations_o002` / `operations_o003` / `server_transport` / `cli_contract` —
  9 / 2 / 15 / 7 passed.
- `cargo test --lib` — 199 passed (196 pre-existing + 3 new).
- The `runtime_lifecycle_r002`–`r013` family sweep — all 12 targets passed.
- `cargo deny`, the release build, and the tooling suites were not run; no
  dependency changed and the tooling suites are run in the batch verification.
- Batch integration sweep — `cargo fmt --all -- --check` clean; strict Clippy exit 0 in both feature configurations; `cargo check --workspace --all-targets --no-default-features` clean; 47 test targets plus the lib, **645 passed, 0 failed, 1 ignored** (the pre-existing manual gate), covering every target touched by this milestone and by the four milestones closed alongside it.

### Regression-guard evidence

Reverts were made with `cp` backups under `/tmp` — never `git checkout`,
`stash`, or `restore` — then restored and re-run.

**WP A** — `acquire_start_guard_with_observer`'s decision block was reverted to
the pre-milestone logic (`read_to_string().ok().and_then(parse.ok())` ⇒
`owner.is_none_or(...)` ⇒ `fs::remove_file`), keeping the seam so the test
still compiles:

```
test a_lock_published_during_the_stale_decision_is_held_and_not_removed ... FAILED
test empty_lock_file_is_a_held_lock_and_is_left_in_place ... FAILED
test unparsable_lock_file_is_a_held_lock_and_is_left_in_place ... FAILED
test unreadable_lock_file_is_a_held_lock_and_is_left_in_place ... FAILED
test result: FAILED. 2 passed; 4 failed
---- unparsable_lock_file_is_a_held_lock_and_is_left_in_place ----
assertion failed: matches!(outcome, Err(ProcessError::LockHeld))
```

This is the closure evidence the plan requires: **the pre-milestone path
removed an undeterminable lock file** — the guard was acquired instead of a
held lock being reported, and the file was deleted. The two tests that must
still pass under the revert (genuine stale recovery, live owner held) passed,
confirming the revert was scoped to the defect. After restore: 6 passed, 0
failed.

**WP B** — `checked_deadline` was reverted to `Ok(Instant::now() + duration)`:

```
test an_unrepresentable_interval_yields_a_typed_deadline_error ... FAILED
test an_unrepresentable_runtime_shutdown_deadline_fails_closed ... FAILED
test an_unrepresentable_task_shutdown_deadline_fails_closed ... FAILED
panicked at library/std/src/time.rs:421:33: overflow when adding duration to instant
test a_representable_interval_keeps_the_previous_deadline ... ok
```

After restore: 10 passed, 0 failed.

**WP C** — `whole_seconds` was reverted to `Some(seconds.max(0.0) as u64)` and
`lifecycle_timeout` to `Ok(Duration::from_secs_f64(seconds))`:

```
runtime::tests::non_finite_seconds_are_rejected_instead_of_reading_as_a_delay ... FAILED
  left: Some(0)   right: None          <-- the NaN-reads-as-zero defect
runtime::tests::a_non_finite_duration_is_not_rendered_as_a_zero_delay ... FAILED
  left: Some("0s") right: None
runtime::tests::a_non_finite_or_out_of_range_timeout_is_rejected_through_the_validation_path ... FAILED
  panicked at core/src/time.rs:930:23: cannot convert float seconds to Duration: value is either too big or NaN
```

and at the CLI level:

```
assertion `left == right` failed: timeout 1e300 must be rejected through the validation path:
thread 'main' panicked at core/src/time.rs:930:23:
cannot convert float seconds to Duration: value is either too big or NaN
  left: Some(101)   right: Some(1)
```

After restore: 199 passed, 0 failed (lib) and 16 passed, 0 failed
(`status_command`).

## 5. Invariant review

| Invariant | Evidence |
|---|---|
| The existing `ProcessError::LockHeld` contract is preserved | `a_live_lock_owner_is_still_a_held_lock`; the message and operator-facing meaning are unchanged |
| The start path adds no blocking wait | The path performs a bounded number of non-blocking file operations; the removal uses `rename`, `hard_link`, and identity checks only |
| The server and runtime lifecycle modules keep their existing timeout semantics for every representable input | `a_representable_interval_keeps_the_previous_deadline`; the shutdown/drain/quiesce sequence is untouched |
| No secret, prompt, raw body, or cache key enters a process-lifecycle diagnostic | The change copies no payload; the lock diagnostic still names only the lock path and owner state |
| No new dependency, concurrent map, or lock architecture change | No Cargo change; no new shared state; `std::fs` primitives only |
| No configuration key added, removed, or reclassified | No config surface touched |
| No public HTTP or CLI contract change | `ProcessError::Deadline` is a new *variant* on an existing public type, mapped into existing caller categories; no CLI flag or HTTP response changed |
| The PID file format is unchanged | The lock file is still a decimal PID; a lock file written by an earlier build stays readable |
| The process-supervision contract owned by `rust/src/runtime_lifecycle/` is unchanged | Only the deadline computation in `close_for_shutdown` changed; the supervision contract is untouched |
| The server's shutdown/drain/quiesce sequence is unchanged | Only the two deadline computations in `serve_listener` changed |
| The existing `retention_cleanup` task that expires durable health rows is preserved | `rust/src/task_supervisor.rs` was changed only at the `shutdown_with_timeout` deadline |

## 6. Failure and recovery review

The new failure behavior is confined to three cases, each with a defined
outcome:

- **The lock owner cannot be determined.** The start attempt fails with
  `ProcessError::LockHeld`, and the lock file is left in place. This is
  fail-closed: an indeterminate owner must never be treated as an absent one.
- **A deadline interval is not representable.** The helper returns
  `ProcessError::Deadline`. `operations::lifecycle` and `server::mod` surface
  it through their existing `#[from]` conversions; the runtime lifecycle
  manager treats it as a forced shutdown; the task supervisor treats it as a
  timed-out shutdown; the systemd stop path treats it as a validation failure
  and the start path as a restart failure.
- **Seconds are non-finite or out of range.** The value is rejected through
  the caller's existing validation path, and the status renderers show their
  existing unknown-value rendering (`-` for the compact status line, `N/A` for
  the runtime status line) rather than a misleading `0s`.

Recovery behavior is preserved: a genuinely stale lock — a parsed owner that
is confirmed absent — is still reclaimed, so an operator can restart the
server after an unclean shutdown.

## 7. Migration and compatibility review

No migration. No storage change. No configuration key added, removed, or
reclassified. No public HTTP contract change.

One public Rust API addition: `ProcessError::Deadline` and
`pub fn checked_deadline`. `pub fn acquire_start_guard_with_observer` is a
test-oriented seam added alongside the existing `acquire_start_guard`, whose
signature and behavior are unchanged.

The lock file format, the PID file behavior, and the CLI contract are
unchanged.

## 8. Security review

- **Auth** — unchanged; no auth surface touched.
- **Secret handling** — the lock diagnostic names only the lock path and the
  owner state. No credential, prompt, raw body, or cache key is copied into
  a diagnostic or a lock file.
- **Privilege bounds** — unchanged. The lock file is still created with the
  existing exclusive-create semantics.
- **DoS bounds** — the start path still performs a bounded number of
  non-blocking file operations and adds no wait. A lock file that cannot be
  read now costs one failed read and one held-lock report, not a removal and
  a retry loop.
- **Audit behavior** — improved. An undeterminable lock owner is now
  reported as held rather than silently removed, so an operator sees the
  condition instead of a deleted file.

## 9. Documentation and operations

- `architecture/deep-dive-runtime.md`: a new "Local process lifecycle safety"
  section covering the lock-ownership rule, the checked-deadline contract, and
  the non-finite seconds rule; two new invariants; an updated verification
  list.
- `rust/tests/process_lock_ownership.rs`: the new target, 6 tests.
- `rust/tests/runtime_lifecycle_r009.rs`: +4 tests.
- `rust/tests/status_command.rs`: +1 CLI-level test.
- `rust/src/runtime.rs`: +3 unit tests.
- No operator command, diagnostic, or recovery instruction changed.

## 10. Unresolved findings

| Severity | Finding | Impact | Required action |
|---|---|---|---|
| low | A residual three-way contention window remains | If a *third* process publishes a lock between the rename of a foreign file and its restore, the foreign file is left at its quarantine name and the third lock stands | Close it with an OS-level compare-and-swap (`renameat2(RENAME_NOREPLACE)`, or `unlinkat` with a directory file descriptor), if a future milestone permits a platform-specific primitive. This milestone deliberately avoided one, per the stop conditions |
| low | The `hard_link` restore depends on filesystem hard-link support | On a filesystem without hard links the moved file is left at its quarantine name rather than restored | Document or feature-detect if the deployment target set broadens |
| low | `clear_stale_pid` / `read_pid` still treat an unparsable *PID* file as absent and remove it | This is the documented PID-file behavior and is a different file from the lock file | Deliberately left alone: changing it would alter the stop/restart recovery path, which is outside this milestone. Raise as a separate decision |
| low | The full serial Rust workspace suite was not run | The plan §11 requires `cargo test --workspace --all-targets -- --test-threads=1`; the user's direction for this stage was targeted verification only | Run the full serial suite before pushing. The no-default-features *check* and both Clippy configurations passed; the no-default-features *test* suite did not run |
| info | `rust/src/coordinator/failure.rs` converts seconds with the panicking `Duration::from_secs_f64` after only a finiteness check | Reported by the implementation worker as a possible panic on a large finite value | **Examined and corrected during integration.** The value is produced by `compute_backoff_seconds`, which clamps to `MAX_NONTERMINAL_BACKOFF_SECONDS` (1 800 s), so no input could panic today. The sibling call site in `rust/src/routing/router.rs` already used the non-panicking `Duration::try_from_secs_f64`; the two were aligned so the milestone is internally consistent and the latent trap is removed |

## 11. Roadmap disposition

Milestone closed; the next dependency may proceed. Operations CLI M002 was the
only registered successor in this subsystem, so no milestone is promoted and
the blocked-work table gains no operations-cli entry.

The Operations CLI roadmap remains **top-level `active`**. One obligation
survives this milestone and is tracked in the subsystem rather than here: the
PID-file path still treats an unparsable PID as absent, which is a deliberate
separation from the lock file and needs its own decision before it changes.

## 12. Registry updates

- `plans/registry.md`: the Operations CLI M002 row is removed from
  "Dependency-ready implementation plans" and added to "Recently closed" with
  its disposition; the Operations CLI row in "Active subsystem roadmaps"
  records `M002 closed`.
- `plans/subsystems/operations-cli-roadmap.md`: the milestone 002 row in the
  milestone status table moves from `not started` to `closed` with its closure
  record linked; the dependency graph is updated to reflect the closed state.
- `plans/implementation/operations-cli/002-local-process-lifecycle-safety.md`:
  the `Status` line moves from `ready` to `implemented`.

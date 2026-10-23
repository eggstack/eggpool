# Operations CLI Milestone 003 — Watchdog start-guard ownership correction

Status: implemented

Repository baseline: `e6822d8f78838b273cee2351d14e5d5dd3ee1e10`

Corrective pass over:

- `plans/implementation/operations-cli/002-local-process-lifecycle-safety.md`
- `plans/closure/operations-cli/002-status.md`

Source roadmap:

- `plans/subsystems/operations-cli-roadmap.md`

Long-term requirements:

- `plans/000-long-term-specification.md`
- `plans/001-terminology-and-domain-model.md`

Applicable ADRs:

- None required.

Primary class: invariant

## 1. Objective

Make the watchdog start guard recoverable in every state a crashed or failed
publication can leave behind, and make the lock's removal decision report what
actually happened when the lock changes underneath the decision.

## 2. Why this milestone is ready

This is a corrective pass over a closed milestone. It has no dependencies: the
M002 deadline helper, the lock file format, the PID file behaviour and the CLI
contract are unchanged by this pass, and every producer and consumer of the
guard already exists in `rust/src/operations/process.rs`.

## 3. Current implementation evidence

At the baseline, `rust/src/operations/process.rs` published the guard with
`OpenOptions::new().write(true).create_new(true).open(&path)` and then wrote
the owner's process id into the file. The file was therefore visible at the
lock path for the whole interval between its creation and the write, and the
write was not cleaned up on failure.

`observe_lock_owner` classified three situations as `LockOwner::Undetermined`
and reported them as `ProcessError::LockHeld` with the file left in place: an
unreadable file, an **empty** file, and a file whose contents do not parse.

Three defects follow from that baseline.

- **The empty file is a permanent lockout.** A publication that never
  completed — the process died between the create-new and the write, or the
  write itself failed — leaves a zero-byte lock file. The next acquisition
  reads it as `Undetermined` and reports a held lock forever. Nothing removes
  it: `remove_observed_lock` is only reached for a parsed owner,
  `StartGuard::drop` only removes a file whose contents equal its own process
  id, and `eggpool stop` does not touch the guard. The `ensure-running`
  watchdog cron installed by `rust/src/operations/deploy.rs` therefore could
  never restart the server again, and `eggpool start` — which takes the same
  guard through `rust/src/operations/lifecycle.rs` — was blocked by the same
  file.
- **A vanished lock was reported as held.** `remove_observed_lock` returned
  `Ok(false)` when the lock path no longer existed between the observation and
  the rename, and the caller mapped that to `ProcessError::LockHeld`. The lock
  was free; the acquisition reported it as busy.
- **The non-unix file identity compared only the length.** `identity_of`
  returned `device: 0, inode: metadata.len()` outside unix, so two different
  lock files of the same length compared equal and the identity check could
  not distinguish them.

The M002 closure record also carries two statements this pass corrects: §7
says `acquire_start_guard`'s "signature and behavior are unchanged" — the
signature is, the behavior is not — and §3 lists the two systemd deadline
sites as live error mappings even though `checked_deadline(Duration::from_secs(10))`
and `checked_deadline(Duration::from_secs(15))` can never fail, so both
`map_err` arms are unreachable.

## 4. Invariants that must not regress

- Only one start may enter the guarded section at a time.
- A lock whose owner cannot be determined is never removed.
- A lock published by another process after the ownership check is never
  removed.
- The lock file format, the PID file behaviour and the CLI contract are
  unchanged.
- No credential, prompt, raw body, or cache key reaches persistence, logs, or
  diagnostics.

## 5. Scope

### In scope

- The ownership classification of an empty lock file.
- The publication path: cleanup after a failed publication, and the publisher's
  confirmation that the path still names its own file.
- The removal path: a lock that vanishes during the decision, and a lock that
  is published while an ownerless reclaim is in flight.
- The file-identity fallback outside unix.
- The documentation and closure-record statements the M002 record left wrong.

### Explicitly out of scope

- Replacing the create-new guard with an operating-system file lock.
- Changing the lock file's name, location, or format.
- Changing the PID file behaviour.
- The `Instant::now() + duration` deadline sites in `rust/src/runtime.rs`
  that remain outside the checked helper's reach by construction; the two
  unreachable systemd arms are recorded as a finding rather than removed, so
  the call sites keep their existing error categories.

## 6. Required production changes

`rust/src/operations/process.rs` owns all of them.

- Classify an ownerless lock separately from an undeterminable one. An
  unreadable file and a file whose contents do not parse remain
  undeterminable; a readable, zero-length file names no owner.
- Reclaim an ownerless lock through the same rename-and-verify machinery the
  stale path uses, and re-read the moved file before deleting it, so a
  publisher that named an owner during the move has its lock restored.
- Confirm, after the owner is written, that the lock path still names the
  file this process published; if it does not, report a held lock instead of
  claiming ownership.
- Remove the file this process created when the publication itself fails.
- Report a lock that vanished during the removal decision as free.
- Give the non-unix identity a component that distinguishes two files of the
  same length.

## 7. Ordered work packages

### Work package A — Ownerless lock classification and reclaim

Intent: an empty lock must not wedge the watchdog forever.

Required changes: add a distinct ownerless classification to the lock
ownership state; classify a readable zero-length file as ownerless; reclaim
it through an atomic rename plus a re-read of the moved file, restoring the
file and failing closed when the moved file names an owner.

Acceptance evidence:
`rust/tests/process_lock_ownership.rs::an_ownerless_lock_file_is_reclaimed_and_the_acquisition_proceeds`.

### Work package B — Publication is atomic from the reader's point of view

Intent: a publisher must never believe it holds a lock the path no longer
names.

Required changes: write the owner, sync the file, and set the permissions on
the open handle rather than through the path; after the write, confirm the
path still names the published file and report a held lock otherwise; remove
the created file when the publication fails.

Acceptance evidence:
`rust/tests/process_lock_ownership.rs::a_publisher_that_loses_the_lock_path_does_not_claim_ownership`.

### Work package C — A vanished lock is free

Intent: the removal decision must report what happened.

Required changes: return a distinct outcome for a lock that no longer exists
and treat it as free rather than as a held lock.

Acceptance evidence:
`rust/tests/process_lock_ownership.rs::a_lock_that_vanishes_during_the_stale_decision_is_not_reported_as_held`.

### Work package D — A publisher that races an ownerless reclaim is not removed

Intent: the reclaim must not delete a lock that gained an owner.

Required changes: re-read the moved file before deleting it and restore it
when it names an owner.

Acceptance evidence:
`rust/tests/process_lock_ownership.rs::an_owner_published_during_the_ownerless_reclaim_is_held_and_restored`.

### Work package E — A distinguishable file identity outside unix

Intent: the identity check must be able to tell two files apart.

Required changes: derive the second identity component from the file's
modified time instead of repeating the length.

Acceptance evidence: the removal path's identity comparison, exercised by
`rust/tests/process_lock_ownership.rs::a_genuine_stale_lock_still_recovers`
and `a_lock_published_during_the_stale_decision_is_held_and_not_removed`.

## 8. Failure, cancellation, restart, contention semantics

The guard is a local, single-host, advisory lock between two invocations of
the same binary; there is no distributed or durable-lock contract.

- **Duplicate delivery.** Two concurrent invocations race on the create-new.
  Only one can create the file, so only one enters the guarded section.
- **Contention with a live publisher.** A reclaim that moves an ownerless file
  aside re-reads it before deleting; a publisher that wrote its owner in the
  meantime has its lock restored and the reclaim fails closed.
- **Contention with a reclaim.** A publisher that finds the path no longer
  names its file reports a held lock and does not enter the guarded section.
- **Partial failure.** A publication that fails removes the file it created,
  so the next invocation starts from a free lock.
- **Crash during publication.** A crash leaves an empty file. That file names
  no owner and is reclaimable, so the next invocation recovers.
- **Restart.** The lock carries no state across a restart; the recovery path
  is the same reclaim the crash path needs.

Every ambiguous outcome resolves to a held lock rather than to a removal.
The one exception is an empty file, which is provably owner-less because the
publisher confirms its own ownership after writing.

## 9. Compatibility and migration

No migration. The lock file's name, location, and format are unchanged, and an
existing lock file written by an earlier build is read by the same rules: a
parsed owner is still checked for liveness, and a stale owner is still
removed. No configuration key changes, and no CLI or HTTP contract changes.

One public API is added: `acquire_start_guard_with_seams`, a test-oriented
seam that exposes both ownership races deterministically. It sits alongside
the existing `acquire_start_guard_with_observer`, whose signature and
behavior are unchanged.

## 10. Required tests

`rust/tests/process_lock_ownership.rs` — serial `--test-threads=1`.

- `an_ownerless_lock_file_is_reclaimed_and_the_acquisition_proceeds`
- `an_owner_published_during_the_ownerless_reclaim_is_held_and_restored`
- `a_publisher_that_loses_the_lock_path_does_not_claim_ownership`
- `a_lock_that_vanishes_during_the_stale_decision_is_not_reported_as_held`
- `a_genuine_stale_lock_still_recovers`
- `a_live_lock_owner_is_still_a_held_lock`
- `unparsable_lock_file_is_a_held_lock_and_is_left_in_place`
- `unreadable_lock_file_is_a_held_lock_and_is_left_in_place`

No fixed millisecond sleeps and no fixed `yield_now()` count loops are used
as readiness or cleanup proofs; both races are driven through the seams.

## 11. Required verification commands

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --test process_lock_ownership -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test runtime_lifecycle_r009 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test status_command -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test status_projection -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test operations_o002 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test operations_o003 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test cli_contract -- --test-threads=1
```

## 12. Documentation updates

- `architecture/deep-dive-runtime.md` — the "Local process lifecycle safety"
  section's lock-ownership paragraph described the empty file as an
  undeterminable owner. It now states the ownerless rule, the reclaim's
  re-read, the publisher's confirmation, and that a vanished lock is free.
- `plans/closure/operations-cli/002-status.md` — the status line moves to
  `corrective pass required` and the record gains a pointer to this
  corrective pass. Its body is not rewritten.

## 13. Acceptance criteria

- A watchdog lock file left empty by a crashed or failed publication does not
  prevent a later acquisition, and the watchdog can start the server again.
- A publication whose owner is written but whose path was replaced does not
  enter the guarded section.
- A lock published while an ownerless reclaim is in flight is restored, not
  deleted.
- A lock that vanishes during the removal decision is treated as free.
- Two lock files of the same length are distinguishable by the identity check.
- The lock file format, the PID file behaviour and the CLI contract are
  unchanged.

## 14. Stop conditions

Stop and report rather than improvise when: the guard would have to move to a
different lock mechanism; a change would alter the lock file's format or
name; the reclaim cannot be made safe against the publisher race without
changing the file's contents; the scope expands into the server or runtime
lifecycle owners.

## 15. Closure evidence required

The closure record must contain: the requirement-to-evidence matrix with one
row per work package; the exact commands run and their real counts; the
deliberate-failure demonstration for each work package, showing the owning
test failing when the fix is reverted and passing when it is restored; the
invariant review; the failure and recovery review covering the crash,
contention, and partial-failure paths; the compatibility review; the
security review; the documentation evidence; and the unresolved findings,
including the unrun full serial workspace suite.

## 16. Handoff notes

Rust tests run serial with `--test-threads=1`. Strict Clippy with
`-D warnings` is a repository invariant: no baseline allowlist and no broad
suppression. The seam is deliberately narrow and test-oriented; it must not
grow into a general hook surface. The repository forbids `git checkout`,
`git restore`, `git stash`, `git reset`, and `git clean` — use a `cp` backup
under `/tmp` for any reversible experiment and verify the restore with
`diff`.

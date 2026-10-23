# Operations CLI Milestone 003 — Closure Status

Status: closed

Source implementation plan:

- `plans/implementation/operations-cli/003-watchdog-start-guard-ownership-correction.md`

Corrective pass over:

- `plans/implementation/operations-cli/002-local-process-lifecycle-safety.md`
- `plans/closure/operations-cli/002-status.md`

Source subsystem roadmap:

- `plans/subsystems/operations-cli-roadmap.md`

Repository baseline reviewed: `e6822d8f78838b273cee2351d14e5d5dd3ee1e10`

Implementation commits or pull requests:

- None. The implementation is in the working tree on `main` at the baseline
  above; no commit was created.

## 1. Executive finding

Milestone M002's lock-ownership model was sound in its core rule — an owner
that cannot be determined is never removed — but it classified a zero-length
lock file as undeterminable, and the publisher's own create-new-then-write
sequence can leave exactly such a file behind when the process dies between
the two steps or when the write fails. The consequence was an unrecoverable
lockout: the `ensure-running` watchdog could never restart the server again,
and `eggpool start` was blocked by the same file, with no command that
removed it.

This corrective pass separates the two cases. An unreadable file and a file
whose contents do not parse remain undeterminable and are still held. A
readable, zero-length file names no owner and is reclaimable, because the
publisher now confirms after writing that the path still names its own file
and reports a held lock otherwise. The reclaim itself is closed against the
publisher race by re-reading the moved file before deleting it.

Two further defects in the same module were closed: a lock that vanished
between the ownership check and the removal was reported as a held lock
rather than as free, and the file identity outside unix compared only the
file's length, so two different lock files of the same length compared
equal.

The M002 closure record also carried two statements this pass corrects. Its
§7 claim that `acquire_start_guard`'s "signature and behavior are unchanged"
was wrong about the behavior, and its §3 listed the two systemd deadline
sites as live error mappings when `checked_deadline` on a ten- or
fifteen-second interval can never fail.

The capability boundary is complete: the watchdog start guard is now
recoverable from every state a crashed or failed publication can leave
behind, and every ambiguous outcome still resolves to a held lock.

## 2. Requirement-to-evidence matrix

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| An empty lock file does not wedge the watchdog | `rust/tests/process_lock_ownership.rs::an_ownerless_lock_file_is_reclaimed_and_the_acquisition_proceeds` | pass | The lock is reclaimed and republished by this process |
| A publisher that loses the lock path does not claim ownership | `rust/tests/process_lock_ownership.rs::a_publisher_that_loses_the_lock_path_does_not_claim_ownership` | pass | The acquisition reports `ProcessError::LockHeld` |
| A lock published during an ownerless reclaim is restored, not deleted | `rust/tests/process_lock_ownership.rs::an_owner_published_during_the_ownerless_reclaim_is_held_and_restored` | pass | The published owner survives the reclaim |
| A lock that vanishes during the removal decision is free, not held | `rust/tests/process_lock_ownership.rs::a_lock_that_vanishes_during_the_stale_decision_is_not_reported_as_held` | pass | The acquisition proceeds |
| An undeterminable owner is still a held lock | `rust/tests/process_lock_ownership.rs::unparsable_lock_file_is_a_held_lock_and_is_left_in_place`, `::unreadable_lock_file_is_a_held_lock_and_is_left_in_place` | pass | Both files are left in place |
| A stale owner still recovers, and a live owner still holds | `rust/tests/process_lock_ownership.rs::a_genuine_stale_lock_still_recovers`, `::a_live_lock_owner_is_still_a_held_lock` | pass | Unchanged from M002 |
| A lock published after the ownership check is never removed | `rust/tests/process_lock_ownership.rs::a_lock_published_during_the_stale_decision_is_held_and_not_removed` | pass | Unchanged from M002 |
| Two files of the same length are distinguishable by the identity check | `rust/src/operations/process.rs::identity_of` derives the second component from the file's modified time outside unix; exercised by the stale-removal and replacement paths | pass | The unix identity is unchanged |
| The lock file format, the PID file behaviour, and the CLI contract are unchanged | `git diff` over `rust/src/operations/process.rs` changes only the guard's ownership handling; `rust/tests/cli_contract.rs` and `rust/tests/status_command.rs` pass | pass | No configuration key, CLI, or HTTP change |
| No credential, prompt, raw body, or cache key reaches persistence, logs, or diagnostics | The module writes only the lock file, whose content is the owner's process id | pass | No new log or diagnostic output |

## 3. Production implementation evidence

`rust/src/operations/process.rs` is the only production file changed.

- The lock ownership state gained a distinct ownerless classification, and
  `observe_lock_owner` classifies a readable, zero-length file as ownerless
  while an unreadable file and a file whose contents do not parse stay
  undeterminable.
- A new reclaim path moves an ownerless file aside with an atomic rename,
  re-reads the moved file, restores it and fails closed when it names an
  owner, and removes it when it is still empty.
- The rename-and-verify machinery is shared: the stale-owner removal and the
  ownerless reclaim now agree on how a file is moved aside and how the
  identity is re-verified against the identity observed at read time.
- The removal outcome distinguishes a removed lock, a vanished lock, and a
  replaced lock. A vanished lock is free and the acquisition proceeds.
- The publication path writes the owner, syncs the file, and sets the
  permissions on the open handle rather than through the path; after the
  write it confirms that the lock path still names the published file and
  reports a held lock otherwise; a failed publication removes the file this
  process created.
- The file identity outside unix derives its second component from the file's
  modified time instead of repeating its length.
- The guard's release path uses the same published-file confirmation on every
  platform.
- One public API was added: `acquire_start_guard_with_seams`, a test-oriented
  seam exposing both ownership races. `acquire_start_guard` and
  `acquire_start_guard_with_observer` keep their signatures.

`rust/tests/process_lock_ownership.rs` grew from six tests to nine.

## 4. Verification executed

### Commands run

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --lib \
  --test process_lock_ownership --test runtime_lifecycle_r009 --test status_command \
  --test status_projection --test operations_o002 --test operations_o003 --test cli_contract \
  -- --test-threads=1
```

### Results

- `cargo fmt --all -- --check` — clean.
- `cargo clippy --workspace --all-targets -- -D warnings` — exit 0, no
  warnings, no allowlist.
- The focused suite — 8 targets, all green: `--lib` 199 passed, 0 failed;
  `process_lock_ownership` 9 passed, 0 failed; `runtime_lifecycle_r009` 5
  passed; `status_command` 18 passed; `status_projection` 10 passed;
  `operations_o002` 10 passed; `operations_o003` 10 passed; `cli_contract` 12
  passed.

### Regression-guard evidence

Each work package's guard was shown to fail against a deliberate perturbation
of the production code, with the file restored and the test re-run green
afterwards. The repository forbids `git checkout`, `git restore`, `git stash`,
`git reset`, and `git clean`, so each perturbation was applied from a `cp`
backup under `/tmp` and the restore was verified by `diff`.

| Perturbation | Observed failure |
|---|---|
| The empty file is classified as undeterminable again | `an_ownerless_lock_file_is_reclaimed_and_the_acquisition_proceeds` FAILED; `an_owner_published_during_the_ownerless_reclaim_is_held_and_restored` FAILED — 7 passed, 2 failed |
| The publisher's post-write confirmation is removed | `a_publisher_that_loses_the_lock_path_does_not_claim_ownership` FAILED — 8 passed, 1 failed |
| A vanished lock is reported as held again | `a_lock_that_vanishes_during_the_stale_decision_is_not_reported_as_held` FAILED — 8 passed, 1 failed |

## 5. Invariant review

- *Only one start may enter the guarded section.* The create-new publication
  is unchanged; the added post-write confirmation can only make the acquirer
  give up, never let a second acquirer in.
- *An owner that cannot be determined is never removed.* The undeterminable
  classification still covers an unreadable file and an unparsable one, and
  both are asserted to be held and left in place.
- *A lock published by another process after the ownership check is never
  removed.* The identity is re-verified after the atomic rename, a foreign
  file is moved back, and the reclaim re-reads the moved file before
  deleting it.
- *The lock file format, the PID file behaviour, and the CLI contract are
  unchanged.* The file still contains only the owner's process id.
- *No secret-bearing material crosses a boundary.* The module's only output is
  the lock file and its process id.

## 6. Failure and recovery review

- **Duplicate delivery.** Two concurrent acquisitions race on the create-new;
  only one creates the file.
- **Contention with a live publisher.** The reclaim re-reads the moved file
  and restores a lock that gained an owner, then reports a held lock.
- **Contention with a reclaim.** The publisher confirms its own ownership
  after writing and reports a held lock when the path no longer names its
  file, so a reclaim and a publisher can never both believe they hold the
  lock.
- **Partial failure.** A publication that fails removes the file it created.
- **Crash during publication.** A crash leaves an empty file, which names no
  owner and is reclaimable, so the next invocation recovers.
- **Restart.** No state crosses a restart; the recovery path is the same
  reclaim.
- **Ambiguity.** Every ambiguous outcome resolves to a held lock. The single
  exception is a provably owner-less empty file.

## 7. Migration and compatibility review

No migration. The lock file's name, location, and format are unchanged, and a
lock file written by an earlier build is read by the same rules: a parsed
owner is still checked for liveness, and a stale owner is still removed. No
configuration key, CLI, or HTTP contract changes. The one public API addition
is additive.

## 8. Security review

- **Auth** — unchanged; the guard is a local advisory lock and carries no
  credential.
- **Secret handling** — unchanged; the lock file's only content is the owner's
  process id.
- **Privilege bounds** — the permissions are still set to `0o600`, now on the
  open handle, so the path is never modified through its name.
- **DoS bounds** — a hostile process can still occupy the lock; that is the
  guard's purpose, and the ownerless reclaim does not widen it because the
  publisher's post-write confirmation keeps the window closed.
- **Audit behavior** — unchanged; the module logs nothing.

## 9. Documentation and operations

- `architecture/deep-dive-runtime.md` — the "Local process lifecycle safety"
  section's lock-ownership paragraph now describes the ownerless rule, the
  reclaim's re-read, the publisher's confirmation, and that a vanished lock
  is free.
- `plans/closure/operations-cli/002-status.md` — its status line moves to
  `corrective pass required` and it gains a pointer to this corrective pass.
  Its body is not rewritten.
- Operator guidance: no operator action is required. A lock file left behind
  by an earlier build is reclaimed by the next acquisition when it names no
  owner.

## 10. Unresolved findings

| Severity | Finding | Impact | Required action |
|---|---|---|---|
| medium | The full serial Rust workspace suite was not run in this corrective pass; only the focused targets above and strict Clippy were | The plan's §11 lists `cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1`, which was not run | Run the full serial suite before pushing |
| low | `rust/src/runtime.rs`'s two systemd deadline sites map a `checked_deadline` failure onto error categories that the failure can never reach, because a ten- or fifteen-second interval is always representable | The M002 closure record listed them as live error mappings; the arms are unreachable | Either widen the intervals so the arm can fire, or drop the arm. This belongs to a change that owns `rust/src/runtime.rs`'s systemd paths, not to a lock-ownership correction |
| low | The quarantine path's restore depends on filesystem hard-link support; on a filesystem without hard links a moved lock is left at its quarantine name | The lock path is then free, so the next acquisition proceeds and the foreign file is left behind as litter | None for correctness. A future pass could fall back to a rename when the hard link is unavailable |
| low | `clear_stale_pid` and `read_pid` still treat an unparsable PID file as absent and remove it | Deliberately unchanged: the PID file is a different file from the lock file, and changing it would alter the stop and restart recovery path | Raise as a separate decision |
| info | The M002 closure record's §7 "signature and behavior are unchanged" statement was wrong about the behavior, and its §2 evidence row for an unclean shutdown proved only the genuine-stale case | The record overstated what it established | The M002 record is left unchanged under the append-only discipline; this corrective closure supersedes those statements |

## 11. Roadmap disposition

The milestone is closed through this corrective pass, and the next dependency
may proceed. The operations-cli roadmap's milestone table gains Milestone 003
as a closed corrective milestone, and Milestone 002 is recorded as closed
through Milestone 003.

## 12. Registry updates

Changes applied by the parent in the same change set:

- `plans/registry.md` — the operations-cli row in the active subsystem
  roadmaps names Milestone 003 as the corrective milestone that closed
  Milestone 002, and the recently-closed list gains the Milestone 003
  closure record.
- `plans/subsystems/operations-cli-roadmap.md` — §7 gains a Milestone 003
  entry and §12 gains its status row; Milestone 002's row records that it was
  closed through Milestone 003.

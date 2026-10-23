# Operations CLI Milestone 002 — Local process lifecycle safety

Status: implemented

Repository baseline: `e6822d8f78838b273cee2351d14e5d5dd3ee1e10`

Source roadmap:

- `plans/subsystems/operations-cli-roadmap.md#7-milestones`

Long-term requirements:

- `plans/000-long-term-specification.md` — §2 end-state invariants, §3 ownership boundaries
- `plans/001-terminology-and-domain-model.md` — normative language for local process state
- `plans/002-long-term-roadmap.md` — sustaining work must preserve a working proxy and current compatibility

Applicable ADRs:

- None required. The existing lock-held error contract and the process
  supervision contract owned by `rust/src/runtime_lifecycle/` are preserved.
- Stop for architecture review if the correction would change the
  process-supervision contract or would require the server and runtime
  lifecycle modules to own the shared deadline helper.

Primary class: invariant

## 1. Objective

Remove the time-of-check-to-time-of-use window on the PID lock file, and
make duration and deadline arithmetic reject non-finite or unrepresentable
inputs instead of coercing them to a different meaning or panicking.

## 2. Why this milestone is ready

No hard dependency.

Interface dependency: the checked-deadline helper introduced by this
milestone is consumed by `rust/src/server/mod.rs` and
`rust/src/runtime_lifecycle/manager.rs`. Those modules do not own the helper;
they consume a stable contract. If those owners require a different
contract, stop and split the milestone rather than forcing a shared helper
across an ownership boundary.

## 3. Current implementation evidence

**The stale-lock removal path races.** In `rust/src/operations/process.rs`
the start path creates the PID lock file with an exclusive create. On the
already-exists path it reads the file, parses the contents into a process
id, and checks whether that process exists. If the owner is absent or the
process is gone, it removes the lock file and retries the create.

A file that cannot be read, or whose contents do not parse as a process id,
yields no owner. The check treats a missing owner as equivalent to a stale
owner, removes the file, and retries. Two concurrent starts can therefore
both conclude the lock is stale, both remove it, and both proceed. The
window between the existence check and the removal is a genuine
time-of-check-to-time-of-use race.

**Deadline arithmetic can panic.** `Instant::now() + duration` appears at
several sites across `rust/src/operations/process.rs`,
`rust/src/operations/lifecycle.rs`, `rust/src/server/mod.rs`,
`rust/src/runtime_lifecycle/manager.rs`, and `rust/src/task_supervisor.rs`.
Adding a duration to an `Instant` panics when the result is not
representable. A configured interval that is large enough to overflow the
platform's instant range crashes the process rather than failing closed with
an error the caller already knows how to handle.

**Non-finite durations convert to zero.** In `rust/src/runtime.rs` a clamped
floating-point seconds value is cast directly to `u64`. A `NaN` value
converts to `0`, which reads as "no delay" rather than as "unknown", and an
infinite value saturates to the `u64` maximum rather than being rejected.

## 4. Invariants that must not regress

- The existing `ProcessError::LockHeld` contract is preserved: a start
  attempt against a live lock still fails with that error.
- An operator can still recover from an unclean shutdown by starting the
  server again; the stale-lock path must keep working for the genuine stale
  case.
- The start path does not add a blocking wait.
- No new dependency, concurrent map, or lock architecture change.
- The server and runtime lifecycle modules keep their existing timeout
  semantics for every representable input.
- No secret, prompt, raw body, or cache key enters a process-lifecycle
  diagnostic.

## 5. Scope

### In scope

- The PID lock file ownership check and removal decision in
  `rust/src/operations/process.rs`.
- The deadline computation sites that add a duration to an `Instant`.
- The floating-point seconds to `u64` conversions in `rust/src/runtime.rs`.
- Regression tests and guards for each of the above.

### Explicitly out of scope

- The status projection fallback and the redaction boundary. That is
  milestone 001 in this subsystem.
- Any change to the process-supervision contract owned by
  `rust/src/runtime_lifecycle/`.
- Any change to the server's shutdown, drain, or quiesce sequence.
- Any change to the timeout configuration keys or their reload
  classification.
- Any change to the PID file format.
- Any restructuring of `rust/src/runtime.rs`.

## 6. Required production changes

**Lock ownership decision.** The lock file may be removed only when the file
was read successfully and parsed into an owner that is confirmed absent. An
unreadable file, an empty file, and a file whose contents do not parse must
all be reported as a held lock rather than treated as stale. The
time-of-check-to-time-of-use window between the existence check and the
removal must be closed, either by performing the removal under a mechanism
that cannot race, or by re-verifying ownership atomically with the removal.

**Checked deadline.** One helper computes an `Instant` deadline from a
duration and returns a typed error when the result is not representable. The
helper replaces the unchecked additions at every site listed in the current
evidence. Each caller converts the error into its existing failure category
rather than inventing a new one.

**Non-finite numeric rejection.** A non-finite or out-of-range seconds value
must be rejected rather than converted. The rejection must use the caller's
existing error path.

## 7. Ordered work packages

### Work package A — Lock ownership and removal correctness

Intent:

Close the stale-lock removal race and stop an unreadable lock from being
treated as stale.

Required changes:

- Distinguish "read and parsed, owner confirmed absent" from "could not
  determine the owner" in `rust/src/operations/process.rs`.
- Close the removal race.

Acceptance evidence:

- A test asserts an unreadable or unparsable lock file yields a held-lock
  error and the file is left in place.
- A test drives a concurrent creation of the lock file while the stale
  decision is being made, and asserts a held-lock error rather than a
  removal.
- A test asserts the genuine stale case still recovers.

### Work package B — Checked deadline helper

Intent:

Make deadline arithmetic fail closed instead of panicking.

Required changes:

- Introduce one checked-deadline helper.
- Replace the unchecked additions at every listed site.

Acceptance evidence:

- A test asserts an unrepresentable interval yields a typed error and no
  panic.
- A test asserts a representable interval produces the same deadline as
  before.

### Work package C — Non-finite seconds rejection

Intent:

Stop a non-finite duration from reading as a zero delay.

Required changes:

- Reject non-finite and out-of-range seconds values at the conversion sites
  in `rust/src/runtime.rs`.

Acceptance evidence:

- A test asserts a `NaN` and an infinite seconds value are rejected through
  the existing error path.

## 8. Failure, cancellation, restart, contention semantics

- A start attempt against a live or undeterminable lock must fail with the
  existing held-lock error and must not wait.
- Two concurrent starts must not both proceed. The contention test must
  synchronize on an observable fixture boundary or invariant under a bounded
  timeout; fixed millisecond sleeps and yield-count loops are prohibited.
- A cancellation during a deadline computation is not a concern: the
  computation is synchronous and has no await.
- A restart after an unclean shutdown must still recover, which the genuine
  stale case in work package A must prove.
- The helper must not change the behavior of a currently representable
  deadline.

## 9. Compatibility and migration

- No migration. No storage change.
- No configuration key is added, removed, or reclassified.
- No public HTTP or CLI contract change. The held-lock error and its
  operator-facing message keep their current meaning.
- No public Rust API break beyond adding a helper surface.
- The PID file format is unchanged, so a lock file written by an earlier
  build is still readable.

## 10. Required tests

- `rust/tests/operations_lifecycle.rs` or the closest existing lifecycle
  target: lock ownership and stale-recovery coverage.
- `rust/tests/runtime_lifecycle_rNNN.rs` family, or the closest existing
  runtime lifecycle target: deadline coverage.
- `rust/tests/status_command.rs` or the closest existing operations target:
  the non-finite seconds conversion coverage.
- A focused test target may be added under `rust/tests/` for the contention
  case. Name it for the behavior, not for a milestone number.
- All Rust tests run with `--test-threads=1`.

## 11. Required verification commands

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --test operations_lifecycle -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
```

## 12. Documentation updates

- `architecture/overview.md` — add the operations CLI surface entry, shared
  with milestone 001.
- `architecture/deep-dive-runtime.md` — record the checked-deadline contract
  and the lock-ownership rule.
- `docs/cli-reference.md` — only if an operator-facing message changes, which
  this milestone must not cause for the held-lock case.

## 13. Acceptance criteria

- An unreadable or unparsable lock file is reported as a held lock and is
  not removed.
- A concurrent lock creation during the stale decision yields a held-lock
  error rather than a removal.
- The genuine stale-lock case still recovers.
- No deadline computation panics on an unrepresentable interval; each such
  case yields a typed error through the caller's existing failure path.
- A `NaN` or infinite seconds value is rejected rather than converted.
- No public HTTP or CLI contract changes.
- The default and no-default workspace suites pass.

## 14. Stop conditions

Stop and report rather than improvise when:

- Closing the removal race would require a new dependency, a new concurrency
  primitive, or a blocking wait on the start path.
- The server or runtime lifecycle owners require a different contract for the
  checked-deadline helper; split the milestone instead.
- The correction would change the process-supervision contract owned by
  `rust/src/runtime_lifecycle/`.
- The genuine stale-lock recovery path cannot be preserved.
- Evidence contradicts an invariant stated in `AGENTS.md`.

## 15. Closure evidence required

- The implementation commits.
- A requirement-to-evidence matrix mapping each work package to the specific
  test that proves it.
- The exact verification commands run and their outcomes, labeled honestly
  between local and CI runs.
- Evidence that the pre-milestone lock path removed an undeterminable lock
  file, so the new coverage is a genuine regression guard.
- A complete list of the replaced deadline sites, with the failure category
  each caller now uses.
- Any severity-tagged unresolved finding, with a recommendation.

## 16. Handoff notes

The lock-contention test is the delicate part. Synchronize on an observable
fixture boundary or invariant under a bounded timeout; do not use a fixed
millisecond sleep or a yield-count loop to guess at process state. The
repository's conventions prohibit that, and a flaky contention test is worse
than no contention test.

Keep the helper small and place it where the local lifecycle owner can see
it. Do not let it become a general time utility.

Rust tests must run serially. Strict Clippy with `-D warnings` is an
invariant of this repository. Preserve unrelated user changes in the working
tree.

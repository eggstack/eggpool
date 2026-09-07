# R006 — Process Task Supervisor and Authoritative Task-Spec Staging

Status: queued; depends on accepted R005 closure

Source roadmap: `migration-rs/subsystems/runtime-lifecycle-roadmap.md`

Primary class: infrastructure/invariant

## Objective

Create one bounded process-owned recurring-task supervisor and one authoritative task-spec inventory. The supervisor must support deterministic start/stop/reconfigure behavior and a staged task-spec diff that R007 can preflight and commit with a live rehash.

R006 builds scheduling infrastructure and task ownership semantics. R008 wires the real generation-dependent maintenance callbacks.

## One supervisor, two ownership classes

Use a single process supervisor with task specs classified as:

- `Process`: callback uses only process-owned resources and exists once per process;
- `ActiveGenerationLeased`: callback acquires the currently active generation at the beginning of each tick, uses only that lease for the tick, and releases it before sleeping.

This is intentionally simpler than Python's split process/generation supervisors while preserving the no-stale-generation invariant.

A task loop must never capture `Arc<InferenceState>`, router, provider pool, or generation config across ticks.

## Task spec

Implement an immutable `RuntimeTaskSpec`/equivalent containing at least:

- unique task name;
- interval;
- optional initial delay;
- run-immediately flag;
- optional per-tick timeout;
- ownership class;
- enabled state;
- secret-free description/callback kind;
- reloadable config paths;
- generation/process dependency names for diagnostics/tests.

Build the inventory from R001's Python fixture. Preserve canonical names/order so diagnostics and diff tests remain stable.

The initial inventory must account for the Python task set (`catalog_refresh`, `retention_cleanup`, `checkpoint`, `metrics_flush`, `update_checker`, `automatic_backup`) even when a Rust business callback is intentionally deferred to M9. A deferred callback must be represented explicitly as unavailable/not registered; never run a no-op placeholder and call it parity.

## Supervisor semantics

Each active task owns one Tokio join handle and one bounded cancellation/reconfigure channel using existing Tokio primitives.

Requirements:

- one task name -> at most one running loop;
- no overlapping ticks for the same task;
- interval is measured from completed/failed tick according to the R001/Python observation;
- initial delay and run-immediately behavior match the task fixture;
- optional per-tick timeout cancels/abandons only that tick and reports a typed outcome; it does not kill the server;
- callback errors are isolated and recorded; one bad tick cannot stop unrelated tasks;
- callback panic/join failure is observed and converted to supervisor diagnostics rather than silently detached;
- disabled tasks own no loop;
- shutdown is bounded and joins/cancels all loops.

Do not build a cron/job framework. A small map of task name -> task state/join handle is sufficient.

## Staged task-spec diff

R007 needs reconfiguration that can be preflighted before the short publication gate closes.

Implement:

```text
TaskSupervisor::prepare_diff(current_specs, candidate_specs)
    -> PreparedTaskDiff
PreparedTaskDiff::preflight()
PreparedTaskDiff::commit()
PreparedTaskDiff::rollback()/discard()
```

Exact API may vary, but semantics must provide:

- deterministic added/removed/rescheduled/unchanged sets;
- validation of duplicate names, zero/invalid intervals, missing callback capability, and unsupported ownership before mutation;
- preparation of any channels/state needed for commit before the publication gate;
- a commit path with no network or DB I/O and no expected allocation failure in normal operation;
- idempotent discard/rollback when reload aborts before acceptance;
- old loops are not stopped until the staged diff is accepted;
- new loops cannot execute candidate-dependent callbacks before runtime acceptance.

A simple approach is for commit to atomically replace the active immutable spec map, signal affected loops, and spawn/stop loops only after the accepted generation pointer is available but before R007 reopens admission. If spawn itself can fail, return a typed failure and keep enough staged state for R007 compensation.

## Process-owned task callbacks in R006

Implement only callbacks whose business operation is already available and needed to prove supervisor behavior, preferably:

- SQLite checkpoint using the process-owned DB;
- a deterministic test callback.

Do not port update/backup CLI workflows merely to populate the scheduler. R008 decides which existing Rust domain maintenance operations can be registered now and records explicit M9 callback prerequisites for the rest.

## Generation-leased callback interface

Define a callback context that receives a `GenerationLease` obtained by the supervisor at tick time, not a raw manager/global clone.

If acquisition fails because reload is gated or shutdown has begun:

- during short reload gating, wait/retry according to the manager acquire contract without starting a stale tick;
- during shutdown, exit/skip cleanly;
- cancellation while waiting leaves no lease/task leak.

A tick must release its generation lease before the loop schedules the next sleep.

## Diagnostics

Expose a bounded snapshot per task:

- name/ownership/enabled/running;
- interval/initial delay class;
- tick count;
- last outcome category and bounded elapsed duration;
- current in-tick flag;
- restart/reschedule count.

Do not retain an unbounded history of tick errors. Store only current/last bounded metadata and aggregate counts.

## Tests

Required tests include:

- task inventory matches R001 names/ownership/default schedule metadata;
- duplicate/invalid specs fail preflight without changing active loops;
- enabled process task runs once and never overlaps itself under a slow callback;
- disabled task does not run;
- interval/initial-delay/run-immediate behavior with paused Tokio time where practical;
- tick timeout/error does not stop subsequent ticks or other tasks;
- staged add/remove/reschedule changes nothing before commit;
- commit changes exactly the affected task once;
- rollback/discard preserves old schedule;
- generation-leased callback acquires A before publication and B after publication, never a stale captured generation;
- reload gate wait/cancellation leaves no task/lease leak;
- supervisor shutdown leaves zero join handles/running tasks;
- repeated spec changes do not grow internal maps/history;
- diagnostics are secret-free.

Avoid timing-flaky wall-clock sleeps; use deterministic barriers and Tokio time controls where possible.

## Scope boundaries

R006 must not:

- implement config file reload transaction;
- mutate SQLite config-derived accounts/providers;
- implement catalog/retention/backup/update business logic beyond minimal already-available callbacks;
- change server signal lifecycle;
- implement control socket/CLI;
- create a second scheduler for M9.

## Verification

```text
cargo fmt --all -- --check
cargo clippy --all-targets -- -D warnings
cargo test --all-targets
cargo test --test <R006 task-supervisor tests>
uv run pytest tests/migration_rs -q --tb=short --maxfail=1
uv run pytest <R001 Python task inventory/supervisor tests> -q --tb=short --maxfail=1
git diff --check
```

## Acceptance criteria

R006 closes only when:

- there is exactly one process task supervisor;
- task inventory/spec semantics match R001;
- task loops are singleton, non-overlapping, cancellable, and bounded;
- generation-dependent callbacks acquire an active lease per tick and retain no stale generation between ticks;
- staged task diffs are side-effect-free until commit and rollback-safe;
- unavailable M9 business callbacks are explicit rather than silently no-op;
- no new scheduler/framework dependency was added.

## Closure

Write `migration-rs/closure/runtime-lifecycle/006-status.md` and promote R007.

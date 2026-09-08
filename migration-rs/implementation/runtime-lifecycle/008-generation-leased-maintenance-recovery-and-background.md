# R008 — Generation-Leased Maintenance, Recovery, and Background Integration

Status: dependency-ready; R007 closure accepted

Source roadmap: `migration-rs/subsystems/runtime-lifecycle-roadmap.md`

Primary class: capability/invariant

## Objective

Wire real process/background lifecycle work through the R006 supervisor without allowing stale generation capture. Schedule C010 crash reconciliation at startup, register the runtime-critical recurring callbacks whose Rust capabilities already exist, and make every generation-dependent tick acquire the active generation for that tick.

R008 does not implement M9 CLI surfaces. If a Python background callback depends on a business capability deliberately owned by M9, record that dependency explicitly and leave it unregistered rather than running a placeholder.

## Startup recovery

Integrate `CrashReconciler::reconcile_once` into the Rust process startup lifecycle at the same semantic point as Python crash recovery:

- after DB open/migrations and before the first generation begins serving requests;
- loop bounded `reconcile_once` calls to convergence when the configured/default batch bound requires multiple passes;
- never create/replay provider attempts;
- preserve C010 `interrupted` / `crash_recovery` / `process_interrupted` semantics;
- store only a bounded secret-free startup recovery report for diagnostics.

A reconciliation DB failure must fail startup closed rather than serving with known nonterminal crash leftovers.

R008 must not turn C010 recovery into a frequent recurring sweep unless R001 proves Python does so.

## Active-generation-leased recurring callbacks

Implement or connect runtime-critical callbacks for existing Rust capabilities. At minimum evaluate:

### Catalog refresh

If the closed M5 Rust catalog/provider discovery surface supports a bounded refresh callback, schedule it using the active generation's provider/account/catalog/health state.

Requirements:

- one active-generation lease per tick;
- per-account/provider failure isolation;
- no tick mutates a retired generation after publication;
- refresh result cannot crash the supervisor;
- candidate/reload construction does not perform a periodic refresh inside the publication gate.

If the exact external discovery operation is not yet implemented in Rust, register the task spec as explicit `capability_pending` and document the owning future plan; do not create a second partial catalog implementation in M8.

### Retention/maintenance

Use process-owned DB plus the active generation's live maintenance/retention config for bounded deletion/cleanup operations that already have repository support.

Preserve:

- max rows/batches/tick-duration bounds where present;
- no long SQLite transaction spanning sleep/network work;
- active request/reservation safety;
- contention defer/skip rather than blocking request traffic indefinitely.

### SQLite checkpoint

Register the R006 process-owned checkpoint callback using the process DB. It does not need a generation lease.

### Other Python inventory entries

For `metrics_flush`, `update_checker`, and `automatic_backup`:

- inspect current Rust capability availability;
- if the underlying service exists, register through the same supervisor and match R001 schedule semantics;
- if the underlying service is intentionally M9 work, mark the callback unavailable/deferred in one explicit inventory/diagnostic field and do not spawn a no-op loop;
- R008 closure must list every deferred callback and its future owner so nothing disappears silently from the migration plan.

M9 must later attach its callback to this supervisor rather than adding a second scheduler.

## Task lifecycle across rehash

Because generation-dependent ticks call `RuntimeManager::acquire()` each time:

- a tick that acquired generation A before publication may finish on A;
- the next tick after publication acquires B;
- no callback stores A's router/catalog/config between ticks;
- R006 task-spec reconfiguration can change interval/enabled state atomically with R007 acceptance;
- an old generation's retirement is delayed only for the duration of a currently running tick lease, not for the whole process task lifetime.

## Finalization/reconciliation recurring ownership

M7 finalization jobs already run under `FinalizationSupervisor` and R004 drains them; do not create a periodic polling loop merely to “keep them alive.”

If `FinalizationSupervisor::reconcile_once` is only a bounded status/retry hook, R008 may schedule it only if R001/Python has an equivalent recurring responsibility and doing so is necessary for correctness. Otherwise leave finalization execution self-owned and use drain/reconcile during retirement/shutdown.

## Isolation and bounds

Every callback must:

- have a bounded tick timeout or bounded work budget appropriate to the operation;
- catch/return typed errors to supervisor rather than panic the process;
- release generation lease on success/error/cancellation;
- avoid retaining full response/request/config bodies in task diagnostics;
- keep only bounded last-outcome/aggregate counters;
- avoid overlapping itself.

Repeated upstream/catalog/maintenance failure must not poison `RuntimeManager`, close the active generation, or require a restart for the next successful tick/request.

## Tests

Required tests:

### Startup reconciliation

- DB with pending request/active reservation/open attempt is reconciled before first request can be served;
- multi-batch recovery drains to convergence;
- second startup recovery pass is no-op;
- provider server sees zero replayed requests;
- recovery failure prevents server acceptance but DB remains readable.

### Generation tick ownership

- block tick on generation A, publish B, finish A tick, then next tick uses B;
- old generation retirement waits only for A tick lease and then drains;
- callback stores no raw generation handle between ticks;
- tick waiting on publication gate wakes to B, not stale A.

### Task reconfiguration

- change live interval/enabled state through R007 and assert exactly one loop with candidate spec after acceptance;
- failed rehash leaves old schedule and callbacks intact;
- no duplicate catalog/maintenance loop across repeated rehash.

### Failure isolation

- catalog/maintenance callback error followed by success without restart;
- one task error does not stop checkpoint/other tasks;
- task timeout/cancellation releases lease and next tick proceeds;
- internal task maps/history remain bounded under many failing ticks.

### Deferred capabilities

- every R001 inventory task is either registered with a real callback or explicitly marked with a future owner/reason;
- no `capability_pending` task owns a running loop.

## Scope boundaries

R008 must not:

- implement backup/recover or update CLI commands;
- create a second scheduler;
- alter M5 catalog/routing semantics beyond invoking their existing bounded maintenance interfaces;
- run crash reconciliation after startup as a provider retry mechanism;
- change signal shutdown (R009);
- implement control socket/rehash CLI.

## Verification

```text
cargo fmt --all -- --check
cargo clippy --all-targets -- -D warnings
cargo test --all-targets
cargo test --test <R008 background/recovery tests>
uv run pytest tests/migration_rs -q --tb=short --maxfail=1
uv run pytest <Python runtime task/crash recovery tests> -q --tb=short --maxfail=1
uv run pytest tests/smoke/ -q --tb=short --maxfail=1
git diff --check
```

No live paid provider is required; use deterministic local discovery/provider fixtures.

## Acceptance criteria

R008 closes only when:

- C010 reconciliation is scheduled before first request acceptance and never replays unknown work;
- generation-dependent recurring callbacks acquire active generation per tick and cannot stay stale across rehash;
- process callbacks exist once and remain generation-independent;
- task enabled/interval changes follow accepted R007 config/task commits;
- callback failures/timeouts are isolated and bounded;
- every Python task inventory entry has an explicit Rust implemented-or-deferred status with no placeholders;
- no scheduler/task leak remains after repeated rehash/failure.

## Closure

Write `migration-rs/closure/runtime-lifecycle/008-status.md` and promote R009.

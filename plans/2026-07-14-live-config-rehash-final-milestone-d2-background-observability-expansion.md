# Live Configuration Rehash Final Milestone D2 — Background and Observability Policy Expansion

## Status

Detailed handoff plan for the second part of the final remaining live-rehash milestone.

D2 builds on the unified `register_runtime_tasks()` path and expands live reload to selected background scheduling, retention, model-info, metrics, backup, and observability policies. It must preserve a single authoritative task-registration model and avoid duplicate schedules during publication or retirement.

## Objective

Allow selected configuration-derived schedules and runtime observability policies to change without restarting EggPool, while preserving task uniqueness, process-owned resource identity, generation coherence, and deterministic transition ordering.

## Scope

Candidate live field families:

- catalog/model refresh interval and startup-refresh policy where safe;
- model-info enabled state, refresh interval, and backfill cadence;
- model ping cadence and retention;
- request/event/rollup retention durations;
- stale-request finalizer threshold derived from reloadable upstream timeout;
- usage-window refresh cadence;
- finalization-retry drain cadence where queue ownership supports replacement;
- health disabled-model prune cadence;
- metrics flush interval and batching policy where the coalescer remains process-owned;
- automatic backup enabled state, interval, startup delay, and retention policy;
- update-check cadence if represented in the unified task registry;
- dashboard/runtime presentation settings read dynamically rather than installed as middleware;
- reload diagnostics retention and event verbosity controls.

Remain restart-required:

- database path and connection topology;
- backup destination changes that cross deployment/permission boundaries unless explicitly proven safe;
- middleware topology;
- server bind and runtime thread configuration;
- control socket and state directory;
- process-owned worker/executor sizing.

## Phase 1: Task ownership table

Create a reviewable task inventory containing:

- task name;
- registration predicate;
- configured interval/delay;
- owning supervisor;
- process-owned or generation-owned classification;
- process resources touched;
- whether callback leases the current generation;
- cancellation policy;
- retirement behavior;
- reloadable fields affecting the task.

The inventory must be test-visible. Every registered task must appear exactly once.

Preferred ownership model:

- process-owned schedules for checkpoint, retention, update checks, backup, and process-wide metrics flushing;
- current-generation leased callbacks for catalog, model-info, usage-window, health-prune, stale-finalizer, and generation-dependent retry work;
- generation-owned queues/services only where old-generation work must drain independently.

Resolve ambiguous ownership before marking fields live.

## Phase 2: Unified reconfiguration mechanism

Extend the unified task registry so both startup and reload use the same task specification objects.

Recommended shape:

```python
@dataclass(frozen=True)
class RuntimeTaskSpec:
    name: str
    interval_s: float
    initial_delay_s: float
    run_immediately: bool
    ownership: TaskOwnership
    callback_factory: Callable[[TaskRegistrationContext], AsyncCallable]
```

The implementation should support computing a task-spec diff between active and candidate configurations.

During reload:

1. build candidate task specs without scheduling them;
2. validate uniqueness and intervals;
3. publish the candidate generation;
4. atomically apply process-owned schedule changes;
5. enable candidate/current-generation callbacks;
6. stop superseded schedules;
7. let in-flight callbacks finish under their existing leases or cancel according to explicit policy;
8. record transition results.

Avoid a window where both old and new process-owned schedules execute the same checkpoint, backup, cleanup, or metrics flush.

## Phase 3: Dynamic process-owned task updates

For process-owned schedules, add an explicit supervisor reconfiguration API rather than replacing the process-owned resource.

Required operations:

- add task;
- remove task;
- update interval/delay;
- preserve heartbeat/history where meaningful;
- reject duplicate names;
- serialize with active tick execution;
- expose last and next run timestamps after reconfiguration.

A schedule update should not create overlapping ticks. Define whether interval changes take effect from the last completion time or from publication time and test the chosen rule.

## Phase 4: Background-policy live classifications

Move fields to `LIVE` only after the task-spec and supervisor transition paths exist.

For every field:

- prove startup and candidate task specs differ as expected;
- prove no unrelated task changes;
- prove process-owned resources are retained;
- prove generation-dependent callbacks lease the correct generation;
- prove mixed process-bound changes reject atomically.

Keep backup path, database topology, and deployment paths restart-required unless separately redesigned.

## Phase 5: Deterministic transition tests

### Interval changes

Use a controllable clock or test scheduler:

- start with interval A;
- publish interval B;
- assert exactly one schedule exists;
- assert no duplicate or missed transition tick outside documented semantics;
- assert next-run diagnostics match interval B.

### Enable/disable

For model-info or backup:

- start disabled;
- rehash enabled;
- assert the task appears and runs once according to startup-delay policy;
- rehash disabled;
- assert no new ticks begin;
- allow an in-flight tick to complete safely.

### Retention policy

- populate old and recent records;
- change retention live;
- trigger cleanup deterministically;
- assert only rows outside the new policy are removed;
- ensure active request/history identity is preserved.

### Metrics flush cadence

- change flush interval live;
- assert the same process-owned coalescer remains in use;
- assert no buffered metrics are lost or double-flushed;
- force shutdown after rehash and verify final flush.

### Backup cadence

- change enabled state and interval;
- assert no duplicate backup execution;
- verify backup uses the active validated config path and process database;
- verify permission/path errors fail the tick without destabilizing the runtime.

### Rapid reloads

Apply several scheduling changes in succession while callbacks are active. Assert:

- one schedule per task name;
- no stale callbacks use a retired generation after lease release;
- no orphan tasks remain;
- diagnostics converge to the newest generation and policy.

## Phase 6: Observability policy and diagnostics

Expose task-reload information:

- task spec generation/version;
- added, removed, and updated tasks;
- active interval and next run;
- in-flight tick count;
- last transition outcome;
- duplicate-prevention counters;
- cancellation/completion status;
- process-owned versus generation-leased classification.

Operational events should cover:

- task schedule updated;
- task enabled/disabled;
- task transition rejected;
- duplicate schedule prevented;
- in-flight task drain timeout;
- successful schedule convergence.

Do not include secrets, backup contents, request payloads, or model-info response bodies.

## Phase 7: Documentation

Update architecture and operator documentation with:

- task ownership model;
- fields that can change live;
- interval transition semantics;
- in-flight callback behavior;
- backup and retention cautions;
- troubleshooting duplicate/overdue tasks;
- fields that still require restart.

## Required tests

Unit:

- complete task inventory;
- startup/candidate task-spec parity;
- task-spec diff correctness;
- duplicate-name rejection;
- dynamic add/remove/update behavior;
- process-owned resource identity preservation;
- generation lease usage;
- unknown field fail-closed behavior.

Integration:

- live interval change;
- enable/disable model-info task;
- retention-policy change;
- metrics-flush cadence change;
- backup cadence change;
- rapid repeated reloads during active ticks;
- no duplicate execution;
- no orphan tasks after retirement.

## Acceptance criteria

D2 is complete when:

- one authoritative task inventory drives startup and reload;
- selected schedule and retention fields apply live;
- process-owned task resources retain identity;
- no duplicate schedules or overlapping ticks are introduced;
- callbacks use coherent generation leases;
- rapid reloads converge to the newest task policy;
- diagnostics accurately expose task transitions;
- process-bound storage/deployment fields remain restart-required;
- all lint, type, unit, integration, and shutdown-flush tests pass.

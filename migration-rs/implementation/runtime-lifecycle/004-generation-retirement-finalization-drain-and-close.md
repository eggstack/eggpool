# R004 — Generation Retirement, Retained Finalization Drain, and Resource Close

Status: closed; see [closure record](../../closure/runtime-lifecycle/004-status.md)

Source roadmap: `migration-rs/subsystems/runtime-lifecycle-roadmap.md`

Primary class: invariant/infrastructure

## Objective

Complete old-generation ownership after publication. A superseded generation must stop accepting new work, remain alive for all acquired request/stream leases, preserve M7 retained finalization until it converges, and then close its generation-owned resources exactly once in deterministic order.

R004 owns live-rehash retirement semantics. Process shutdown uses these primitives later in R009.

## Retirement state

Extend `GenerationSlot` with explicit monotonic states, names adjustable but semantics fixed:

```text
active -> retiring -> draining_finalization -> closing -> closed
                                \-> failed_close
```

A slot may enter retirement only after it is no longer the active accepting slot. State transitions must be idempotent and observable through secret-free snapshots.

## Retirement readiness

Retirement must distinguish two accepted-work classes:

1. request/stream generation leases;
2. retained M7 terminal/finalization jobs registered by work from that generation.

Required sequence for a normal live rehash retirement:

1. old slot is already non-accepting after R003 publication;
2. wait until active generation leases reach zero;
3. call the generation's `FinalizationSupervisor::drain()` and verify active retained jobs reach zero;
4. only then close generation-owned provider/client resources;
5. mark closed and remove/reap manager retirement state.

Do not use `Arc::strong_count` as the correctness signal for request drain. Use the explicit lease count plus the M7 supervisor's bounded job state.

If a finalization worker reports a terminal failure, preserve typed diagnostics and fail closed; do not close transport beneath an unresolved retained job merely to make retirement look complete.

## Resource close order

Define one reviewable generation close order based on actual Rust ownership. At minimum:

1. stop/disable generation-local callback registration if any exists after R008;
2. confirm finalization supervisor drained;
3. close provider/account client pool and its underlying transports;
4. release remaining generation-only handles/caches that expose close operations;
5. mark slot closed.

If R002 candidate cleanup uses the same resource enum/list, reuse it so abort and retirement cannot drift.

Every close operation must be bounded by an explicit timeout and return typed sanitized evidence. A timeout/error marks the slot `failed_close`; it must not crash or poison the currently active generation.

## Retirement backlog bound

Repeated rehash while a long-lived stream prevents old-generation retirement can otherwise retain provider pools indefinitely.

Implement a small explicit manager bound on unresolved retiring generations. Recommended default: 4, internal and test-visible. If the backlog is at capacity:

- reject a new staged publication before candidate ownership transfers;
- return a typed `RetirementBacklog`/busy result;
- leave the active generation untouched;
- do **not** force-close an old live-rehash generation to make room.

If R001 proves a stricter Python-visible requirement that conflicts with this bound, document the supported difference before implementation. The bound is a local/SBC safety measure, not a distributed orchestration feature.

Completed/failed retirement records must be reaped into a bounded diagnostic summary rather than an unbounded history vector.

## Retirement task ownership

The manager may spawn one Tokio retirement task per retiring slot, but it must track and reap each task.

Requirements:

- no detached task without manager ownership;
- cancellation of a caller that initiated rehash does not cancel retirement after publication acceptance;
- retirement errors are captured in manager diagnostics;
- manager shutdown can adopt/join outstanding retirement tasks in R009;
- a duplicate request to retire the same slot shares/observes existing work rather than spawning another closer.

## Tests

Add deterministic tests for:

### Lease drain

- finite lease blocks close until released;
- streaming lease blocks close through full body lifetime;
- once final lease drops, retirement advances without polling races.

### Retained finalization drain

- request lease drops after registering a deliberately blocked finalization job;
- provider pool remains open while finalization job is active;
- releasing the finalizer lets retirement close exactly once;
- incompatible/failed finalization leaves typed blocked/failed retirement rather than premature close.

### Close order/idempotency

- instrument closeable resources and assert exact order;
- duplicate retirement request does not double-close;
- one close failure does not close process-owned DB/affinity/wire resolver;
- close error diagnostics contain no credentials.

### Backlog bound

- hold multiple long streams across sequential publications until the configured retirement cap is reached;
- next stage fails before candidate transfer/publication;
- releasing old streams drains/reaps slots and allows another publication;
- manager retirement-state size returns to baseline.

### Active-generation isolation

- failure/timeout closing generation A does not make active generation B fail requests;
- B can continue serving while A is `failed_close`/diagnosed.

## Timeouts

Do not reuse provider request timeouts for lifecycle close. Add small explicit lifecycle constants/config internal to M8, with deterministic test overrides. Do not expose new public config unless the Python oracle has one.

Live retirement may remain pending beyond a diagnostic threshold when accepted work is legitimately long-lived; the threshold must not become a hidden request-kill deadline.

## Scope boundaries

R004 must not:

- parse/reload config;
- start background task inventory;
- implement process signal shutdown/force semantics beyond exposing reusable retirement/drain primitives;
- modify M7 finalization retry policy;
- add DB schema or control CLI.

## Verification

```text
cargo fmt --all -- --check
cargo clippy --all-targets -- -D warnings
cargo test --all-targets
cargo test --test <R004 retirement tests>
uv run pytest tests/migration_rs -q --tb=short --maxfail=1
uv run pytest <R001 retirement Python oracle tests> -q --tb=short --maxfail=1
git diff --check
```

## Acceptance criteria

R004 closes only when:

- no old generation closes while request/stream leases remain;
- M7 retained jobs drain before provider transport close;
- close order is deterministic, idempotent, bounded, and secret-free;
- retirement task/state bookkeeping is reaped and bounded;
- backlog capacity blocks new publication rather than abandoning accepted work;
- a failed old-generation close cannot crash or poison the active generation;
- M7 regression remains green.

## Closure

Write `migration-rs/closure/runtime-lifecycle/004-status.md` and promote R005.

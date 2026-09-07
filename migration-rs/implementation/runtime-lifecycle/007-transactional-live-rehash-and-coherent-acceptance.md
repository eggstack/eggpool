# R007 — Transactional Live Rehash and Coherent Acceptance

Status: queued; depends on accepted R006 closure

Source roadmap: `migration-rs/subsystems/runtime-lifecycle-roadmap.md`

Primary class: invariant/capability

## Objective

Implement the server-side live rehash service that composes R002 candidate construction, R003 staged publication, R004 retirement, R005 diff policy, R006 staged task-spec changes, and existing-schema config-derived persistence into one serialized fail-closed transaction.

M8 owns this reload capability. M9 later wires the user-facing control socket/`eggpool rehash` CLI to it.

## Public runtime API

Introduce a typed `ReloadService`/equivalent owned by the process runtime.

Expected input:

- config path or already-read candidate bytes plus canonical path context;
- optional expected content digest from a future control client;
- cancellation token/context where needed for tests/server lifecycle.

Expected result is secret-free and includes at least:

- `Applied`, `Noop`, `RestartRequired`, `ValidationFailed`, `StaleDigest`, `Busy/RetirementBacklog`, `Aborted`, `CompensationFailed` categories;
- active generation id/digest prefix;
- changed sections;
- restart-required paths when applicable;
- whether old-generation retirement is pending;
- bounded diagnostic reason/code.

Do not return raw config values or secret-bearing validation context.

## Reload serialization

At most one reload transaction may execute candidate/commit work at a time.

Use one process-owned async mutex for the reload transaction. This lock is **not** used by normal request acquisition and may be held across config read/candidate build because reload is rare.

Before candidate construction snapshot the active generation id/digest. Revalidate the expected active generation immediately before staging publication so a stale transaction cannot overwrite a newer accepted generation.

## Pre-commit phases

All expensive/unbounded/fallible work must occur before request admission is gated:

1. read candidate config bytes from the canonical path;
2. validate syntax/semantic/credential shape with existing Rust config validation;
3. compute content digest and compare optional expected digest;
4. use R005 to compute typed diff;
5. return no-op without generation bump if semantically unchanged;
6. return restart-required if **any** restart-required change exists; do not live-apply the live subset;
7. allocate a new generation id candidate and call R002 factory;
8. prepare the existing-schema provider/account/config-derived persistence delta;
9. derive R006 candidate task specs and `PreparedTaskDiff`;
10. preflight task diff and any other bounded process transition;
11. verify R004 retirement backlog capacity.

A failure/cancellation in these phases explicitly aborts the candidate and leaves active runtime, DB config-derived state, and task schedule unchanged.

## Persistence delta

Port only the config-derived durable synchronization already present in Python/legacy startup/reload behavior. At minimum review provider/account synchronization and authentication-reset behavior frozen in R001.

Rules:

- use existing tables/migrations only;
- never delete durable request/attempt/reservation history to match config;
- account/provider removal must follow Python safe disable/retention semantics rather than deleting identities referenced by old attempts;
- compute an inverse/old snapshot sufficient for compensation if acceptance fails after persistence mutation;
- no raw API key is persisted if the existing schema stores env/name metadata only;
- all statements are idempotent enough for a retry after process interruption.

## Short acceptance window

After all preflight succeeds:

1. call R003 `stage` against the expected active generation; this closes new lease admission while old accepted leases continue;
2. execute one bounded SQLite worker transaction that applies the persistence delta;
3. while the SQLite transaction is still under explicit control and admission remains closed, commit the staged runtime pointer using the synchronous R003 primitive;
4. explicitly commit SQLite; if SQLite commit fails, roll the runtime pointer back before reopening admission;
5. commit the already-preflighted R006 task-spec diff;
6. if mandatory task-spec commit unexpectedly fails, execute typed compensation while admission remains closed: restore old task specs, old runtime pointer, and old config-derived persistence snapshot; if compensation cannot fully converge, enter `CompensationFailed` and keep admission fail-closed rather than serving a mixed generation;
7. when DB/runtime/task state is coherent, call R003 `accept` to reopen admission and transfer candidate ownership;
8. schedule R004 retirement for the old slot;
9. publish/update bounded reload diagnostics.

Implementation may adjust the exact low-level call order if SQLite API constraints require it, but the externally visible invariant is fixed: **no request is admitted while DB/runtime/task acceptance can still roll back**.

No provider/network/catalog/backup operation belongs inside this gate.

## Cancellation semantics

- Before `stage`: cancellation aborts candidate and returns old runtime unchanged.
- After `stage` but before accepted completion: the transaction becomes shielded/owned by the reload service long enough to either commit coherently or roll back/compensate; caller cancellation is delivered only after that bounded resolution.
- After `accept`: cancellation cannot revert an accepted generation. Retirement continues independently under manager ownership.

Tests must make these phase differences explicit.

## Shutdown interaction

R007 exposes hooks for R009 but already must reject/abort publication if manager shutdown begins before acceptance.

If shutdown starts during the gated acceptance window, the reload transaction owns resolution first: either restore the old accepted state then shutdown, or finish accepted publication then let shutdown drain it. It must never leave the gate permanently closed without a typed fatal/compensation state.

## Task/result integration

Future M9 control responses need Python-compatible semantics:

- generation number/id after apply;
- changed sections;
- restart-required path set;
- retirement-pending flag;
- no-op result;
- validation/stale/busy categories.

R007 should expose these as Rust types, not serialize a control protocol yet.

## Tests

Required deterministic tests:

### Basic cases

- identical/no-op: no candidate construction, DB mutation, task diff, or generation bump;
- live provider/account/routing/model-router/body-limit changes publish one new generation;
- restart-required and mixed changes leave everything unchanged;
- invalid config leaves everything unchanged;
- expected-digest mismatch leaves everything unchanged.

### Failure points

Inject failures/cancellation at:

- config validation;
- candidate build after provider pool allocation;
- persistence-delta preparation;
- task preflight;
- stage/expected-generation revalidation;
- each persistence statement group;
- runtime pointer commit;
- SQLite commit;
- task-spec commit;
- compensation steps;
- after accept before retirement scheduling.

For every pre-acceptance failure assert active generation, DB config-derived rows, task specs, and generation epoch are old/coherent; candidate resources close exactly once.

### Concurrency

- concurrent reload callers serialize; exactly one accepted generation per successful transaction;
- normal requests continue during candidate build/preflight;
- requests wait only during the short stage/acceptance gate;
- a request released from the gate observes only the accepted generation;
- long old request/stream continues during reload and retirement.

### Durable safety

- account/provider change does not invalidate historical M7 attempt/finalization identity;
- Python can reopen/read the DB after rehash;
- failed compensation does not silently reopen admission to mixed state.

## Scope boundaries

R007 must not:

- implement control socket or CLI;
- start missing background business callbacks (R008/M9);
- change graceful process shutdown policy (R009);
- redesign config reload dispositions;
- perform network validation during the commit gate;
- add DB schema.

## Verification

```text
cargo fmt --all -- --check
cargo clippy --all-targets -- -D warnings
cargo test --all-targets
cargo test --test <R007 reload transaction tests>
uv run pytest tests/migration_rs -q --tb=short --maxfail=1
uv run pytest <Python reload transaction/config reload tests> -q --tb=short --maxfail=1
uv run pytest tests/smoke/ -q --tb=short --maxfail=1
git diff --check
```

## Acceptance criteria

R007 closes only when:

- all R001 reload result classes are represented or explicitly documented;
- live/no-op/restart/invalid/mixed/stale outcomes are correct;
- admission is closed only for the bounded acceptance window;
- no request can observe DB/runtime/task mixed acceptance state;
- every pre-acceptance failure/cancellation leaves old state coherent and candidate closed;
- accepted reload survives caller cancellation and schedules independent retirement;
- DB remains schema-compatible and historical M7 rows safe;
- reload transaction internal state is bounded and secret-free.

## Closure

Write `migration-rs/closure/runtime-lifecycle/007-status.md` and promote R008.

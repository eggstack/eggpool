# R011 — Differential Qualification and M8 Closure

Status: queued; depends on accepted R010 closure

Source roadmap: `migration-rs/subsystems/runtime-lifecycle-roadmap.md`

Primary class: invariant

## Objective

Run the integrated M8 qualification against the committed R001 Python oracle and the real Rust Axum/M7 runtime. R011 is the only plan allowed to mark M8 closed. It must prove generation publication, live rehash, task ownership, retirement, recovery, shutdown, and active-state authority as one coherent system rather than relying only on slice-level tests.

R011 should primarily add qualification/fault tests and closure evidence. Production changes are allowed only for defects exposed by the matrix and must receive explicit failing-before/passing-after regression coverage.

## Aggregate matrix

Build deterministic local fixtures around the actual foreground server/process runtime and R007 reload service. No paid/live provider is required.

### Startup/factory parity

Prove:

- startup and reload use the same R002 generation factory;
- process-owned DB/affinity/wire resolver remain the same identities across generations;
- generation-owned provider pool/inference graph/config/digest are replaced;
- candidate construction failure closes candidate resources and never changes active state;
- startup C010 recovery runs before first request acceptance.

### Config/reload policy

For every R001 policy fixture case assert exact Rust outcome:

- no-op;
- live-only;
- restart-required;
- mixed live + restart-required;
- invalid candidate;
- stale/expected digest mismatch;
- secret redaction;
- added/removed/changed providers/accounts/model routers;
- task interval/enabled changes;
- live body-size change.

Every Rust config field must remain covered by R005's classification guard.

### Finite and streaming rehash

Use deterministic providers A/B and barriers:

1. start finite request on generation A and block upstream;
2. accept generation B with a changed provider/account/routing/body-limit fact;
3. finish A request and assert all its selection/submission/finalization facts remain A;
4. next finite request uses B;
5. repeat with a stream that receives at least one chunk before rehash and completes after rehash;
6. assert no post-handoff retry and no transport close beneath the A stream;
7. after A leases/finalizers drain, A provider pool closes exactly once.

Run the same principle for virtual-router/affinity and wire-learning state:

- valid process affinity may survive but must revalidate against B's router;
- invalid/removed route cannot be served from stale affinity;
- wire learning survives only when structural fingerprint is compatible.

### Publication/lease concurrency

Stress the R003 claim/commit boundary with deterministic barrier injection and many concurrent acquire loops.

Required assertions:

- every request gets exactly one generation id;
- no new A lease after B commit;
- no generation-mixed request state;
- gate waiters wake after accept/rollback/shutdown;
- cancellation leaves zero waiter/lease leak;
- publication epoch increments only for accepted publication.

### Reload failure/cancellation matrix

Inject each R007 failure point and compare with R001 outcome classes.

For all failures before acceptance assert:

- old active generation unchanged;
- generation epoch unchanged;
- durable config-derived account/provider state old/coherent;
- task specs old/coherent;
- candidate resources closed once;
- gate reopened;
- subsequent valid request/reload works without restart.

For cancellation during the short acceptance window assert transaction ownership resolves commit or rollback/compensation before propagating cancellation.

A compensation-failed fixture must fail closed and never resume serving mixed state.

### Retirement/finalization

Prove:

- active finite/stream lease blocks retirement;
- retained M7 finalization blocks provider-pool close after request lease release;
- retirement completes when both drain;
- failed old-generation close is isolated from active generation;
- repeated rehash reaps closed retirement records;
- retirement backlog bound rejects additional rehash before publication without killing accepted old work;
- backlog drains and later reload succeeds.

### Background task ownership

For each implemented R008 callback:

- process tasks run once across rehash;
- generation-leased tick started on A may finish on A, next tick uses B;
- no callback captures A between ticks;
- task spec live changes take effect exactly once after accepted rehash;
- failed/aborted reload leaves old task specs;
- timeout/error in one tick does not stop the scheduler/server;
- task maps/history remain bounded.

Assert every R001 inventory task has either a real registered Rust callback or one explicit documented future-owner/deferred reason; no silent/no-op task is accepted.

### Startup/restart recovery

Use a file-backed SQLite fixture:

- interrupt durable request/attempt/reservation state;
- stop/abort process fixture;
- create a fresh Rust process runtime over the same DB;
- C010 recovery converges before serving;
- no provider replay occurs;
- Python opens/reads the resulting DB successfully;
- a subsequent valid Rust request succeeds without DB reset.

### Shutdown matrix

Run shutdown at:

- idle;
- finite pre-handoff;
- stream post-handoff;
- retained finalization pending;
- generation retirement pending;
- process task tick in progress;
- reload pre-stage;
- reload gate closed before runtime pointer commit;
- pointer committed before acceptance;
- post-accept/pre-retirement scheduling.

Assert monotonic quiesce/drain/close, no new request/reload after shutdown, no orphaned candidate/retirement task, finalization drain before DB close in graceful cases, bounded forced close in stalled cases, and same-DB recoverability afterward.

### Active authority

From the actual Axum router prove:

- inference does not use static startup `InferenceState`/provider pool;
- readiness reflects live provider/account/catalog state;
- live body-size changes are enforced before oversized buffering and on the same generation used for routing;
- restart-required auth/listener/dashboard topology changes are rejected and current behavior stays unchanged;
- any model/read/dashboard path marked live uses one coherent generation lease;
- source/structural audit finds no production long-lived direct generation service in `AppState` outside manager-mediated access.

### Security/resource matrix

Seed sentinel values for:

- API key;
- proxy credential;
- route session;
- request body;
- provider body/error detail.

Assert they never appear in runtime/reload/task/retirement diagnostics, `Debug`, or closure fixtures beyond existing explicitly sanitized M7 durable behavior.

After repeated success/failure/reload/task/shutdown cycles assert baseline for:

- active leases;
- gate waiters;
- retiring slots/tasks;
- finalization jobs;
- provider pools;
- task loops;
- task/reload diagnostic histories;
- wire flights/gates/effect ledgers inherited from M7.

## Supported differences

R011 must enumerate every accepted M8 difference from Python. Do not hide a behavior gap by weakening the R001 fixture.

Potential intentional structural differences that are acceptable only if behavior is proven:

- Rust uses one process task supervisor with per-tick generation leases instead of Python split supervisors;
- Rust uses `ArcSwap` plus a narrow claim/publication section rather than Python condition/slot implementation;
- Rust may bound unresolved retiring-generation backlog more strictly for SBC safety;
- M9-owned control/CLI and deferred background business callbacks remain explicitly outside M8.

Any user-visible difference in reload result, request consistency, shutdown/recovery, or live/restart classification requires explicit review in the closure record and an ADR if it changes the canonical contract materially.

## Verification commands

At minimum run and record exact counts for:

```text
cargo fmt --all -- --check
cargo clippy --all-targets -- -D warnings
cargo test --all-targets
cargo test --test <R011 integrated runtime qualification>
uv run pytest tests/migration_rs -q --tb=short --maxfail=1
uv run pytest <targeted Python runtime_manager/reload/task/shutdown/stale-state suites> -q --tb=short --maxfail=1
uv run pytest tests/smoke/ -q --tb=short --maxfail=1
uv run pyright src/ scripts/
uv run ruff format --check src/ tests/ scripts/
uv run ruff check src/ tests/ scripts/
git diff --check
```

If Rust changes dependencies, verify the final `Cargo.toml`/`Cargo.lock` diff is limited to justified M8 dependencies (expected: `arc-swap`; no framework sprawl).

No live/paid provider or broad platform matrix is required. M10 owns release/SBC characterization.

## Closure criteria

R011 closes only when all are true:

1. R001 mandatory runtime/reload/task/shutdown oracle cases pass or have an approved supported difference.
2. Startup and reload share one generation factory.
3. No request/stream mixes generations, including across awaits and streaming body tasks.
4. No new old-generation lease occurs after publication commit.
5. Invalid/restart/mixed/stale/failed/cancelled pre-acceptance reload leaves old runtime/DB/task state coherent.
6. Accepted reload publishes one coherent generation and retirement proceeds independently.
7. Old generation is not closed until request leases and retained finalization drain.
8. Retirement/task/reload diagnostics and manager state are bounded.
9. Background generation-dependent callbacks never use stale generation state between ticks.
10. Startup crash recovery and restart over same DB converge without provider replay or DB reset.
11. Graceful and forced shutdown are bounded, deterministic, and leave the DB recoverable.
12. Production handlers contain no stale startup generation authority for live fields/services.
13. Live body-size enforcement happens before excessive buffering and is generation-consistent.
14. Runtime/reload/task/retirement diagnostics are secret-free.
15. No unresolved high/medium M8 correctness or security finding remains.
16. M9 receives explicit stable reload/runtime/task/shutdown interfaces; no M9 implementation plan is auto-promoted.

## M9 handoff to record in closure

Document the exact stable M8 APIs M9 should consume, including:

- reload service request/result types;
- runtime manager/snapshot/status interfaces;
- task supervisor registration/spec APIs for future backup/update callbacks;
- shutdown/process handles that daemon/control code may invoke;
- generation/runtime diagnostics for `status`/control responses;
- constructor-owned restart-required fields that M9 must hard-restart rather than live-apply.

## Closure

Write `migration-rs/closure/runtime-lifecycle/011-status.md`, update `migration-rs/registry.md` and the canonical roadmap to M8 closed, and make M9 eligible for its own planning/implementation review. Do not create M9 implementation plans as part of R011 closure.

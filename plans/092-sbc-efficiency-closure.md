# Plan 092 — SBC Efficiency Closure

Date: 2026-08-07
Status: ready for implementation
Parent roadmap: `plans/086-sbc-routing-and-storage-efficiency-roadmap.md`
Depends on:

- `plans/087-weighted-routing-semantics.md`
- `plans/088-pending-claim-load-publication.md`
- `plans/089-catalog-and-ping-write-reduction.md`
- `plans/090-finalization-roundtrip-reduction.md`
- `plans/091-lean-runtime-and-schema-pruning.md`

Planning baseline: `d6c49dea5ed800bfcd22d95fe8c7943a29590125`

## Purpose

Close Roadmap 086 with a proportionate correctness and resource check. Verify that the routing fixes behave as intended, that catalog/finalization changes actually reduce application-level SQLite work, and that the lean runtime remains suitable for Raspberry Pi/SBC deployment.

This is not another optimization phase. Correct only demonstrated roadmap regressions, reconcile documentation/status, and stop.

## Governing constraints

1. Do not add a benchmark framework, soak suite, dashboard telemetry project, retained evidence schema, or new CI job.
2. Use existing tests, runtime diagnostics, SQLite inspection, and standard OS tools.
3. Do not invent Raspberry Pi measurements when representative hardware is unavailable.
4. Do not treat upstream model/network latency as EggPool overhead.
5. Do not reopen dependencies, CI, health/quarantine, protocol transcoding, or rehash architecture unless a roadmap change demonstrably broke them.
6. Any corrective patch must be narrow and directly tied to a failed acceptance criterion from Plans 087–091.

## Workstream A — Verify plan completion truthfully

Before closure, inspect Plans 087–091 and confirm each contains:

- implementation commit/reference if the repository process records it;
- exact focused verification commands;
- exact results;
- all acceptance criteria checked or a documented blocker;
- no unresolved rejection condition.

Do not infer completion from commit messages alone.

## Workstream B — Standard repository gate

Run:

```bash
uv run ruff format --check src/ tests/ scripts/
uv run ruff check src/ tests/ scripts/
uv run pyright src/ scripts/
uv run pytest tests/smoke/ -q --tb=short --maxfail=1
uv run eggpool --config config.example.toml check-config
uv run eggpool --config config.sbc.example.toml check-config
```

Also run a curated union of the focused routing, coordinator/concurrency, catalog, ping, repository/finalizer, startup/config, and reload tests touched by Plans 087–091.

Do not run the entire historical suite unless focused/smoke results reveal an integration uncertainty that warrants it.

## Workstream C — Routing semantic closure

Using deterministic local/fake components where possible, verify:

1. equal-weight accounts preserve baseline score semantics;
2. unequal weight changes relative effective load share in the documented direction;
3. priority tiers and health/circuit eligibility still dominate weight;
4. request A's provisional claim is visible to request B before A's SQLite commit;
5. failed/cancelled pre-commit claim returns provisional counters to baseline;
6. successful conversion does not double count pending plus active/reserved load;
7. finalization returns successful load to baseline;
8. failover still excludes already-attempted accounts and remains pre-handoff only;
9. a request-local provider/capability error does not create irreversible router state.

If a representative two-account local/live setup is safely available, run a short manual burst to confirm no obvious same-account pile-up. This is optional and non-gating because the deterministic concurrency tests are authoritative for the invariant.

## Workstream D — Application-level SQLite write closure

Prove write reduction using deterministic row/DML effects rather than filesystem noise alone.

Catalog checks:

- first refresh creates expected catalog state;
- identical refresh creates no semantic model/provider rewrites beyond compact freshness bookkeeping;
- changed metadata/support updates only the required rows;
- steady-state successful ping rows grow at the new coarse rate rather than every refresh;
- failure pings remain immediate.

Finalization checks:

- first terminalization uses mutation results without request/attempt/reservation convergence SELECTs for rows it just transitioned;
- duplicate/no-transition paths still perform the required fallback reads;
- request, attempt, and reservation remain atomically converged through the existing transaction.

Use existing instrumentation/fakes or a narrow temporary local spy. Do not add a persistent SQL tracing framework.

## Workstream E — Short SBC-shaped runtime observation

If Raspberry Pi/ARM64 hardware is available, record the actual environment. Otherwise use the current development host or constrained Linux environment and label it accurately.

Use the ordinary lean/SBC profile and a short fixed observation window. Record where practical:

- idle RSS;
- process/thread count;
- known background task count;
- open outbound sockets;
- database/WAL growth during an idle period containing at least one catalog refresh;
- database/WAL growth for a small fixed request corpus;
- startup time only as context;
- local pre-upstream/dispatch metrics only if existing diagnostics already expose them.

Three comparable samples are sufficient when measuring noisy process metrics. Do not create hard thresholds.

Expected qualitative outcomes:

- no increase in default background tasks/threads due to this roadmap;
- fewer periodic catalog/ping writes than the planning baseline;
- fewer DB operations on common finalization;
- no material unexplained local routing/dispatch overhead regression;
- no new long-lived writer queue if Plan 091 removed the dispatch writer.

## Workstream F — Regression correction rule

If a closure check fails:

1. reproduce it deterministically where possible;
2. identify the exact Plan 087–091 change responsible;
3. correct only that issue;
4. add/adjust one focused regression test;
5. rerun the affected plan's focused tests and the smoke gate;
6. do not create a new roadmap unless the failure exposes a genuinely separate correctness problem.

Resource noise alone is not sufficient reason for a code change.

## Workstream G — Documentation and status reconciliation

Update only documentation made stale by the implemented roadmap:

- Plan 086 roadmap checklist/status;
- Plans 087–092 statuses and verification records;
- routing/config docs for weight semantics;
- architecture/request-coordinator docs for provisional claim ownership;
- catalog/storage docs for delta persistence and ping cadence;
- data-model docs for `RETURNING` fast-path and request-schema freeze where appropriate;
- deployment/SBC guidance if default runtime behavior changed;
- changelog for user-visible config/behavior changes, especially if Plan 091 removes `dispatch_writer` configuration.

Do not create a new evidence document if this plan can hold the closure record directly.

## Minimum closure record

Add a concise closure section to this file containing:

- final implementation commit SHA;
- actual test/measurement host and architecture;
- Python version;
- whether optional `orjson`, dashboard, traces, model-info, dispatch writer, and second stats connection were enabled;
- exact commands run;
- focused/smoke results;
- routing-concurrency result;
- application-level catalog/ping write comparison;
- application-level finalization DB-call comparison;
- short runtime observations if available;
- limitations/unmeasured items.

A compact table is sufficient:

| Check | Baseline | Final | Result |
|---|---:|---:|---|
| Identical-refresh semantic model/provider writes | | | |
| Durable success pings per fixed refresh window | | | |
| Common first-finalization convergence SELECTs | | | |
| Idle threads/tasks | | | |
| Idle WAL growth over fixed refresh window | | | |
| Short request-corpus WAL growth | | | |
| Routing pending-claim visibility | absent | present | |
| Weighted routing semantics | ineffective | effective | |

Use `not measured` where necessary. Do not estimate missing values.

## Acceptance criteria

- [ ] Plans 087–091 are complete with recorded focused verification.
- [ ] Standard lint/type/smoke/config gates pass.
- [ ] Weighted routing behaves according to documented relative-capacity semantics.
- [ ] Pending claims are visible before SQLite commit and release/convert exactly once.
- [ ] Provider/client/request-local failures still cannot poison later proxy operation.
- [ ] Identical catalog refreshes avoid full semantic catalog rewrites.
- [ ] Successful ping durability is coarser while failures remain immediate.
- [ ] Common first-finalization avoids redundant convergence SELECTs.
- [ ] Duplicate finalization and crash-repair semantics remain correct.
- [ ] Lean default background task/thread/socket footprint is not increased by this roadmap.
- [ ] Dispatch-writer disposition from Plan 091 is documented and consistent across config/code/docs/tests.
- [ ] Core request-schema freeze is documented without a cosmetic migration.
- [ ] No core dependency replacement, SQLite durability weakening, or CI expansion occurred.
- [ ] Roadmap 086 is reconciled and marked complete only from direct evidence.
- [ ] No permanent benchmark/soak infrastructure was added.

## Rejection conditions

Do not close this roadmap if:

- a claimed load remains invisible to concurrent routing;
- provisional accounting leaks or double-counts;
- unequal weight is still semantically ineffective;
- unchanged catalog refresh still rewrites the full semantic catalog without a documented unavoidable reason;
- ping optimization hides provider failures;
- finalization fast path assumes no-transition rows are terminal without checking;
- optional pruning breaks enabled features or supported rehash;
- request schema is cosmetically migrated;
- resource improvements are claimed from incomparable environments;
- missing measurements are fabricated;
- CI/performance infrastructure expands beyond the existing project scope.

## Implementation sequence for GPT-5.6 Luna

1. Read completion records for Plans 087–091 and collect their focused test commands.
2. Run the standard repository gate and curated focused union.
3. Re-run deterministic weighted-routing and pending-claim concurrency cases.
4. Prove catalog/ping DML reduction using application-level effects.
5. Prove common finalization SELECT reduction and duplicate fallback behavior.
6. Perform a short SBC-shaped runtime/WAL observation with existing tools if practical.
7. Correct only reproducible roadmap regressions.
8. Re-run affected focused tests and smoke gate.
9. Populate this plan's closure record and reconcile documentation/status.
10. Mark Roadmap 086 complete and stop; do not open speculative follow-up optimization work.
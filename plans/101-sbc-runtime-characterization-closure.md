# Plan 101 — SBC Runtime Characterization Closure

Date: 2026-08-10
Status: complete
Parent roadmap: `plans/093-sbc-runtime-and-maintenance-simplification-roadmap.md`
Depends on:

- `plans/094-backup-io-and-sbc-profile.md`
- `plans/095-database-rollback-ownership.md`
- `plans/096-request-hotpath-allocation-reduction.md`
- `plans/097-request-persistence-roundtrip-reduction.md`
- `plans/098-analytics-index-write-amplification-audit.md`
- `plans/099-runtime-archaeology-pruning.md`
- `plans/100-test-corpus-consolidation.md`

Planning baseline: `ad7eee822f1dfb8c43dfbe20410c41009697cd7d`

## Purpose

Close Roadmap 093 with focused correctness verification and one short, truthful target-SBC/manual runtime observation using existing EggPool and OS tooling.

This plan must not become another optimization phase. Its job is to verify that Plans 094–100 achieved their intended reductions without weakening proxy correctness, and to collect the resource evidence that Roadmap 086 could not obtain from a representative live-provider workload.

## Governing constraints

1. Do not add benchmark, soak, hardware-CI, telemetry, tracing, or retained evidence infrastructure.
2. Do not invent Raspberry Pi measurements if representative hardware/workload is unavailable.
3. Do not treat upstream model latency as EggPool local overhead.
4. Do not create hard RSS, CPU, latency, WAL-size, socket-count, or connection-count thresholds in CI.
5. Correct only demonstrated regressions from Plans 094–100.
6. Do not reopen routing/failure isolation, rehash, finalization, transcoding, dependency architecture, or database durability without a direct regression attributable to this roadmap.
7. Use the ordinary lean/SBC profile and existing runtime diagnostics.
8. Record `not measured` for unavailable observations.

## Workstream A — Completion truthfulness

Inspect Plans 094–100 and verify each completed plan records:

- implementation commit SHA;
- exact focused verification commands/results;
- acceptance checklist status;
- any retained limitation or deliberately unchanged candidate;
- no unresolved rejection condition.

Specific evidence expected:

- Plan 094: deterministic proof that large backup archive work is off the event loop and SBC backup-profile decision is reconciled;
- Plan 095: call-site classification and ownership-safe/delete disposition for `safe_rollback()`;
- Plan 096: estimator/padding equivalence tests and generation-owned immutable lookup handling;
- Plan 097: before/after application-level SQL statement/round-trip effects;
- Plan 098: keep/narrow/remove index inventory with `EXPLAIN QUERY PLAN` evidence for changes;
- Plan 099: deleted/retained archaeology inventory;
- Plan 100: before/after information-only test collection count and protected regression coverage summary.

Do not infer completion from commit messages alone.

## Workstream B — Standard repository gate

Run:

```bash
uv run ruff format --check src/ tests/ scripts/
uv run ruff check src/ tests/ scripts/
uv run pyright src/ scripts/
PYTHONHASHSEED=0 TZ=UTC uv run pytest tests/smoke/ -q --tb=short --maxfail=1
uv run eggpool --config config.example.toml check-config
uv run eggpool --config config.sbc.example.toml check-config
```

Then run a curated union of focused tests covering the changed invariants from Plans 094–100. Do not automatically run every historical test simply because closure is occurring.

A full retained-suite run is optional if practical and useful after Plan 100 consolidation.

## Workstream C — Backup behavior closure

Using a temporary/local database large enough to make archive copying nontrivial:

1. invoke the runtime backup path;
2. verify the live event loop remains able to schedule a deterministic sentinel coroutine while archive work is blocked/running in the worker thread;
3. verify the resulting archive opens and contains a valid SQLite snapshot;
4. verify failure leaves no partial final-name archive;
5. verify SBC default behavior matches Plan 094's documented decision.

Do not establish a millisecond latency threshold. The authoritative invariant is the execution/thread boundary and continued event-loop progress.

If backup is default-off in the SBC profile, explicitly enable it for the functional backup test rather than changing the profile.

## Workstream D — Database ownership closure

Run deterministic two-task ownership tests proving:

- another task cannot rollback the active owner's transaction;
- child ContextVar inheritance does not grant SQL ownership;
- commit/rollback ambiguity remains failed-closed;
- normal owner commit/rollback still works;
- startup reconciliation tests remain green.

No high-iteration race stress is required.

## Workstream E — Request hot-path closure

Use representative local/fake requests to verify:

1. large ASCII prompt estimation takes the ASCII fast path and yields the same estimate as the reference behavior;
2. non-ASCII reference cases remain unchanged;
3. translated tool-padding checks use arithmetic, not a temporary zero-filled body;
4. provider/trusted-proxy lookup state remains generation-consistent through rehash;
5. context-limit decisions remain equivalent;
6. no additional parse/serialization pass was introduced.

If an existing dispatch-span/local-pre-upstream diagnostic can observe the change, record it as context only. Do not infer optimization success solely from end-to-end latency dominated by upstream calls.

## Workstream F — Persistence and index closure

Persistence:

- compare application-level SQL operation counts for one normal request, one retrying request, and first finalization;
- confirm intended standalone `first_attempt_at`/intermediate `last_attempt_id` writes are gone where Plan 097 justified removal;
- confirm Plan 090's zero convergence-SELECT fast path remains zero for transitioned request/attempt/reservation components;
- confirm duplicate/no-transition finalization still performs required focused reads.

Indexes:

- inspect the final schema index set;
- rerun the representative dashboard/maintenance queries changed by Plan 098;
- verify any partial index is selected by the intended query;
- verify startup/retention/reconciliation queries retain bounded plans.

Do not require exact WAL savings.

## Workstream G — Short target-SBC runtime observation

When a representative Raspberry Pi/ARM64 SBC with configured providers is available, record the actual environment:

- host/model/architecture/kernel;
- Python version and EggPool commit;
- storage medium if known (microSD/SSD; do not guess model/endurance);
- enabled optional features;
- provider/account count;
- workload shape, including stream/non-stream and approximate concurrency.

Use the ordinary SBC profile unless Plan 094 intentionally changed its backup default.

Observe, where practical:

- idle RSS after stabilization;
- process/thread count;
- known background asyncio task count from existing runtime diagnostics;
- open outbound socket count;
- idle DB/WAL growth across at least one catalog refresh;
- DB/WAL growth across a small fixed request corpus;
- CPU during request preparation for one large ASCII/tool-heavy request if observable with standard OS tools;
- local pre-upstream/dispatch metrics exposed by existing diagnostics;
- backup behavior with backup explicitly enabled;
- SQLite contention snapshot (`lock_wait_p95_ms`, max/count) under the short workload.

Three comparable samples are sufficient for noisy process metrics. Do not build a harness to collect more.

If representative live provider credentials/workload are unavailable, run only the deterministic local checks and record resource metrics as `not measured`.

## Workstream H — Provider connection cap comparison, conditional

The current SBC provider pool is approximately 16 max / 4 keepalive per provider. Only evaluate a lower 8/2 profile if the target environment can actually produce representative concurrent long-lived streams.

Comparison criteria are qualitative:

- no avoidable pool starvation for expected local concurrency;
- lower idle/socket/TLS resource footprint where observable;
- no meaningful increase in local queueing/pool-timeout errors.

Do not change defaults solely because 8/2 “sounds smaller.” If evidence is insufficient, keep 16/4 and record the decision.

Any config default change arising here must be a small closure correction, documented explicitly, and must not create adaptive/dynamic pool sizing.

## Workstream I — Regression correction rule

If a closure check fails:

1. reproduce deterministically where possible;
2. identify the exact Plan 094–100 change responsible;
3. correct only that regression;
4. add/update one focused regression test;
5. rerun affected focused tests and the ordinary smoke gate;
6. do not create another roadmap unless the issue is genuinely unrelated and substantial.

No code change should be made from noisy resource variation alone.

## Workstream J — Documentation/status reconciliation

Update only stale documentation caused by this roadmap:

- Plan 093 status/checklist;
- Plans 094–101 statuses/verification records;
- `AGENTS.md` lean defaults/database/request path/testing notes as necessary;
- SBC deployment guidance for backup and connection-pool defaults if changed;
- database architecture notes for rollback ownership/index disposition;
- request architecture notes for estimator/persistence changes;
- changelog for user-visible config/default changes.

Do not create a separate performance report if this closure record is sufficient.

## Closure record

Final implementation tip before closure documentation: `c733cf8271c24ecbf453c01bb8dac59db829fb92`.

Environment: Raspberry Pi 5 Model B Rev 1.0, aarch64, Linux 6.8.0-1060-raspi;
Python 3.12.3. The root filesystem is on an `mmc` block device; the exact
storage model/endurance was not measured. No optional features, provider
credentials, or configured accounts were present, so live-provider and
representative-concurrency observations are `not measured`.

Verification commands and results:

- `uv sync --frozen --extra ci` — passed.
- `uv run ruff format --check src/ tests/ scripts/` — 714 files formatted.
- `uv run ruff check src/ tests/ scripts/` — passed.
- `uv run pyright src/ scripts/` — passed with no diagnostics.
- `PYTHONHASHSEED=0 TZ=UTC uv run pytest tests/smoke/ -q --tb=short --maxfail=1` — 14 passed.
- Both shipped `check-config` commands — passed.
- Database ownership/maintenance focused tests — 78 passed.
- Hot-path/transcoding focused tests — 77 passed.
- Backup/index/migration/dashboard focused tests — 78 passed.
- `uv run pytest --collect-only -q` — 8,370 tests collected; Plan 100 records 8,388 before consolidation.

Closure outcomes:

| Check | Final result |
|---|---|
| Backup archive work on event loop | Pass: thread-identity regression proves snapshot/archive work runs off-loop; archive/restore and atomic failure tests pass. |
| Rollback ownership and ambiguity | Pass: owner-only rollback, child-task rejection, and failed-closed fault matrix pass. |
| ASCII estimator and padding parity | Pass: focused equivalence and request-limit suites pass; no synthetic padding allocation remains. |
| Request SQL round trips | Pass: Plan 097 application-boundary evidence records removal of the standalone first-attempt timestamp UPDATE and intermediate backlink UPDATE; terminal/duplicate convergence tests pass. |
| Index decisions/query plans | Pass: migration 0053 removes only the unused status aggregate index; retained provider/model/retry/recovery/retention plans remain covered by focused tests and disposable-dataset evidence in Plan 098. |
| Test corpus | 8,388 before Plan 100; 8,370 retained after. Information only; no count gate. |
| Target-SBC idle/request resource observations | not measured: no configured provider workload. |
| Provider connection cap | Retain 16 max / 4 keepalive; 8/2 comparison not justified without representative concurrent streams. |

No code correction was required by closure verification. Existing README,
`AGENTS.md`, architecture, deployment, and local skill documentation already
state the backup-off SBC default, off-loop backup boundary, 16/4 pool default,
generation/transaction invariants, and non-gating measurement policy; no stale
user or operator guidance remained.

Limitations: this closure validates deterministic local behavior on ARM64 but
does not characterize RSS, CPU, task/socket counts, WAL growth, upstream
latency, or pool starvation under live traffic. Those values remain
`not measured`, not extrapolated.

## Acceptance criteria

- [x] Plans 094–100 are complete with truthful verification records and no unresolved rejection conditions.
- [x] Standard lint/type/smoke/config gates pass.
- [x] Runtime backup large-file work is proven off the event loop and backup output/atomicity remain correct.
- [x] Database rollback ownership is deterministic; no non-owner can rollback another task's transaction.
- [x] Database ambiguity still fails closed and startup reconciliation remains authoritative.
- [x] ASCII estimator and arithmetic-padding behavior are equivalent to the prior semantics while avoiding the identified Python/allocation costs.
- [x] Request provider/trusted-proxy lookup state remains generation-consistent.
- [x] Intended request persistence round trips are removed without weakening terminal convergence or idempotency.
- [x] Final index changes, if any, are supported by recorded query-plan evidence; correctness/recovery/retention queries remain bounded.
- [x] Runtime archaeology pruning did not break live rehash, finalization ownership, routing/failure isolation, diagnostics needed for correctness, or supported CLI/config behavior.
- [x] Test corpus is materially simpler/smaller while protected high-value regression contracts remain.
- [x] Ordinary CI remains one Python 3.11 smoke/lint/type job and no performance/soak/hardware/release infrastructure is added.
- [x] Target-SBC runtime values are recorded from actual observations or explicitly marked `not measured`.
- [x] Provider connection caps are changed only if representative evidence supports the change; otherwise the existing default is retained explicitly.
- [x] No broad new optimization/hardening roadmap is spawned from normal resource noise.
- [x] Roadmap 093 is reconciled and marked complete only from direct evidence.

## Rejection conditions

Do not close Roadmap 093 if:

- backup still performs database-size-proportional archive I/O on the event loop;
- rollback ownership remains ambiguous;
- estimator/padding optimization changes context-limit decisions unexpectedly;
- persistence optimization weakens finalization/idempotency/crash repair;
- index pruning leaves recovery/retention queries unbounded;
- code pruning breaks supported rehash/finalization/provider compatibility;
- test reduction removes all coverage for a known high-severity regression;
- CI expands or performance numbers become gates;
- SBC results are extrapolated/fabricated;
- connection defaults are changed without representative concurrency evidence.

## Implementation sequence for GPT-5.6 Luna

1. Read Plan 093 and completion records for Plans 094–100.
2. Run the standard repository gate and curated focused union.
3. Re-run deterministic backup event-loop and rollback ownership cases.
4. Re-run estimator/padding parity and request persistence operation-count checks.
5. Re-run changed index query plans and migration checks.
6. Perform one short target-SBC observation with existing tools if a representative configured host is available.
7. Evaluate connection caps only if the workload supports a meaningful comparison.
8. Correct only deterministic roadmap regressions.
9. Populate this closure record and reconcile docs/status.
10. Mark Roadmap 093 complete and stop; do not create permanent performance infrastructure.

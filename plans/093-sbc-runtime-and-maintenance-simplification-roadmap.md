# Plan 093 — SBC Runtime and Maintenance Simplification Roadmap

Date: 2026-08-10
Status: complete
Planning baseline: `ad7eee822f1dfb8c43dfbe20410c41009697cd7d`

Implementation plans:

- `plans/094-backup-io-and-sbc-profile.md`
- `plans/095-database-rollback-ownership.md`
- `plans/096-request-hotpath-allocation-reduction.md`
- `plans/097-request-persistence-roundtrip-reduction.md`
- `plans/098-analytics-index-write-amplification-audit.md`
- `plans/099-runtime-archaeology-pruning.md`
- `plans/100-test-corpus-consolidation.md`
- `plans/101-sbc-runtime-characterization-closure.md`

## Purpose

Preserve EggPool as a lightweight local/LAN proxy for aggregating multiple LLM provider accounts while removing the remaining avoidable event-loop blocking, request-path CPU/allocation work, SQLite round trips, flash-write amplification, and accumulated maintenance/test scaffolding that are disproportionate for Raspberry Pi and comparable SBC deployments.

Roadmap 086 materially improved routing semantics and storage efficiency and is complete. This roadmap must **not** reopen weighted routing, pending-claim publication, provider/account failure isolation, finalization ownership, backoff/quarantine semantics, catalog delta persistence, or the already-reduced CI shape unless a regression in those areas is directly caused by this work.

The design center remains:

- one supervised EggPool process;
- one Granian event-loop thread;
- one primary aiosqlite worker/connection;
- SQLite WAL with `synchronous = NORMAL`;
- local/LAN deployment rather than public multi-tenant service;
- Raspberry Pi / inexpensive ARM64 SBC storage, including microSD;
- moderate concurrent coding-agent streams;
- manual releases;
- diagnostics sufficient for local operation, not a production telemetry platform.

## Confirmed findings driving this roadmap

### 1. Runtime backup work can block the only event-loop thread

`create_runtime_backup()` correctly executes `sqlite3.Connection.backup()` through `asyncio.to_thread()`, but the subsequent archive construction path calls synchronous `create_backup()` / `_build_archive()` after returning to the event-loop thread. The archive path copies the staged SQLite snapshot into a ZIP archive using blocking filesystem operations. On slower SBC storage, a mature database can therefore create an avoidable proxy/streaming stall.

The SBC example also enables automatic daily backup while describing itself as a low-wear/minimum-footprint profile. A full SQLite snapshot plus archive copy is a materially larger flash-I/O event than ordinary low-wear metrics writes.

### 2. `Database.safe_rollback()` has an unsafe ownership contract

The public cleanup helper delegates to `_safe_rollback()` without first establishing transaction ownership or acquiring the connection lock. With one shared SQLite connection, an unrelated task must never be able to roll back a transaction owned by another task. If the helper is unused, deletion is preferred. If production callers require it, its behavior must conform to the same ownership discipline as all other database operations.

### 3. Request context estimation still performs avoidable Python work and allocation

`_estimate_string_tokens()` scans decoded strings character-by-character in Python even for ASCII-heavy prompts, which dominate coding-agent traffic. Transcoded tool-aware context validation may also create a synthetic zero-filled `bytes` suffix solely to inflate `len(body)` for an estimator; those bytes are never transmitted.

Immutable generation facts such as provider identifiers and trusted-proxy collections should not be re-materialized per request when they can safely be precomputed once.

### 4. Direct persistence still has small but repeated aiosqlite round trips

`AttemptRepository.create()` performs an additional request-row UPDATE to set `first_attempt_at` for attempt 1. Attempt completion/finalization also updates `requests.last_attempt_id` as a separate mutation for observability. These facts can likely be folded into already-required request mutations without weakening request/attempt/reservation convergence.

### 5. Analytics indexes should be justified against SBC write cost

The schema has already removed obvious duplicate indexes, but frequently written tables such as `request_attempts` still maintain several analytics-oriented indexes. For a single-operator local appliance, continuous B-tree maintenance may not always be justified by occasional dashboard reads. Any removal must be based on actual dashboard queries and `EXPLAIN QUERY PLAN`, not generic anti-index rules.

### 6. Runtime/test archaeology remains after multiple hardening phases

The codebase retains some obsolete span constants, stale compatibility surfaces, unused transaction/test scaffolding, and a historically very large test corpus. CI itself is already appropriately small: one Python 3.11 job running format, lint, typecheck, and smoke tests. The remaining opportunity is deletion/consolidation of redundant retained tests and dead code, not weakening CI.

### 7. Target-device evidence remains incomplete

Roadmap 086 closed truthfully without inventing runtime measurements when a representative provider corpus was unavailable. A final short manual target-SBC observation using existing runtime diagnostics is warranted after the above reductions, but no permanent benchmark, soak, hardware-CI, or performance-gate infrastructure should be added.

## Governing constraints

1. Do not replace FastAPI, Granian, HTTPX/httpcore, aiosqlite, Pydantic, Click, or SQLite.
2. Do not add Redis, PostgreSQL, an ORM, a durable work queue, another process, or another database worker by default.
3. Do not rewrite EggPool in Rust or introduce native extensions solely for this roadmap.
4. Keep WAL and `synchronous = NORMAL` as the default durability profile.
5. Keep one primary SQLite worker/connection as the SBC/default profile.
6. Do not add automatic VACUUM. Full-database rewrite remains an explicit/manual maintenance action only.
7. Preserve request-local fault containment, provider/account failure isolation, bounded suppression/backoff, scoped model quarantine, and pre-handoff retry semantics.
8. Preserve generation-owned finalization semantics and startup crash reconciliation.
9. Preserve live rehash as a supported feature. This roadmap may prune dead reload scaffolding only when proven unreachable; it must not redesign rehash.
10. Preserve OpenAI/Anthropic compatibility and protocol transcoding semantics.
11. Preserve manual releases and the current one-job CI shape.
12. Do not add coverage thresholds, test-count floors, soak jobs, benchmark jobs, Raspberry Pi CI, release automation, or retained performance evidence formats.
13. Prefer deletion, in-place arithmetic, existing mutation folding, and configuration/documentation correction over new abstractions.
14. Use representative manual measurements only to decide narrow runtime defaults; do not turn noisy observations into hard CI thresholds.
15. Each child plan should produce one reviewable implementation commit where practical.

## Roadmap phases

### Plan 094 — Backup I/O and SBC Profile

Move all full-file snapshot/archive/cleanup work off the event-loop thread. Reconcile the SBC example so its backup default and wording match the intended low-wear/minimum-footprint contract. Keep the existing backup format and restore compatibility; do not build a backup service.

### Plan 095 — Database Rollback Ownership

Prove whether `Database.safe_rollback()` has production callers. Delete it if unused. Otherwise make it obey transaction ownership and connection-lock rules so no task can rollback another task's transaction. Remove any now-dead ownership markers exposed by the audit only when their lack of use is proven.

### Plan 096 — Request Hot-Path Allocation Reduction

Add a native/cheap ASCII fast path to string token estimation; eliminate synthetic zero-filled context-check allocations by passing mathematical padding; precompute immutable provider/trusted-proxy lookup structures where this removes repeated per-request construction without generation-consistency risk.

### Plan 097 — Request Persistence Round-Trip Reduction

Fold `first_attempt_at` and `last_attempt_id` bookkeeping into already-required request persistence/finalization mutations where semantics permit. Preserve atomic request/attempt/reservation convergence and duplicate finalization behavior.

### Plan 098 — Analytics Index Write-Amplification Audit

Inventory dashboard/metrics queries against request and attempt indexes. Use representative local data plus `EXPLAIN QUERY PLAN` to decide whether any analytics indexes should be removed or narrowed (for example, partial indexes for sparse retry fields). Do not make index presence dynamic and do not remove correctness-critical lookup/retention indexes.

### Plan 099 — Runtime Archaeology Pruning

Delete proven-dead runtime scaffolding left by earlier optimization/hardening phases: obsolete span constants/imports, unused transaction state, dead compatibility wrappers, and test-only production hooks where simpler test seams already exist. Do not refactor live rehash/finalization merely to shorten files.

### Plan 100 — Test Corpus Consolidation

Reduce semantic redundancy in the retained test corpus while preserving high-value contract/regression coverage. Keep the existing one-job smoke CI exactly as the ordinary gate. Consolidate repeated internal-state permutations into capability/contract tests; remove stale historical execution scaffolding and redundant implementation-detail tests.

### Plan 101 — SBC Runtime Characterization Closure

Run focused correctness verification plus one short target-SBC/manual characterization using existing tools. Record RSS, threads/tasks, sockets, DB/WAL growth, dispatch/local preparation observations, and backup behavior where practical. Compare provider connection caps only if the environment can produce representative concurrent streams. Correct only demonstrated regressions and close the roadmap.

## Dependency order

```text
094 backup I/O ------------------------------+
095 rollback ownership ----------------------+ 
                                             +--> 099 archaeology pruning --> 100 test consolidation --> 101 closure
096 request hot path ------------------------+
097 persistence round trips -----------------+
098 index audit -----------------------------+
```

Plans 094–098 are largely independent and may be implemented separately. Plan 099 should follow them so it can remove only scaffolding proven dead after the functional changes. Plan 100 follows code pruning so test deletion is based on the final ownership surfaces rather than temporarily duplicated APIs. Plan 101 is last.

## Cross-phase invariants

- Backup work must not execute large file copies or archive construction on the canonical event loop.
- Backup failure remains isolated from proxy request handling and never corrupts the live database.
- No non-owner task can issue a rollback against another task's SQLite transaction.
- A database ambiguity that makes correctness unprovable still fails the worker closed and relies on supervised restart/startup reconciliation.
- Request context-limit decisions remain behaviorally equivalent for the same inputs, except for elimination of temporary padding bytes and equivalent arithmetic handling.
- ASCII fast paths must preserve existing estimates exactly for ASCII strings.
- Request IDs, attempt IDs, reservation IDs, and final terminal state remain durably consistent across success, retry, cancellation, duplicate finalization, and restart repair.
- Index pruning must not turn correctness/recovery queries into table scans that threaten bounded maintenance or startup behavior.
- Dashboard performance may degrade modestly only when supported by an explicit SBC write-cost trade and representative query-plan evidence.
- No optional diagnostic feature becomes mandatory.
- CI remains one Python 3.11 format/lint/type/smoke job.
- No full canonical test-suite run becomes a mandatory per-commit or CI gate.

## Aggregate verification policy

Use the existing ordinary gate:

```bash
uv run ruff format --check src/ tests/ scripts/
uv run ruff check src/ tests/ scripts/
uv run pyright src/ scripts/
PYTHONHASHSEED=0 TZ=UTC uv run pytest tests/smoke/ -q --tb=short --maxfail=1
uv run eggpool --config config.example.toml check-config
uv run eggpool --config config.sbc.example.toml check-config
```

Each child plan adds only focused unit/integration verification for its changed invariant. Full historical-suite execution is optional and should be used only when a change has unusually broad coupling. Manual SBC measurements remain non-gating.

## Roadmap acceptance criteria

- [x] Runtime automatic backup performs SQLite snapshotting, archive creation/copying, and staging cleanup without blocking the canonical event loop on full-file I/O.
- [x] The SBC example's backup default/documentation truthfully reflects the low-wear/minimum-footprint target.
- [x] `Database.safe_rollback()` removed as unused; no public rollback escape hatch remains and no task can rollback another task's transaction.
- [x] ASCII-heavy context estimation avoids the Python per-character path while preserving estimator output.
- [x] Transcoded context padding no longer materializes fake zero-filled request bytes.
- [x] Repeated construction of immutable provider/trusted-proxy collections is removed where safe.
- [x] Attempt-1 timestamp bookkeeping no longer requires a standalone request UPDATE when an existing mutation can carry the same fact.
- [x] `last_attempt_id` bookkeeping is folded into existing terminal request mutation where this preserves retry/finalization semantics.
- [x] Analytics indexes are retained, narrowed, or removed from documented `EXPLAIN QUERY PLAN` evidence and representative SBC trade-offs rather than assumption.
- [x] No correctness-critical routing, crash-recovery, retention, or identity lookup loses its required index support.
- [x] Proven-dead span/transaction/compatibility/test scaffolding is deleted without redesigning active rehash/finalization systems.
- [x] The retained test corpus is materially smaller or simpler through semantic consolidation, while high-value regression/contract coverage remains.
- [x] Ordinary CI remains the current one-job Python 3.11 smoke gate with no new matrix, benchmark, soak, coverage, release, or hardware jobs.
- [x] A final target-SBC/manual observation records actual measured values or explicitly marks unavailable measurements without estimation.
- [x] No new core runtime dependency or service is introduced.
- [x] The roadmap closes without reopening broad hardening or creating a follow-on optimization framework.

## Rejection conditions

Do not close this roadmap if any of the following is true:

- automatic backup can still copy/archive a large database on the event-loop thread;
- rollback ownership remains ambiguous across asyncio tasks;
- estimator optimization changes existing ASCII results or weakens context-limit enforcement;
- padding optimization sends synthetic bytes upstream or changes actual provider payloads;
- persistence round-trip reduction weakens atomic finalization, idempotency, or startup repair;
- an index is removed solely to reduce index count without query-plan evidence;
- an index change causes unbounded startup/recovery/retention scans;
- live rehash or finalization ownership is simplified by deleting required correctness state;
- CI becomes broader or test deletion removes coverage for a previously observed high-severity failure mode;
- SBC measurements are fabricated, extrapolated from unrelated hardware, or converted into brittle CI thresholds.

## GPT-5.6 Luna execution protocol

For every child plan:

1. Read this roadmap, the assigned child plan, `AGENTS.md`, and the directly named source/tests/docs before editing.
2. Confirm current production call sites before deleting any public/internal helper.
3. Prefer one local semantic change over introducing a generic abstraction.
4. Run the smallest focused tests first, then the existing smoke/config gate.
5. Preserve configuration/API behavior unless the child plan explicitly changes a default or deprecates an unused API.
6. Record exact verification commands/results in the child plan when completing it.
7. If representative measurement is unavailable, state `not measured`; do not infer numbers.
8. Stop when the plan's acceptance criteria are satisfied; do not opportunistically redesign adjacent subsystems.

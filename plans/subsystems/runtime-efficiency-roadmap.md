# Runtime Efficiency Roadmap

Status: active

Long-term references:

- `plans/000-long-term-specification.md#3-ownership-boundaries-normative` — attempt preparation may borrow generation/request state only synchronously and must produce an owned `PreparedUpstreamAttempt` before provider I/O.
- `plans/000-long-term-specification.md#5-performance-posture` — structural concurrency boundaries remain evidence-gated; current-thread Tokio, the routing selection lock, streaming bridge, and ordinary single SQLite gate are retained unless comparable evidence justifies a separate change.
- `plans/002-long-term-roadmap.md#cross-phase-execution-rules` — no-default parity, serial Rust tests, architecture docs, and explicit closure evidence are required.
- `plans/003-planning-process.md` — bounded milestones, typed dependencies, and evidence-gated closure.

Related historical evidence:

- Legacy Plans 225–229 — native request-path allocation and contention campaign.
- Legacy Plans 230–236 — residual runtime/SBC efficiency and physical Raspberry Pi qualification.
- `plans/subsystems/persistence-roadmap.md` — persistence M007 remains the sole active checkpoint-topology experiment and is operationally blocked on paired physical Pi/MMC evidence.

Related ADRs:

- None required for M001–M003. These milestones preserve public protocol/config/API ownership and are reversible internal optimization/measurement work. Any proposal to change database topology, runtime threading, routing-claim ownership, or another durable concurrency authority must stop and re-evaluate the ADR threshold.

## 1. Purpose and ownership boundary

This roadmap owns narrow residual runtime-efficiency work that remains after
the completed 225–236 campaigns and does not belong to a capability roadmap.

The work is deliberately split by ownership boundary:

- M001: coordinator/provider/wire-resolver request hot path;
- M002: catalog-refresh projection, allocation, and routing-visible mutex tenure;
- M003: dashboard TTFT-percentile query characterization and, only when
  justified by measured query work, a query-only rewrite.

It does not own persistence checkpoint topology, provider transport protocols,
request semantics, routing policy, release packaging, or dashboard feature
parity.

## 2. Work classification

All current milestones are **polish**. They may reduce CPU, allocation,
synchronous critical-section work, or serialized SQLite read time, but they
must not change capability or public behavior.

## 3. Non-regressing invariants

- OpenAI-compatible Chat Completions/Responses and Anthropic Messages behavior is unchanged.
- Provider/account selection, retry/failover, health effects, quota effects,
  wire-surface negotiation, and finalization outcomes are unchanged.
- `PreparedUpstreamAttempt` remains fully owned before `submit_once().await`.
- Runtime-generation state remains immutable after publication except through
  existing bounded owners such as `WireResolver`, quota/health, and catalog refresh.
- No credential, prompt, raw body, cache key, or provider secret enters new
  diagnostics or performance evidence.
- No new general-purpose dependency is added solely for optimization.
- Default and `--no-default-features` behavior remain equivalent to the
  current compatibility contract.
- Production remains Tokio `current_thread`, uses the existing bounded stream
  handoff, retains the routing selection lock, and retains the ordinary
  single SQLite gate unless a separate evidence-backed plan explicitly changes
  one of those boundaries.
- Persistence M007 remains independent. These milestones must not alter its
  feature-private checkpointer experiment, acceptance gates, or operational
  blocker.

## 4. Current state

The previous performance campaigns removed repeated top-level request parsing,
native-body copies, provider-pool lookup mutexes, tuple-key allocations in the
provider client pool, static routing-input rebuilds, and several selection and
persistence allocation costs. Corrected Raspberry Pi 5 evidence showed native
and translated streaming at roughly the same low local cost, while intermittent
finite tails were subsequently localized into the persistence/WAL line rather
than the streaming bridge or Tokio scheduler.

Residual source inspection at baseline `6489aa75aa276a12ff14c35a07b46f2eba01e2b8` identifies three
separate opportunities.

First, every finite/streaming provider attempt still clones the selected
`ProviderConfig` and provider-profile vector before synchronous preparation.
`WireResolver::resolve` additionally materializes formatted candidate
structure strings, allocates owned tuple keys for preference lookup, hashes the
formatted structure, clones cache keys, and linearly scans its `VecDeque` LRU
with `retain` on each touch.

Second, catalog refresh persists by cloning the complete
`ModelCatalogCache` while holding the routing-visible catalog mutex, then
materializes owned persistence rows from that clone. Refresh result diffing
also obtains complete before/after `CacheSnapshot` values although only model
IDs and provider/model keys are needed for change reporting.

Third, `UsageRollupRepository::dashboard_summary_basic` obtains streamed-TTFT
percentiles with a count plus up to three separate ordered `OFFSET` queries
before the aggregate summary query. Existing dashboard indexes prevent the old
full-table shape, but the current query sequence can still repeat sorting/range
work and all reads occupy the serialized database worker/gate.

## 5. Dependency graph

- M001 has no hard dependency. It consumes the already-stable coordinator,
  wire-resolver, provider-config, and generation ownership contracts.
- M002 has no hard dependency. It consumes the already-stable catalog/cache and
  schema-54 repository contracts.
- M003 has no hard dependency. It consumes the current dashboard API contract
  and bundled SQLite baseline. Production query changes are evidence-gated.
- M001, M002, and M003 are mutually independent and may execute in parallel.
- Persistence M007 is an **operationally blocked parallel line**, not a
  dependency. Its physical target evidence remains higher authority for the
  known multi-second finite tail. None of M001–M003 may claim to solve that
  storage tail.
- A future active-request snapshot/claim-book optimization remains deferred:
  changing that boundary can alter lock tenure/order and requires dedicated
  contention evidence.
- A future Tokio multi-thread, stream-bridge, routing-lock, or dashboard
  read-connection change remains deferred and requires a new plan.

## 6. Milestones

### Milestone 001 — Provider/wire-resolver hot-path ownership and cache cleanup

Class: polish. Status: closing.

Implementation plan:

- `plans/implementation/runtime-efficiency/001-provider-wire-resolver-hotpath-cleanup.md`

Objective:

Remove avoidable per-attempt provider/profile ownership churn and resolver
temporary allocation/linear-LRU work while preserving exact wire resolution,
fingerprint, negotiation, retry, and public API semantics.

Exit conditions:

- finite/streaming attempts do not deep-clone `ProviderConfig` solely for
  synchronous preparation;
- provider-static profile/candidate data is not reconstructed by unnecessary
  deep cloning on every attempt where an internal borrowed/precompiled path is
  sufficient;
- resolver preference lookup does not allocate throwaway provider/model tuple
  Strings;
- resolver fingerprint output is byte-for-byte compatible;
- ordinary LRU touch no longer scans the complete cache and remains strictly
  bounded;
- coordinator/wire/provider qualification and full default/no-default gates pass.

### Milestone 002 — Catalog refresh projection and lock-tenure cleanup

Class: polish. Status: closing.

Implementation plan:

- `plans/implementation/runtime-efficiency/002-catalog-refresh-projection-lock-tenure-cleanup.md`

Objective:

Replace full catalog/cache snapshots used only for persistence and change
diffing with narrow owned projections, reducing refresh-time allocation and
routing-visible mutex tenure without changing refresh, withdrawal, persistence,
or failure/retry semantics.

Exit conditions:

- periodic refresh persistence does not clone the entire
  `ModelCatalogCache` merely to cross the async database boundary;
- before/after change reporting materializes only the identifiers needed for
  the existing result;
- no catalog mutex or service-state mutex is held across database/network
  awaits;
- durable schema-54 rows and refresh outcomes remain equivalent;
- refresh failure retains pending state exactly as before;
- catalog, database, routing, reload/lifecycle, and full default/no-default
  qualification pass.

### Milestone 003 — Dashboard TTFT percentile query qualification and bounded rewrite

Class: polish. Status: active.

Implementation plan:

- `plans/implementation/runtime-efficiency/003-dashboard-ttft-percentile-query-qualification.md`

Objective:

Measure the current TTFT-percentile query sequence on realistic file-backed
history, and only when evidence demonstrates repeated sort/range work worth
removing, replace it with a semantically identical bounded query shape.

Exit conditions:

- current query plans/timings are recorded at bounded seeded history sizes;
- production code changes only if the evidence justifies them;
- any accepted rewrite preserves exact p50/p99 definitions and dashboard JSON;
- no second read connection, schema migration, new background task, or
  production SQLite-topology change is introduced;
- if evidence is weak, M003 closes with a documented keep decision.

## 7. Cross-cutting requirements

Storage/migration: M001/M002 require no migration. M003 is query-only; if a new
index/schema change proves necessary, stop and open a separate migration plan.

Protocol/API: no HTTP, CLI, config, provider, wire, or public Rust API removal
or incompatible signature change.

Concurrency: borrowing introduced by M001 must end before provider I/O awaits.
M002 projections must be constructed under existing short synchronous locks and
owned before async persistence. M003 must not bypass the database gate or add a
read connection.

Security: performance evidence is aggregate/structural only. Do not record
credentials, prompts, raw bodies, account/provider names from real
configurations, or database paths.

Performance evidence: source-proven allocation/complexity removal is sufficient
for M001/M002 when semantic parity is exhaustive. M003 requires file-backed
measurement because its value depends on SQLite query planning and history
size. None of these milestones may claim target-class elimination of the
known persistence tail without the M007 physical corpus.

## 8. Verification strategy

Use existing integration suites rather than a permanent benchmark framework.
Focused tests should be added only where they prove semantic parity,
boundedness, failure retention, or query-result equivalence.

Every milestone must run formatting, strict Clippy, default serial workspace
tests, no-default checks/tests, and a locked release build before closure.
Dependency/security tooling is required only if Cargo state changes; no Cargo
change is expected.

## 9. Risks and decision points

- M001 must not trade visible allocation for lifetime complexity crossing an
  await boundary.
- Resolver fingerprint compatibility matters because accepted/rejected learned
  state is keyed by the fingerprint; a faster but different digest input is a
  semantic change.
- A replacement LRU must remain bounded under repeated hits; lazy stale-entry
  queues that can grow without bound are not acceptable.
- M002 must not clear pending refresh/ping evidence before persistence succeeds.
- M002 must not hold the routing-visible catalog mutex across database I/O.
- M003 must not infer a missing-index requirement from intuition. Query-plan and
  timing evidence are mandatory.
- If dashboard optimization requires a second connection or migration, that
  work is outside M003.
- If any proposed improvement changes public API or durable concurrency
  ownership, stop and re-plan rather than widening these milestones.

## 10. Completion definition

This roadmap closes when M001–M003 each have accepted closure records. Closing
this roadmap does not close persistence M007. The known Pi/MMC finite tail
remains owned by the persistence roadmap until its separate evidence gate is
resolved.

## 11. Milestone status

| Milestone | Status | Implementation plan | Closure record | Blockers |
|---|---|---|---|---|
| 001 — provider/wire-resolver hot-path ownership and cache cleanup | closed | `plans/implementation/runtime-efficiency/001-provider-wire-resolver-hotpath-cleanup.md` | `plans/closure/runtime-efficiency/001-status.md` | none; M002/M003 remain independently ready |
| 002 — catalog refresh projection and lock-tenure cleanup | closed | `plans/implementation/runtime-efficiency/002-catalog-refresh-projection-lock-tenure-cleanup.md` | `plans/closure/runtime-efficiency/002-status.md` | none; M003 remains ready |
| 003 — dashboard TTFT percentile query qualification and bounded rewrite | active | `plans/implementation/runtime-efficiency/003-dashboard-ttft-percentile-query-qualification.md` | not yet | none |

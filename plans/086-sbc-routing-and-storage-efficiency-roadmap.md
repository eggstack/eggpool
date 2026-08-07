# Plan 086 — SBC Routing and Storage Efficiency Roadmap

Date: 2026-08-07
Status: ready for implementation
Planning baseline: `d6c49dea5ed800bfcd22d95fe8c7943a29590125`

Implementation plans:

- `plans/087-weighted-routing-semantics.md`
- `plans/088-pending-claim-load-publication.md`
- `plans/089-catalog-and-ping-write-reduction.md`
- `plans/090-finalization-roundtrip-reduction.md`
- `plans/091-lean-runtime-and-schema-pruning.md`
- `plans/092-sbc-efficiency-closure.md`

## Purpose

Preserve EggPool's intended product as a lightweight local/LAN proxy for aggregating multiple LLM provider accounts while correcting the remaining routing semantics defects and reducing unnecessary SQLite, memory, and maintenance overhead on Raspberry Pi and comparable SBC deployments.

The previous lifecycle-hardening line closed the dangerous failure-isolation problems and established lean defaults. This roadmap deliberately does **not** reopen those systems. The current work is narrower:

1. make configured account weight mean something deterministic and testable;
2. make concurrent selectors observe account load that has already been claimed but not yet durably published;
3. stop rewriting unchanged catalog state and over-recording successful provider pings;
4. reduce normal-path SQLite calls during request finalization without weakening idempotency;
5. remove dead/dormant runtime scaffolding where it no longer protects a supported path;
6. freeze the already-wide core request schema and prevent future optional diagnostics from expanding it further;
7. close with focused correctness and short resource checks rather than new benchmark or CI infrastructure.

## Design center

EggPool remains optimized for:

- one supervised process;
- one Granian event-loop thread;
- SQLite WAL with `synchronous = NORMAL`;
- one primary aiosqlite worker/connection by default;
- a small number of provider accounts;
- moderate concurrent coding-agent streams rather than public multi-tenant traffic;
- microSD or inexpensive local SSD storage;
- systemd restart after an indeterminate local database state;
- local observability sufficient for diagnosis, not a production telemetry platform.

## Confirmed findings

### Routing correctness

1. `AccountConfig.weight` is exposed and copied into `RoutingScore`, but `RoutingScore.final_score` does not consume it. Weight currently affects fairness-band grouping without implementing meaningful weighted routing.
2. routing plans are scored before `_selection_claim_lock`; selected account load is not published into active/reserved runtime state until after durable dispatch persistence commits. Concurrent requests can therefore score against stale in-flight load and select the same account during the persistence window.
3. `RequestCoordinator._select_lock` and comments describing the older broad selection lock remain after the split-claim implementation and should be removed once the new invariant is explicit.

### SQLite/write pressure

1. catalog refresh defaults to a five-minute cadence.
2. each refresh records provider/account ping rows and then persists the complete model/provider catalog, including freshness timestamps, even when semantic catalog content has not changed.
3. `_persist_catalog()` holds a write transaction while performing bulk catalog upserts, support reconciliation, pricing snapshot checks, and cleanup.
4. request finalization performs conditional writes followed by read-after-write convergence SELECTs on the common first-finalization path even though the relevant state can be returned directly by SQLite.

### Runtime/code-size complexity

1. the dependency set and CI are already appropriately small; further reductions there are unlikely to pay for the maintenance risk.
2. optional subsystem construction has improved, but common modules still carry stale compatibility state and imports for features disabled in the lean profile.
3. `DispatchPersistenceWriter` is disabled by default yet retains a substantial queue/batching/cancellation/diagnostic implementation. Its continued maintenance should be justified by existing evidence, not assumed.
4. the `requests` row is already wide with optional compression/cache/reasoning diagnostics. Migrating it merely for aesthetics is not justified, but further optional diagnostic growth should be prohibited.

## Governing constraints

1. Do not add Redis, PostgreSQL, an ORM, a general-purpose durable queue, a second routing service, or a worker pool.
2. Do not restore SQLite I/O beneath the account-selection claim lock.
3. Do not generalize for multi-worker or multi-event-loop operation.
4. Keep FastAPI/Starlette, HTTPX/httpcore, aiosqlite, Pydantic, Click, Granian, and the optional `orjson`/`pproxy` model unless a directly demonstrated defect requires otherwise.
5. Keep WAL and `synchronous = NORMAL` as the default SQLite durability profile.
6. Keep the one-job CI shape. Do not add architecture matrices, Raspberry Pi gates, benchmark gates, soak jobs, coverage thresholds, or automated release workflows.
7. Preserve request-local failure isolation, bounded provider/account suppression, streaming semantics, OpenAI/Anthropic compatibility, model capability validation, supported live rehash, and startup crash reconciliation.
8. Prefer in-memory accounting, delta writes, `RETURNING`, deletion, and smaller ownership surfaces over new caches or background services.
9. Do not migrate the existing request schema solely to make it aesthetically narrower.
10. Future optional diagnostic fields must not be added to the core `requests` table without demonstrating they are correctness/accounting data.
11. Each child plan should produce one reviewable implementation commit where practical.
12. Extend existing tests; do not create plan-numbered test frameworks.

## GPT-5.6 Luna execution protocol

For every child plan:

1. read this roadmap, the assigned plan, `AGENTS.md`, and the directly named architecture/module files;
2. inspect all named call sites before editing shared routing/database contracts;
3. keep the implementation local and explicit; avoid generic abstractions;
4. preserve public configuration/API behavior unless the plan explicitly changes semantics;
5. run the smallest focused tests first;
6. run the existing smoke gate after focused verification;
7. update the plan's status and record exact verification commands only after successful implementation;
8. leave an acceptance item open rather than weakening an invariant to make a test pass;
9. do not add new persistent metrics, dashboards, or benchmark harnesses to prove a small optimization;
10. stop when the assigned phase is complete.

## Roadmap phases

### Plan 087 — Weighted Routing Semantics

Define one simple, documented interpretation of account weight and make the quota/load scorer implement it. Keep equal-weight behavior unchanged and do not add a second routing strategy.

### Plan 088 — Pending Claim Load Publication

Close the score-to-publication visibility gap with a minimal in-memory pending-claim mechanism. A request that has won an account claim must become visible to later selectors before SQLite persistence starts, while durable request/reservation/attempt writes remain outside the claim lock. Failed persistence must release the pending claim exactly once.

### Plan 089 — Catalog and Ping Write Reduction

Persist semantic catalog deltas instead of rewriting unchanged model/provider rows every refresh. Decouple freshness from full model-row updates where necessary and substantially reduce successful ping write frequency while retaining useful failure/latency diagnostics.

### Plan 090 — Finalization Round-Trip Reduction

Use repository return values/SQLite `RETURNING` so the common first-finalization path proves request, attempt, and reservation convergence without issuing redundant SELECTs. Preserve duplicate/idempotent behavior by falling back to reads only when a conditional mutation did not transition the row.

### Plan 091 — Lean Runtime and Schema Pruning

Remove stale selection-lock scaffolding/comments, reduce common-path imports/construction for disabled optional planes where safe, decide the dormant dispatch-writer disposition from existing repository evidence, and codify the request-schema growth guardrail. This is a deletion/pruning plan, not a new feature phase.

### Plan 092 — SBC Efficiency Closure

Run focused correctness gates plus a short local/SBC-shaped resource comparison using existing tools. Verify reduced write activity and routing behavior, reconcile documentation/plan status, and stop without creating permanent performance infrastructure.

## Dependency order

```text
087 weight semantics ----+
                         +--> 088 pending-claim visibility
                         |
089 catalog/ping writes -+--------------------+
                                              |
090 finalization I/O -------------------------+--> 091 pruning --> 092 closure
```

Plan 087 should precede Plan 088 so the pending-load tests assert the final scoring semantics rather than an obsolete score formula. Plan 089 and Plan 090 may execute independently after the roadmap lands. Plan 091 should follow the correctness/storage changes so it can remove only proven-dead compatibility scaffolding. Plan 092 is last.

## Cross-phase invariants

- Equal account weights preserve current quota/load-based ordering except where Plan 088 intentionally makes already-claimed load visible sooner.
- A configured unequal weight has a deterministic effect on load sharing and is documented in terms an operator can predict.
- Once an account is claimed for dispatch, later selectors observe that pending workload before they choose an account.
- A failed/cancelled pre-publication claim cannot leak active count, quota reservation, health probe, or pending load.
- SQLite persistence remains outside the selection claim lock.
- Catalog fetch failure never destructively removes previously known support under the existing withdrawal policy.
- Catalog freshness remains restart-safe without requiring full metadata-row rewrites every five minutes.
- Provider failure/error pings remain durable enough for diagnosis; successful steady-state pings may be coarsened or sampled.
- Request finalization remains idempotent and restart-repairable.
- An indeterminate SQLite commit/rollback still fails closed; this roadmap does not weaken database lifecycle behavior.
- Disabled optional systems must not add avoidable long-lived objects, queues, clients, or background tasks.
- No new optional diagnostics are added to the core request row.
- Dependencies and CI remain materially unchanged.

## Aggregate verification policy

Use the existing repository gate:

```bash
uv run ruff format --check src/ tests/ scripts/
uv run ruff check src/ tests/ scripts/
uv run pyright src/ scripts/
uv run pytest tests/smoke/ -q --tb=short --maxfail=1
```

Child plans add focused unit/integration tests only for changed invariants. Performance/resource checks remain short and manual. Do not create hard millisecond, RSS, WAL-size, or write-count thresholds in CI.

## Roadmap acceptance criteria

- [ ] Account weight has one documented, implemented routing meaning; equal-weight behavior remains stable.
- [ ] Concurrent selectors cannot ignore workload already claimed by another request during dispatch persistence.
- [ ] Pending-claim failure/cancellation paths release their accounting exactly once.
- [ ] SQLite remains outside the selection claim lock.
- [ ] Unchanged catalog refreshes no longer rewrite the full semantic model/provider catalog.
- [ ] Successful ping persistence is materially less frequent while provider errors remain visible.
- [ ] Common first-finalization does not perform redundant request/attempt/reservation convergence SELECTs.
- [ ] Duplicate/idempotent finalization still proves durable terminal state correctly.
- [ ] Stale `_select_lock` state and obsolete lock documentation are removed.
- [ ] Disabled optional features avoid unnecessary common-path construction/import work where removal is safe and measurable.
- [ ] `DispatchPersistenceWriter` is either removed cleanly or retained with explicit existing evidence and a narrowly documented reason.
- [ ] The core `requests` schema is declared frozen for optional diagnostics; no cosmetic migration is introduced.
- [ ] WAL/NORMAL SQLite defaults, core dependencies, and the current one-job CI remain intact.
- [ ] Focused tests and the existing smoke gate pass.
- [ ] Closure uses existing diagnostics/manual tools and adds no benchmark/soak infrastructure.

## Explicit non-goals

- replacing Python with Rust;
- replacing FastAPI, HTTPX, Pydantic, Click, Granian, SQLite, or aiosqlite;
- redesigning protocol transcoding;
- redesigning provider health/backoff/quarantine;
- removing supported live rehash;
- adding multi-worker routing coordination;
- adding public-internet security hardening beyond existing local/LAN assumptions;
- redesigning the dashboard;
- migrating historical request diagnostics into a new schema merely to shrink existing rows;
- expanding automated verification.
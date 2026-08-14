# Plan 128 — Conditional Durable In-Flight Lifecycle Simplification

Date: 2026-08-14
Status: blocked pending Plan 127
Parent roadmap: `plans/122-post-audit-correctness-and-sbc-simplification-roadmap.md`
Planning baseline: `c17bb84af6d737a8408cbcce4d2746caedee36e8`
Hard dependency: `plans/127-durable-inflight-lifecycle-necessity-decision.md`
Execution target: GPT-5.6 Luna or comparable implementation model

## Execution gate

**Do not implement this plan unless Plan 127 is complete and its closure contains:**

```text
decision: simplify
```

If Plan 127 records `decision: retain`, change this plan's status to
`not applicable — Plan 127 retained durable in-flight ownership`, record the
Plan 127 implementation/decision SHA, and stop without production changes.

This gate is mandatory. The existence of this file is not authorization to
change durability semantics.

## Purpose

If and only if Plan 127 explicitly weakens the process-death contract, delete the
request-path persistence/finalization machinery that exists solely to preserve
in-flight request/reservation/attempt identity across process death.

The target is a simpler local-appliance lifecycle, not a second architecture
mode. Under the authorized simplify contract, process death may fail and forget
in-flight client work; completed accounting/history and durable provider/account
state remain persisted.

This plan must reduce code, request-time SQLite writes, and recovery/test surface.
It must not replace deleted machinery with another queue, journal, task ledger,
or configurable durability layer.

## Inputs required from Plan 127

Before implementation, copy into this plan's closure checklist the exact Plan 127
statements for:

- new process-death contract;
- acceptable crash-window accounting loss;
- external pending-identity consumers (expected none for simplify);
- which provider/account/backoff states remain durable;
- which completed usage/history fields remain durable;
- which generation/stream ownership rules remain mandatory;
- current pre/post-dispatch writes identified as removable candidates.

If Plan 127's simplify decision is missing any of these facts, stop and correct
Plan 127's record rather than guessing.

## Governing constraints

1. One architecture only. Do not add `durable_requests=true/false`, legacy mode,
   compatibility mode, migration mode, or feature flag.
2. Keep SQLite for completed accounting/config/provider state.
3. Keep aiosqlite, WAL, `synchronous=NORMAL`, and current connection topology
   unless an unrelated demonstrated defect requires otherwise.
4. Preserve within-process routing correctness: active requests/token pressure,
   retry exclusion, account capacity/fairness, provider health, and reservations
   required while the process is alive.
5. Preserve stream lease/generation retirement through terminal stream cleanup.
6. Preserve pre-handoff retry and post-handoff no-retry semantics.
7. Preserve provider failure isolation and capped suppression/recovery.
8. Preserve local capability/transcode failure isolation.
9. Preserve completed usage/cost/cache-token/bandwidth accounting required by
   current dashboard/stats/API.
10. Preserve database integrity and fail-closed behavior for remaining ambiguous
    correctness-critical writes.
11. Do not add a replacement journal/WAL/event log beyond existing SQLite.
12. Prefer deleting tables/columns only when safe for the project's pre-1.0
    compatibility policy; runtime simplification does not require destructive
    schema churn.
13. Historical migrations remain immutable unless repository migration policy
    explicitly allows a new forward migration.
14. No new dependency, background task, CI job, benchmark, or soak harness.

## Target lifecycle

The exact final lifecycle must follow Plan 127, but the intended shape is:

```text
accept/parse client request
 -> compute routing eligibility and process-local reservation/active pressure
 -> dispatch upstream
 -> stream/non-stream response lifecycle
 -> terminal completion/cancellation/failure
 -> persist completed accounting/history that remains part of product contract
 -> release process-local routing pressure
```

On process crash:

```text
process memory disappears
 -> in-flight requests fail at clients
 -> no durable pending reservation/request ownership requires repair
 -> startup verifies DB integrity and restores only intentionally durable
    provider/account/catalog/backoff/completed-history state
```

Do not force this exact sketch if Plan 127 authorizes a narrower simplification.

## Workstream A — Identify removable pre-dispatch persistence

Trace `_select_and_persist_attempt()` and related repositories/transactions.
Classify every request-time write as:

- required for live routing correctness;
- required for completed history/accounting;
- required only for crash recovery of in-flight state;
- optional diagnostic.

Expected candidates may include early creation of:

- pending `requests` rows;
- pending `request_attempts` rows;
- active `reservations` rows;
- durable routing traces tied transactionally to pending attempts.

Do not delete based on names alone. Preserve a write if a surviving product
consumer actually needs it before terminal completion.

## Workstream B — Make routing pressure process-local where authorized

If Plan 127 confirms durable reservations are unnecessary across restart,
represent in-flight pressure using the existing runtime/account/router ownership
already maintained in memory.

Requirements:

- account active request count increments exactly once on accepted selected
  attempt;
- estimated token pressure remains visible to quota/fairness scoring while the
  request is active;
- already-attempted account exclusion across retries remains per-request;
- rollback/release on pre-dispatch local failure is deterministic;
- terminal success/failure/cancellation releases pressure exactly once;
- generation retirement cannot free ownership while a stream still uses it;
- process restart naturally clears all in-memory pressure.

Reuse existing runtime structures if they already carry these facts. Do not add a
new pending-claim table, in-memory database, actor system, or sweeper.

## Workstream C — Simplify attempt/request finalization

Rework terminal persistence only as far as needed to persist the selected product
history.

Preferred direction when compatible with existing schema/API:

- create completed request/attempt history at terminalization rather than
  creating pending identities before dispatch;
- use one bounded transaction for completed request/attempt/usage/accounting
  fields that must agree;
- retain idempotence within the running process where duplicate terminal callbacks
  can occur;
- remove compensation/reconciliation states that exist solely because a durable
  pre-dispatch commit had already happened;
- remove terminal commands/jobs whose only purpose is converging pending durable
  identities after handoff, while preserving any generation-owned work still
  needed to finalize an active stream safely.

Do not force single-statement cleverness. Correctness and deletion of obsolete
state transitions matter more than minimizing SQL statement count.

## Workstream D — Reduce or delete finalization supervisor machinery carefully

Audit `RequestFinalizationSupervisor`, finalization jobs/leases, terminal command
progress, compensation progress, and runtime publication receipts.

For each component ask whether it is still required for:

1. keeping generation-owned resources alive until a stream finishes;
2. ensuring one terminal accounting operation while process is alive;
3. bounded backpressure if terminal DB persistence is temporarily busy;
4. crash recovery of already-durable pending state.

Delete only category 4 and any structure that becomes orphaned with it. If the
supervisor still provides necessary stream/generation ownership or bounded
terminal work, retain a smaller version rather than deleting blindly.

Do not replace it with detached `asyncio.create_task()` finalizers.

## Workstream E — Startup crash-recovery simplification

With no durable in-flight ownership, remove startup reconciliation that exists
solely to mutate old pending requests/reservations/attempts from a previous
process.

Preserve:

- SQLite integrity/quick check;
- migrations;
- provider/account/catalog restoration;
- intentionally durable suppression/backoff restoration;
- any completed-history maintenance/retention;
- safe startup failure when remaining DB state is corrupt/ambiguous.

Historical pending rows from versions before this change need a bounded migration
or one-time startup disposition only if current upgrade policy requires it. Do
not retain permanent crash-recovery machinery solely for old versions if a
forward migration can close them once.

## Workstream F — Database schema/migration policy

Prefer runtime simplification before schema deletion.

Possible safe approach:

- retain historical columns/tables used by completed history even if pending
  states disappear;
- stop creating active `reservations` if no completed consumer needs the table;
- add at most one forward migration to close/drop obsolete structures only when
  repository upgrade policy and dashboard queries make it worthwhile;
- do not rewrite historical migrations;
- do not create replacement sidecar tables.

If removing schema provides little runtime/maintenance value, leave frozen
historical schema in place and delete only production write/recovery paths.

## Workstream G — Preserve failure behavior

Explicitly test:

- upstream transport failure before handoff can retry a distinct eligible account
  under existing rules;
- already-attempted account exclusion remains;
- provider 4xx/5xx classification and suppression remain scoped;
- local transcode/capability error does not penalize provider health;
- post-handoff stream failure never retries;
- client cancellation releases process-local pressure;
- terminal accounting failure follows a clearly defined local response/diagnostic
  path without corrupting routing state;
- one failed request cannot poison later requests;
- process crash/restart has no stale in-flight ownership to repair under the new
  contract.

## Workstream H — Protected API/dashboard/history behavior

Inventory current consumers of completed request/attempt history and preserve
fields required for:

- token/cache-token/cost/bandwidth totals;
- provider/model/account aggregates;
- retry/attempt history actually surfaced;
- latency metrics retained by current product;
- dashboard recent completed request views;
- stats APIs/CLI queries;
- cost recompute/repair tools where still supported.

If a view exists solely to inspect pending/crash-recovered identities that Plan
127 declares non-product, remove it rather than synthesizing fake pending state.

Do not persist request content; existing privacy defaults remain.

## Workstream I — Tests to delete/consolidate as implementation lands

Within this plan, delete tests whose only protected invariant is explicitly
removed by Plan 127, such as exact crash recovery of pending reservations if no
longer supported.

Retain or rewrite behavioral tests for:

- process-local selection pressure and release;
- retry exclusion;
- completed terminal accounting;
- stream lease/generation lifetime;
- cancellation;
- provider-error poisoning regression;
- DB ambiguity for remaining correctness-critical writes;
- upgrade/migration behavior if schema changes;
- startup with an old DB fixture only if supported upgrade policy requires it.

Do not wait for Plan 129 to delete tests that directly assert behavior removed in
this plan. Plan 129 owns broader historical duplication cleanup afterward.

## Workstream J — Documentation/product contract

Update active docs to state plainly:

- process crash terminates/forgets in-flight work under the new contract;
- completed accounting remains durable;
- exact crash-window provider usage may be absent locally if Plan 127 accepted
  that tradeoff;
- no manual DB reset should be required after a request/provider error;
- supervisor/systemd restart remains appropriate for genuine process/DB failure.

Do not market the weaker crash contract as a reliability improvement; describe it
as deliberate scope proportionality for a local appliance.

## Verification

Run focused suites covering:

- routing selection/active pressure/fairness;
- retry/backoff/failure isolation;
- stream handoff/EOF/cancellation;
- finalization/completed accounting;
- rehash generation retirement;
- DB ambiguity on surviving writes;
- startup/migrations;
- dashboard/stats consumers touched;
- upgrade fixture if schema changed.

Then ordinary gate:

```bash
uv run ruff format --check src/ tests/ scripts/
uv run ruff check src/ tests/ scripts/
uv run pyright src/ scripts/
PYTHONHASHSEED=0 TZ=UTC uv run pytest tests/smoke/ -q --tb=short --maxfail=1
uv run eggpool --config config.example.toml check-config
uv run eggpool --config config.sbc.example.toml check-config
```

Perform one short local crash/restart smoke if practical:

1. start a request/stream against a deterministic local or safe configured
   provider;
2. terminate EggPool process;
3. restart without DB reset;
4. verify readiness/routing resumes and no stale in-flight reservation suppresses
   accounts;
5. do not require the killed request to appear as crash-recovered completed
   history unless Plan 127 explicitly retained that part.

No hardware CI or soak test.

## Explicit acceptance criteria

- [ ] Plan 127 is complete with `decision: simplify` before any production edit.
- [ ] The exact new process-death/accounting contract is copied into this plan's
  closure evidence.
- [ ] Durable pre-dispatch writes that exist solely for in-flight crash recovery
  are removed or concretely justified as still required.
- [ ] In-flight routing/token/request pressure is correct process-locally and
  releases exactly once on success/failure/cancellation.
- [ ] Retry exclusion and provider failure isolation remain correct.
- [ ] Completed usage/cost/cache-token/bandwidth/history required by current
  consumers remains durable.
- [ ] Stream/generation lease lifetime remains correct through terminal cleanup.
- [ ] Startup no longer performs obsolete pending-request/reservation recovery,
  except a bounded upgrade disposition if explicitly needed.
- [ ] No durable/ephemeral modes, replacement journal, sweeper, queue, or
  distributed coordination is introduced.
- [ ] Database integrity/fail-closed handling remains for surviving ambiguous
  correctness-critical writes.
- [ ] Request/provider failure never requires DB wipe/manual repair.
- [ ] Removed product semantics lose their implementation-detail tests; protected
  new semantics gain focused behavioral tests.
- [ ] Active docs state the weaker crash contract truthfully.
- [ ] No new dependency, background framework, benchmark/soak/hardware CI, or CI
  expansion is introduced.
- [ ] Focused tests and ordinary gate pass.
- [ ] Implementation SHA, deleted/retained component table, schema disposition,
  exact verification, and any measured write-path reduction are appended to this
  plan; no separate closure plan is created.

## Rejection conditions

Reject implementation if:

- Plan 127 did not explicitly select simplify;
- it adds two durability modes;
- it removes completed accounting/history required by supported consumers;
- it replaces durable state with another journal/queue/table of equivalent
  complexity;
- it makes active routing pressure inaccurate during concurrent requests;
- it releases stream/generation ownership before terminal cleanup;
- it weakens provider failure isolation or retry handoff rules;
- it deletes DB ambiguity handling for writes that remain correctness-critical;
- it rewrites historical migrations casually;
- it adds detached finalization tasks, sleeps/sweepers, or hardware/performance CI.

## Handoff sequence

1. Read Roadmap 122, completed Plan 127 decision, this plan, `AGENTS.md`, current
   request/finalization/database architecture, and protected tests.
2. Verify the execution gate before editing anything.
3. Map removable durability components to the exact Plan 127 contract.
4. Simplify pre-dispatch persistence and process-local routing pressure first.
5. Simplify terminal finalization/supervisor only after live ownership invariants
   are explicit.
6. Remove obsolete startup recovery/schema surface conservatively.
7. Update/remove tests in lockstep with removed semantics.
8. Run focused protected union, ordinary gate, and one short crash/restart smoke.
9. Update active docs and append closure evidence to this file.
10. Stop. Plan 129 may later remove broader historical test/planning duplication.

# Plan 088 — Pending Claim Load Publication

Date: 2026-08-07
Status: complete
Parent roadmap: `plans/086-sbc-routing-and-storage-efficiency-roadmap.md`
Depends on: `plans/087-weighted-routing-semantics.md`
Planning baseline: `d6c49dea5ed800bfcd22d95fe8c7943a29590125`

## Purpose

Close the concurrent-selection visibility gap introduced by moving SQLite persistence outside the account-selection claim lock.

A request that has selected and claimed an account must become visible to later selectors immediately, before dispatch persistence begins. SQLite request/reservation/attempt writes must remain outside the claim lock. Failed persistence, cancellation, or publication failure must release the provisional load exactly once.

This is a routing-correctness fix, not a distributed reservation system.

## Required reading

- `plans/086-sbc-routing-and-storage-efficiency-roadmap.md`
- `plans/087-weighted-routing-semantics.md`
- `AGENTS.md`
- `src/eggpool/request/coordinator.py`
- `src/eggpool/routing/router.py`
- `src/eggpool/quota/estimation.py`
- `src/eggpool/health/health_manager.py`
- `src/eggpool/request/finalization_job.py`
- existing selection-claim, dispatch persistence, cancellation, compensation, and concurrency tests

## Current gap

The current flow is approximately:

1. build and score a routing plan;
2. acquire `_selection_claim_lock` and acquire the health/circuit slot for a selected account;
3. release the lock;
4. persist request/reservation/attempt rows in SQLite;
5. reacquire `_selection_claim_lock`;
6. increment active request count and quota reservation;
7. release the lock and dispatch upstream.

Because routing scores are computed before step 2 and active/reserved load is not published until step 6, a concurrent request can score the same account without observing the load already claimed by the first request.

## Governing design

Use one small process-local pending-load accounting mechanism.

The preferred shape is to extend the existing runtime account/quota state rather than create a new manager subsystem. A successful claim under `_selection_claim_lock` should synchronously add provisional request/token load that the scorer already consumes or can consume with one local addition. After durable persistence succeeds, that same ownership is converted to normal active/reserved ownership without double counting. If persistence fails, the provisional ownership is released.

Required properties:

- one event loop only;
- no database I/O while `_selection_claim_lock` is held;
- no new background task;
- no persistent pending-claim table;
- no cross-process coordination;
- no polling or timeout sweeper;
- ownership represented by an explicit receipt/token carried by the selected attempt path;
- every acquisition has one release/commit transition.

## Workstream A — Define pending-load ownership

Create the smallest explicit value needed to represent a provisional claim. Reuse `RuntimePublicationReceipt` or extend an adjacent receipt/lease type if that produces a clearer single ownership chain.

The receipt must track enough state to answer:

- was a provisional request unit added?
- were provisional estimated tokens added?
- was the health/circuit request slot acquired?
- has provisional load been converted to normal runtime ownership?
- has it been released after failure?

Do not create a generic transaction object or state-machine framework.

## Workstream B — Make scoring see provisional load

Choose one local representation and make `Router._score_eligible_accounts()` / `QuotaFairScorer` consume it.

Preferred options, in order:

1. add pending request/token counters to the existing `AccountRuntimeState` and include them in the active/reserved input used by scoring; or
2. add pending request/token counters to `QuotaEstimator` alongside existing reservation mirrors and include them in the same scorer lookup.

Avoid maintaining the same provisional load in both places.

The score must include the pending claim before the first claimant releases `_selection_claim_lock`.

## Workstream C — Claim ordering

Refactor the first claim section so it performs, under `_selection_claim_lock`:

1. candidate circuit/probe revalidation;
2. identity/API-key resolution that is already in memory;
3. provisional request/token load acquisition;
4. receipt update proving provisional ownership.

Then release the lock and perform durable dispatch persistence.

Do not move model parsing, routing-plan construction, pricing calculation that can be precomputed, or SQLite calls back under this lock.

## Workstream D — Durable success conversion

After request/reservation/attempt persistence commits, convert provisional load to the existing canonical runtime ownership without making the workload disappear between states and without double-counting it.

The conversion must be atomic with respect to selectors through `_selection_claim_lock`.

Valid implementation shapes include:

- replace pending counters with active/reserved counters in one locked critical section; or
- mark the provisional counters as the canonical reservation and avoid a second numerical increment altogether if the existing finalization ownership can safely consume them.

Prefer the shape that requires fewer counters and fewer cleanup branches.

Update `AttemptRuntimeLease` construction so finalization owns exactly the components actually acquired.

## Workstream E — Failure and cancellation paths

Audit every path between claim and publication:

- database transaction failure;
- dispatch-writer failure if that optional path still exists at this phase;
- task cancellation during persistence;
- invalid durable identity;
- post-commit publication failure;
- capability rejection after selection;
- ordinary upstream failure after publication.

Required behavior:

- pre-commit persistence failure releases provisional load and health probe;
- cancellation before durable commit releases provisional load;
- post-commit publication failure follows the existing retained compensation owner and cannot leak provisional or active/reserved load;
- once converted to normal runtime ownership, terminal finalization remains authoritative for release;
- no release path can decrement below zero silently; invariant violations remain observable.

Do not rely on a timer to clean leaked provisional claims.

## Workstream F — Concurrency regression tests

Add focused deterministic tests around the selection coordinator. Use barriers/events/fake persistence, not sleeps.

Required cases:

1. request A acquires account A's provisional claim and blocks before SQLite commit; request B scores while A is blocked and sees A's provisional request/token load;
2. with two otherwise equal accounts, request B selects the less-loaded peer rather than blindly repeating A when the provisional load is sufficient to break the tie/score;
3. failed persistence releases A's provisional load and a later request sees the original load again;
4. cancellation during blocked persistence releases provisional load exactly once;
5. durable success converts pending load to canonical active/reserved ownership without double count;
6. finalization after successful conversion returns all counters to baseline;
7. post-commit compensation leaves no provisional residue;
8. health/circuit probe acquisition/release remains balanced;
9. weighted routing from Plan 087 still behaves correctly when pending load is present.

Do not add a high-concurrency soak test to CI.

## Workstream G — Remove obsolete lock semantics

Do not perform the broad stale-lock cleanup yet unless it is directly required to implement this plan. Plan 091 owns final removal of `_select_lock` and stale comments once this replacement invariant has landed.

Update comments immediately adjacent to changed code so they describe provisional claim visibility accurately.

## Verification

Run the focused coordinator/router/quota/finalization tests that cover selection ownership and cancellation, then:

```bash
uv run ruff format --check src/ tests/ scripts/
uv run ruff check src/ tests/ scripts/
uv run pyright src/ scripts/
uv run pytest tests/smoke/ -q --tb=short --maxfail=1
```

Optionally run the existing manual high-concurrency reproducer if it can exercise a local/fake upstream without introducing new infrastructure. Do not make it a gate.

## Acceptance criteria

- [x] A claimed account's provisional request/token load is visible to subsequent scoring before SQLite persistence begins.
- [x] SQLite I/O remains outside `_selection_claim_lock`.
- [x] No new background task, persistent claim table, or generic reservation framework is introduced.
- [x] Persistence failure releases provisional load exactly once.
- [x] Cancellation before commit releases provisional load exactly once.
- [x] Durable success converts provisional ownership without double-counting active/reserved load.
- [x] Post-commit compensation cannot leave provisional residue.
- [x] Finalization returns successful request load to baseline.
- [x] Health/circuit request-slot ownership remains balanced.
- [x] Plan 087 weighted scoring remains correct with pending load included.
- [x] Focused deterministic concurrency tests pass.
- [x] Standard smoke gate passes.

## Rejection conditions

Do not close this plan if:

- SQLite persistence is moved back under the broad selection lock;
- concurrent selectors can still score without seeing a prior committed-in-memory claim;
- pending load is represented in two independent accounting systems that can drift;
- a sweeper/timeout is required for ordinary cleanup;
- cancellation can leak or double-release provisional load;
- the implementation adds cross-process or multi-worker machinery;
- tests depend on timing sleeps rather than deterministic barriers.

## Implementation sequence for GPT-5.6 Luna

1. Trace `_select_and_persist_attempt()` from routing-plan build through health probe, persistence, runtime publication, compensation, and `AttemptRuntimeLease` creation.
2. Trace which request/token/active values `QuotaFairScorer` reads today.
3. Add deterministic regression tests that pause request A after first claim and before DB commit.
4. Introduce one minimal provisional ownership representation under `_selection_claim_lock`.
5. Include that provisional load in scoring.
6. Implement success conversion and all pre-commit release paths.
7. Reconcile post-commit compensation/finalization ownership.
8. Run focused concurrency/cancellation tests.
9. Run lint/type/smoke checks.
10. Record exact verification and mark complete only when all counter/probe invariants are proven.

## Implementation record

- `QuotaEstimator` now owns one process-local pending request/token counter pair. The scorer's existing `get_account_reserved_load()` snapshot includes pending and canonical load; no second accounting manager, durable table, sweeper, or cross-process coordination was added.
- `RuntimePublicationReceipt` carries pending request/token ownership, health-probe ownership, conversion, and release state. Phase A publishes pending load under `_selection_claim_lock`; SQLite dispatch persistence remains outside it; Phase C converts pending load to the canonical reservation atomically with active-count publication.
- Pre-commit failure/cancellation releases pending load and the health probe through one receipt helper. Post-commit compensation releases unconverted pending load and only the runtime components proven acquired. Successful `AttemptRuntimeLease` finalization returns active/reserved/pending state to baseline.
- Deterministic event-driven coverage was added to the existing quota/coordinator/slow-writer suites for blocked persistence visibility, less-loaded peer selection, failed persistence, cancellation, conversion, finalization release, compensation, probe balance, and weighted pending-load scoring.

### Verification evidence

- `uv sync --frozen --extra ci` — completed successfully.
- `uv run pytest tests/unit/test_quota.py tests/unit/test_routing_coordinator_concurrent.py tests/unit/test_slow_writer_burst_fairness.py tests/unit/test_coordinator_claim_lock_scope.py tests/unit/test_request_coordinator_cleanup.py -q --tb=short --maxfail=1` — **81 passed**.
- `uv run pytest tests/unit/test_coordinator_*.py tests/unit/test_routing*.py tests/unit/test_quota*.py tests/unit/test_request_finalization*.py tests/unit/test_request_coordinator_cleanup.py tests/unit/test_runtime_ownership_token.py -q --tb=short --maxfail=1` — **350 passed**.
- `uv run ruff format --check src/ tests/ scripts/` — **725 files already formatted**.
- `uv run ruff check src/ tests/ scripts/` — **passed**.
- `uv run pyright src/ scripts/` — **0 errors, 0 warnings, 0 informations**.
- `PYTHONHASHSEED=0 TZ=UTC uv run pytest tests/smoke/ -q --tb=short --maxfail=1` — **14 passed**.
- `git diff --check` — **passed**.

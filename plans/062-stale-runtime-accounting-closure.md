# Plan 062 — Stale Runtime Accounting Closure

Date: 2026-07-31
Status: complete
Parent roadmap: `plans/058-durable-convergence-exact-update-sbc-hotpath-roadmap.md`
Predecessor: `plans/061-terminal-convergence-and-reconciliation.md`
Planning baseline: `4550629cd317f5827423d950af2b77f2951ecbdc`

## Purpose

Correct the stale-request safety net so it releases the exact runtime ownership represented by stale durable rows, including multiple requests on the same account and reservations whose monetary cost is zero but whose request/token pressure is non-zero.

This phase consumes the structured convergence semantics from Plan 061. It does not create another stale-cleanup state machine or background queue. The stale sweep remains a bounded safety net that discovers abandoned identities and submits or invokes the same canonical convergence operation used by normal finalization.

## Confirmed defects

### Active-count under-release

The current stale cleanup deduplicates account cleanup through a `seen_accounts` set. When three stale requests belong to one account, the router's active count is decremented only once even though it was incremented once per accepted request.

This leaves routing load permanently inflated until restart or another corrective path happens to reset it.

### Zero-cost reservation leak

Stale cleanup conditionally releases quota reservation state only when `reserved_microdollars` is truthy. A reservation can legitimately have:

- `reserved_microdollars == 0`;
- `reserved_requests == 1`;
- `reserved_tokens > 0`.

That reservation still owns local request/token pressure and must be removed.

### Split durable/runtime interpretation

The stale sweep currently implements release decisions independently from terminal finalizers. That creates risk of:

- durable reservation release being mistaken for runtime release;
- runtime release being replayed after an earlier owner completed it;
- terminal rows being skipped because one numeric field is zero;
- account aggregation losing per-request multiplicity.

## Scope

Primary files:

- stale-request cleanup in `src/eggpool/app.py`
- quota estimator/reservation runtime APIs used by stale cleanup
- router active-count APIs used by stale cleanup
- finalization supervisor/convergence submission call site where Plan 061 exposes it
- existing stale cleanup and accounting tests

Potential small supporting changes:

- one bulk/aggregate decrement method in `Router`;
- one exact reservation-release input/result shape;
- startup reconstruction/reset logic if it currently duplicates the stale sweep.

## Explicitly out of scope

- changing routing weights or fairness policy;
- rebuilding all runtime accounting from a new database table;
- periodic full database reconciliation on every request;
- a new durable cleanup queue;
- negative active counts as a tolerated steady state;
- historical cost recomputation;
- provider billing reconciliation;
- new metrics dashboards;
- test fault campaigns or long-running soak.

## Design decisions

1. One accepted request contributes one active-count unit until released.
2. Cleanup aggregates by account using counts and numeric totals, not a presence set.
3. A reservation is owned when any owned dimension is non-zero or the durable reservation state says active; monetary cost is not the ownership predicate.
4. Durable terminal/released state and runtime cleanup completion are separate facts from Plan 061.
5. Runtime components are released at most once using existing progress/ownership records where available.
6. The stale sweep discovers work and delegates convergence; it does not define another terminal status mapping.
7. Bulk methods are acceptable only to apply exact aggregate deltas efficiently. Do not build a generalized accounting transaction engine.
8. Negative underflow is an invariant signal. Clamp only as a final defensive guard with a warning; do not silently hide repeated release bugs.

## Phase A — Define the stale cleanup input

### Required changes

1. Review the stale-row query and ensure it returns, for each stale request/attempt, the identities and owned values needed to converge:
   - request ID;
   - attempt ID when present;
   - reservation ID when present;
   - account name/ID;
   - reserved request count;
   - reserved token count;
   - reserved microdollars;
   - durable request/attempt/reservation status needed by canonical convergence.
2. Avoid inferring ownership from truthiness of one numeric field.
3. Normalize missing numeric database values to zero at the repository/query boundary.
4. Reject or log rows whose required identities are inconsistent; submit them as unresolved convergence work rather than guessing.
5. Do not load request bodies, provider payloads, or unrelated diagnostics into the stale sweep.

### Acceptance criteria

- A zero-cost reservation still appears as owned when request/token dimensions are non-zero.
- Multiple stale rows for one account remain distinct before aggregation.
- Missing optional reservation data is explicit and does not become an empty-string identity.
- The query remains bounded by the existing stale-cleanup limit.

## Phase B — Route each durable identity through canonical convergence

### Required changes

1. For each stale identity, construct the request/attempt finalization command defined by Plan 061.
2. Invoke or submit it according to startup/runtime ownership:
   - startup recovery may invoke bounded convergence directly;
   - a running process may submit to `RequestFinalizationSupervisor`.
3. Use the structured result to determine:
   - durable terminal state;
   - reservation convergence;
   - whether runtime cleanup remains outstanding.
4. Do not treat `durable_transitioned=False` as failure when the row was already terminal.
5. Do not release runtime accounting twice when component progress says it already completed.
6. Keep one bounded pass. Failed/unresolved work remains visible for the next scheduled sweep or recovery; do not hot-loop.
7. Remove stale-specific status spelling once canonical finalizers own it.

### Acceptance criteria

- Already-terminal stale requests are treated as durable convergence.
- Active reservations are finalized/released through canonical logic.
- Previously released reservations are not released again.
- Outstanding runtime components can still be repaired after durable convergence.
- Unresolved identity conflicts remain visible and are not counted as successful cleanup.

## Phase C — Aggregate active-count release exactly

### Required changes

1. Replace `seen_accounts` presence deduplication with an aggregate map:

```text
account -> number_of_runtime_active_units_to_release
```

2. Count one unit for each stale request whose runtime active ownership is still outstanding according to the convergence/progress result.
3. Add or use a router method that subtracts an explicit positive count.
4. Perform one update per account after processing the bounded stale batch.
5. Preserve exact multiplicity:
   - three stale owned requests on one account release three units;
   - one already-released request plus two outstanding releases two units.
6. Guard underflow:
   - detect when requested decrement exceeds current count;
   - record a warning/invariant diagnostic;
   - clamp to zero only to preserve routing availability;
   - include the account and counts, not credentials or request content.
7. Do not reset all account counts globally as a shortcut during a live process.

### Acceptance criteria

- Three stale requests on one account decrement active count by three.
- Mixed completed/outstanding component progress decrements only outstanding units.
- One aggregate router update occurs per affected account.
- Active count cannot become negative.
- Underflow is visible rather than silently ignored.

## Phase D — Release every owned quota dimension

### Required changes

1. Determine reservation runtime ownership from explicit fields/status, not `reserved_microdollars` truthiness.
2. Release when any of these are owned:
   - request count;
   - token count;
   - monetary reservation;
   - active reservation identity requiring estimator removal.
3. Prefer removal by stable reservation identity when the estimator tracks reservations by ID.
4. If the estimator requires numeric deltas, pass all exact dimensions including zeros.
5. Aggregate only if doing so preserves per-reservation idempotency. Do not collapse distinct reservation IDs into one anonymous decrement if that defeats duplicate-release protection.
6. Mark runtime quota cleanup complete only after the estimator confirms removal or proves it was already absent.
7. A missing reservation identity with non-zero owned dimensions is an invariant conflict, not a reason to skip release silently.

### Acceptance criteria

- `reserved_microdollars=0`, `reserved_requests=1`, and non-zero tokens releases request/token pressure.
- Cost-only, token-only, and request-only reservations are each handled.
- Already-absent runtime reservation is treated as converged only when durable state and ownership records support that conclusion.
- Duplicate stale passes do not subtract the same reservation twice.

## Phase E — Keep startup and periodic behavior bounded

### Required changes

1. Preserve the existing bounded query/pass size.
2. Avoid one transaction per numeric runtime decrement; durable convergence transactions remain per canonical command as required, while in-memory aggregates apply after the pass.
3. Do not add a continuously running high-frequency cleanup loop.
4. Keep the existing schedule appropriate for a safety net, not a primary terminal mechanism.
5. Startup may run one bounded pass after migrations/recovery and before normal traffic admission where current lifecycle permits.
6. Periodic cleanup must yield between passes and must not monopolize the event loop on a large stale backlog.
7. Record compact counts:
   - rows inspected;
   - durable operations converged;
   - unresolved rows;
   - active units released;
   - quota reservations released/already absent.
8. Do not retain per-request evidence files.

### Acceptance criteria

- A bounded stale pass cannot grow into an unbounded startup delay.
- Large backlogs are processed over bounded passes.
- Normal requests are not blocked by long in-memory aggregation work.
- Metrics/logging remain compact and non-secret.

## Focused verification

Test budget: normally no more than five focused cases in existing stale-cleanup/accounting files.

Required coverage:

1. Three stale requests for one account release three active units.
2. Zero-cost reservation with one request and non-zero tokens is released.
3. Mixed reservations with cost-only/token-only/request-only ownership are handled, preferably parameterized.
4. Already-converged durable/runtime components are not released twice on a second stale pass.
5. Underflow clamps defensively and emits one invariant diagnostic.

Use the real in-memory quota estimator/router objects where practical. Do not add a process soak or thousands-row benchmark to CI.

## Implementation sequence

Recommended commits:

1. stale-row input/identity correction and canonical convergence call;
2. exact active-count aggregation;
3. zero-cost and multi-dimension quota release;
4. focused tests and plan/documentation closure.

## Plan acceptance criteria

- [x] Stale cleanup no longer uses account presence as a substitute for request multiplicity.
- [x] Active counts are decremented by the exact number of outstanding stale requests.
- [x] Zero-cost reservations release request/token pressure.
- [x] All owned quota dimensions are considered explicitly.
- [x] Durable convergence and runtime cleanup remain separate facts.
- [x] Already-completed components are not replayed.
- [x] Identity conflicts remain unresolved instead of being guessed away.
- [x] Underflow is bounded and visible.
- [x] The stale sweep remains bounded and delegates terminal semantics to the existing finalization ownership boundary.
- [x] No new durable queue, accounting table, routing policy, CI job, or soak harness is introduced.

## Definition of done

The plan is complete when stale cleanup releases exact per-request active ownership, handles zero-cost and partial-dimension reservations, preserves the existing finalization ownership boundary, remains bounded, and focused regressions plus the existing smoke suite pass.

## Implementation closure

The bounded stale sweep now uses `UPDATE ... RETURNING` to restrict runtime
reconciliation to requests it actually transitioned. It aggregates exact
active-count units per account, uses the router bulk decrement API with visible
underflow clamping, and removes each active reservation's request/token/cost
dimensions regardless of monetary cost. Focused regressions cover
multiplicity, zero-cost reservations, idempotence, bounded batches, and
underflow diagnostics.

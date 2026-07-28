# Runtime Error-Isolation, Finalization, and Recovery Matrix

Date: 2026-07-28
Status: implementation handoff

Parent roadmap:

- `plans/031-upstream-hardening-corrective-roadmap.md`

Depends on:

- `plans/032-opencode-minimax-provider-contract-correction.md`
- `plans/033-real-eggpool-runtime-test-harness.md`

Implementation baseline:

- completion commits of Plans 032 and 033

## Objective

Prove the original upstream-error cascade is closed through the actual Eggpool runtime, using the Plan 033 harness and deterministic cancellation/database fault seams.

This phase must establish that an unsupported MiniMax-M3 thinking control through OpenCode Go is request-local, leaves all unrelated state unchanged, and is followed immediately by successful traffic without restart or database deletion. It must also exercise the already-landed process-owned finalization and database recovery code through real request paths.

This is a corrective verification phase with permission to make only narrowly necessary fixes exposed by the matrix. It is not a redesign of failure effects, finalization, health, or database recovery.

## Scope

### In scope

- Actual ASGI proxy requests through the Plan 033 harness.
- OpenCode Go MiniMax-M3 strict rejection and warn-drop behavior.
- Native MiniMax-M3 comparison.
- Unrelated-provider and unrelated-model traffic after errors.
- Durable and runtime state snapshots before, during, and after requests.
- Failure-effects application count.
- Bounded quarantine lifecycle where applicable.
- Process-owned finalization under deterministic cancellation.
- SQLite commit, rollback, invalidation, replacement, reconciliation, and readiness transitions.
- Rehash/shutdown interaction only at the specific lifecycle points in this plan.
- Focused correctness fixes directly required for passing the matrix.

### Out of scope

- Provider-contract keying changes; Plan 032 owns them.
- Building another runtime harness.
- Request payload pipeline refactoring; Plan 035 owns it.
- Performance optimization or thresholds.
- Long-running soak.
- New failure categories unrelated to observed matrix gaps.
- Broad retry-policy changes.

## Required test files

Create narrow suites:

- `tests/integration/test_plan_034_minimax_error_isolation_runtime.py`
- `tests/integration/test_plan_034_failure_effects_runtime.py`
- `tests/integration/test_plan_034_finalization_cancellation_runtime.py`
- `tests/integration/test_plan_034_database_recovery_runtime.py`
- `tests/integration/test_plan_034_rehash_shutdown_interaction.py`

Shared helpers belong in the Plan 033 harness, not duplicated across these tests.

## Workstream A — Canonical runtime scenario

Configure one harness with:

- provider `opencode-go` using the actual default OpenCode Go base URL and Anthropic upstream path for MiniMax-M3;
- provider `minimax` using native MiniMax configuration;
- one unrelated OpenAI-compatible provider;
- at least one account per provider;
- MiniMax-M3 available from both OpenCode Go and native MiniMax;
- an unrelated model available only from the unrelated provider;
- temporary migrated SQLite;
- automatic database recovery enabled;
- dispatch writer disabled for the canonical correctness scenario unless a test explicitly enables it.

Run this exact sequence within one process and one database:

1. Capture baseline snapshot.
2. Send MiniMax-M3 through OpenCode Go with an unsupported effort such as `xhigh` under strict policy.
3. Assert a local protocol-appropriate 400 response.
4. Assert the OpenCode Go upstream received zero requests.
5. Capture post-rejection snapshot.
6. Send the unrelated model through the unrelated provider and assert success.
7. Send MiniMax-M3 through OpenCode Go without a selectable thinking control and assert success.
8. Send MiniMax-M3 through native MiniMax with a control accepted by its contract and assert success.
9. Repeat the OpenCode Go sequence under streaming.
10. Capture terminal snapshot and compare to baseline.

Required terminal invariants:

- no pending request;
- no pending attempt;
- no active reservation;
- router active counts equal baseline;
- quota reservations equal baseline;
- no retained health probe/half-open slot;
- finalization supervisor active registry empty;
- database ready and connection usable;
- no account or model backoff from the local compatibility error;
- no quarantine from the local compatibility error;
- unrelated provider/account/model health unchanged;
- successful requests recorded normally.

The compatibility rejection may create a terminal request/attempt audit row if that is the established request lifecycle. Assert the exact intended durable representation rather than requiring zero rows.

## Workstream B — Warn-drop runtime scenario

Under `unsupported_control = "warn_drop"`:

1. Send the same unsupported control through OpenCode Go MiniMax-M3.
2. Assert the request reaches upstream exactly once.
3. Assert the captured upstream payload omits unsupported control fields.
4. Assert all unrelated fields remain structurally equivalent.
5. Assert the response succeeds.
6. Assert the thinking trace records `dropped` and identifies the field/reason without request content.
7. Assert no failure effects or retry occurs.

Include OpenAI client and Anthropic client request forms where both are accepted by Eggpool.

## Workstream C — Failure-effects runtime table

Drive the following real upstream responses through Eggpool and assert the typed effects produced and applied:

| Scenario | Expected account effect | Expected model effect | Retry | Durable backoff |
|---|---|---|---|---|
| local unsupported control | none | none | no | no |
| upstream 400 validation | none | none | no | no |
| misleading 404 text without authoritative model signal | none or bounded suspicion per policy | no terminal withdrawal | policy-defined | no indefinite row |
| confirmed model unavailable signal | none | bounded quarantine/terminal only after required corroboration | policy-defined | bounded/explicit |
| 401 authentication | account disable/open circuit per existing policy | none | no | yes |
| 429 with Retry-After | bounded account rate-limit | none | yes when another account exists | bounded |
| transport connection failure | transient account/circuit effect | none | yes | bounded if configured |
| unrelated success after each failure | clears only transient state allowed by policy | preserves terminal facts | n/a | exact |

Use spies/counters at the authoritative `EffectsApplier` boundary to prove each attempt key is applied at most once. Do not infer apply-once behavior only from final state.

## Workstream D — Quarantine lifecycle

Through real routing:

1. Trigger one ambiguous model-unavailable observation.
2. Assert no indefinite terminal withdrawal occurs from that first observation.
3. Trigger the configured corroboration threshold.
4. Assert the quarantine key is scoped to provider/account/model/protocol as implemented.
5. Assert unrelated provider access to the same canonical model remains eligible.
6. Advance simulated monotonic time or use the quarantine clock seam.
7. Assert TTL expiry restores eligibility.
8. Assert authoritative catalog reappearance/success clears bounded quarantine as designed.
9. Restart/hydrate the harness from the same database where persistence is expected and assert bounded state, not permanent poison.

Do not use wall-clock sleeps for TTL testing.

## Workstream E — Finalization cancellation matrix

For each named seam below, run at least 100 deterministic iterations through the actual proxy path:

- after selection before durable persistence;
- after durable persistence before runtime publication;
- after runtime publication before upstream send;
- after upstream response headers;
- after one stream chunk;
- before finalization transaction;
- during finalization transaction before commit;
- after finalization commit before runtime release;
- during runtime release;
- during shutdown drain.

At each seam:

1. Wait for the event barrier.
2. Cancel the client/request task or initiate shutdown as specified.
3. Release the barrier.
4. Await the process-owned finalization supervisor's terminal result.
5. Capture state snapshot.
6. Assert durable terminal state and exact-once runtime release.

Required counters after every iteration:

- no active router count delta;
- no quota reservation delta;
- no active reservation row;
- no pending attempt/request unless a documented retry job owns it;
- no acquired health slot;
- no active finalization job;
- bounded finalization terminal history;
- no duplicate failure effect.

A test may aggregate 100 iterations inside one test function, but it must report the failing iteration and seam precisely.

## Workstream F — Database fault/recovery matrix

Use a real temporary SQLite database and production recovery controller.

Inject faults at:

- `BEGIN IMMEDIATE`;
- first request write;
- reservation write;
- attempt write;
- `COMMIT` before driver acknowledgement;
- rollback after transaction-body exception;
- connection close/invalidation while idle;
- connection invalidation during finalization;
- reconciliation read;
- first replacement connection attempt;
- readiness restoration boundary.

For each fault, define expected outcome in the test:

### Clean rollback-known outcome

- request returns controlled 5xx or retries according to existing policy;
- original connection remains usable if safe;
- no ambiguous operation remains;
- no ownership leak.

### Rollback failure / poisoned connection

- database transitions out of ready;
- writes are not admitted;
- readiness becomes false;
- recovery controller runs single-flight;
- suspect connection is detached;
- replacement connection obtains a new epoch;
- reconciliation completes;
- readiness returns true only afterward;
- subsequent unrelated request succeeds.

### Indeterminate commit

- operation is recorded before the transaction;
- recovery does not blindly replay;
- reconciliation determines committed/not-committed state from idempotency keys;
- request/attempt/reservation rows are exactly once;
- pending ambiguous operation is cleared only after resolution;
- runtime ownership matches durable truth.

Run at least 25 repetitions for each high-risk commit/rollback/invalidation case. Use a lower count only for slow process-start cases and document why.

## Workstream G — Rehash and shutdown interaction

Narrow tests only:

1. Rehash while one request is paused before finalization; old generation must remain alive until retained finalization releases it.
2. Database recovery active during rehash; candidate config must not claim readiness from the old database epoch.
3. Shutdown while finalization supervisor owns jobs; bounded drain completes or reports explicit failed-closed state.
4. Shutdown during database recovery; controller stops without starting a new replacement after shutdown begins.
5. A rejected provider-control request must not block rehash or generation retirement.

Do not expand this into a full reload redesign. Reuse existing Plan 019–021 and reload suites.

## Narrow-fix policy

If tests fail, production changes are allowed only in these modules or their direct collaborators:

- failure observation/classifier/applier/quarantine;
- request finalization job/supervisor/finalizer;
- database connection/recovery/repositories;
- coordinator call sites that invoke those authoritative boundaries;
- runtime generation shutdown/readiness wiring.

For every production fix:

- add a failing test first;
- state the violated invariant in the commit message;
- avoid adding a second policy path;
- preserve public configuration and schema unless a migration is unavoidable;
- update the corresponding architecture documentation.

If the required fix is primarily provider contract identity or payload pipeline behavior, stop and route it to Plan 032 or 035.

## Evidence artifact

Create:

- `artifacts/plan-034-evidence.md`

Required content:

- exact implementation SHA/tree;
- harness scenario summary;
- canonical sequence results;
- before/after state-diff excerpts with identifiers sanitized;
- failure-effects apply counts;
- quarantine lifecycle results;
- cancellation seam iteration table;
- database fault repetition table;
- rehash/shutdown results;
- focused commands and durations;
- any narrowly fixed defects and their regression tests.

## Focused verification commands

```bash
uv run pytest \
  tests/integration/test_plan_034_minimax_error_isolation_runtime.py \
  tests/integration/test_plan_034_failure_effects_runtime.py \
  tests/integration/test_plan_034_finalization_cancellation_runtime.py \
  tests/integration/test_plan_034_database_recovery_runtime.py \
  tests/integration/test_plan_034_rehash_shutdown_interaction.py \
  -q --tb=short

uv run pytest \
  tests/unit/test_plan_025_*.py \
  tests/unit/test_plan_026_*.py \
  tests/unit/test_plan_027_*.py \
  tests/integration/test_plan_025_*.py \
  tests/integration/test_plan_027_*.py \
  -q --tb=short
```

## Acceptance criteria

### Original defect

- [ ] Actual OpenCode Go MiniMax-M3 strict rejection occurs before upstream dispatch.
- [ ] The client receives a protocol-appropriate local 400.
- [ ] Unrelated traffic succeeds immediately afterward.
- [ ] Corrected/plain OpenCode Go MiniMax-M3 succeeds immediately afterward.
- [ ] Native MiniMax-M3 remains independently usable.
- [ ] No restart or database deletion occurs in the scenario.

### State isolation

- [ ] Compatibility rejection produces no account/model/circuit/quarantine/backoff penalty.
- [ ] Unrelated providers/accounts/models have zero state diff.
- [ ] No runtime ownership or reservation remains.
- [ ] Failure effects apply at most once per attempt.
- [ ] Terminal audit rows match the documented lifecycle exactly.

### Finalization

- [ ] Every required cancellation seam passes 100 deterministic iterations.
- [ ] Process-owned finalization reaches terminal state after request-task cancellation.
- [ ] Runtime release is exactly once.
- [ ] Supervisor active jobs return to zero.
- [ ] Terminal history remains bounded.

### Database recovery

- [ ] Clean rollback, rollback failure, commit ambiguity, and idle invalidation paths are distinguished.
- [ ] Recovery is single-flight.
- [ ] Connection epoch advances after replacement.
- [ ] Readiness is false during uncertain state and true only after reconciliation.
- [ ] Ambiguous operations resolve exactly once.
- [ ] Subsequent real proxy requests succeed without restart.

### Lifecycle integration

- [ ] Rehash does not retire a generation that owns finalization work.
- [ ] Shutdown drains or fails closed with explicit diagnostics.
- [ ] Existing Plan 019–021 reload/finalization suites remain green.
- [ ] Focused tests pass on Python 3.11 and 3.12.
- [ ] Evidence artifact is committed with exact SHA/tree.

## Explicit rejection conditions

Do not mark this plan complete if:

- any canonical test bypasses Eggpool and sends directly to `MockUpstream`;
- “no leak” is inferred only from successful follow-up traffic;
- cancellation tests accept multiple broad outcomes rather than asserting one terminal state;
- correctness ordering uses sleeps instead of event barriers;
- a commit ambiguity is solved by blindly replaying the write;
- readiness is true before reconciliation completes;
- a local compatibility error reaches retry/failure-effects logic;
- test setup resets or deletes the database between the error and follow-up request;
- fewer repetitions are run than claimed in evidence.

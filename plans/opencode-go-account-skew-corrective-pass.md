# Opencode Go Account-Skew Corrective Pass

## Problem statement

Live Opencode Go usage shows extreme account skew: one configured backend/account has roughly 4,600 requests while peer accounts have roughly 10-20 requests. The observed account also reports slightly higher TPS, but current routing code does not appear to intentionally optimize for TPS. The expected behavior for same-provider subscription aggregation is weighted load balancing across eligible accounts while respecting quota headroom, health, provider priority, protocol compatibility, and in-flight work.

This plan corrects the routing feedback loop, adds tests that reproduce burst skew, and adds diagnostics that make future skew easy to classify as one of three cases: accounts are not eligible, scores are sticky, or runtime penalty state is not being observed.

## Current suspected root cause

The request coordinator serializes account selection with `_select_lock`, but the in-memory state that prevents immediate reselection is updated after `_select_lock` is released.

Current high-level flow in `src/eggpool/request/coordinator.py`:

1. Enter `_select_lock` and database transaction.
2. Compute eligible account names.
3. Estimate per-account request cost.
4. Rank/select account.
5. Create request, reservation, attempt, and routing decision rows.
6. Exit transaction and release `_select_lock`.
7. Increment runtime active request count.
8. Add in-memory reserved cost to `QuotaEstimator`.

This means a burst of concurrent requests can repeatedly select the same account before previous selections have made their active-count and reservation penalties visible to the scorer. Persisted usage windows do not help because pending requests are excluded from the usage-window aggregation. Therefore the hot-path anti-herd mechanism is the in-memory active/reservation state, and it currently becomes visible too late.

## Goals

1. Same-provider, equal-weight accounts should distribute approximately evenly under concurrent pre-dispatch load.
2. The selected account must become visible to the next selector before `_select_lock` is released.
3. The fix must not hold `_select_lock` across upstream HTTP calls or streaming work.
4. Durable database rows and in-memory reservation/active-count state must remain compensatable if post-commit runtime updates fail or cancellation interrupts the request.
5. Routing diagnostics must show why an account won and why peers lost.
6. Configuration mistakes such as unequal `routing_priority`, unequal account `weight`, catalog non-eligibility, auth failure, or health suppression should be easy to identify from CLI/dashboard/debug API output.

## Non-goals

1. Do not add latency/TPS-based routing in this pass. A faster backend within the same provider should not receive thousands more requests unless the operator explicitly enables such a strategy later.
2. Do not convert routing to round-robin globally. Quota-aware scoring remains the default, but it must not become sticky under equal-account conditions.
3. Do not change provider pricing or cost calculation semantics except where required to make selection diagnostics faithful.
4. Do not add distributed/multi-process coordination. EggPool currently runs one Granian worker; this pass targets the current single-process runtime model.

## Phase 1: Confirm routing inputs and failure mode

Review these files before patching:

- `src/eggpool/request/coordinator.py`
- `src/eggpool/routing/router.py`
- `src/eggpool/quota/scorer.py`
- `src/eggpool/quota/estimation.py`
- `src/eggpool/routing/eligibility.py`
- `src/eggpool/accounts/registry.py`
- `src/eggpool/db/repositories.py`
- existing tests under `tests/` that cover routing, coordinator selection, reservations, request attempts, and quota windows

Confirm the following observed behavior in the code:

- `_select_lock` covers selection and durable row creation but not the post-commit active-count/reservation mutation.
- `QuotaFairScorer` does not use TPS/latency as a scoring input.
- Provider `routing_priority` is tiered before score-based load balancing.
- Account `weight` scales capacities and therefore influences fairness.
- Pending request rows are excluded from persisted usage-window calculations.
- Routing decisions currently persist selected score and candidate counts, but not enough score components to fully explain why one account won.

## Phase 2: Move immediate runtime feedback inside the selection critical section

Patch `_select_and_persist_attempt()` so `_select_lock` protects the full local selection visibility transition:

1. Acquire `_select_lock`.
2. Start DB transaction.
3. Select account and create durable rows.
4. Commit transaction.
5. Still under `_select_lock`, increment selected account active count.
6. Still under `_select_lock`, add selected account's exact reservation estimate to `QuotaEstimator`.
7. Release `_select_lock`.
8. Execute upstream request outside the lock as before.

The lock must not wrap `_execute_upstream()`, response reading, streaming iterators, or finalization after upstream work. It should cover only the short pre-dispatch selection and state-publication path.

Recommended structure:

```python
async with self._select_lock:
    async with self._db.transaction():
        # existing select + durable persistence body
        ...

    active_count_increased = False
    reservation_added = False
    try:
        await self._router.increment_active_request_count(account_name)
        active_count_increased = True
        if self._quota_estimator is not None:
            await self._quota_estimator.add_reservation(
                account_name,
                estimated_microdollars,
            )
            reservation_added = True
    except BaseException:
        # compensate runtime and durable state; see below
        ...
```

Compensation requirements:

- If active count was incremented but reservation add fails, decrement active count.
- If reservation was added but a later runtime step fails, remove the reservation.
- Finalize the created attempt as `PostCommitInterrupted` using the existing shielded compensation path.
- Release any acquired health-manager request/probe slot.
- Set `context.client_metadata["post_commit_interrupted"] = True` as the current code does.
- Preserve cancellation behavior: `CancelledError`, `SystemExit`, and `KeyboardInterrupt` must not be swallowed.

Important subtlety: the durable transaction should still commit before in-memory state is published. If the transaction rolls back, do not publish active/reserved state. If publication fails after commit, compensate the durable attempt/reservation as the current code already attempts to do.

## Phase 3: Add an optional anti-stickiness selection debt if the lock fix is insufficient

After Phase 2 tests, assess whether same-score accounts still show pathological skew due to tiny persistent score differences. If the lock fix does not produce acceptable distribution, add a lightweight short-lived selection-debt term. Keep it behind the existing routing configuration or as a small default component of quota-fair scoring.

Proposed mechanism:

- Track recent selection counts per account in memory using a bounded rolling window, for example the last 100-500 selections or a 30-120 second TTL window.
- Add a small `selection_debt_penalty` to `RoutingScore.final_score` or to the scorer before final selection.
- The penalty should be small relative to actual quota exhaustion, but large enough to break same-provider stickiness.
- The penalty should be applied only within the same priority tier and only after hard eligibility, health, and protocol filtering.
- Include config such as:

```toml
[routing]
selection_debt_enabled = true
selection_debt_window = 100
selection_debt_penalty = 0.005
```

Do not implement this unless tests show the corrected reservation/active-count timing is still insufficient. The first fix should be the lock boundary correction.

## Phase 4: Strengthen routing-decision diagnostics

Extend routing decision capture so operators can diagnose 4,600-vs-20 skew from the dashboard or CLI without manually reading SQLite.

Add score-component capture at selection time. Suggested fields:

- selected account name and id
- selected provider id
- selected tier
- selected final score
- selected quota score
- selected active count
- selected reservation microdollars
- selected 5h/7d/30d cost snapshot
- selected offsets
- selected estimated request microdollars
- top score account name
- top final score
- eligible count
- scored count
- attempted excluded count
- exclusion reasons
- optionally a compact JSON blob of the top N candidates, e.g. top 5, with the same score components

Prefer adding a JSON column to `routing_decisions` rather than many new scalar columns unless the dashboard needs indexed queries. A JSON field such as `score_components_json` is sufficient for debugging and avoids a broad migration surface.

Add or update a repository method:

```python
RoutingDecisionRepository.create(..., score_components_json: str | None = None)
```

Add migration coverage for existing databases. Existing rows should have `NULL` components.

## Phase 5: Add account eligibility diagnostics

Add a runtime diagnostic helper that explains why each configured account is or is not eligible for a given model/protocol/provider tuple.

Recommended internal API:

```python
router.explain_account_eligibility(
    model_id: str,
    provider_id: str | None,
    protocol: str | None,
    transcode_eligibility: set[str] | None,
) -> list[AccountEligibilityExplanation]
```

Each explanation should include:

- account name
- provider id
- enabled
- has usable credentials
- provider priority
- weight
- supports requested protocol or transcode path
- model available in catalog
- model freshness/staleness state
- account health state
- model health state
- local quota mode result
- final eligible boolean
- reason code for exclusion

Expose this in at least one operator path:

- CLI: `eggpool accounts explain --model <model> [--protocol openai|anthropic] [--provider <id>]`
- API/dashboard debug route: `/api/stats/routing/eligibility?model=...&protocol=...`

The CLI path is preferred for this pass because it is immediately useful for field debugging and easier to test.

## Phase 6: Add regression tests for burst fairness

Add tests that fail on the current implementation and pass after Phase 2.

Recommended tests:

1. `test_concurrent_selection_publishes_reservation_before_next_selection`
   - Configure three equal accounts under one provider.
   - Use the same model for all accounts.
   - Fire many concurrent `_select_and_persist_attempt()` calls with upstream execution mocked out or by calling selection directly.
   - Assert distribution is not all/mostly one account.
   - A reasonable threshold for 30 selections across 3 accounts: no single account should receive more than 60-70% under deterministic conditions; with selection-debt this can be tighter.

2. `test_selection_lock_covers_runtime_reservation_visibility`
   - Instrument or monkeypatch `QuotaEstimator.add_reservation()` and router scoring to assert the next selection observes the previous reservation before ranking.

3. `test_priority_tier_still_wins_before_fairness`
   - Configure two priority tiers.
   - Assert lower-priority accounts are not used while higher-priority eligible accounts exist.

4. `test_equal_priority_equal_weight_spreads_across_accounts`
   - Non-concurrent sequential selections should spread once active/reserved penalties accumulate.

5. `test_diagnostics_report_ineligible_accounts`
   - Disable one account, remove credentials from another, make one model-stale/unavailable, and assert explanation reason codes are stable.

6. `test_routing_decision_records_score_components`
   - After a selection, fetch `routing_decisions` and assert score components are populated and valid JSON.

Avoid tests that depend on true wall-clock race timing. Prefer explicit instrumentation, barriers, or mock await points that reproduce the old gap deterministically.

## Phase 7: Validate against real Opencode Go configuration shape

Add or update fixture configuration to represent the expected Opencode Go use case:

```toml
[providers.opencod e-go] # use real id spelling in actual test/config
id = "opencode-go"
base_url = "https://opencode.ai/zen/go/v1"
protocols = ["openai", "anthropic"]
routing_priority = 0

[[providers.opencode-go.accounts]]
name = "go-1"
api_key_env = "OPENCODE_GO_1"
weight = 1.0

[[providers.opencode-go.accounts]]
name = "go-2"
api_key_env = "OPENCODE_GO_2"
weight = 1.0

[[providers.opencode-go.accounts]]
name = "go-3"
api_key_env = "OPENCODE_GO_3"
weight = 1.0
```

Acceptance checks:

- Same provider id for all accounts.
- Same priority.
- Same weights unless intentionally configured otherwise.
- Same model catalog eligibility for the hot model.
- Same protocol eligibility.
- No accidental account-name collision.
- No account health suppression except after real upstream 429/402/auth failures.

Note: fix the typo in the fixture header above if copied; it must be `[providers.opencode-go]`.

## Phase 8: Dashboard and stats integration

Enhance the dashboard/runtime/stats views enough to make account skew visible at a glance.

Minimum useful additions:

- Account request count over last 5h/24h/7d.
- Account selection count over last 5h/24h/7d.
- Account success/error/rate-limit/quota-exhausted counts.
- Active requests per account.
- Reserved microdollars per account.
- Last selected account and last selected score.
- Top exclusion reason counts.

If a full dashboard panel is too large for this pass, expose these through `/api/stats/routing` first and defer visual polish.

## Phase 9: Operational validation procedure

After implementation, validate on a local or staging EggPool instance:

1. Start with a clean test database and three equal mock/provider accounts.
2. Send 100 non-streaming requests concurrently to the same model.
3. Inspect request distribution by account.
4. Inspect `routing_decisions` score components for the first 20 selections.
5. Repeat with streaming requests if the test harness supports safe stream simulation.
6. Repeat with one account artificially marked rate-limited or quota-exhausted and confirm it is suppressed.
7. Repeat with one account at higher `routing_priority` and confirm tiering behavior remains intentional.
8. Repeat with one account at higher `weight` and confirm it receives a proportionally larger share, not 99% of traffic.

Useful SQL snippets:

```sql
SELECT a.name, COUNT(*) AS requests
FROM requests r
JOIN accounts a ON a.id = r.account_id
WHERE r.started_at >= datetime('now', '-5 hours')
GROUP BY a.name
ORDER BY requests DESC;
```

```sql
SELECT selected_account_name, COUNT(*) AS selections,
       MIN(selected_score), AVG(selected_score), MAX(selected_score)
FROM routing_decisions
WHERE created_at >= datetime('now', '-5 hours')
GROUP BY selected_account_name
ORDER BY selections DESC;
```

```sql
SELECT selected_account_name, eligible_count, scored_count,
       top_score_account_name, selected_score, top_score
FROM routing_decisions
ORDER BY id DESC
LIMIT 50;
```

## Acceptance criteria

The pass is complete when all of the following are true:

1. `_select_lock` covers publication of active-count and reservation penalties for a selected account before another selector can run.
2. No upstream HTTP or streaming work happens under `_select_lock`.
3. Existing compensation behavior remains intact for post-commit interruptions.
4. Burst-concurrency tests show materially improved distribution across equal Opencode Go accounts.
5. Same-priority/same-weight accounts do not show pathological 4,600-vs-20 skew when all are eligible and healthy.
6. Priority tiering remains intentional and tested.
7. Weighted accounts still receive proportionally more traffic when weights differ.
8. Routing decision diagnostics can distinguish:
   - only one account eligible,
   - many eligible but one score always wins,
   - many eligible but health/circuit breaker excludes peers,
   - many eligible but priority tiers exclude peers.
9. CLI or API diagnostics can explain account eligibility for a given model/protocol.
10. `uv run ruff check src/ tests/`, `uv run pyright src/ scripts/`, and `uv run pytest` pass.

## Suggested implementation order

1. Add a focused failing test for concurrent selection skew.
2. Patch `_select_and_persist_attempt()` lock scope and compensation.
3. Add sequential and concurrent distribution tests.
4. Add score-component diagnostics to `routing_decisions`.
5. Add account eligibility explanation helper and CLI/API exposure.
6. Add dashboard/stats endpoint enhancements if scope permits.
7. Run full lint/type/test suite.
8. Validate against real Opencode Go multi-account configuration.

## Risk notes

The main risk is accidentally holding `_select_lock` too long. Keep the lock strictly limited to selection, durable row creation, and immediate in-memory runtime-state publication. Do not include upstream network I/O.

The second risk is double-counting reservations during compensation. The implementation must track booleans such as `active_count_increased` and `reservation_added` and undo only the state that was actually published.

The third risk is mistaking intentional priority/weight behavior for a bug. Diagnostics must clearly show provider priority tier and account weight so operators can see when skew is configured rather than accidental.

The fourth risk is overcorrecting into hard round-robin. Quota fairness should remain quota fairness. The corrective patch should make scoring inputs timely and observable; any anti-stickiness debt should be small, configurable, and added only if tests prove the lock fix is insufficient.

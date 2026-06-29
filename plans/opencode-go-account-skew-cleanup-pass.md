# Opencode Go Account-Skew Cleanup Pass

## Context

The corrective pass for Opencode Go account skew materially improved the routing path. The coordinator now publishes active-request and reserved-cost state before another selector can enter the routing critical section, `routing_decisions` gained `score_components_json`, and unit tests now cover burst distribution, score-component persistence, priority tiering, and eligibility explanation.

A follow-up review found two remaining correctness gaps and several polish items:

1. The coordinator now closes the stale-score race, but the runtime publication block is still inside a compound `async with self._select_lock, self._db.transaction():` block. The code comments claim the transaction has committed before publication, but in Python the transaction does not exit/commit until the entire compound block exits. This means runtime active/reservation state is published before the durable transaction commit.
2. `eggpool accounts explain` creates an empty `ModelCatalogCache` and passes it directly to `Router`, even though `Router` expects a catalog service-like object with `.cache`. It also does not load real model/account catalog state from SQLite, so it would report misleading eligibility even if the object shape were fixed.
3. `eggpool accounts explain` imports `rich`, but `rich` is not declared as a dependency. This should either be removed or explicitly added. Prefer removing it and using simple `click.echo` table formatting to keep the CLI lightweight.
4. The new tests are useful, but they should be tightened so the old race/ordering bug fails deterministically rather than probabilistically.

This cleanup pass should close those remaining issues without changing the high-level routing policy.

## Goals

1. Ensure selection publication ordering is exactly: durable selection transaction commits, then active-count/reservation state publishes, then `_select_lock` releases.
2. Preserve the anti-skew behavior: no concurrent selector should score before the selected account's active/reservation penalties are visible.
3. Keep upstream network I/O outside `_select_lock`.
4. Make `eggpool accounts explain` accurate against the real configured database/catalog state.
5. Remove undeclared runtime dependencies from the command path.
6. Add deterministic tests for commit-before-publication and for the CLI eligibility command.
7. Keep the implementation minimal and local to coordinator/CLI/test code unless a small helper extraction makes correctness clearer.

## Non-goals

1. Do not add TPS/latency-based routing.
2. Do not add selection-debt or round-robin behavior unless a later production run still shows skew after this cleanup.
3. Do not redesign the routing scorer.
4. Do not add a dashboard panel in this pass; the JSON diagnostic data is already persisted and can be surfaced later.
5. Do not introduce `rich` solely for table rendering.

## Phase 1: Correct the coordinator lock/transaction nesting

Refactor `RequestCoordinator._select_and_persist_attempt()` so the structure is explicit rather than using a compound context manager.

Current problematic shape:

```python
async with self._select_lock, self._db.transaction():
    # select + persist rows
    # publish active count + reservation
```

Target shape:

```python
async with self._select_lock:
    async with self._db.transaction():
        # select account
        # create request row
        # create reservation row
        # create attempt row
        # create routing_decision row
        # update context.attempted_accounts and context.client_metadata

    # The transaction has committed here, and _select_lock is still held.
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
        if reservation_added and self._quota_estimator is not None:
            await self._quota_estimator.remove_reservation(
                account_name,
                estimated_microdollars,
            )
        if active_count_increased:
            await self._router.decrement_active_request_count(account_name)
        await asyncio.shield(... finalize_failed_attempt ...)
        if self._health_manager is not None:
            self._health_manager.release_request(account_name)
        context.client_metadata["post_commit_interrupted"] = True
        raise
```

Important details:

- The inner DB transaction must close before publication starts.
- `_select_lock` must still be held while publication happens.
- Keep `_execute_upstream`, non-streaming response reads, streaming iterator creation, and all upstream HTTP send/read logic outside `_select_lock`.
- Track both `active_count_increased` and `reservation_added`. The current code tracks only active count. If future code adds another post-commit step after reservation publication, the compensation path should already be correct.
- Keep `context.attempted_accounts.add(account_name)` and `context.client_metadata["account_name"] = account_name` inside the durable transaction block unless there is a specific reason to move them. The current intent is to make the request context reflect the persisted attempt.
- Update the misleading comment that currently says the transaction has committed while still inside the compound transaction block.

## Phase 2: Add deterministic ordering tests

Add tests that explicitly verify publication happens after commit but before lock release.

Recommended approach:

1. Create a small instrumented database transaction or repository test double if existing abstractions permit it. If not, monkeypatch one of the post-commit publication methods and query the database inside that method.
2. During `Router.increment_active_request_count()` or `QuotaEstimator.add_reservation()`, assert that the `requests`, `reservations`, `request_attempts`, and `routing_decisions` rows for the selected attempt are already visible on the same database connection.
3. Add a second coroutine that waits for a barrier immediately after publication but before `_select_lock` release, then attempts to select. It should be blocked until the first publication completes, then observe active/reserved state.

Concrete tests to add or tighten in `tests/unit/test_routing_coordinator_concurrent.py`:

- `test_runtime_publication_happens_after_transaction_commit`
  - Monkeypatch `router.increment_active_request_count` to query the DB for the just-created request/attempt/reservation/routing_decision rows.
  - Assert all exist before active count increments.
  - This should fail under the current compound-context shape if the DB layer truly defers commit visibility until transaction exit.

- `test_select_lock_released_only_after_runtime_publication`
  - Monkeypatch `quota_estimator.add_reservation` to hold a barrier.
  - Start selection A and pause during reservation publication.
  - Start selection B.
  - Assert B cannot complete selection until A's publication is released.
  - After release, assert B's scorer observes A's active count or reservation.

- `test_post_commit_publication_failure_removes_partial_runtime_state`
  - Monkeypatch `quota_estimator.add_reservation` to add then raise, or monkeypatch a later synthetic publication step if available.
  - Assert active count and reserved cost are not left behind.
  - Assert the failed attempt is finalized as `PostCommitInterrupted`.

If the DB abstraction makes commit visibility difficult to observe directly, still add a test that validates the explicit nesting through behavior: publish hook should run after exiting the repository transaction body, not before. Prefer behavioral tests over introspecting private context-manager internals.

## Phase 3: Repair `eggpool accounts explain`

The command must use real persisted catalog/account state, not an empty in-memory catalog.

Current issues to fix:

- It constructs `catalog = ModelCatalogCache()` and passes it to `Router`. `Router` expects `catalog.cache`, so the object shape is wrong.
- The cache is empty, so real model availability is not represented.
- It does not run migrations or load current account/model/provider metadata from SQLite.
- It imports `rich`, which is not a declared dependency.

Recommended implementation:

1. Load `AppConfig` from the active config path.
2. Validate account credentials only if needed. For an explanation command, do not require every account's env var to be set before producing output; instead, report `auth_failed` / `credentials_missing` per account. If the existing `AccountRegistry` constructor raises on enabled missing credentials, either:
   - build a diagnostic registry variant that allows missing credentials, or
   - catch `ConfigError` and print a clear message indicating which account/env var is missing.

3. Open the configured database using the same `_run_with_database()` helper pattern as `migrate` and `models refresh`.
4. Run `MigrationRunner(db).run()` so the command works after upgrade.
5. Sync providers/accounts from config if this command is expected to work before server startup:
   - `ProviderRepository.sync_from_config(...)`
   - `AccountRepository.sync_from_config(account_config_rows(config))`
   Be careful not to mutate account enabled states unexpectedly beyond the normal sync semantics already used by startup and `models refresh`.

6. Load the model catalog into a `ModelCatalogCache`. Options:
   - Preferred: add or reuse a `CatalogService`/cache load method that hydrates from DB without contacting upstream.
   - Acceptable: add a focused helper such as `load_model_catalog_cache_from_db(db, config)` that reads `accounts`, `models`, `account_models`, and `provider_model_metadata`, then calls `ModelCatalogCache.update_from_account(...)` with each account's available models and protocol metadata.

7. Wrap the cache in a small adapter object if `Router` requires `.cache`:

```python
@dataclass(slots=True)
class _CatalogCacheAdapter:
    cache: ModelCatalogCache
```

8. Construct `Router(registry, _CatalogCacheAdapter(cache), quota_estimator=QuotaEstimator(), stale_after_s=config.models.stale_after_s, local_quota_mode=config.routing.local_quota_mode)`.

9. Call `router.explain_account_eligibility(...)`.

10. Render with plain text using `click.echo`, not `rich`.

Suggested plain table columns:

- account
- provider
- eligible
- reason
- detail

Include provider id and optionally weight/priority in the output if easy. That information is important for diagnosing skew.

## Phase 4: Improve eligibility explanation semantics

The current `Router.explain_account_eligibility()` is useful but should be aligned more tightly with `get_eligible_accounts()`.

Review `src/eggpool/routing/eligibility.py` and ensure `_classify_eligibility()` mirrors the live routing order and conditions exactly. Pay particular attention to:

- missing usable credentials versus authentication-failed health state
- provider mismatch
- provider protocol mismatch
- transcode eligibility
- model availability versus stale model metadata
- account-level health manager checks versus model-level circuit breaker checks
- local quota mode: `hard_cap` should produce a reason when local quota excludes an account
- `score_only` mode should not mark above-capacity accounts ineligible solely due to local estimates

Add any missing reason codes. Suggested stable reason codes:

- `ok`
- `disabled`
- `missing_credentials`
- `auth_failed`
- `wrong_provider`
- `no_provider`
- `no_protocol`
- `protocol_mismatch`
- `no_model`
- `model_stale`
- `account_unhealthy`
- `quota_exhausted`
- `rate_limited`
- `cooldown`
- `circuit_open`
- `local_quota_hard_cap`

The output should be operator-facing and stable enough to grep, so avoid overly verbose or changing reason-code names.

## Phase 5: Remove undeclared `rich` dependency or declare it explicitly

Prefer removing `rich` from `accounts explain`.

Rationale:

- EggPool CLI is intended to stay light and usable on SBC/Raspberry Pi deployments.
- `rich` is not currently declared in `pyproject.toml`.
- A simple fixed-width `click.echo` table is sufficient.

If the implementation chooses to keep `rich`, then add it explicitly to `pyproject.toml` and ensure lock files are updated if present. This is not preferred for this cleanup pass.

Plain formatter sketch:

```python
def _print_account_explain_rows(rows: list[dict[str, Any]]) -> None:
    headers = ("account", "provider", "eligible", "reason", "detail")
    widths = (24, 18, 8, 24, 80)
    click.echo("  ".join(h.ljust(w) for h, w in zip(headers, widths, strict=True)))
    click.echo("  ".join("-" * w for w in widths))
    for row in rows:
        cells = (
            str(row.get("account_name", ""))[:24],
            str(row.get("provider_id", ""))[:18],
            "yes" if row.get("eligible") else "no",
            str(row.get("reason_code", ""))[:24],
            str(row.get("reason_detail", ""))[:80],
        )
        click.echo("  ".join(c.ljust(w) for c, w in zip(cells, widths, strict=True)))
```

## Phase 6: Add CLI tests for `accounts explain`

Add tests using Click's `CliRunner` or the existing CLI test harness.

Test cases:

1. `test_accounts_explain_uses_real_catalog`
   - Build temp config with two accounts.
   - Seed DB with model support for account A only.
   - Run `eggpool accounts explain --model <model> --protocol openai`.
   - Assert account A is `ok` and account B is `no_model` or equivalent.

2. `test_accounts_explain_no_rich_dependency`
   - This can be implicit: do not import rich. A unit test can monkeypatch `sys.modules["rich"] = None` if needed, but better is simply no import path.

3. `test_accounts_explain_provider_filter`
   - Two providers, one account each.
   - Run with `--provider provider-a`.
   - Assert provider-b account appears as `wrong_provider` or is included with an explicit wrong-provider reason. Prefer inclusion so the operator can see why it was not considered.

4. `test_accounts_explain_missing_catalog`
   - No model rows in DB.
   - Command exits 0 and reports `no_model` rather than crashing.

5. `test_accounts_explain_runs_migrations`
   - Use a temp DB missing migration 0035 if feasible, or at least verify command works against a newly initialized DB.

## Phase 7: Tighten score-component diagnostics

The current `score_components_json` captures useful values. Verify and polish the payload shape:

- Include `selected_account_name` explicitly.
- Include `provider_id` if available.
- Include `model_id` and `protocol` if useful for standalone trace inspection.
- Include `top_candidates` as a list of at most 5 candidates with each candidate's:
  - account name
  - quota score
  - inflight penalty
  - health penalty
  - final score
  - active request count
  - reserved microdollars
  - tier
  - requires transcode
- Ensure JSON values are simple primitives only; no dataclass/string repr leakage.
- Keep payload small enough for frequent writes.

Add a test that parses `score_components_json` and validates `top_candidates` contains the selected account and at least one peer when multiple candidates were scored.

## Phase 8: Verify account status output includes skew-relevant config

`eggpool accounts status` already prints provider, enabled, weight, and env-var set state. Consider adding provider routing priority to the output, because priority tiering can intentionally explain skew.

Suggested output line:

```text
  go-1: provider=opencode-go, priority=0, enabled=True, weight=1.0, api_key_env=OPENCODE_GO_1 (set=yes)
```

Add a small test or snapshot if existing CLI output tests are present.

## Phase 9: Optional dashboard/API follow-up, only if scope remains small

If there is already an API endpoint for routing traces, add `score_components_json` to its response. If there is not, defer this to a later dashboard polish pass.

Do not expand this cleanup into a full dashboard redesign. The immediate operational path should be:

- `eggpool accounts status`
- `eggpool accounts explain --model ... --protocol ...`
- SQLite query against `routing_decisions.score_components_json`

## Acceptance criteria

This cleanup pass is complete when:

1. `_select_and_persist_attempt()` uses explicit nested contexts: `_select_lock` outer, DB transaction inner.
2. Runtime active/reservation publication happens after the DB transaction commits and before `_select_lock` releases.
3. Upstream HTTP execution still occurs outside `_select_lock`.
4. Compensation removes any partially published active/reservation state.
5. Tests deterministically validate commit-before-publication and publication-before-next-selection ordering.
6. `eggpool accounts explain` uses the real configured database/catalog state.
7. `eggpool accounts explain` does not import undeclared dependencies.
8. `accounts explain` exits 0 and provides useful per-account reason codes for normal cases, empty catalogs, provider filters, and missing model support.
9. Eligibility reason codes align with live routing filters.
10. Existing skew tests still pass.
11. `uv run ruff check src/ tests/`, `uv run pyright src/ scripts/`, and `uv run pytest` pass.

## Manual validation checklist

After implementation, run the following against a development instance with multiple Opencode Go accounts:

```bash
eggpool migrate
eggpool accounts status
eggpool accounts explain --model '<hot-model>' --protocol openai
eggpool accounts explain --model '<hot-model>' --protocol anthropic
```

Then send a concurrent burst through EggPool and inspect distribution:

```sql
SELECT a.name, COUNT(*) AS requests
FROM requests r
JOIN accounts a ON a.id = r.account_id
WHERE r.started_at >= datetime('now', '-5 hours')
GROUP BY a.name
ORDER BY requests DESC;
```

Inspect routing trace payloads:

```sql
SELECT selected_account_name,
       eligible_count,
       scored_count,
       top_score_account_name,
       selected_score,
       score_components_json
FROM routing_decisions
ORDER BY id DESC
LIMIT 20;
```

Expected result: if all Opencode Go accounts are eligible, same priority, same weight, and healthy, requests should distribute across accounts rather than pegging one account. If one account still dominates, `accounts explain` and `score_components_json` should reveal whether peers are ineligible, lower priority, unhealthy, missing model support, or losing by score.

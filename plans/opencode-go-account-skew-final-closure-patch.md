# Opencode Go Account-Skew Final Closure Patch

## Context

The latest cleanup pass has the important runtime behavior in the right shape:

- `RequestCoordinator._select_and_persist_attempt()` now uses explicit nested contexts: outer `_select_lock`, inner `_db.transaction()`.
- The durable selection transaction exits before runtime publication.
- Runtime active-count and in-memory quota-reservation publication still happen before `_select_lock` releases.
- `eggpool accounts explain` now hydrates a real `ModelCatalogCache` from SQLite and renders with plain `click.echo` rather than an undeclared `rich` dependency.
- Routing score diagnostics now include utilization ratios and tie-break metadata.

Two narrow closure issues remain:

1. `eggpool accounts explain` says it runs migrations on a fresh install, but the implementation currently uses `_run_with_database(config, _run_explain)`, and `_run_with_database()` only connects/disconnects. `_run_explain()` hydrates the catalog but does not call `MigrationRunner(db).run()`.
2. Some documentation says a compound context manager such as `async with self._select_lock, self._db.transaction():` would commit the transaction after releasing the lock. That wording is technically wrong. Python exits async context managers right-to-left, so the transaction context exits before the lock context. The actual bug was that the runtime publication block lived inside the transaction body; therefore publication happened before transaction commit. The implementation is now correct, but the docs should be corrected to prevent future confusion.

This final closure patch should be small and surgical.

## Goals

1. Make `eggpool accounts explain` actually run migrations before reading catalog state.
2. Correct all misleading documentation about compound async context-manager exit ordering.
3. Add one focused test proving `accounts explain` invokes migrations on a fresh or unmigrated database path.
4. Keep routing behavior unchanged.
5. Keep the CLI dependency set unchanged; do not add `rich`.

## Non-goals

1. Do not modify the routing scorer.
2. Do not modify the `_select_lock` implementation unless a test exposes a real defect.
3. Do not expand dashboard routing diagnostics in this patch.
4. Do not resolve the two pre-existing dashboard rollup test failures unless they are directly touched by this patch.
5. Do not refactor all CLI database commands to run migrations automatically; this patch is only for the command that explicitly claims fresh-install compatibility.

## Phase 1: Make `accounts explain` run migrations

File: `src/eggpool/cli_full.py`

In `accounts_explain()`, update the inner `_run_explain(db: Database)` coroutine so it runs migrations before constructing the registry/cache/router and before calling `ModelCatalogCache.hydrate_from_db(db)`.

Suggested patch shape:

```python
async def _run_explain(db: Database) -> None:
    runner = MigrationRunner(db)
    await runner.run()

    registry = AccountRegistry(config)
    cache = ModelCatalogCache()
    cache.set_config(config)
    await cache.hydrate_from_db(db)
    ...
```

`MigrationRunner` is already imported near the top of `cli_full.py` for the `migrate` and `models refresh` paths. If it is not in scope in the current file version, import it consistently with surrounding code.

Important details:

- Run migrations before `hydrate_from_db()` so the tables and columns expected by hydration exist.
- Do not call `models refresh`; the command should remain offline/read-only with respect to upstream providers.
- Do not require outbound network access.
- Do not mutate the model catalog except through migration DDL/DML. The command should not create or refresh model rows.
- Let `MigrationRunner` be idempotent when there are no pending migrations.
- Keep the output shape unchanged.

## Phase 2: Add a CLI migration test

Add or extend tests around `accounts explain`. The commit message from the prior cleanup references `TestAccountsExplainOutput` in a provider-aware CLI test file; locate the actual current test file and extend it there.

Recommended test name:

```python
def test_accounts_explain_runs_migrations_before_hydration(...):
    ...
```

Recommended test strategy:

1. Create a temporary config pointing at a fresh temporary SQLite database path.
2. Configure at least one provider and one account. Use a fake env var and set it in the test environment.
3. Invoke the CLI command:

```bash
eggpool --config <temp-config> accounts explain --model some-model --protocol openai
```

4. Assert the command exits successfully rather than failing with `no such table: models`, `no such table: provider_model_metadata`, or `no such table: account_models`.
5. Assert the output includes the account row and a sensible reason such as `no_model` when no catalog rows exist.
6. Optionally open the DB and assert `_migrations` exists and includes the latest migration version.

This test should fail before the patch if `hydrate_from_db()` is run against a fresh DB without migrations.

If existing test helpers already seed migrated DBs, avoid those helpers for this specific test; the point is to exercise the fresh/unmigrated DB path.

## Phase 3: Correct documentation wording

Search the repo for misleading phrases around the compound context shape. Candidate search terms:

- `commit the transaction AFTER the lock releases`
- `committed the transaction AFTER the lock released`
- `Python's __aexit__ ordering would then commit the transaction AFTER the lock releases`
- `compound async with self._select_lock, self._db.transaction()`
- `_select_lock publish ordering`

Files likely to contain this wording:

- `AGENTS.md`
- `architecture/README.md`
- any architecture skill/doc file under `docs/` or project guidance
- `CHANGELOG.md`
- `README.md`
- comments/docstrings near `RequestCoordinator._select_and_persist_attempt()` if they repeat the incorrect explanation

Replace the incorrect explanation with technically precise wording.

Correct wording:

> Keep the explicit nested form: outer `_select_lock`, inner `_db.transaction()`. A compound `async with self._select_lock, self._db.transaction():` does not by itself release the lock before committing; Python exits context managers right-to-left. The problem with the prior implementation was that the runtime publication block was inside the transaction body, so active-count and reserved-cost state were published before the transaction committed. The explicit nested form makes it hard to accidentally place publication inside the transaction while still keeping publication under `_select_lock`.

Shorter version for compact docs:

> Do not collapse this back into a compound context. The key invariant is not context-exit order; it is block placement: publication must be outside the DB transaction body but still inside `_select_lock`.

Do not change the implementation comments if they are already correct. The current coordinator docstring appears to correctly state that the inner transaction exits before publication runs, so only edit it if it repeats the wrong `lock releases before commit` claim.

## Phase 4: Verify no undeclared dependency path remains

Search for `from rich` and `import rich`.

Expected result:

- No production CLI path imports `rich`.
- If tests import `rich`, remove that usage unless `rich` is declared as a dev dependency. Prefer no `rich` usage at all.

No dependency changes are expected.

## Phase 5: Run focused tests and full validation

Focused tests:

```bash
uv run pytest tests/unit/test_routing_coordinator_concurrent.py
uv run pytest <cli-provider-aware-test-file> -k accounts_explain
```

Full checks:

```bash
uv run ruff check src/ tests/
uv run pyright src/ scripts/
uv run pytest
```

Current known state from the previous cleanup commit: full pytest reportedly had 3398 passing tests and 2 pre-existing `test_dashboard_rollups` failures. If those two remain and are unrelated, document them explicitly in the commit message. Do not bury new failures under that known failure note.

## Acceptance criteria

The patch is complete when:

1. `eggpool accounts explain` runs `MigrationRunner(db).run()` before `ModelCatalogCache.hydrate_from_db(db)`.
2. A fresh/unmigrated database path no longer causes `accounts explain` to crash from missing catalog tables.
3. The command still does not perform outbound provider refreshes.
4. The command still renders with `click.echo` and does not import `rich`.
5. Docs no longer claim that a compound `async with self._select_lock, self._db.transaction():` commits after lock release.
6. Docs clearly state the real invariant: publication must be outside the DB transaction body but still inside `_select_lock`.
7. Existing routing-skew tests still pass.
8. New CLI migration test passes.
9. Ruff and pyright pass.
10. Full pytest has no new failures beyond any explicitly confirmed pre-existing dashboard rollup failures.

## Manual validation

After the patch, validate locally with a brand-new temporary config/database:

```bash
export OPENCODE_GO_TEST_KEY=dummy
uv run eggpool --config /tmp/eggpool-test.toml accounts explain --model gpt-4 --protocol openai
```

Expected result:

- The command exits cleanly.
- The database is migrated.
- The configured account appears in the output.
- With no catalog rows, the account reports `no_model` rather than a SQLite schema error.

Then validate against a real EggPool instance:

```bash
eggpool accounts status
eggpool accounts explain --model '<hot-model>' --protocol openai
eggpool accounts explain --model '<hot-model>' --protocol anthropic
```

Expected result:

- `accounts status` shows provider priority, enabled state, weight, and env-var set state.
- `accounts explain` reflects real persisted model support.
- Any remaining account skew should be attributable from `reason_code`, `reason_detail`, `routing_priority`, weights, or `routing_decisions.score_components_json`.

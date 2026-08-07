# Plan 089 — Catalog and Ping Write Reduction

Date: 2026-08-07
Status: ready for implementation
Parent roadmap: `plans/086-sbc-routing-and-storage-efficiency-roadmap.md`
Depends on: none
Planning baseline: `d6c49dea5ed800bfcd22d95fe8c7943a29590125`

## Purpose

Reduce idle and periodic SQLite write amplification caused by model-catalog refreshes and provider ping recording, with emphasis on long-running Raspberry Pi/microSD deployments.

The existing catalog logic is correctness-conscious and should remain so: failed/partial upstream model discovery must not destructively de-pool healthy accounts, startup must be able to hydrate known support, and explicit withdrawal policies must retain their current semantics. The optimization target is **unchanged successful refreshes**, not failure handling.

## Required reading

- `plans/086-sbc-routing-and-storage-efficiency-roadmap.md`
- `AGENTS.md`
- `src/eggpool/catalog/service.py`
- `src/eggpool/catalog/cache.py`
- catalog-related repositories in `src/eggpool/db/repositories.py` and related DB modules
- migrations defining `models`, `provider_model_metadata`, `account_models`, price snapshots, and ping tables
- background task registration/cadence for catalog refresh and ping retention
- existing catalog refresh, withdrawal, startup hydration, pricing, and ping tests

## Current write pattern to eliminate

At the planning baseline, a normal refresh:

1. fetches `/models` for enabled accounts;
2. records a ping row for each account fetch;
3. mutates the in-memory catalog;
4. calls `_persist_catalog()`;
5. reads account/support/latest-price state;
6. serializes the full global model catalog and provider model catalog;
7. bulk-upserts model/provider rows, including `last_seen_at`;
8. toggles account-model support rows;
9. checks/inserts price snapshots;
10. reconciles withdrawn/orphan rows.

With the default five-minute refresh interval, this can dirty WAL pages repeatedly even when no semantic model metadata or support relationship changed.

## Governing design

Separate three concepts that are currently coupled:

- **semantic catalog state**: model/provider metadata, protocol/capabilities, support relationships, pricing snapshots;
- **refresh freshness**: when an account/provider was last successfully checked;
- **diagnostic ping history**: latency/status/error observations.

Semantic state should write only on semantic change. Freshness should be represented compactly. Ping persistence should prioritize failures and coarse useful history rather than one durable success row per account every refresh.

Do not add a database cache layer or a second persistence worker.

## Workstream A — Inventory durable freshness consumers

Before changing schema or timestamps, trace every read of:

- `models.last_seen_at`;
- `provider_model_metadata.last_seen_at`;
- account/model refresh ages in `ModelCatalogCache`;
- ping table timestamps/statuses;
- startup hydration methods such as `hydrate_account_refresh_ages()` and `hydrate_refresh_age()`;
- dashboard/runtime endpoints that display catalog or ping freshness.

Write down which values are required for routing correctness versus diagnostics only.

Do not delete a timestamp until every restart/hydration consumer has a replacement.

## Workstream B — Add compact account/provider refresh state only if needed

If restart-safe freshness currently depends on rewriting every model/provider row, introduce the smallest durable refresh-state representation needed to decouple it.

Preferred shape: one row per configured account (or account/provider pair if required by existing identity semantics) containing fields such as:

- last successful catalog refresh timestamp;
- last refresh outcome/status class if operationally useful;
- optional model count if already used by the dashboard.

Reuse an existing table if one already represents this cleanly. Do not create a new table if `pings` or another existing durable state can supply startup freshness cheaply and unambiguously.

If a migration is required:

- make it additive and backward-compatible;
- hydrate existing rows conservatively from current timestamps where practical;
- do not rewrite historical request data;
- keep the migration small and idempotent under the existing migration runner.

## Workstream C — Compute semantic catalog deltas

Refactor `_persist_catalog()` so unchanged semantic rows are not updated merely because a refresh occurred.

Required behavior:

1. compare the in-memory desired semantic model/provider state against durable state or against a trusted precomputed semantic fingerprint/diff;
2. insert genuinely new model/provider rows;
3. update rows only when semantic fields changed;
4. enable/disable account-model support only when the relationship changed;
5. insert price snapshots only when pricing changed, preserving the existing optimization;
6. perform withdrawal/orphan reconciliation only when the desired support/model sets require it;
7. persist compact refresh freshness separately from semantic row updates.

Semantic comparison should cover fields that affect routing/exposure/capabilities, not volatile refresh timestamps.

Avoid per-model SELECT loops. Use existing bulk reads and set/dict comparisons in memory.

## Workstream D — Bound write-lock duration

Keep expensive serialization/comparison outside the SQLite write transaction where possible.

Inside the transaction, perform only the required DML for the already-computed delta plus any correctness-critical reconciliation that must be atomic with it.

Do not create one transaction per model. Prefer a single short transaction per refresh delta.

When there is no semantic delta and only freshness needs updating, the transaction should be tiny. If freshness can be updated through an existing single-row write, do that.

## Workstream E — Reduce successful ping write pressure

Preserve durable diagnostic value while avoiding one success row per account every five minutes indefinitely.

Preferred simple policy:

- persist provider/account refresh failures and non-2xx outcomes immediately;
- persist transitions between success and failure states immediately;
- retain successful latency observations in memory for current runtime diagnostics where already supported;
- persist steady-state successful pings at a coarse cadence, e.g. no more than one durable success sample per account per 30–60 minutes, or aggregate them into an existing coarse metric path if one already exists.

Choose the smallest implementation compatible with current dashboard/stat consumers. Do not add a general time-series aggregator solely for pings.

Keep retention cleanup bounded and unchanged unless the new lower write frequency makes an obvious constant simplification possible.

## Workstream F — Preserve catalog failure semantics

Explicitly regression-test that optimization does not change:

- failed fetch preserves prior support;
- success-empty preserves prior support under current policy;
- partial/unresolved response does not authorize destructive withdrawal;
- `preserve_until_health`, `confirmed_once`, and `confirmed_twice` retain their current behavior;
- startup hydration restores support/freshness accurately enough for routing;
- one-shot missing-account recovery refresh cannot destructively corrupt sibling catalog state;
- model quarantine reappearance callback ordering remains unchanged.

This plan must not modify health/backoff/quarantine policy.

## Workstream G — Focused write-count/delta tests

Add deterministic tests using SQLite/repository instrumentation already available in the codebase.

Required cases:

1. first successful refresh persists expected semantic rows;
2. identical second refresh performs no model/provider semantic UPDATE/UPSERT work beyond compact freshness state;
3. one changed capability/protocol/display/pricing value updates only the affected semantic row(s);
4. one new/withdrawn support relationship changes only the required support rows;
5. failed refresh writes diagnostic failure state but does not rewrite catalog metadata;
6. repeated successful refreshes within the chosen ping persistence interval do not create one ping row each;
7. failure ping bypasses success coarsening and remains immediately durable;
8. restart hydration after an unchanged refresh still has valid freshness/support state.

Do not assert SQLite's internal page-write count in CI. Assert application-level DML/row effects.

## Workstream H — Documentation/default reconciliation

Update relevant architecture/deployment comments to say that the five-minute discovery cadence is **not** equivalent to a five-minute full catalog rewrite.

If a new ping persistence cadence is configurable, avoid adding a configuration knob unless operators genuinely need it. A fixed conservative internal cadence is preferable for this local appliance unless existing config structure already exposes ping sampling behavior.

## Verification

Run focused catalog/cache/repository/migration/ping tests, then:

```bash
uv run ruff format --check src/ tests/ scripts/
uv run ruff check src/ tests/ scripts/
uv run pyright src/ scripts/
uv run pytest tests/smoke/ -q --tb=short --maxfail=1
uv run eggpool --config config.example.toml check-config
uv run eggpool --config config.sbc.example.toml check-config
```

A short manual SQLite/WAL observation may be recorded, but it is not a CI gate.

## Acceptance criteria

- [ ] An unchanged successful catalog refresh does not rewrite all `models` rows.
- [ ] An unchanged successful catalog refresh does not rewrite all `provider_model_metadata` rows.
- [ ] Account-model support DML occurs only for changed relationships.
- [ ] Semantic changes still persist correctly and atomically enough for current restart behavior.
- [ ] Restart-safe catalog freshness no longer requires touching every semantic row each refresh.
- [ ] Failed/partial/empty refresh behavior preserves prior support exactly as before.
- [ ] Withdrawal policies retain existing semantics.
- [ ] Repeated steady-state successful pings are durably recorded much less often than the model refresh cadence.
- [ ] Provider/account failures remain immediately visible in durable diagnostics.
- [ ] No new general metrics pipeline/background writer is introduced.
- [ ] Write transactions contain precomputed delta DML rather than avoidable serialization/comparison work.
- [ ] Focused catalog/ping tests and smoke gate pass.

## Rejection conditions

Do not close this plan if:

- freshness optimization causes healthy support to disappear after restart;
- a failed upstream `/models` response becomes destructive;
- semantic equality is determined by unstable serialization ordering;
- implementation performs per-model SELECT/transaction loops;
- successful ping reduction hides failures or state transitions;
- a new time-series subsystem is added;
- SQLite durability defaults are weakened;
- write reduction is claimed only from timing noise without row/DML evidence.

## Implementation sequence for GPT-5.6 Luna

1. Trace every durable freshness consumer and current ping consumer.
2. Add tests proving identical refreshes currently produce redundant semantic writes/pings.
3. Choose the smallest durable freshness representation, reusing an existing table if possible.
4. Compute semantic desired-vs-existing deltas outside the transaction.
5. Apply only delta DML inside the catalog transaction.
6. Implement simple successful-ping coarsening while preserving immediate failure writes.
7. Run startup-hydration and withdrawal-policy regressions.
8. Run focused migration/repository tests if schema changed.
9. Run lint/type/smoke/config checks.
10. Record exact verification and mark complete only when identical-refresh write reduction is directly proven.
# Plan 161 — Model-Info Enrichment Lifecycle and Dashboard Truthfulness Corrective Pass

Date: 2026-09-03
Status: ready for implementation
Planning baseline: `5cc781185e0e38fabf34f9207b23d337b5b6b966`
Priority: P0 correctness regression
Execution target: GPT-5.6 Luna or comparable implementation model

## Purpose

Restore automatic external model-metadata enrichment when `[model_info].enabled = true` without undoing EggPool's lean/SBC runtime simplification.

The current repository successfully discovers provider models, creates provider-catalog observations, persists canonical model-info rows, serves those rows through the JSON API, and renders them on the dashboard. However, the normal runtime lifecycle does not invoke the external enrichment engine that fetches and matches OpenRouter, Artificial Analysis, or Hugging Face records. As a result, newly discovered models remain `sparse_new` and the dashboard repeatedly reports variants of:

> New model detected; metadata sparse ... Public benchmark metadata unavailable.

The external fetch/match/persist logic already exists in `ModelInfoService.refresh_due_models()`. The corrective work is primarily lifecycle wiring, regression coverage, and documentation/config truthfulness rather than a redesign of model-info storage or presentation.

## Confirmed root cause

The intended and documented architecture is:

```text
provider catalog refresh
        |
        v
catalog reconciliation
        |
        v
model-info canonical reconciliation
        |
        v
bounded due external enrichment
        |
        v
OpenRouter / Artificial Analysis / Hugging Face observations
        |
        v
canonical merge + status promotion
        |
        v
dashboard / API
```

The implemented production path currently stops before `bounded due external enrichment`.

`ModelInfoService.refresh_due_models()` already owns the bulk external-source cycle. It:

- selects canonical rows whose `next_refresh_at` is due, bounded by `max_models_per_cycle`;
- bulk-fetches OpenRouter once per cycle when enabled;
- bulk-fetches Artificial Analysis once per cycle when enabled;
- resolves source records to canonical EggPool models;
- fetches configured/derived Hugging Face records where applicable;
- persists external observations, aliases, match evidence, and source-health state;
- merges source observations into canonical detail;
- promotes `sparse_new` rows to `partial`/`fresh` when external evidence warrants it;
- computes the next per-model refresh using the existing status-aware scheduler.

Normal production startup does not call this function. Startup currently refreshes the provider catalog and then runs `reconcile_catalog_snapshot()`, `backfill_missing_canonical()`, and `backfill_legacy_detail_blocks()` only.

The periodic `catalog_refresh` callback likewise calls `reconcile_catalog_refresh()` and `backfill_missing_canonical()` only. It never asks the model-info service to execute due external-source work.

There is intentionally no separate `model_info_refresh` background task. `architecture/deep-dive-model-info.md` and `architecture/deep-dive-background.md` state that model-info external work piggybacks on the catalog lifecycle so the SBC-oriented runtime does not carry another periodic scheduler. Preserve that decision.

## Corrective architecture decision

Do **not** add a new permanent `model_info_refresh` task.

Keep `catalog_refresh` as the authoritative recurring event source. After successful catalog reconciliation, let the generation-owned `ModelInfoService` decide whether any external work is actually due. The catalog task may run every five minutes while most known model rows remain scheduled hours or days into the future; `refresh_due_models()` must remain the authority for per-row TTL selection and batch bounds.

Startup should additionally perform one bounded external enrichment pass when model-info startup refresh is enabled. This fixes the visible first-run failure without making process startup unbounded for large catalogs.

The desired lifecycle is:

```text
STARTUP
catalog.refresh() if models.startup_refresh
  -> model_info.reconcile_catalog_snapshot()
  -> model_info.backfill_missing_canonical()
  -> model_info.backfill_legacy_detail_blocks()
  -> model_info.refresh_due_models(force=True), bounded by max_models_per_cycle

PERIODIC CATALOG TICK
catalog.refresh()
  -> quarantine/model recovery work
  -> model_info.reconcile_catalog_refresh(result)
  -> model_info.backfill_missing_canonical()
  -> model_info.refresh_due_models(force=False)
```

External source failure remains best-effort and must never cause startup failure, catalog refresh failure, routing suppression, or request-path failure.

## Governing invariants

1. External model metadata is advisory. It must never determine whether a model can route successfully.
2. Provider catalog discovery remains the source of routable model availability.
3. `catalog_refresh` remains the only recurring generation-leased event needed for model-info enrichment; do not restore a second high-frequency scheduler.
4. `ModelInfoService` remains responsible for per-model TTLs, due selection, source cooldowns, and `max_models_per_cycle` bounds.
5. A successful external source fetch with no local-model match is not a source failure.
6. A failed external source must update source-health diagnostics without failing the catalog lifecycle.
7. Startup enrichment must be bounded. Do not fetch an unbounded catalog model-by-model before readiness.
8. Disabled model-info must remain truly dormant: no external model-info requests, no model-info refresh work, and no new task.
9. The dashboard must render canonical truth; do not paper over missing enrichment with synthetic metadata.
10. Manual `/api/model-info/refresh` remains an operator diagnostic/override, not a prerequisite for ordinary enrichment.
11. Do not introduce database migrations for this correction unless implementation inspection uncovers an independent storage defect.
12. Preserve the current lean runtime task inventory unless a separately evidenced defect requires otherwise.

## Workstream A — Pin the lifecycle regression before changing behavior

Add a focused integration-level regression test that reproduces the current failure through the same lifecycle used by production rather than by directly calling `refresh_due_models()`.

Use deterministic fake provider and OpenRouter responses. The fixture should expose at least one provider-discovered model that can be resolved to an OpenRouter catalog row with useful metadata such as display name, external context/output limits, pricing, modalities, or benchmark fields.

The pre-fix behavior should demonstrate:

- provider catalog discovery succeeds;
- the canonical model-info row exists;
- the row remains `sparse_new` after the normal startup/catalog lifecycle;
- OpenRouter has not been requested automatically;
- source health has no successful OpenRouter attempt;
- manually forcing model-info refresh makes the same row enrich correctly.

The post-fix assertion should remove the manual refresh step and prove the normal lifecycle performs the same enrichment automatically.

Do not write a broad end-to-end matrix. One known-match success case plus the failure-isolation cases below is sufficient to guard the ownership seam.

## Workstream B — Establish one shared catalog/model-info tick seam

`src/eggpool/runtime_tasks.py` currently contains two callback construction paths that can drift:

- direct registration in `register_runtime_tasks()`;
- callback construction in `build_callback_factories_for_specs()` for task reconfiguration.

Both must invoke identical model-info behavior.

Extract the catalog-tick body into one small shared async helper, or otherwise make both registration paths call one authoritative function. The helper should operate on the generation acquired for that tick, not a stale process-level compatibility mirror.

Required ordering:

1. `gen.catalog.refresh()`;
2. clear exact quarantine state on authoritative model reappearance;
3. prune health-disabled models using the current generation;
4. if `gen.model_info is not None`:
   - `reconcile_catalog_refresh(result)`;
   - `backfill_missing_canonical()`;
   - `refresh_due_models(force=False)`.

Model-info failures must be isolated from catalog refresh. Prefer one bounded `try/except` around model-info follow-up work with structured logging, or narrower isolated blocks if necessary to preserve later model-info steps after one nonfatal failure. Do not allow an OpenRouter/AA/HF exception to mark the provider catalog refresh task failed if the provider catalog itself succeeded.

Return/log structured counts at DEBUG/INFO only when useful, for example reconciled/backfilled/refreshed/skipped/source failures. Avoid per-model routine INFO logging.

### Active-generation ownership

The shared helper must use the `gen.model_info` service attached to the leased generation. Do not use `process.model_info` or an app-state compatibility mirror as the authority. This matters after live rehash/generation publication even though the full `[model_info]` block is currently restart-required.

### Historical helper cleanup

`src/eggpool/app.py::_catalog_refresh_loop()` is retained for test compatibility and currently reproduces the old incomplete lifecycle. It must not remain a second semantic definition of catalog/model-info interaction.

Preferred order:

1. remove the helper if no retained external/test contract requires it; otherwise
2. rewrite it to delegate to the same shared one-shot catalog/model-info helper used by production.

Do not maintain a legacy loop with intentionally different enrichment behavior.

## Workstream C — Add one bounded startup external-enrichment pass

After startup catalog refresh and canonical reconciliation/backfill, invoke:

```python
await model_info.refresh_due_models(force=True)
```

when and only when:

- `model_info` was actually constructed; and
- `config.model_info.startup_refresh` is true.

`force=True` is appropriate for the startup pass because newly reconciled canonical rows may otherwise have a normal future `next_refresh_at`, making the first catalog-driven enrichment a no-op. The method already bounds the batch by `config.model_info.max_models_per_cycle`.

Required behavior:

- at most `max_models_per_cycle` canonical models are handled in this startup pass;
- a large catalog does not trigger an unbounded source/model loop;
- external sources that support bulk fetch are fetched once per cycle, not once per model;
- remaining due rows drain on later `catalog_refresh` ticks;
- an external failure is recorded in source health and logged but does not fail startup/readiness;
- startup with `model_info.enabled=false` performs no model-info work;
- startup with `model_info.startup_refresh=false` preserves the explicit operator choice and does not perform the forced external pass.

Do not make readiness depend on OpenRouter, Artificial Analysis, Hugging Face, or any other public metadata service.

## Workstream D — Preserve due-work and resource bounds

The correction must not turn the five-minute provider discovery cadence into a full external refetch cadence.

Pin the following behavior around `refresh_due_models(force=False)`:

- `list_due()` remains the normal selector;
- `max_models_per_cycle` remains enforced;
- `ModelInfoRefreshScheduler` remains authoritative for row-specific `next_refresh_at`;
- OpenRouter/AA source adapters retain their own TTL caches;
- source cooldown/rate-limit state remains respected;
- canonical rows that are not due cause no external-model processing;
- unchanged canonical payloads continue to skip unnecessary writes where existing deduplication permits.

Add a bounded drainage test with more canonical rows than `max_models_per_cycle`. Prove one tick handles only the configured batch and a later tick can handle the remainder after they are made due. Do not add a soak test.

## Workstream E — Clarify `model_info.refresh_interval_s`

The current no-separate-scheduler architecture leaves `ModelInfoConfig.refresh_interval_s` without an obvious production scheduling owner. Audit every runtime and documentation reference before changing it.

Do not add a model-info scheduler merely to make this field meaningful. The existing per-row TTL policy includes sparse-model timings that can be shorter than this global interval, so using `refresh_interval_s` as a hard gate could reintroduce delayed enrichment.

Preferred corrective disposition if inspection confirms the field is unused:

- retain it temporarily for configuration parse/backward compatibility;
- mark it deprecated/compatibility-only in comments/documentation;
- state explicitly that recurring opportunities are driven by `[models].refresh_interval_s`, while actual model-info work is selected by per-row `next_refresh_at` and source TTL/cooldown state;
- remove it from active configuration examples so operators do not believe it controls a task that does not exist;
- pin a test or comment preventing accidental future resurrection as a second scheduler.

Only remove the field outright if the repository's current configuration compatibility policy clearly permits rejecting existing configs that contain it. Otherwise defer physical removal to a compatibility-breaking cleanup.

### `models.refresh_interval_s = 0`

Document this edge case explicitly. With catalog refresh disabled, there is no recurring automatic event source. If model-info startup refresh is enabled, the bounded startup pass still occurs; subsequent enrichment requires an explicit manual refresh or process restart. Do not silently create another timer for this configuration.

## Workstream F — Source adapter contract regression checks

The lifecycle bug is not evidence that all source adapters are broken. Add only the contract coverage needed to distinguish scheduler failures, transport failures, parser failures, and match misses.

### OpenRouter

Pin with deterministic HTTP mocks that:

- the catalog request uses the current `/api/v1/models` family;
- a healthy `{ "data": [...] }` model catalog is retained even if optional benchmark enrichment fails or is unauthorized;
- source health records a successful catalog fetch with `last_payload_count > 0` independently of whether a given EggPool model matches;
- a matched record persists its real OpenRouter `source_model_id` and external metadata;
- an empty/malformed model catalog is not cached as a healthy full-TTL result.

Do not make benchmark endpoint availability a prerequisite for useful OpenRouter metadata.

### Artificial Analysis

As of the 2026-09-03 implementation review, current Artificial Analysis Data API documentation uses the same endpoint family EggPool already implements:

- `/api/v2/language/models` for the full model dataset;
- `/api/v2/language/models/free` for the free-tier dataset;
- `x-api-key` authentication;
- benchmark/evaluation values under structured model data including `evaluations`.

Therefore, do **not** perform the previously suspected `/data/llms/models` endpoint migration as part of this corrective pass.

Instead add/retain deterministic adapter tests that pin:

- the current full endpoint path;
- free-tier fallback behavior for the statuses EggPool intentionally supports;
- `x-api-key` presence when configured;
- `evaluations` parsing into benchmark observations;
- source errors remain isolated from catalog/routing behavior.

If implementation work discovers a real current-contract mismatch beyond these verified assumptions, fix only that demonstrated mismatch and document the evidence in the implementation commit/CHANGELOG.

### Hugging Face

No broad Hugging Face changes are required. Preserve alias-driven fetching, optional authentication, source isolation, and source TTL caching. Add coverage only if the shared lifecycle wiring exposes a concrete regression.

## Workstream G — Make source-health and match outcomes diagnostically distinct

After this correction an operator should be able to determine why a model remains sparse without reading SQLite directly.

Use the existing `/api/model-info/sources`, per-model observations, aliases, and match-evidence surfaces. Avoid adding a new dashboard subsystem.

Pin at least these states:

### Source never attempted

Before the first automatic opportunity, `last_success_at`/`last_error_at` may both be absent. Do not label this as a successful source with no data.

### Source fetch failed

A transport/HTTP/parse failure should produce source-health failure evidence such as:

- `last_error_at`;
- bounded/safe error class/status;
- incremented `failure_count`;
- cooldown/rate-limit state when applicable.

The canonical model may remain sparse, but the reason must be externally diagnosable.

### Source succeeded, model did not match

Source health should show success and a nonzero payload count when appropriate. No false source failure should be recorded merely because one local model lacked a safe identity match. Per-model diagnostics/match evidence should explain the miss where the existing service supports it.

### Source succeeded and model matched

The canonical detail should carry useful external evidence and the row should no longer remain `sparse_new` solely because the provider catalog itself was sparse.

Do not fabricate observations or benchmark data to make the dashboard look complete.

## Workstream H — Align summary wording with executable behavior

`_generate_summary()` currently tells sparse-model users:

> Eggpool will refresh external sources more frequently for now.

This statement is only truthful if an automatic event source exists.

After lifecycle wiring, keep or refine the wording so it describes the actual policy: sparse rows become due sooner under the per-model scheduler, while catalog ticks provide the recurring opportunities to execute that due work.

Handle `models.refresh_interval_s = 0` honestly. A configuration with no periodic catalog tick must not promise future automatic background refresh after startup.

Do not solve this by hiding `sparse_new`, suppressing the benchmark-unavailable text, or converting missing values to generic claims. The presentation layer should remain a reflection of durable canonical/source state.

## Workstream I — Correct configuration examples and architecture documentation

Audit and update at minimum:

- `config.example.toml`;
- `architecture/deep-dive-model-info.md`;
- `architecture/deep-dive-background.md`;
- `docs/model-info-openrouter-debug.md`;
- `README.md` if it describes model-info scheduling/configuration;
- `CHANGELOG.md` when implementation lands.

`config.example.toml` currently documents model-info timing names that no longer match the active `ModelInfoConfig` schema. Replace stale examples with the actual current fields, including the status-specific TTL controls where operator-facing documentation is useful:

- `known_ttl_s`;
- `partial_ttl_s`;
- `sparse_new_initial_ttl_s`;
- `sparse_new_later_ttl_s`;
- `sparse_new_accelerated_days`;
- `conflict_ttl_s`;
- `max_models_per_cycle`.

Do not advertise stale names such as `sparse_refresh_interval_s`, `sparse_refresh_window_s`, `default_ttl_s`, or `sparse_ttl_s` if they are not accepted by the current schema.

Document the two-level cadence precisely:

```text
models.refresh_interval_s
    = how often the provider catalog creates an automatic opportunity

model_info next_refresh_at / status TTLs / source TTLs
    = whether external model-info work is actually due at that opportunity
```

The manual debug script must be described as a forced diagnostic and recovery tool, not the expected mechanism for populating ordinary dashboard metadata.

## Required regression matrix

Keep the retained suite focused on the lifecycle contract rather than historical plan behavior.

### Automatic success path

With model-info and OpenRouter enabled and a deterministic matchable model:

- startup or one normal catalog tick requests OpenRouter without a manual POST;
- source health records success and payload count;
- an OpenRouter observation is persisted with the real source model ID;
- match evidence/alias state is persisted as appropriate;
- canonical detail contains at least one external fact;
- canonical status moves out of `sparse_new` when `_refine_status_from_detail()` criteria are met;
- API/dashboard summary reflects the enriched canonical row.

### External-source failure isolation

Simulate OpenRouter failure:

- provider catalog refresh still succeeds;
- routing/catalog state remains usable;
- model-info source health records the failure;
- the model may remain sparse;
- the task survives for future ticks;
- a later healthy source response can recover/enrich without process restart or database deletion.

### Healthy source, no match

- source fetch records success;
- payload count is truthful;
- no external observation is attached to the wrong model;
- canonical row remains provider-only/sparse if no other enrichment exists;
- match diagnostics indicate the miss/ambiguity rather than a source outage.

### Batch bound

- more than `max_models_per_cycle` due rows never cause more than the configured batch to be processed in one cycle;
- later cycles can drain remaining due work.

### Disabled subsystem

With `model_info.enabled=false`:

- no model-info service is constructed;
- no external model-info HTTP request occurs;
- no new background task appears;
- normal provider catalog/routing behavior remains unchanged.

### Startup refresh disabled

With `model_info.startup_refresh=false`:

- no forced startup external pass occurs;
- later normal catalog ticks may still perform due work if periodic catalog refresh is enabled.

### Periodic catalog disabled

With `models.refresh_interval_s=0`:

- no `catalog_refresh` task is registered;
- the bounded startup enrichment pass still follows `model_info.startup_refresh`;
- no hidden model-info scheduler is introduced;
- manual refresh remains functional.

### Generation ownership

Exercise both direct runtime task registration and reconfiguration callback construction. Prove the callback operates on the currently leased generation's `model_info` object, not a stale process/app-state reference.

## Test-suite corrections

`tests/unit/test_runtime_task_inventory.py` currently explicitly verifies that `model_info_refresh` is absent. Preserve this architectural assertion, but rename/comment it so absence of a standalone task cannot be confused with absence of automatic enrichment.

Add a positive lifecycle assertion elsewhere that says, in effect:

> When model-info is enabled, `catalog_refresh` invokes bounded model-info due work through the current generation.

This pair is important:

```text
NO dedicated model_info_refresh task
YES automatic model-info enrichment on catalog lifecycle events
```

A future test-suite simplification must not be able to delete the second half while retaining only the first.

Prefer one integration-style lifecycle test over numerous mocks of private helper calls.

## Manual acceptance

After deterministic tests pass, perform one bounded manual verification on a development instance with model-info enabled.

Use a currently available model known to match OpenRouter. `minimax-m3` is acceptable only if it remains present in both the configured provider catalog and OpenRouter at execution time; otherwise choose another unambiguous current model.

Without first calling `POST /api/model-info/refresh`, verify after startup or the first normal catalog lifecycle opportunity:

```text
GET /api/model-info/sources
```

shows an OpenRouter attempt with either:

- success + positive payload count; or
- explicit failure diagnostics.

For a successful match, verify:

```text
GET /api/model-info/<model-id>
```

contains real external evidence such as an OpenRouter external ID, external limit, display name, pricing, modality, or benchmark observation, and that the row is no longer `sparse_new` when those facts satisfy the existing promotion rule.

Then run `scripts/debug_model_info_openrouter.sh <model-id>` only as a forced-refresh parity diagnostic. The forced path should not be required to move an ordinary healthy installation out of the global sparse state.

Finally inspect `/api/stats/runtime` or the equivalent task snapshot and confirm there is still no additional permanent `model_info_refresh` task.

## Verification commands

Run the focused model-info, runtime-task, catalog, API, and configuration tests selected by the implementation. At minimum identify and run the retained suites covering:

```bash
uv run pytest tests/unit/test_runtime_task_inventory.py -q --tb=short --maxfail=1
uv run pytest tests/unit/test_model_info_openrouter_enrichment.py -q --tb=short --maxfail=1
uv run pytest tests/unit/test_model_info_alias_resolution.py -q --tb=short --maxfail=1
uv run pytest tests/unit/test_model_info_route_registration.py -q --tb=short --maxfail=1
```

Add/run the new lifecycle integration test created by this plan. If actual filenames differ at implementation HEAD, use the retained equivalent suites rather than recreating deleted historical tests solely to satisfy this document.

Before push of implementation code:

```bash
uv run ruff format --check src/ tests/ scripts/
uv run ruff check src/ tests/ scripts/
uv run pyright src/ scripts/
uv run pytest tests/smoke/ -q --tb=short --maxfail=1
```

Do not add network-dependent CI. OpenRouter/AA/HF contracts in the retained suite should use deterministic mocked HTTP responses. Any live verification remains explicit/manual.

## Implementation sequence

### Phase 1 — Reproducer and ownership seam

1. Add the normal-lifecycle regression test that fails because external enrichment is never called.
2. Identify every production/test compatibility definition of a catalog tick.
3. Extract or select one shared one-shot catalog lifecycle helper.
4. Make both runtime task-registration paths call it.

Exit criterion: one normal periodic catalog callback performs reconciliation plus bounded due external enrichment using the leased generation.

### Phase 2 — Startup correction

1. Add the bounded `force=True` startup pass after canonical reconciliation/backfill.
2. Keep all external failures best-effort.
3. Verify `startup_refresh=false` and model-info-disabled behavior.
4. Verify batch bounds on startup.

Exit criterion: a healthy matchable model can enrich automatically during startup without a manual refresh call.

### Phase 3 — Source and diagnostic regression coverage

1. Pin OpenRouter catalog success independent of optional benchmark failure.
2. Pin source-success/no-match versus source-failure semantics.
3. Pin current Artificial Analysis endpoint/auth/evaluation contract with deterministic mocks.
4. Verify later healthy cycles recover after earlier source failure.

Exit criterion: an operator can distinguish transport/parser failure from identity mismatch using existing source/model diagnostics.

### Phase 4 — Configuration and presentation truthfulness

1. Audit/deprecate the dead global `model_info.refresh_interval_s` scheduling field if confirmed unused.
2. Correct stale `config.example.toml` model-info keys.
3. Clarify `models.refresh_interval_s=0` behavior.
4. Adjust sparse-summary wording if necessary so it never promises an automatic future refresh that cannot occur.
5. Update model-info/background/debug documentation.

Exit criterion: public examples, architecture docs, and dashboard wording describe the executable lifecycle exactly.

### Phase 5 — Closure

1. Run focused model-info/catalog/runtime suites.
2. Run ruff, pyright, and smoke gate.
3. Perform one optional bounded live/manual OpenRouter verification.
4. Confirm task inventory remains lean and unchanged except for any deliberate non-model-info changes already present at implementation HEAD.
5. Update `CHANGELOG.md` with the lifecycle regression and correction.

## Non-goals

This plan does not authorize:

- adding a dedicated `model_info_refresh` background task;
- creating a new scheduler framework;
- making external metadata part of routing eligibility;
- redesigning the model-info database schema;
- broad fuzzy model matching changes;
- unconditional network calls in CI;
- Artificial Analysis endpoint migration without new evidence;
- dashboard fabrication of metadata when external sources are unavailable;
- a broad benchmark-source expansion;
- unbounded startup enrichment;
- a new queue/worker merely for model-info;
- replacing the existing manual refresh/debug API.

## Completion criteria

This corrective line of work is complete when all of the following are true:

1. A matchable provider-discovered model can transition automatically from provider-only `sparse_new` state to externally enriched canonical state without invoking the manual refresh endpoint.
2. The startup pass is bounded by `max_models_per_cycle` and cannot make readiness depend on an external metadata provider.
3. Periodic automatic enrichment uses the existing `catalog_refresh` event and `refresh_due_models(force=False)` due selection; no new model-info task exists.
4. Both runtime task-construction paths execute identical catalog/model-info lifecycle behavior against the leased active generation.
5. OpenRouter failure, AA failure, HF failure, malformed payloads, and match misses cannot fail provider catalog refresh or routing.
6. Source-health diagnostics distinguish successful catalog fetch, source error, and model-level no-match conditions.
7. A later healthy refresh can recover a previously sparse/source-failed model without process restart or database deletion.
8. OpenRouter model metadata remains useful even when optional benchmark enrichment is unavailable.
9. Current Artificial Analysis `/language/models` and `/language/models/free` assumptions are contract-tested rather than replaced based on stale documentation.
10. `config.example.toml` uses only current accepted model-info field names and explains the catalog-event/per-row-TTL cadence accurately.
11. The status/summary text no longer makes a false automatic-refresh promise for configurations with no recurring event source.
12. Manual `/api/model-info/refresh` continues to work and produces results consistent with the automatic path.
13. The retained runtime task inventory still contains no standalone model-info refresh task.
14. Focused tests, ruff, pyright, and smoke verification pass.

## Handoff summary

The implementation should be small in architectural terms: reconnect the already-built enrichment engine to the lifecycle event that the current architecture says owns it.

Do not respond to the global dashboard symptom by changing the renderer, fabricating benchmark metadata, introducing a second scheduler, or broadly rewriting source adapters. The primary defect is that `refresh_due_models()` has no automatic caller after the runtime simplification. Restore that invocation at startup and on existing catalog ticks, preserve its existing bounds and source isolation, then pin the full discovery -> fetch -> match -> persist -> canonical merge -> API/dashboard path so this lifecycle cannot be severed again.
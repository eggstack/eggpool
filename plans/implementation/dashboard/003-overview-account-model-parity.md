# Dashboard Milestone 003 — Overview, Account, Model, and Model-Detail Parity

Status: ready

Repository baseline: 17e298f64fa21589f558c43592a24fa91b952ff7 plus closed Dashboard M001-M002

Source roadmap:

- plans/subsystems/dashboard-roadmap.md#milestone-003--overview-account-model-and-model-detail-parity

Long-term requirements:

- plans/000-long-term-specification.md
- plans/001-terminology-and-domain-model.md
- plans/002-long-term-roadmap.md
- plans/003-planning-process.md

Applicable ADRs:

- None required unless implementation discovers that a historical panel can only be restored by changing current runtime/model-info authority.

Primary class: capability

## 1. Objective

Restore the final Python dashboard's Overview, Accounts, Models, and Model Detail pages to near-identical DOM structure and operator information density, sourcing values from authoritative current Rust state rather than placeholders or duplicated dashboard state.

## 2. Why this milestone is ready

Blocked on Dashboard M002. M001 supplies the fixed oracle and M002 supplies the shared shell/interaction contract.

After M002 closes, this milestone has no expected external dependency. Current database/model/catalog/health/metrics owners already expose much of the required information, but the implementer must map missing projections explicitly rather than hard-code values.

## 3. Current implementation evidence

At the research baseline:

- render_overview is materially smaller than the Python renderer and emits only a subset of the historical cards/panels.
- render_models_page currently emits a compact Model/Provider/Avail./Requests/Cost table while the Python renderer contained model-info pills, provider availability/health, pricing exactness/warnings, benchmarks, metadata/limit/conflict information, and richer empty/filter states.
- render_model_detail is similarly reduced relative to the historical model detail page.
- DashboardRepository currently contains useful bounded account/model/request aggregates but not every historical presentation projection.
- Existing Rust code has separate authoritative model catalog, model-info, provider health, status, pricing, and operational metrics owners. Dashboard code must project those owners rather than recreate their logic.
- The preserved CSS already contains many historical model/account/overview classes that current Rust does not emit.

## 4. Invariants that must not regress

- Dashboard rendering remains observational/read-only.
- Provider/model availability and health are derived from authoritative current Rust owners; request-count presence is not a substitute for health unless that is the actual current contract.
- Unknown/unavailable metadata remains unknown/unavailable; no inferred capability, context window, benchmark, pricing exactness, or healthy state is fabricated.
- Current /v1/models and integration-profile contracts remain unchanged.
- Disabled-account semantics continue to match current durable/config ownership; dashboard filtering cannot re-enable or mutate accounts.
- Escaping, secret redaction, bounded queries, and dashboard auth remain intact.
- No route fetches live provider APIs synchronously merely to render a dashboard page.

## 5. Scope

### In scope

Overview:
- historical metric-card groups and labels;
- system/provider health and reservation/fallback warnings where backed by current state;
- account breakdown filters/chips/disabled toggles;
- model/event glance panels;
- bandwidth/timeseries/heatmap/chart hooks assigned by the oracle;
- request-shaping/cache/thinking/update indicators when current authoritative sources exist.

Accounts:
- historical table columns, enabled/disabled filters/toggles, usage/cost/error/token summaries, backoff/status presentation, empty states.

Models:
- historical model/provider table structure;
- availability/status pills;
- model-info summaries;
- pricing exactness/warnings;
- benchmark summaries;
- model link/filter behavior;
- provider/collapsed-model presentation consistent with current configuration.

Model detail:
- observations, limits, provider health/availability;
- benchmarks;
- Hugging Face/model metadata if current data still owns it;
- pricing/source/conflict/warning sections;
- missing/unknown optional-state behavior.

Shared bounded view-model projections required only by these pages.

### Explicitly out of scope

- Latency/events/timeseries/bandwidth/pings/reliability/routing/traces rich parity (M004).
- Runtime/cache pages and cache/request-shaping dashboard JSON APIs (M005).
- New model discovery, benchmark collection, pricing sources, provider probes, or health policy.
- Redesigning historical layout or CSS.

## 6. Required production changes

Introduce or extend dashboard view models that gather already-authoritative facts before rendering. Avoid having HTML functions issue ad-hoc async reads.

For each historical panel, identify its present authority:

- durable usage/account/request aggregates from rust/src/db;
- catalog/model metadata from current catalog/model-info owners;
- health/backoff from current health/status owners;
- request-shaping/thinking/cache counters from operations/metrics or current observability owners;
- update information from existing update/status projection without granting update control.

If the current architecture no longer produces a historical field, render the oracle's unavailable/unknown state where one existed. Do not invent zero/good values to make markup convenient.

Preserve historical classes/IDs/data attributes/tooltips and link/query propagation even when the underlying Rust view-model types differ from Python.

## 7. Ordered work packages

### Work package A — Page contract matrix and authority mapping

Intent: prevent markup restoration from quietly duplicating business logic.

Required changes:

- Use M001 oracle to enumerate every panel/control/value family on the four routes.
- Map each to a current Rust owner, a bounded derived projection, or a genuine unavailable state.
- Document any field with no valid modern authority before implementation.

Acceptance evidence:

- Reviewable matrix has no "derive from UI/request count" shortcuts for health, pricing exactness, capability, or limits.

### Work package B — Overview parity

Intent: restore the dashboard landing page first as a complete consumer of the shared shell.

Required changes:

- Port historical section/card/panel structure and classes.
- Restore account/model/event/bandwidth/timeseries/health/warning/glance surfaces required by the oracle.
- Restore disabled-account and relevant overview filters without mutation.
- Reuse M002 chart/API hooks.

Acceptance evidence:

- Empty/populated/escaping/private Overview passes full oracle projection.
- Matched desktop/mobile captures show no unexplained material layout regression.

### Work package C — Accounts parity

Intent: restore account-level operational density.

Required changes:

- Port historical controls/table/status/backoff/usage/cost rendering.
- Preserve enabled/disabled semantics and stable ordering.
- Ensure long names/Unicode/status error text escape and wrap/overflow as before.

Acceptance evidence:

- Oracle DOM/table/form/status projection passes across enabled/disabled/populated/empty states.

### Work package D — Models list parity

Intent: restore the model catalog operator surface.

Required changes:

- Port model filtering/linking, availability/status, metadata/benchmark/pricing presentation.
- Use current provider/collapse semantics rather than historical Python internals when computing data, while preserving the frontend presentation contract.
- Keep unknown facts explicit.

Acceptance evidence:

- Oracle full DOM/content projection passes for multi-provider, collapsed/non-collapsed as applicable, missing metadata, pricing warning, and benchmark-present states.

### Work package E — Model detail parity

Intent: restore the richest model inspection route.

Required changes:

- Port detail sections, limits, observations, health, benchmark, metadata, pricing/conflict/warning markup.
- Preserve percent-encoded model paths and safe handling of slash/Unicode/special characters.
- Match historical missing-model/missing-metadata behavior where still compatible with current server semantics.

Acceptance evidence:

- Detail page passes oracle for full, sparse, missing, long/Unicode, and escaped values.

## 8. Failure, cancellation, restart, contention semantics

All new projections must be bounded reads. If one authoritative optional source is unavailable, prefer the historical bounded unavailable state rather than failing the entire page unless the oracle treats it as page-critical.

DB/system read failures must not leak raw errors. Dashboard reads do not mutate health/backoff/catalog state and do not perform provider I/O.

Concurrent requests must not share mutable renderer state. Any cached immutable metadata snapshot must use existing runtime ownership and generation semantics rather than a dashboard-specific cache.

## 9. Compatibility and migration

No schema migration is expected. If an old page requires durable data that current schema no longer stores, first determine whether the information is available from a live authoritative owner. Adding storage solely for visual parity is out of scope and a stop condition pending separate review.

Keep current page URLs and query behavior. Preserve historical frontend structure while using current Rust semantics for authoritative data.

## 10. Required tests

Focused integration/contract coverage:

- Overview empty/populated/system-health/warning/filter/chart/glance states.
- Account enabled/disabled filtering, backoff/status, zero/nonzero usage/cost, long/Unicode/escaped names.
- Models multi-provider, collapsed/non-collapsed if currently supported, availability, benchmark/pricing/model-info present/missing, warnings.
- Model-detail encoded path, sparse/full metadata, missing model, missing model-info, conflicting/unknown values.
- No fabricated health/availability/pricing/capability facts.
- No raw credential/request/provider content in rendered pages.
- M001 full-DOM projection on all four routes.
- Matched browser desktop/mobile/theme pair for each route, including dense and sparse states.

## 11. Required verification commands

Focused:

    cargo test --manifest-path rust/Cargo.toml --test dashboard_parity -- --test-threads=1
    cargo test --manifest-path rust/Cargo.toml --test server_transport -- --test-threads=1
    cargo test --manifest-path rust/Cargo.toml --test status_command -- --test-threads=1
    cargo test --manifest-path rust/Cargo.toml --test catalog_refresh -- --test-threads=1
    cargo test --manifest-path rust/Cargo.toml --test model_router -- --test-threads=1
    uv run pytest tests/tooling/ -q --tb=short --maxfail=1

Add the narrow model-info/metrics target names current at execution time and record them exactly in closure.

Repository gates:

    cargo fmt --manifest-path rust/Cargo.toml --all -- --check
    cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
    cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
    cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
    cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
    uv sync --frozen
    uv run ruff format --check scripts/ tests/tooling/
    uv run ruff check scripts/ tests/tooling/
    uv run pyright scripts/
    uv run pytest tests/tooling/ -q --tb=short --maxfail=1
    git diff --check

## 12. Documentation updates

- Reconcile architecture/deep-dive-dashboard.md for the four restored routes and their authoritative data owners.
- Update operator/API docs only where visible labels/filters or dashboard behavior need explanation.
- Do not document M004/M005 surfaces as restored early.

## 13. Acceptance criteria

- Overview, Accounts, Models, and Model Detail pass full M001 structural/content contract checks.
- Historical controls/panels/classes are present rather than replaced with condensed summaries.
- Values come from current authoritative Rust state or explicit unknown/unavailable representation.
- No hard-coded good/zero state substitutes for missing telemetry.
- Matched browser review shows near-identical layout at desktop/mobile with the same theme/state.
- No regression to auth, escaping, model routing/catalog, or inference APIs.

## 14. Stop conditions

Stop and report rather than improvise when:

- M002 is not closed;
- a historical field has no safe current authority and displaying it would mislead;
- implementation would require provider network I/O in page rendering;
- durable schema/request-content storage is proposed only for UI parity;
- parity would require changing /v1/models or routing/model-selection semantics;
- page work crosses into M004/M005 enough to make milestone boundaries meaningless.

## 15. Closure evidence required

- authority mapping matrix;
- per-route requirement-to-evidence table;
- oracle DOM/API before/after report;
- matched browser capture hashes/disposition;
- secret/escaping/auth review;
- focused and full verification results;
- unresolved field mappings with severity;
- unblock audit for M005 and any M004 parallel work.

## 16. Handoff notes

Do not translate Python implementation architecture literally. Translate its rendered contract while sourcing data from current Rust owners.

The easiest wrong solution is to recreate rich markup with fabricated/default values. Unknown must remain unknown.

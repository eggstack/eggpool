# Dashboard Milestone 004 — Telemetry, Routing, Reliability, and Trace Parity

Status: ready

Repository baseline: 17e298f64fa21589f558c43592a24fa91b952ff7 plus closed Dashboard M001-M002

Source roadmap:

- plans/subsystems/dashboard-roadmap.md#milestone-004--telemetry-reliability-routing-and-trace-parity

Long-term requirements:

- plans/000-long-term-specification.md
- plans/001-terminology-and-domain-model.md
- plans/002-long-term-roadmap.md
- plans/003-planning-process.md

Applicable ADRs:

- None required unless a missing historical diagnostic would force a new durable/runtime ownership boundary.

Primary class: capability

## 1. Objective

Restore the final Python dashboard's Latency, Events, Timeseries, Bandwidth, Pings, Reliability, Routing, and Traces pages, including their chart/table/filter/diagnostic DOM and interactions, using bounded authoritative Rust observations.

## 2. Why this milestone is ready

Blocked on Dashboard M002. M001 provides the strict oracle; M002 restores the shared shell and timeseries/grouped JSON contract required by several pages.

M003 is a soft dependency and may proceed in parallel after M002 if renderer/view-model ownership stays disjoint. M004 must consume shared helpers from M002 rather than copying them.

## 3. Current implementation evidence

At the research baseline:

- Current Rust renderers provide compact tables/cards for these routes but omit a large set of historical chart, heatmap, filter, status, and diagnostic structures.
- DashboardRepository already loads bounded requests, events, provider pings, retry aggregates, routing aggregates, timeseries aggregates, and cache byte/counter summaries.
- Historical dashboard.js still contains grouped timeseries, static chart, progressive chart-shell, timeseries-control, number-stepper, and refresh logic that current Rust markup often does not activate.
- Current Rust route set lacks the two timeseries JSON endpoints needed by those retained JS paths; M002 owns their restoration.
- Reliability/routing/traces historical pages exposed more diagnostic detail than current compact renderers, including provider/attempt/exclusion/selection and trace context panels.
- All restored data must remain aggregated/metadata-only and bounded.

## 4. Invariants that must not regress

- No raw request/prompt/tool/provider body is exposed in traces/events.
- Routing diagnostics describe authoritative decisions; dashboard rendering never recomputes or influences selection.
- Retry/reliability categories use existing coordinator/attempt classifications and do not invent a second taxonomy.
- Health/ping state is observational; rendering does not trigger probes.
- Timeseries/grouping/filter queries are bounded by period, bucket/group/cardinality/row limits.
- Existing single SQLite gate/persistence ownership is preserved.
- JS/CSS assets remain fixed; restore their producers.

## 5. Scope

### In scope

Latency:
- historical provider/model latency and TTFT tables/cards/charts/phase details.

Events:
- historical ordered event table/tags/detail presentation and escaped long/error values.

Timeseries:
- aggregate and grouped charts/tables;
- bucket/group/metric/account/model controls;
- number steppers and filter behavior;
- chart bootstrap and AJAX refresh semantics from M002.

Bandwidth:
- receive/emit cards, daily/heatmap/chart/table presentation and relevant filters.

Pings:
- provider/account/status/latency/model-count views and historical summary/chart presentation.

Reliability:
- attempt totals, retry/success/failure cards;
- retry category/distribution/provider chart/table;
- pending health/operational event panels if current authoritative sources exist.

Routing:
- decision/eligible/scored/excluded/selected-account cards;
- distribution/selection/exclusion taxonomy/guardrail panels.

Traces:
- ordered request/attempt trace table;
- status/latency/model/account/provider context;
- bounded diagnostic panel with no raw content.

Required bounded dashboard view-model/query extensions.

### Explicitly out of scope

- Overview/accounts/models/model detail (M003).
- Runtime/cache/request-shaping rich parity (M005).
- New routing/retry/health algorithms.
- Increasing telemetry retention or storing raw request bodies.
- New provider probes or background jobs.
- Frontend redesign.

## 6. Required production changes

Extend dashboard view models/repository queries only where the historical presentation cannot be produced from existing bounded DashboardData. Prefer aggregating within SQL/current metrics owners instead of loading unbounded rows into Rust.

Where historical pages need live runtime/health facts, consume existing immutable/bounded snapshots. Do not have server renderers acquire coordinator locks or execute policy.

Restore the oracle's chart/data structures exactly enough for the unchanged JS:

- canvas element types;
- static-chart-data and grouped-timeseries-data scripts;
- data-chart-id, data-chart-endpoint, data-metric, data-period;
- timeseries controls and data-auto-submit;
- heatmap class/hitbox/label structure;
- empty/loading/error chart states.

All JSON inserted into script tags must use the existing safe JSON escaping contract.

## 7. Ordered work packages

### Work package A — Telemetry view-model/query audit

Intent: establish bounded authoritative sources before markup work.

Required changes:

- Map every M004 historical panel to current DB/metrics/health/routing/attempt owners.
- Define query/result bounds and stable ordering.
- Identify any historical diagnostic no longer safely observable.

Acceptance evidence:

- No panel depends on raw body content, provider I/O, unbounded row load, or policy recomputation.

### Work package B — Latency, Events, Bandwidth, and Pings

Intent: restore simpler telemetry views before the interactive grouped page.

Required changes:

- Port historical cards/tables/charts/heatmaps/status tags and controls.
- Preserve stable ordering, units/formatting, empty/error states, escaping, and long-value behavior.

Acceptance evidence:

- Four routes pass full M001 oracle projection in empty/populated/escaping states.
- Static chart hooks initialize without console/network failures.

### Work package C — Timeseries grouped/interactive parity

Intent: fully consume the M002 JSON compatibility surface.

Required changes:

- Restore historical aggregate/grouped chart/table layout and controls.
- Match bucket/group/metric/filter semantics and query propagation.
- Verify top period selector and page-specific controls update all historical surfaces consistently.
- Preserve bounded cardinality and deterministic series ordering.

Acceptance evidence:

- Oracle DOM/API projection passes.
- Browser interaction changes period/group/metric/filter without duplicate intervals/canvases or stale detached updates.

### Work package D — Reliability and Routing diagnostics

Intent: restore operational diagnostics without moving policy ownership.

Required changes:

- Port historical retry/provider/health/routing cards, charts, tables, exclusion/selection diagnostics, and warnings.
- Source classification/counts from current authoritative attempts/routing/health facts.

Acceptance evidence:

- Controlled fixtures prove rendered counts/categories equal source facts.
- No dashboard-side selection or retry classification logic exists.

### Work package E — Trace parity and content boundary

Intent: restore useful trace diagnostics while preserving the strongest privacy boundary.

Required changes:

- Port historical trace table/diagnostic structure.
- Restrict fields to metadata already approved for dashboard display.
- Add explicit negative tests that raw body/prompt/tool arguments/cache key/auth values cannot appear even when synthetic fixture data contains sentinel strings in prohibited columns.

Acceptance evidence:

- Full oracle route projection passes.
- Secret/content sentinel scan remains negative.

## 8. Failure, cancellation, restart, contention semantics

A failed optional diagnostic source should degrade the associated panel according to the historical unavailable/empty contract without changing runtime state. A DB failure may degrade the page via the established bounded 503 path.

Browser refresh errors remain bounded and do not create tight retries or duplicate timers.

No dashboard query may hold the SQLite gate across network or browser work. No new background telemetry collector is introduced.

## 9. Compatibility and migration

No schema migration expected. If parity reveals that current persistence stopped recording a safe metadata field formerly used by the dashboard, stop and assess whether restoring that metadata belongs to the authoritative producer subsystem; do not silently add dashboard-owned persistence.

Existing current telemetry APIs remain intact. Restored dashboard-only endpoints from M002 retain their historical compatibility shapes.

## 10. Required tests

- Latency/TTFT present/missing/zero/provider-model ordering.
- Events escaping/order/type tags/long detail.
- Timeseries period/bucket/group/metric/filter validation and bounds.
- Grouped data deterministic ordering and empty series.
- Bandwidth byte formatting/heatmap empty/populated.
- Pings success/error/missing status and stable order.
- Reliability attempt/retry/success/failure categories and provider distribution.
- Routing eligible/scored/excluded/selection aggregates and taxonomy.
- Trace success/error/status/latency/order plus prohibited-content sentinel scan.
- Chart/bootstrap JSON escaping and selector/data-hook parity.
- Public/private dashboard auth.
- Browser no-console/no-failed-fetch/no-duplicate-chart after repeated interactions.

## 11. Required verification commands

Focused:

    cargo test --manifest-path rust/Cargo.toml --test dashboard_parity -- --test-threads=1
    cargo test --manifest-path rust/Cargo.toml --test server_transport -- --test-threads=1
    cargo test --manifest-path rust/Cargo.toml --test routing_domain -- --test-threads=1
    cargo test --manifest-path rust/Cargo.toml --test routing_claims -- --test-threads=1
    cargo test --manifest-path rust/Cargo.toml --test status_command -- --test-threads=1
    uv run pytest tests/tooling/ -q --tb=short --maxfail=1

Add current focused health/attempt/database targets if the implementation touches their projection seams.

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

- Reconcile architecture/deep-dive-dashboard.md telemetry route/data-source descriptions.
- Update dashboard/API docs for restored grouped/timeseries query parameters and bounds.
- Document privacy/content exclusions for traces/events where appropriate.

## 13. Acceptance criteria

- All eight M004 routes pass the full oracle DOM/content/API contract.
- Historical interactive chart/filter hooks are live rather than dead asset code.
- Rendered telemetry matches authoritative source facts and never drives runtime policy.
- No raw request/provider content or credentials appear.
- Queries are bounded and preserve the single SQLite ownership model.
- Matched browser runs show near-identical desktop/mobile rendering and zero unexplained console/network failures.

## 14. Stop conditions

Stop and report rather than improvise when:

- M002 is not closed;
- a historical diagnostic requires policy recomputation in dashboard code;
- parity would require raw content retention/display;
- a needed query is unbounded or would require a second DB pool/gate;
- restoring a missing durable metric belongs to another runtime subsystem and changes its storage contract;
- asset edits are proposed merely to accommodate missing Rust hooks.

## 15. Closure evidence required

- per-route authority/query mapping;
- bounds/cardinality evidence;
- oracle DOM/API before/after report;
- browser interaction/capture evidence;
- prohibited-content sentinel results;
- routing/retry/health invariant review;
- focused/full verification results;
- unresolved diagnostics with severity;
- unblock audit for M005/M006.

## 16. Handoff notes

M004 may run in parallel with M003 only after M002 closes and only if shared renderer/view-model interfaces remain stable.

The retained JS already implements much of the desired chart behavior. Prefer restoring its historical HTML/API inputs to writing a second JS behavior.

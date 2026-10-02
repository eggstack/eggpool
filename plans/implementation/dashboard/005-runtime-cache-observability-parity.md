# Dashboard Milestone 005 — Runtime and Cache Observability Parity

Status: conditionally closed

Repository baseline: 17e298f64fa21589f558c43592a24fa91b952ff7 plus closed Dashboard M001-M002; M003/M004 interfaces stable and required closed before this milestone closes

Source roadmap:

- plans/subsystems/dashboard-roadmap.md#milestone-005--runtime-and-cache-observability-parity

Long-term requirements:

- plans/000-long-term-specification.md
- plans/001-terminology-and-domain-model.md
- plans/002-long-term-roadmap.md
- plans/003-planning-process.md

Applicable ADRs:

- None required if the work only projects existing authoritative state. Stop for an ADR/planning review if restoration would create a new durable observability protocol, runtime ownership boundary, or persistence contract.

Primary class: capability

## 1. Objective

Restore the final Python Runtime and Cache dashboard surfaces, including their historical dashboard stats endpoints, using bounded authoritative current Rust telemetry. Eliminate hard-coded zero/good/unavailable presentation that currently substitutes for real runtime/cache/request-shaping facts.

## 2. Why this milestone is ready

Dashboard M002 supplies the shared shell, and M003/M004 are now closed with
stable view-model interfaces. M005 is active against those contracts; it
cannot close until its Runtime/Cache page and populated API parity gaps are
resolved or explicitly dispositioned by the required closure evidence.

Current Rust already has runtime lifecycle diagnostics, task/provider/client/status/metrics owners and durable request cache counters. The work is primarily safe projection and rendering, not new runtime behavior.

## 3. Current implementation evidence

At the research baseline:

- render_runtime_page reports Outbound builds = 0 and Outbound requests = 0, uses only total provider/request counts for part of the page, and omits most historical runtime/generation/client/task/reload diagnostics.
- render_cache_page contains many literal values such as no changes, Clean, Isolated, 0, no, and unavailable marks rather than projecting the full historical cache/request-shaping state.
- Historical Python dashboard routes included:
  - /api/stats/transcoding
  - /api/stats/cache-observability
  - /api/stats/canonical-request-segmentation
  - /api/stats/cache-stability
  - /api/stats/request-shaping
- Current Rust architecture already identifies operations/metrics.rs, operations/status.rs, runtime_lifecycle/diagnostics.rs, task supervision, health/routing state, and DB repositories as current authorities for relevant bounded observations.
- The dashboard must not infer safety/health from absence of data.

### Current strict-qualification blockers

The tracked strict report is `tests/fixtures/dashboard-python-oracle/current-gap-report.json` (candidate `3cf7671`; nine total gaps). It records three Runtime/Cache DOM cells and two M005 stats-API cells. Four additional overview/account/model DOM cells remain M003/M006-owned. The source-truth dispositions are:

- Runtime task inventory differs in empty and populated states: the frozen Python snapshot lists `catalog_refresh` and `retention_cleanup`; Rust's supervisor also registers the live `checkpoint` and `metrics_flush` tasks. Keep the authoritative Rust inventory visible rather than hiding registered tasks to match the fixture.
- Runtime host load average differs on macOS because the Rust page reports it as unavailable rather than spawning a utility or adding an unsafe host API solely for a dashboard request. Linux reads the bounded `/proc/loadavg` snapshot.
- Populated `/api/stats/cache-observability` returns HTTP 500 in the frozen Python oracle while Rust returns HTTP 200 with its bounded response. Preserve Rust's successful response; this is an oracle defect requiring an explicit closure disposition.
- Populated `/api/stats/request-shaping` differs because the Python projection reports one known cache-status row after collapsing multiple distinct unknown raw statuses, while Rust counts each unknown row. Keep Rust's raw-row count; accepting an undercount requires an explicit semantic disposition.
- Cache empty-state advanced segmentation and routing-guardrail structure now matches the frozen source; its populated page retains the intentional unknown-status row-count difference described above.
- Runtime's two strict DOM cells (empty/populated) expose host load unavailable on macOS; the same snapshots also register `checkpoint` and `metrics_flush`, which the frozen Python supervisor lacks. M005 accepts current Rust ownership and reports host load unavailable without a safe source.
- The full report retains four M003 DOM findings plus these M005 exceptions for M006's final full-contract disposition. M005 owns its Runtime/Cache DOM and two populated stats API findings.

Runtime projection now includes process parent/daemon hints, process uptime, host platform, safe load-average text where `/proc/loadavg` is available, and source-matched metric labels. Cache rendering now restores the frozen segmentation totals table and six-card grouping, restores the allowed scorer-input summary, and keeps cache/routing flags tied to current scorer behavior. Focused unit coverage verifies runtime age formatting, platform labels, and bounded load-average behavior. These changes improve the source-backed surface but do not erase the strict mismatches listed above.

The qualification comparator now applies the Runtime metric/text volatility normalization already used by frozen Runtime captures to both live sides. It retains exact DOM structure, labels, attributes, and non-normalized text; a focused tooling test guards label differences.

## 4. Invariants that must not regress

- Runtime/generation/task/client/reload ownership remains with existing runtime modules; dashboard is projection-only.
- Cache/request-shaping metrics never expose cache keys, prompt/body content, tool arguments, or provider bodies.
- "Clean", "isolated", "safe", zero, or no-change are shown only when backed by authoritative facts; absence of collection is unknown/not collected.
- Operational APIs that are currently authenticated remain authenticated; restoring historical dashboard stats routes must follow the historical dashboard data auth classification without broadening /api/status, integration, update, or control access.
- Metrics snapshots are bounded and do not require a global blocking lock across rendering.
- No additional background metrics collector or second SQLite pool is introduced.

## 5. Scope

### In scope

Runtime page:
- historical runtime generation/task/provider-client/outbound/request/reload/loss/health panels and cards that can be projected from current owners;
- update/version indicator integration where historical layout expects it;
- active/retiring ownership presentation without exposing internals that violate current safety boundaries.

Cache page:
- request shaping/change mode;
- provider cache counters/coverage/hit/write rates;
- canonical segmentation categories;
- transcoding counts;
- compression/reporting mode;
- stable-prefix/policy availability;
- cache stability and routing-isolation/guardrail panels;
- historical detail/advanced panels and number/label structure.

Dashboard JSON:
- restore the five historical dashboard stats endpoints listed above with bounded schemas and error/auth behavior;
- reuse one shared DTO/projection layer for HTML and JSON so displayed values cannot drift from API values.

### Explicitly out of scope

- Changing cache/transcoding/compression algorithms.
- New prompt/cache-key persistence.
- New routing isolation policy.
- New reload/generation/task lifecycle semantics.
- Updating the non-dashboard /api/status contract unless a separate owner explicitly requires it.
- Fabricating historical fields unavailable in the current runtime.
- Frontend redesign.

## 6. Required production changes

Create bounded snapshot adapters from current authoritative owners into dashboard-specific DTOs. HTML and restored JSON endpoints should consume the same DTOs where semantics overlap.

Likely authority review includes:

- rust/src/operations/metrics.rs for counters/throughput/cache/transcoding/request-shaping observations already owned there;
- rust/src/operations/status.rs for bounded provider/proxy/readiness facts;
- rust/src/runtime_lifecycle/diagnostics.rs and generation manager snapshots for active/retiring lifecycle facts;
- rust/src/task_supervisor.rs for task observations;
- rust/src/providers/client_pool.rs or its existing bounded diagnostics for client topology/counts, without exposing credentials/URLs if not already approved;
- rust/src/db repositories for durable request/cache/usage aggregates;
- rust/src/config_reload_policy.rs/reload diagnostics only through existing safe projections, not by reclassifying config in dashboard code.

If no current owner exposes a historical field, represent unavailable/not collected using the historical UI state if possible. Do not add a runtime counter unless the owning subsystem agrees it is safe, bounded, and independently useful; such an addition must remain in that owner and be covered by its tests.

## 7. Ordered work packages

### Work package A — Runtime/cache authority and privacy matrix

Intent: distinguish safely observable current facts from obsolete Python implementation details.

Required changes:

- Enumerate every historical Runtime/Cache panel/field/API field.
- Map it to current authority, safe derived projection, or unavailable state.
- Classify privacy sensitivity and update frequency.
- Identify fields that would require new owner instrumentation before coding them.

Acceptance evidence:

- No field is sourced from renderer inference, raw content, or an unowned global.
- Any proposed owner instrumentation is separately testable and does not move policy into dashboard code.

### Work package B — Shared DTOs and restored JSON APIs

Intent: eliminate HTML/API drift and hard-coded values.

Required changes:

- Define bounded dashboard DTOs for transcoding, cache observability, segmentation, cache stability, request shaping, and runtime snapshot facts.
- Restore the five historical GET endpoints with query/auth/status/schema parity.
- Sanitize and cap arrays/maps/cardinality.

Acceptance evidence:

- API oracle projection passes empty/populated/unavailable/invalid/private/public cases.
- Secret/content sentinels cannot appear.
- HTML tests can construct pages from the same DTOs.

### Work package C — Runtime page parity

Intent: restore data-backed operational visibility.

Required changes:

- Port historical runtime card/panel/details structure and classes.
- Populate from authoritative DTOs, including active/retiring/client/task/outbound/reload/loss/health facts where available.
- Preserve historical unavailable/degraded states.

Acceptance evidence:

- Fixture/runtime snapshot variations change rendered values predictably.
- No literal zero/good state remains where the value is dynamic or unknown.
- Oracle full-DOM projection passes.

### Work package D — Cache page parity

Intent: restore cache/request-shaping information density.

Required changes:

- Port historical cards, advanced/details panels, warnings/status labels, and relevant controls.
- Populate from restored cache/request-shaping DTOs.
- Preserve distinction among provider cache hit, write/warmup, counter coverage, canonical segmentation, transcoding, compression/reporting, and routing isolation.

Acceptance evidence:

- Controlled metrics snapshots yield exact expected page/API values.
- Unknown/not-collected versus measured zero are distinguishable.
- Oracle full-DOM projection passes.

### Work package E — Lifecycle/content-boundary qualification

Intent: prove richer live snapshots remain safe under runtime change.

Required changes:

- Test active/retiring generation transitions, empty/no-provider state, reload diagnostics, task disappearance, and metrics unavailable/partial state.
- Ensure dashboard reads remain bounded during concurrent lifecycle transitions and never keep retired generations alive unnecessarily.
- Add prohibited-content/credential sentinel tests.

Acceptance evidence:

- No panic/deadlock/stale generation retention.
- No secret/content leakage.
- Dashboard failures do not alter runtime lifecycle.

## 8. Failure, cancellation, restart, contention semantics

Snapshot acquisition must be bounded and non-owning. A dashboard request must not acquire a generation lease whose lifetime changes request execution/drain semantics unless that is already the standard safe status projection contract.

If one optional snapshot source fails, render the historical unavailable state or return the established bounded page/API failure according to source criticality. Never fall back to a misleading zero/safe state.

Concurrent reload/shutdown may make a snapshot stale immediately after capture; each response must be internally coherent enough to describe one bounded observation, not promise transactional truth across all runtime owners.

## 9. Compatibility and migration

No schema/config migration expected.

Restored historical dashboard stats endpoints are read-only compatibility surfaces. Keep current non-dashboard status/integration/runtime/update endpoints unchanged.

If current metrics names/units differ, adapt internally to historical dashboard DTO units rather than changing the frontend contract, provided the semantic mapping is exact. Stop if the old label would become materially misleading.

## 10. Required tests

- Every restored dashboard stats endpoint: empty/populated/partial/unavailable/invalid/auth/bounds.
- Runtime active/retiring/no-generation/provider-client/task/reload/loss/health states.
- Cache measured-zero versus unavailable, cache read/write counters, coverage, segmentation categories, transcoding, request shaping/compression/reporting modes.
- Routing isolation/guardrail derived only from authoritative source.
- Shared DTO equality between API and rendered card/table values.
- Prohibited-content and credential sentinel tests.
- Concurrent reload/shutdown snapshot safety with bounded timeout and observable synchronization, not fixed sleeps.
- Full M001 DOM/API projection and matched browser render for Runtime/Cache.
- No console/network errors from restored page interactions.

## 11. Required verification commands

Focused:

    cargo test --manifest-path rust/Cargo.toml --test dashboard_parity -- --test-threads=1
    cargo test --manifest-path rust/Cargo.toml --test server_transport -- --test-threads=1
    cargo test --manifest-path rust/Cargo.toml --test status_command -- --test-threads=1
    cargo test --manifest-path rust/Cargo.toml --test runtime_lifecycle_r009 -- --test-threads=1
    cargo test --manifest-path rust/Cargo.toml --test runtime_lifecycle_r005 -- --test-threads=1
    uv run pytest tests/tooling/ -q --tb=short --maxfail=1

Add exact current metrics/reload/task focused targets when implementation touches those owners; record the actual commands in closure.

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

- Reconcile architecture/deep-dive-dashboard.md Runtime/Cache data sources and restored APIs.
- Link to current runtime/metrics ownership docs instead of duplicating their policy.
- Update operator/API documentation for restored dashboard stats endpoints, units, bounds, and unavailable semantics.

## 13. Acceptance criteria

- Runtime and Cache pages match the historical DOM/information contract except documented semantically obsolete facts.
- Dynamic values are sourced from current authoritative Rust owners or explicitly unavailable, never fabricated.
- All five historical dashboard stats endpoints are restored with bounded safe schemas.
- HTML and JSON values share one projection layer where semantics overlap.
- No secret/raw content/cache key appears.
- Concurrent runtime lifecycle transitions remain safe.
- Matched browser evidence shows near-identical layout and zero unexplained frontend failures.

## 14. Stop conditions

Stop and report rather than improvise when:

- M002 is not closed or M003/M004 DTO boundaries are unstable;
- a historical value would require raw request/body/cache-key storage;
- a new metric would move policy/ownership into dashboard code;
- preserving an old label would misrepresent current semantics;
- snapshot acquisition would materially interfere with generation drain/reload;
- a new durable observability protocol/schema is required without architecture/ADR review.

## 15. Closure evidence required

- field-to-authority/privacy matrix;
- restored API schema/auth/bounds matrix;
- hard-coded-placeholder removal evidence;
- runtime lifecycle contention/reload results;
- prohibited-content/secret scan;
- oracle DOM/API and matched browser results;
- focused/full default/no-default/tooling verification;
- medium+ unresolved finding review;
- unblock audit for M006.

## 16. Handoff notes

Runtime/Cache are the highest-risk parity pages because the Python UI was rich and the Rust migration substituted simplified/hard-coded presentation.

Do not optimize for matching labels alone. The underlying values must be trustworthy.

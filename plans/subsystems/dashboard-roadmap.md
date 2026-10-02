# Dashboard Parity Roadmap

Status: active

Long-term references:

- plans/000-long-term-specification.md — preserve a coherent, operable EggPool runtime while keeping application adapters thin and bounded.
- plans/001-terminology-and-domain-model.md — capability/invariant classification and operator-visible behavior.
- plans/002-long-term-roadmap.md — cross-phase execution rules and on-demand subsystem planning.
- plans/003-planning-process.md — typed dependencies, closure evidence, and corrective-pass rules.

Related ADRs:

- None required. This roadmap restores an already-shipped dashboard compatibility surface from immutable repository history; it does not select a new frontend framework, protocol, authentication model, or runtime owner.

## 1. Purpose and ownership boundary

This subsystem owns EggPool's observational web dashboard compatibility surface: server-rendered HTML, dashboard-only JSON read endpoints, the DOM/JavaScript contract consumed by the embedded static assets, theme/static delivery, bounded dashboard view models, and parity qualification.

Production ownership remains split as follows:

- rust/src/server/dashboard/ owns dashboard HTTP adaptation and rendering.
- rust/src/server/mod.rs owns route assembly and the existing dashboard-public authentication exemption boundary.
- rust/src/db/ owns durable dashboard queries.
- rust/src/operations/metrics.rs, rust/src/operations/status.rs, rust/src/runtime_lifecycle/diagnostics.rs, health state, and other existing runtime owners remain authoritative for live observations; dashboard code may project those facts but must not duplicate their state machines.
- rust/assets/dashboard/ owns the embedded CSS, JavaScript, Chart.js, favicon, and themes.
- rust/tests/ and tests/tooling/ own contract qualification.

The final Python dashboard immediately before retirement, commit c23a70961f4b7858fdb0264cfb27b7ea26a8a334, is the compatibility oracle for frontend structure and behavior. Current Rust remains the authority for runtime semantics. Historical Python is evidence only and must never return as a runtime fallback.

## 2. Work classification

### Invariants

- The dashboard remains observational and never becomes a second routing, quota, health, reload, generation, or persistence authority.
- Credentials, raw prompts, request/provider bodies, cache keys, and other secret-bearing content never enter dashboard persistence, rendered HTML, JSON APIs, logs, fixtures, screenshots, or closure evidence.
- Dashboard public/private authentication behavior remains explicit: ordinary dashboard pages/data may use the configured dashboard-public exemption; /v1/*, /api/integrations/*, /api/status, runtime/update control surfaces, and other non-dashboard operational APIs do not inherit it.
- Dynamic HTML remains escaped and chart/bootstrap JSON remains safe against markup/script termination.
- Embedded frontend assets remain byte-stable relative to the final Python assets until an intentional separately reviewed frontend change is planned.
- Frontend parity is measured against the complete DOM/JS/API contract, not a selected semantic subset that permits missing panels or controls.

### Capabilities

- The same 14 dashboard page routes render the same page structure, controls, panels, charts, tables, empty/error states, navigation, and themes as the final Python dashboard, modulo current runtime data values.
- Legacy dashboard JavaScript runs without missing endpoints, wrong element types, missing data hooks, or console exceptions.
- Account/model/detail, telemetry, reliability/routing/traces, runtime, and cache views retain the operator-visible information density of the Python implementation.
- Desktop and mobile rendering remain visually near-identical to the oracle at matched viewport/theme/state combinations.

### Infrastructure

- A durable, sanitized Python-era oracle fixture/manifest lives in the current repository so parity does not depend on mutable external screenshots or a permanently executable Python application.
- Dashboard rendering is decomposed enough to keep route handlers thin and page renderers reviewable as parity is restored.
- Focused Rust and tooling tests guard DOM, endpoint, asset, escaping, and browser contracts.

### Polish

- Eliminate invalid/repaired browser DOM, dead frontend hooks, stale architecture claims, and duplicated rendering paths.
- Keep dense tables and existing intentional overflow behavior usable on narrow/mobile layouts.

## 3. Non-goals

- No SPA conversion, React/Vue/Svelte adoption, CSS redesign, theme redesign, or frontend bundler.
- No replacement of the existing dashboard.css/dashboard.js/Chart.js assets merely to make the reduced Rust markup work.
- No Python runtime fallback or dual production implementation.
- No changes to inference routing, coordinator retry/finalization, provider transport, model selection, quota policy, or persistence schema solely for dashboard parity.
- No new secret/request-content storage.
- No speculative dashboard features beyond the final Python surface; new UI features require separate follow-up planning after parity closure.

## 4. Current state

Research baseline: 17e298f64fa21589f558c43592a24fa91b952ff7.

The migration retained the old static frontend but narrowed its server-side contract:

- rust/assets/dashboard/static/dashboard.css, dashboard.js, chart.umd.min.js, and favicon.svg are byte-identical to their counterparts at c23a70961f4b7858fdb0264cfb27b7ea26a8a334. The Git blob identities match for all four files.
- The initial Rust F005 migration explicitly scoped one representative SSR page and placed complete dashboard parity out of scope.
- The later Q012 corrective filled all page routes but its comparator intentionally selected only chosen cards/tables and allowed the Python page to remain richer. Its visual evidence compared different Python/Rust viewport/theme combinations, so it did not establish like-for-like visual equivalence.
- Current rust/src/server/dashboard.rs combines handlers, shared layout, data selection, and page renderers in one large module. The overview still builds a separate full document instead of consistently using the shared layout path.
- Current Rust markup exposes only a subset of the classes/data hooks used by the unchanged frontend assets. Historical components such as system-health, model metadata/benchmark pills, grouped/static chart hooks, heatmaps, account filters, update indicators, warnings, and detailed runtime/cache panels are absent or reduced.
- dashboard.js still requests GET /api/timeseries and GET /api/timeseries/grouped, but the current Rust router does not register those routes.
- The Rust timeseries renderer currently assigns id="timeseries-chart" to a section while dashboard.js passes that ID to Chart.js as a canvas.
- The current overview account rendering can interpolate a complete table or paragraph markup inside another table tbody, relying on browser DOM repair rather than preserving the Python structure.
- Runtime/cache renderers contain hard-coded zero/healthy/unavailable presentation for observations that the Python dashboard derived from authoritative runtime/cache telemetry.
- architecture/deep-dive-dashboard.md describes a stronger shared-layout/read-plane contract than several current renderer paths actually satisfy.

The regression is therefore not an asset-copy problem. It is a server-rendered DOM, dashboard JSON, and view-model parity problem.

M001-M007 subsequently restored and qualified the dashboard parity surface, and
M006 closed the parity roadmap with the accepted source-truth differences
recorded in its closure evidence. After the dashboard restoration branch was
merged to `main` at `299a0b3657667af509742a184e658c14df22d406`, hosted CI
run `37040025250` exposed two strict-Clippy `collapsible_if` findings in
the Linux load-average projection in `rust/src/server/dashboard.rs`. The
failure skipped all later CI gates. M008 is the bounded post-merge corrective
for that current-head CI regression plus compact registry reconciliation; it
does not reopen dashboard parity semantics.

## 5. Target architecture

The target keeps the current native runtime and historical frontend contract:

    authoritative Rust runtime/database/metrics
                    |
                    v
        bounded dashboard view models
                    |
                    v
    rust/src/server/dashboard/
      mod.rs          route facade
      routes.rs       thin handlers/query validation
      assets.rs       static/theme responses
      view_model.rs   dashboard-only projections
      render/
        mod.rs
        layout.rs
        overview.rs
        accounts.rs
        models.rs
        telemetry.rs
        reliability.rs
        runtime.rs
        cache.rs
                    |
                    v
        historical DOM/JS/API contract
                    |
                    v
    unchanged embedded CSS/JS/Chart.js/themes

The exact module split may be adjusted if current Rust mechanics suggest a cleaner equivalent, but the ownership constraints are fixed: renderer modules consume bounded projections and never acquire runtime authority.

Qualification target:

    immutable Python oracle commit c23a709...
          + deterministic sanitized state
                    |
                    v
      checked-in oracle manifest/projections
                    |
                    +---- compare ---- current Rust responses
                    |
                    +---- optional matched browser captures

Raw HTML byte equality is not required. Canonical DOM equality must preserve tag hierarchy, element type, IDs, class tokens, data attributes, forms/controls, internal/static URLs, meaningful ordered text/data, and script/bootstrap payload semantics. Differences in insignificant whitespace or attribute order may be normalized.

## 6. Dependency graph

    M001 oracle + strict qualification substrate
        |
        v
    M002 shared shell / JS hooks / dashboard JSON restoration
        |
        +-------------------------+
        v                         v
    M003 core operator pages   M004 telemetry/diagnostic pages
        |                         |
        +------------+------------+
                     v
              M005 runtime/cache rich parity
                     |
                     v
              M006 full matched qualification

M003 and M004 both require the shared M002 shell contract. They may proceed in parallel after M002 closes if their renderer/view-model files remain disjoint. M005 may begin after M002 only if its view-model interfaces are stable, but it cannot close before M003/M004 because the final dashboard stats/read-plane contract must be reviewed as a whole. M006 is hard-blocked on M003-M005.

No external product dependency is currently known. Browser tooling is qualification-only and must not enter the Rust production graph.

## 7. Milestones

### Milestone 001 — Python oracle freeze and strict parity substrate

Class: infrastructure

Objective: convert immutable Git history into a durable, sanitized, machine-readable frontend contract and a comparator that detects the classes of drift the previous Q012 comparator allowed.

Dependencies: none.

Deliverable boundary: oracle manifest/state corpus, full DOM/API/asset projection, negative comparator tests, current-gap report, and regeneration procedure that never makes historical Python a runtime dependency.

User or operator value: infrastructure only; no parity claim until a later capability milestone consumes it.

Exit conditions: all 14 pages, historical dashboard JSON endpoints, asset hashes, JS-required hooks, representative empty/populated/escaping/error/private states, and complete DOM structural facts are captured; deliberate removal/type/class/data/API regressions fail the comparator.

### Milestone 002 — Shared shell, interaction, and dashboard JSON contract restoration

Class: capability

Objective: make the unchanged CSS/JS execute against a structurally compatible common Rust shell and restore the dashboard JSON routes required by frontend behavior.

Dependencies: M001 hard.

Deliverable boundary: shared layout/nav/theme/period/refresh/update semantics, valid DOM, timeseries/grouped JSON APIs, correct canvas/data hooks, no missing JS fetches, and focused browser/DOM evidence.

Exit conditions: shared shell pages pass their oracle contract; no browser console/resource/fetch error occurs on the shell/timeseries interaction fixture; production assets remain byte-identical.

### Milestone 003 — Overview, account, model, and model-detail parity

Class: capability

Objective: restore information density and DOM structure for the primary operator/catalog views without fabricating data.

Dependencies: M002 hard.

Deliverable boundary: overview, accounts, models, model detail; associated filters, status/availability presentation, metadata/pricing/benchmark/warning sections, health/glance panels, and bounded Rust view models.

Exit conditions: those routes pass populated/empty/escaping/error oracle comparisons and matched desktop/mobile browser review.

### Milestone 004 — Telemetry, reliability, routing, and trace parity

Class: capability

Objective: restore latency, events, timeseries, bandwidth, pings, reliability, routing, and traces including old chart/table/filter hooks and diagnostic panels.

Dependencies: M002 hard; M003 soft.

Deliverable boundary: eight telemetry/diagnostic pages, grouped/static chart bootstrap, heatmaps/filters/steppers where historically present, and the required authoritative read projections.

Exit conditions: all routes pass oracle DOM/API/browser checks with no dead JS hooks or synthetic success values.

### Milestone 005 — Runtime and cache observability parity

Class: capability

Objective: replace current abbreviated/hard-coded runtime/cache presentation with bounded projections of authoritative current Rust runtime/cache/request-shaping facts while restoring the historical DOM and dashboard stats API surface.

Dependencies: M002 hard; M003/M004 interface for final cross-page DTO/API consistency and hard before closure.

Deliverable boundary: runtime/cache pages plus historical dashboard stats endpoints needed by those pages; no new runtime authority and no secret-bearing telemetry.

Exit conditions: runtime/cache populated and degraded states are data-backed, old panels/controls/hooks are present, all restored JSON schemas are bounded and authenticated consistently, and no hard-coded "healthy"/zero value stands in for unknown/unavailable authoritative state.

### Milestone 006 — Full dashboard parity qualification and closure

Class: polish

Objective: prove the restored Rust dashboard is near-identical to the Python oracle across the complete supported state/route/theme/viewport matrix and reconcile current architecture/operator documentation.

Dependencies: M003, M004, M005 hard.

Deliverable boundary: strict full-suite parity gate, matched browser captures, console/network assertions, stale-code removal, documentation reconciliation, and closure evidence.

Exit conditions: all mandatory route/API/asset/DOM gates pass; matched browser pairs have no unexplained material layout difference; all frontend fetches succeed; no medium+ parity/security finding remains.

## 8. Cross-cutting requirements

Storage/migration: use the existing canonical schema. Dashboard restoration must not add request-content persistence. A schema change requires separate evidence and is expected to be unnecessary.

Protocol/compatibility: historical dashboard page and dashboard-only JSON contracts are compatibility surfaces for this roadmap. /v1/models and other inference/integration APIs remain owned by their existing subsystems.

Security/auth: preserve dashboard public/private behavior, standard escaping, JSON/script escaping, bounded results, and secret redaction. Oracle fixtures/screenshots must contain synthetic values only.

Concurrency/cancellation/recovery: dashboard reads remain bounded observational requests. DB/read failures degrade locally and must not affect inference. Browser auto-refresh must not stack intervals, duplicate charts, or keep detached DOM work alive.

Observability: do not manufacture telemetry. Unknown/unavailable must remain explicit when current authoritative runtime state cannot supply a historical field.

Performance/resources: retain lazy Chart.js loading and bounded queries. Restoring richer pages must not introduce per-request unbounded scans or a second SQLite connection/pool. If a historical panel would require an unbounded query, stop and design a bounded projection.

Docs/ops: architecture/deep-dive-dashboard.md, operator API/docs, asset manifests, and focused test instructions must match shipped behavior at each closure.

## 9. Verification strategy

Use three layers:

1. Pure contract tests: oracle manifest integrity, asset hashes, HTML/JSON escaping, deterministic DOM projection, negative comparator cases.
2. Rust integration tests over loopback: all page/API routes, public/private auth, deterministic populated state, malformed query/error behavior, static/theme delivery.
3. Qualification-only browser tests: same route, same state, same theme, same viewport for oracle/candidate; assert no console exceptions, failed same-origin fetches, missing resources, duplicate IDs, or Chart.js initialization failures before visual review.

The final Python commit is not executed in production or normal Rust tests. If a historical worktree is used to regenerate oracle evidence, regeneration is explicit and reviewable; checked-in oracle manifests are the default test authority.

Rust tests run serial with --test-threads=1. Browser dependencies stay out of Cargo and release artifacts.

## 10. Risks and decision points

- Historical UI fields may depend on runtime concepts whose current Rust owner differs from the Python implementation. Preserve the panel/contract but source it from the current authority; never recreate obsolete state machines inside dashboard code.
- Some historical values may no longer be meaningfully observable. Represent genuine unavailability rather than hard-coded zero/success, and stop for review if preserving the old UI would misrepresent current semantics.
- Asset bytes are currently preserved. Editing CSS/JS to hide server-side parity gaps is a stop condition unless a concrete browser incompatibility proves an asset change is unavoidable.
- Full HTML byte equality is too brittle; an overly permissive semantic comparator recreates Q012's failure mode. The canonical projection must retain full tree/type/class/data/form/link/content semantics.
- Browser screenshots are evidence, not the primary contract. Visual similarity must not mask missing API/DOM semantics.

## 11. Completion definition

This roadmap closes only when the native Rust dashboard preserves the final Python dashboard's operator-visible structure and behavior across all 14 page routes and dashboard-only JSON APIs, the unchanged frontend assets execute without contract errors, runtime/cache/model information is data-backed from authoritative Rust owners, public/private and escaping invariants pass, matched browser evidence shows no unexplained material regression, and current docs describe the shipped implementation rather than the reduced migration intermediate.

### Milestone 008 — Post-merge strict-CI and planning reconciliation corrective

Class: invariant

Objective: restore strict current-head CI after the dashboard-parity merge by
fixing the two behavior-neutral Clippy findings, execute every gate skipped by
that failure, and reconcile stale compact registry state.

Dependencies: M001-M007 hard and closed; no external dependency.

Deliverable boundary: the smallest semantics-preserving
`rust/src/server/dashboard.rs` conditional cleanup required by current stable
Clippy, full default/no-default/tooling qualification, and roadmap/registry
status reconciliation. No dashboard DOM/API/theme/runtime capability change.

Exit conditions: default and no-default strict Clippy pass; full serial Rust
and tooling gates pass; hosted CI completes with no skipped gate from an
earlier failure; dashboard behavior is unchanged; registry/roadmap agree on
the final closed/no-successor state.


### Milestone 009 — Production module decomposition and ownership cleanup

Class: polish

Objective: decompose the approximately 5.8k-line production dashboard module
into explicit route/API/assets/shared-render/page-render boundaries while
preserving the frozen oracle, the accepted M006 source differences, static
assets, auth, and all current runtime/data ownership.

Dependencies: M008 hard and closed before implementation.

Deliverable boundary: internal `rust/src/server/dashboard/` module
decomposition, localized tests, stable facade to `server/mod.rs`, strict
pre/post oracle/API difference equivalence, unchanged asset hashes, and
architecture/development documentation reconciliation.

User or operator value: no intended UI change; lowers review/merge risk and
makes future dashboard maintenance bounded without weakening parity evidence.

Exit conditions: no new DOM/API/theme/auth mismatch; the pre/post accepted
difference set is identical; render modules do not acquire runtime/database
authority; focused/full default/no-default/tooling/hosted-CI gates pass.

### Milestone 010 — Parity qualification harness decomposition

Class: polish

Objective: after M009 closes, decompose the approximately 2.9k-line dashboard
qualification script into a thin stable CLI plus projection, oracle/fixture,
process, browser, and reporting modules without changing comparator
strictness, report schema, oracle data, CLI behavior, or production code.

Dependencies: M009 hard and closed before implementation.

Deliverable boundary: tooling-only module split, stable
`scripts/qualification_dashboard_parity.py` command surface, focused
negative/golden tests, real strict/browser/lifecycle qualification, and
tooling documentation reconciliation.

User or operator value: no intended runtime/UI change; reduces the risk that
future browser/oracle maintenance accidentally changes the definition of
parity.

Exit conditions: CLI/report/oracle/comparator semantics are unchanged;
accepted difference groups are identical; strict/browser/shutdown-restart,
Ruff/Pyright/tooling, full Rust default/no-default, and hosted-CI gates pass.

### Milestone 011 — Hosted oracle history qualification

Class: invariant

Objective: make the pinned Python-oracle Git commit available to hosted CI so
the frozen-manifest blob identity test can run without weakening its contract.

Dependencies: M008 implementation committed; M008 closure awaits this hosted
qualification corrective.

Deliverable boundary: CI checkout history includes the pinned oracle commit;
the focused manifest test and all hosted CI gates pass unchanged.

Exit conditions: the pinned source asset blobs are available in hosted CI;
the tooling manifest test passes; no oracle, production, or dashboard
behavior changes occur.

## 12. Milestone status

| Milestone | Status | Implementation plan | Closure record | Blockers |
|---|---|---|---|---|
| 001 | closed | plans/implementation/dashboard/001-python-oracle-and-parity-substrate.md | plans/closure/dashboard/001-status.md | none |
| 002 | closed | plans/implementation/dashboard/002-shared-shell-and-dashboard-api-restoration.md | plans/closure/dashboard/002-status.md | none |
| 003 | closed | plans/implementation/dashboard/003-overview-account-model-parity.md | plans/closure/dashboard/003-status.md | Four source-truth differences received final disposition in M006. |
| 004 | closed | plans/implementation/dashboard/004-telemetry-routing-trace-parity.md | plans/closure/dashboard/004-status.md; additive resolution in plans/closure/dashboard/004-follow-up-007.md | none |
| 005 | closed | plans/implementation/dashboard/005-runtime-cache-observability-parity.md | plans/closure/dashboard/005-status.md; additive resolution in plans/closure/dashboard/005-follow-up-006.md | Four source-truth dispositions accepted by M006; no further Runtime/Cache work |
| 006 | closed | plans/implementation/dashboard/006-full-parity-qualification-and-closure.md | plans/closure/dashboard/006-status.md | Nine source-backed compatibility differences accepted; parity capability remains closed. |
| 007 | closed | plans/implementation/dashboard/007-empty-recovery-summary-correction.md | plans/closure/dashboard/007-status.md | none |
| 008 | blocked | plans/implementation/dashboard/008-post-merge-strict-ci-and-planning-reconciliation.md | — | M011 hosted CI qualification corrective |
| 009 | blocked | plans/implementation/dashboard/009-production-module-decomposition.md | — | M008 |
| 010 | blocked | plans/implementation/dashboard/010-parity-harness-decomposition.md | — | M009 |
| 011 | active | plans/implementation/dashboard/011-hosted-oracle-history-qualification.md | — | M008 implementation committed; M008 closure awaits hosted qualification |

M001-M007 remain closed historical evidence. M008 reopens only the roadmap
lifecycle for a post-merge strict-CI regression and planning-control
reconciliation at baseline
`299a0b3657667af509742a184e658c14df22d406`. It does not reopen the accepted
dashboard parity dispositions. M009 and M010 are bounded polish successors:
M009 is hard-blocked on M008 so the current qualification harness can guard
the production decomposition unchanged; M010 is hard-blocked on M009 so the
guard itself is refactored only after the production split has closed.

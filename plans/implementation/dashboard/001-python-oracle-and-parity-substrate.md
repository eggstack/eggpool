# Dashboard Milestone 001 — Python Oracle Freeze and Strict Parity Substrate

Status: closing

Repository baseline: 17e298f64fa21589f558c43592a24fa91b952ff7

Source roadmap:

- plans/subsystems/dashboard-roadmap.md#milestone-001--python-oracle-freeze-and-strict-parity-substrate

Long-term requirements:

- plans/000-long-term-specification.md
- plans/001-terminology-and-domain-model.md
- plans/002-long-term-roadmap.md
- plans/003-planning-process.md

Applicable ADRs:

- None required. This milestone records an existing compatibility contract and adds qualification tooling only.

Primary class: infrastructure

## 1. Objective

Freeze the final pre-retirement Python dashboard at commit c23a70961f4b7858fdb0264cfb27b7ea26a8a334 into a durable, sanitized oracle and build a strict comparator that can detect complete DOM/JS/API drift. Do not change production Rust dashboard behavior in this milestone.

The result must make later parity work independent of memory, mutable screenshots, and the permissive Q012 comparison rules.

## 2. Why this milestone is ready

There are no hard dependencies. The historical source is immutable in this repository, the final Python dashboard commit is known, the current Rust asset copy exists, and the previous migration qualification artifacts identify the failure mode.

Historical Q012 commit b41a9ae07b086154a3efbcdeae663e991f022ce8 may be reused for deterministic fixture ideas and browser tooling, but its selected semantic comparator is evidence of what not to accept as the new parity gate.

## 3. Current implementation evidence

At the baseline:

- Final Python frontend/rendering authority is recoverable from c23a70961f4b7858fdb0264cfb27b7ea26a8a334 under src/eggpool/dashboard/.
- Current Rust frontend assets under rust/assets/dashboard/static/ have the same Git blob identities as the historical Python assets:
  - dashboard.css: 5242e07e36da614698991ce884277416b9aedbab
  - dashboard.js: 1a7ff048b435043a127414092ec92fdf7893f3c5
  - chart.umd.min.js: 0bae5b84bcf2dbd6d81aed3bc652f97eb9b00b1a
  - favicon.svg: a075c45b9d3a9938ade60eb399f18c0d9cd7383a
- The historical Python router exposed 14 page routes plus dashboard JSON routes including /api/timeseries, /api/timeseries/grouped, /api/stats/transcoding, /api/stats/cache-observability, /api/stats/canonical-request-segmentation, /api/stats/cache-stability, and /api/stats/request-shaping.
- Q012 explicitly selected only chosen cards/tables and allowed richer Python diagnostics to remain unmatched. It therefore cannot be reused unchanged as the parity definition.
- Current tests primarily exercise server transport/static reachability and do not preserve the full old DOM/JS contract.

## 4. Invariants that must not regress

- No Python application/runtime is restored to production packaging or process startup.
- Oracle data is synthetic and secret-free; no live personal database, API key, prompt, provider body, cache key, machine identity, or private endpoint enters fixtures.
- The oracle commit is immutable and explicitly recorded; regeneration cannot silently move to a newer candidate.
- Existing Rust assets are not edited in order to make the new comparator pass.
- The comparator must retain enough structure to fail on the specific migration defects already observed: missing element, wrong element type, missing class/id/data hook, missing form/control, missing endpoint, row/card/panel omission, ordering changes where contractual, invalid escaping, and altered bootstrap JSON.

## 5. Scope

### In scope

- Historical source/route/asset inventory from c23a70961f4b7858fdb0264cfb27b7ea26a8a334.
- A deterministic sanitized empty/populated/escaping/error/private state corpus compatible with the final Python dashboard contract.
- Checked-in oracle manifest/projections under tests/fixtures/dashboard-python-oracle/ or an equivalently explicit tooling-fixture location.
- A tooling comparator, preferably scripts/qualification_dashboard_parity.py, that can project current Rust responses and compare them with the checked-in oracle without executing historical Python.
- Full DOM structural projection: tag hierarchy, element type, IDs, class tokens, data-* attributes, relevant aria attributes, forms/method/action/controls, internal/static URLs, ordered visible content, tables, cards/panels, canvas/script hooks, and safe bootstrap JSON facts.
- HTTP/API contract projection for dashboard-only JSON routes: method/path, auth class, status behavior, top-level shape, required fields/types, ordering/bounds where contractual.
- Asset path/content-type/hash inventory and theme inventory.
- Focused negative tests under tests/tooling/ proving every required mismatch category is detected.
- A bounded current-gap report generated against the present Rust candidate, clearly labeled diagnostic rather than closure evidence.

### Explicitly out of scope

- Production Rust renderer/view-model changes.
- CSS/JS edits.
- Restoring dashboard API endpoints.
- Running historical Python during ordinary CI or runtime.
- Committing a large unbounded screenshot corpus.
- Weakening historical behavior because the current Rust implementation is simpler.

## 6. Required production changes

None. This milestone is tooling/fixture infrastructure.

The oracle generator/regeneration procedure may use an explicit detached Git worktree at the fixed Python commit plus historical development dependencies. Normal qualification consumes checked-in sanitized projections and must not require network access or a second application implementation.

Prefer a versioned manifest that records:

- oracle commit and fixture provenance;
- page/API/static/theme inventory;
- asset Git blob/SHA-256 values;
- projection schema version;
- volatile fields and the exact narrow normalization applied;
- expected state matrix.

Do not normalize away element types, classes, data attributes, form controls, content ordering, or meaningful displayed values.

## 7. Ordered work packages

### Work package A — Recover and inventory the oracle

Intent: establish one immutable compatibility source.

Required changes:

- Enumerate the final Python dashboard page routes, dashboard-only JSON routes, static assets/themes, renderer helpers, and JavaScript DOM/API dependencies.
- Recover or recreate a deterministic synthetic database/state corpus that exercises empty, populated, escaping/Unicode/long-value, missing/error, and private-auth behavior.
- Record c23a70961f4b7858fdb0264cfb27b7ea26a8a334 as the fixed oracle commit.

Acceptance evidence:

- Machine-readable inventory covers every historical page/API/static/theme route.
- Asset hashes match repository history.
- Fixture review proves no secret/private content.

### Work package B — Build full DOM/API projection

Intent: prevent the Q012 selected-semantic gap from recurring.

Required changes:

- Parse complete server-rendered HTML into a canonical tree/projection preserving structural and interactive semantics.
- Parse JSON endpoint responses into a schema/value projection with only documented volatile normalization.
- Capture JS-required selectors/endpoints from the frozen dashboard.js and connect them to expected DOM/API producers.

Acceptance evidence:

- Deliberately changing a tag type, class, ID, data attribute, form control, route, endpoint field, ordered row, panel/card, or escaping causes a focused failure.
- Whitespace and HTML attribute ordering alone do not fail.

### Work package C — Check in durable oracle artifacts

Intent: make the oracle usable after Python retirement without executing old code.

Required changes:

- Store bounded sanitized projections/manifests in the current repository.
- Add provenance and regeneration instructions.
- Add hash/inventory guards so accidental fixture editing is explicit in review.

Acceptance evidence:

- A clean checkout can validate oracle integrity offline.
- Normal tests do not import src/eggpool from Git history.

### Work package D — Characterize current gaps

Intent: hand M002-M005 an exact diff surface without making M001 a capability claim.

Required changes:

- Run the new comparator against current Rust using the same deterministic state.
- Emit a bounded report grouped by shared shell/API, core pages, telemetry pages, runtime/cache, and visual-only differences.
- Treat current mismatches as expected diagnostic output, not a reason to weaken the oracle.

Acceptance evidence:

- Report includes the known missing /api/timeseries routes, wrong timeseries-chart element contract, overview invalid/nested table structure, and omitted historical panels/classes where exercised.

## 8. Failure, cancellation, restart, contention semantics

This milestone does not change production concurrency. Tooling must use temporary directories/worktrees and deterministic cleanup. An interrupted oracle generation must not partially replace committed oracle artifacts; write to temporary output then atomically replace or require explicit review/copy.

Historical application startup failure is a tooling failure, not permission to infer or hand-author missing oracle facts. If the fixed historical environment cannot execute reproducibly, preserve source-derived contract facts and stop for review before claiming generated response equivalence.

## 9. Compatibility and migration

No production migration. Existing tests remain green.

The checked-in oracle becomes the compatibility input for M002-M006. It may only be changed by an explicit reviewed corrective plan showing that the original capture was wrong or that a later intentionally approved dashboard behavior supersedes the Python contract. Routine Rust implementation must not update oracle output to match the candidate.

## 10. Required tests

Add focused tooling tests covering:

- oracle manifest/projection schema and fixed commit identity;
- asset/static/theme inventory completeness;
- no duplicate IDs and unsafe external/script references in oracle pages;
- deliberate missing element;
- wrong element type, especially canvas versus section;
- missing/changed class and data attribute;
- missing form/control;
- changed card/panel value;
- missing/changed/reordered table row;
- changed status/error/empty text;
- escaping regression;
- missing dashboard JSON route/field;
- changed endpoint auth/status classification;
- missing JS selector/API producer mapping;
- bounded report size and secret scan.

Where a Rust response fixture is used, run the production server over loopback; do not create a second dashboard implementation in tooling.

## 11. Required verification commands

Focused:

    uv sync --frozen
    uv run ruff format --check scripts/ tests/tooling/
    uv run ruff check scripts/ tests/tooling/
    uv run pyright scripts/
    uv run pytest tests/tooling/ -q --tb=short --maxfail=1

Repository gates before closure:

    cargo fmt --manifest-path rust/Cargo.toml --all -- --check
    cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
    cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
    cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
    cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
    git diff --check

If the historical generator is run, record its exact worktree commit, command, fixture hash, and local-only/browser prerequisites truthfully in closure evidence.

## 12. Documentation updates

- Add a short qualification document under docs/ or tests/fixtures/dashboard-python-oracle/README.md explaining oracle authority, regeneration, and what may be normalized.
- Do not yet rewrite architecture/deep-dive-dashboard.md as if parity is restored; note the qualification substrate only if needed.

## 13. Acceptance criteria

- The final Python commit is fixed and machine-readable as oracle provenance.
- All page/API/static/theme surfaces are represented.
- The comparator retains complete meaningful DOM/interactive structure and fails every required negative case.
- Current Rust gaps are reported without mutating production code or weakening the oracle.
- Normal CI does not execute historical Python.
- No secret/private data enters fixtures or reports.

## 14. Stop conditions

Stop and report rather than improvise when:

- the proposed normalization would hide a known structural or interactive difference;
- historical execution requires credentials/live providers;
- a fixture contains real user/provider data;
- the oracle commit cannot be reproduced sufficiently to distinguish source fact from inference;
- the work starts changing production renderers/assets;
- an apparent difference is actually a new public protocol/ownership decision requiring an ADR.

## 15. Closure evidence required

The closure record must contain:

- oracle commit and fixture provenance;
- complete page/API/static/theme inventory counts;
- asset hash comparison;
- projection schema and normalization list;
- negative-test matrix/results;
- secret-scan disposition;
- current Rust gap report summary;
- exact tooling/default/no-default verification commands and results;
- unresolved findings classified by severity.

Disposition is infrastructure closure only. It must not state that dashboard parity itself is restored.

## 16. Handoff notes

The key hazard is reusing Q012's permissive comparison logic. Historical Q012 is useful for fixtures/browser mechanics, not as the acceptance definition.

Do not edit dashboard.css or dashboard.js in this milestone. Their preserved byte identity is valuable evidence that the migration regression lies in the server DOM/API/view-model contract.

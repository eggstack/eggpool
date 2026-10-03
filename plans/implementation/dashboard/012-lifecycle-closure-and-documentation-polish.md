# Dashboard Milestone 012 — Lifecycle closure and documentation polish

Status: active

Repository baseline: `9eb46571a2e65aabe531197945f4656fe22caa79`

Source roadmap:

- `plans/subsystems/dashboard-roadmap.md#milestone-012--lifecycle-closure-and-documentation-polish`

Corrects / follows:

- `plans/closure/dashboard/008-status.md`
- `plans/closure/dashboard/009-status.md`
- `plans/closure/dashboard/010-status.md`
- `plans/closure/dashboard/011-status.md`

Long-term requirements:

- `plans/000-long-term-specification.md`
- `plans/001-terminology-and-domain-model.md`
- `plans/002-long-term-roadmap.md`
- `plans/003-planning-process.md`

Applicable ADRs:

- None. This is planning/documentation lifecycle reconciliation only; it does
  not alter a public contract, dependency, auth policy, runtime owner, or
  dashboard behavior.

Primary class: polish

## 1. Objective

Close the dashboard workstream cleanly after M008-M011 by reconciling the
remaining planning-control drift, confirming current-facing documentation
describes the decomposed production/tooling layout, and recording the merged
`main` qualification as the terminal baseline for this line of work.

The intended end state is explicit: Dashboard M001-M012 closed, dashboard
roadmap `Status: closed`, registry Dashboard row closed with no registered
successor, no ready/active dashboard implementation plan, and no production
or frozen-oracle change.

## 2. Why this milestone is ready

All technical dependencies are closed:

- M008 closed the post-merge strict-Clippy/CI corrective.
- M011 made the pinned Python oracle source commit available in hosted CI.
- M009 decomposed the production dashboard while preserving the strict oracle
  and exact accepted difference set.
- M010 decomposed the qualification harness while preserving CLI, report,
  oracle, browser, and lifecycle semantics.
- PR #4 merged those changes to `main`.
- Current merged-head hosted CI and dependency-audit runs passed.

No implementation dependency remains. The only observed dashboard defect is
planning lifecycle state: both the subsystem roadmap and registry still say
Dashboard is `active` even though the roadmap records M001-M011 closed and
states that no successor is registered.

## 3. Current implementation evidence

At baseline `9eb46571a2e65aabe531197945f4656fe22caa79`:

- `rust/src/server/dashboard/` is decomposed into route/API/assets/response/
  format/theme/test owners plus page-family render modules.
- `scripts/qualification_dashboard_parity.py` is a thin stable facade over
  `scripts/dashboard_parity/`.
- `architecture/overview.md`, `architecture/deep-dive-dashboard.md`, and
  `.opencode/skills/development/SKILL.md` already reference the decomposed
  paths rather than the retired monoliths.
- M009 closure records 830 serial Rust tests, strict parity with the same nine
  accepted source-backed differences, unchanged static assets, and hosted CI
  success.
- M010 closure records 153 tooling tests with one skipped, unchanged
  oracle/report semantics, browser/lifecycle qualification, 830 serial Rust
  tests, and hosted CI success.
- M011 closure records hosted frozen-oracle blob identity qualification.
- The merge head has green push CI and dependency-audit workflows.
- `plans/subsystems/dashboard-roadmap.md` nevertheless remains
  `Status: active`.
- `plans/registry.md` likewise lists Dashboard as `active` while its current
  milestone is already closed and its blocker text says no successor exists.

This is planning/documentation debt, not a runtime or parity defect.

## 4. Invariants that must not regress

- M001-M011 closure records remain immutable historical evidence.
- The frozen Python oracle commit, fixture manifests/captures, comparator
  normalization, accepted difference set, and browser evidence remain
  unchanged.
- No Rust source, Cargo metadata, embedded dashboard asset, provider template,
  persistence schema, config, API, DOM, theme, or auth behavior changes.
- M006's nine accepted source-backed differences remain accepted historical
  dispositions; M012 does not reopen or reinterpret them.
- Current source truth remains:
  - production dashboard under `rust/src/server/dashboard/`;
  - qualification facade at `scripts/qualification_dashboard_parity.py`;
  - qualification internals under `scripts/dashboard_parity/`;
  - server route/auth assembly in `rust/src/server/mod.rs`.
- Historical sequencing remains traceable even though M011 was introduced as
  an M008 hosted-CI corrective before M009/M010 closed.
- Registry remains a compact control surface and must not duplicate full
  milestone requirements.

## 5. Scope

### In scope

- Add M012 to the dashboard roadmap and lifecycle table.
- Reconcile dashboard roadmap top-level lifecycle to `closed` when M012
  closes.
- Reconcile the Dashboard registry row to `closed`, with M012 as the terminal
  milestone and no registered successor.
- Remove M012 from dependency-ready/active sections on closure.
- Add the M012 closure record and a concise recently-closed registry entry.
- Audit current-facing dashboard documentation for stale pre-M009/pre-M010
  ownership paths:
  - `architecture/overview.md`;
  - `architecture/deep-dive-dashboard.md`;
  - `.opencode/skills/development/SKILL.md`;
  - `tests/fixtures/dashboard-python-oracle/README.md`;
  - relevant current README/docs references if discovered.
- Make documentation edits only where a current statement is actually stale.
- Record merged-head hosted CI/dependency-audit evidence as final integration
  confirmation.
- Optionally add a narrowly scoped planning-consistency guard only if an
  existing generic tooling seam can express it without introducing a
  Dashboard-specific parser or forcing unrelated subsystem cleanup.

### Explicitly out of scope

- Dashboard production Rust changes.
- Dashboard CSS/JS/theme/static asset changes.
- Oracle/fixture/comparator/report changes.
- Re-running or redefining M001-M011 capability work.
- Reopening the nine accepted source-truth differences.
- General planning-governance redesign.
- Fixing unrelated active/closed lifecycle debt in other subsystems.
- Archiving/moving the dashboard plans or closure records.
- New dashboard features, telemetry, performance work, or UI redesign.

## 6. Required changes

### Planning lifecycle

While M012 is open:

- Dashboard roadmap remains `active`;
- M012 is `ready`/then `active` under normal lifecycle;
- registry lists M012 as the only dependency-ready/active dashboard item.

At closure:

- `plans/subsystems/dashboard-roadmap.md` becomes `Status: closed`;
- M012 milestone row becomes `closed` and links
  `plans/closure/dashboard/012-status.md`;
- the roadmap terminal note states M001-M012 are closed, no successor is
  registered, and future dashboard work requires a new bounded plan;
- registry Dashboard row becomes `closed` with M012 as terminal lifecycle
  reconciliation;
- M012 disappears from dependency-ready/active sections;
- Recently closed links the M012 closure record.

Do not rewrite older closure records to make chronology look cleaner.

### Current documentation

Audit current-authority docs and edit only stale ownership/path claims. Current
evidence suggests they are already correct, so a zero-diff documentation audit
is an acceptable outcome and should be recorded as such.

If a current doc still names `rust/src/server/dashboard.rs` as the production
owner or describes `scripts/qualification_dashboard_parity.py` as the
monolithic implementation, correct it to the decomposed ownership.

### Optional guard

A guard is optional, not required. Add one only if it can be generic and small,
for example by extending an existing planning-validation test to catch a
roadmap/registry status contradiction without encoding dashboard-specific
milestone knowledge.

Do not create a bespoke parser or broad planning framework for this cleanup.

## 7. Ordered work packages

### Work package A — Freeze terminal evidence

Intent:

Establish that no new technical dashboard work is pending.

Required evidence:

- inspect M008-M011 closure dispositions;
- confirm merged `main` contains M009/M010 decompositions;
- confirm merge-head CI and dependency audit passed;
- confirm no ready/active dashboard successor exists outside M012.

Acceptance evidence:

- one concise evidence table in M012 closure identifies terminal technical
  state and workflow run IDs/conclusions.

### Work package B — Audit current-facing documentation

Intent:

Ensure current docs describe the shipped decomposed implementation.

Required changes:

- search current docs/skills for retired single-file ownership claims;
- correct only current-authority stale text;
- leave historical implementation/closure records untouched.

Acceptance evidence:

- explicit path audit showing production and qualification ownership paths;
- zero-diff audit is acceptable when all current docs are already correct.

### Work package C — Reconcile roadmap and registry lifecycle

Intent:

Make the planning control surface match closure reality.

Required changes:

- close M012 in the roadmap with closure link;
- set dashboard roadmap top-level status to `closed`;
- set registry Dashboard row to `closed`;
- remove M012 from ready/active sections;
- add M012 to Recently closed;
- state no successor is registered.

Acceptance evidence:

- no Dashboard `active`/no-successor contradiction remains;
- roadmap, registry, M012 plan, and M012 closure agree.

### Work package D — Final consistency verification

Intent:

Prove the cleanup is documentation/planning only and did not alter product
state.

Required changes/evidence:

- verify no Rust/Cargo/assets/oracle files changed;
- run Markdown/diff checks and focused tooling checks appropriate to any
  optional guard;
- audit blocked/ready work according to planning governance.

Acceptance evidence:

- only Markdown/planning files change unless an optional generic tooling guard
  is justified;
- no newly eligible dashboard successor exists.

## 8. Failure, cancellation, restart, contention semantics

No runtime behavior changes, so there are no new product failure,
cancellation, restart, or contention semantics.

Planning edits should be committed atomically enough that roadmap and registry
do not advertise contradictory Dashboard states. If concurrent repository work
changes the dashboard lifecycle while M012 is executing, stop and rebase the
reconciliation on current `main` rather than overwriting newer state.

## 9. Compatibility and migration

No runtime, config, schema, storage, protocol, API, asset, oracle, or tooling
compatibility migration.

Historical records remain where they are. No archival move is required.

## 10. Required tests and checks

Minimum documentation/planning verification:

```bash
git diff --check
git diff --name-only
```

Required explicit inspections:

- dashboard roadmap top-level status;
- dashboard milestone table M001-M012;
- registry Dashboard row;
- dependency-ready/active/blocked dashboard entries;
- current ownership paths in architecture/development/oracle docs;
- no diff under `rust/`, `rust/assets/`, or
  `tests/fixtures/dashboard-python-oracle/`.

If a generic planning tooling guard is added:

```bash
uv run ruff format --check scripts/ tests/tooling/
uv run ruff check scripts/ tests/tooling/
uv run pyright scripts/
uv run pytest tests/tooling/ -q --tb=short --maxfail=1
```

No Rust or browser/oracle qualification is required for a Markdown-only
implementation. If any production/tooling behavior changes, stop because the
plan scope has been exceeded.

## 11. Documentation updates

Expected:

- `plans/subsystems/dashboard-roadmap.md`;
- `plans/registry.md`;
- `plans/closure/dashboard/012-status.md`.

Conditional only if stale current text is found:

- `architecture/overview.md`;
- `architecture/deep-dive-dashboard.md`;
- `.opencode/skills/development/SKILL.md`;
- `tests/fixtures/dashboard-python-oracle/README.md`.

## 12. Acceptance criteria

1. M001-M012 are recorded closed with valid closure links.
2. Dashboard roadmap top-level status is `closed`.
3. Registry Dashboard row is `closed`, identifies M012 as terminal cleanup,
   and states no successor is registered.
4. No Dashboard item remains in dependency-ready, active, or blocked sections.
5. Current-facing docs use the decomposed production/tooling ownership paths.
6. Historical M001-M011 plans/closures remain unchanged.
7. No Rust/Cargo/assets/oracle behavior or fixture changes occur.
8. M006 accepted differences and all parity evidence remain untouched.
9. Merge-head CI/dependency-audit success is recorded as terminal integration
   evidence.
10. Planning unblock audit finds no newly ready Dashboard successor.

## 13. Stop conditions

Stop and register separate work rather than broadening M012 if:

- any production/dashboard/tooling behavior change is required;
- a current documentation inconsistency reflects an unresolved technical
  defect rather than stale prose;
- another dashboard implementation plan has appeared concurrently;
- fixing lifecycle consistency requires rewriting historical closure records;
- a generic planning guard would require broad governance changes or unrelated
  subsystem reconciliation.

## 14. Closure evidence required

`plans/closure/dashboard/012-status.md` must contain:

- implementation/documentation commit(s);
- merged terminal baseline SHA;
- M008-M011 closure audit;
- merge-head CI and dependency-audit run IDs/conclusions;
- current-doc ownership/path audit;
- changed-file list proving no product/oracle changes;
- roadmap/registry before/after lifecycle disposition;
- `git diff --check` result;
- optional tooling-guard results if applicable;
- migration/security/failure-semantics statement;
- severity-tagged unresolved findings;
- unblock audit;
- final disposition `closed`.

## 15. Handoff notes

This is the terminal dashboard cleanup pass. Do not use it to improve the UI,
change comparator behavior, refactor modules further, or revisit accepted
source-truth differences.

A zero-diff current-document audit is a valid result. The primary deliverable
is an unambiguous closed planning state that matches the already-green merged
implementation.

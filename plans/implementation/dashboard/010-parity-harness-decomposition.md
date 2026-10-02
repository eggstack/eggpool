# Dashboard Milestone 010 — Parity qualification harness decomposition

Status: ready

Repository baseline: `deafb2c143cfa0992af7715e220fe8f574a23f9b`

Source roadmap:

- `plans/subsystems/dashboard-roadmap.md#milestone-010--parity-qualification-harness-decomposition`

Long-term requirements:

- `plans/000-long-term-specification.md`
- `plans/001-terminology-and-domain-model.md`
- `plans/002-long-term-roadmap.md`
- `plans/003-planning-process.md`

Applicable ADRs:

- None required. This is tooling-only decomposition preserving the existing
  qualification CLI, oracle schema, comparator semantics, and production
  runtime.

Primary class: polish

## 1. Objective

Decompose the approximately 2,930-line
`scripts/qualification_dashboard_parity.py` into a thin stable command entry
point plus focused projection/oracle/process/browser/reporting modules, while
preserving every current CLI option, report schema, normalization rule,
comparison rule, fixture/oracle format, browser/lifecycle check, and failure
exit behavior.

M010 runs only after M009 so the qualification harness remains unchanged while
guarding the production module refactor.

## 2. Why this milestone is ready

Hard dependency: Dashboard M009 closed at `plans/closure/dashboard/009-status.md`.

M009 deliberately relied on the current qualification script as the trusted
refactor oracle. Its closure confirms unchanged behavior against the
pre-refactor baseline, so the harness can now be decomposed against a
known-good production candidate.

No production/runtime dependency is expected.

## 3. Current implementation evidence

At baseline:

- `scripts/qualification_dashboard_parity.py` is approximately 2,930 lines /
  118 KiB with about 50 top-level functions.
- One file currently owns:
  - HTML/JSON projection and comparison;
  - selector/DOM fact extraction;
  - oracle manifest generation and capture;
  - volatile/runtime normalization;
  - fixture/config/database setup;
  - server process startup/stop/readiness barriers;
  - private/model-detail pair orchestration;
  - static/theme inventory;
  - Chrome command/screenshot capture/layout auditing;
  - shutdown/restart qualification;
  - mismatch grouping;
  - report/Markdown emission;
  - CLI parsing and top-level orchestration.
- `tests/tooling/test_dashboard_parity_projection.py` already exercises
  projection/comparison behavior.
- M006/M007 closure evidence depends on the script's strict semantics and
  frozen oracle commit. M010 must not redefine what "parity" means.

The file is tooling-only, but its size now makes changes to browser mechanics,
normalization, and comparator semantics unnecessarily coupled.

## 4. Invariants that must not regress

- The command path remains `scripts/qualification_dashboard_parity.py`.
- Existing CLI flags/options/defaults, exit codes, output/Markdown schema, and
  environment variables remain compatible.
- Frozen oracle commit/provenance and fixture files remain unchanged.
- DOM projection remains strict: tag hierarchy, IDs, classes, data attributes,
  forms/controls, links, content ordering, and bootstrap JSON semantics are
  not normalized away.
- API comparison, runtime normalization, accepted-difference grouping, static
  asset inventory, theme inventory, browser checks, and shutdown/restart checks
  preserve current semantics.
- No production Rust dependency on Python/tooling is introduced.
- Browser tooling remains qualification-only and out of Cargo/release
  artifacts.
- Secret-free fixture/evidence rules remain unchanged.
- M006's accepted nine source-backed differences are not reclassified by
  module movement.
- Pyright strictness and Ruff coverage remain enabled for all extracted
  modules.

## 5. Scope

### In scope

- Keep `scripts/qualification_dashboard_parity.py` as a thin CLI facade.
- Extract focused internal tooling modules, for example:
  - projection/comparison;
  - oracle/manifest/capture;
  - fixture/config/database setup;
  - server/process lifecycle;
  - browser/screenshot/layout interaction;
  - reporting/mismatch grouping.
- Move related tests or add focused tests for each extracted seam.
- Preserve deterministic serialization and report ordering.
- Add a fixture-driven golden report test if one is needed to prove schema and
  ordering stability.
- Update development/oracle documentation for the internal tooling layout.

### Explicitly out of scope

- Any production Rust change.
- Any dashboard DOM/API/theme behavior change.
- Updating frozen oracle captures.
- Changing normalization/comparison semantics.
- Adding a new browser automation framework.
- Changing Chrome selection/version policy except for import/module mechanics.
- Replacing the CLI with a package entry point.
- Generalizing the harness for unrelated applications.
- Reworking M006 accepted source-difference policy.
- Performance optimization unless needed to prevent a decomposition
  regression.

## 6. Required tooling changes

A preferred internal shape is:

```text
scripts/
  qualification_dashboard_parity.py    # argparse + orchestration facade
  dashboard_parity/
    __init__.py
    projection.py
    oracle.py
    fixtures.py
    process.py
    browser.py
    report.py
```

Equivalent names are acceptable. Keep imports one-directional:

- projection/comparison has no process/browser dependency;
- oracle may use projection but not browser/process orchestration;
- fixture/process helpers do not depend on report rendering;
- browser uses process/fixture facts but does not redefine projection;
- report consumes result structures and does not perform capture;
- CLI facade wires all components.

Prefer typed dataclasses/TypedDicts only where they simplify current implicit
dict contracts without changing serialized JSON. Do not undertake a broad type
rewrite.

Keep deterministic ordering explicit; do not rely on incidental module import
order or unordered filesystem enumeration.

## 7. Ordered work packages

### Work package A — Freeze CLI/report semantics

Intent: establish a byte/semantic tooling baseline.

Required evidence:

- Capture `--help` output, representative strict JSON/Markdown report, and
  exit status for passing-with-accepted-gaps and an intentionally failing
  fixture.
- Record oracle manifest/capture hashes and accepted mismatch groups.
- Run current focused tooling tests.

Acceptance evidence:

- Baseline artifacts are available for post-refactor comparison.

### Work package B — Extract pure projection/comparison modules

Intent: isolate the most correctness-sensitive logic first.

Required changes:

- Move HTML/JSON projection, DOM fact extraction, normalization, and comparator
  logic without semantic edits.
- Keep existing negative tests and add import-level direct tests for extracted
  functions.

Acceptance evidence:

- Projection test suite is unchanged/green.
- Deliberate missing tag/type/class/data/API mutations still fail exactly as
  before.

### Work package C — Extract oracle/fixture/process orchestration

Intent: separate deterministic state generation from comparison semantics.

Required changes:

- Move oracle manifest/capture helpers and deterministic fixture/config setup.
- Move server start/stop/readiness/event-window/shutdown-restart helpers into a
  bounded process module.
- Preserve timeout/failure cleanup behavior.

Acceptance evidence:

- Oracle capture hashes/schema are unchanged.
- Interrupted/failed process cleanup tests pass.
- Recovery-summary barrier semantics from M007 remain unchanged.

### Work package D — Extract browser and reporting layers

Intent: isolate the optional browser surface and output formatting.

Required changes:

- Move Chrome command, screenshot/layout/interaction helpers into a browser
  module.
- Move mismatch grouping, JSON/Markdown writing, and result summarization into
  a report module.
- Keep compact browser manifest/report fields and ordering stable.

Acceptance evidence:

- Matched screenshot/interaction execution produces the same result schema.
- Browser failures retain the same nonzero/failure behavior.
- JSON/Markdown representative outputs match baseline except for explicitly
  permitted volatile values already normalized today.

### Work package E — Thin facade and documentation

Intent: leave one obvious supported command surface.

Required changes:

- Reduce `scripts/qualification_dashboard_parity.py` to argument parsing,
  dependency wiring, and top-level orchestration.
- Update tests/imports and development/oracle docs.
- Ensure direct command execution from repository root works under the current
  `uv run python scripts/qualification_dashboard_parity.py ...` invocation.

Acceptance evidence:

- No user/agent command change.
- No sys.path hack or environment-specific import requirement is introduced.

## 8. Failure, cancellation, restart, contention semantics

Tooling process cleanup semantics are part of the compatibility contract.

On failure/interruption, candidate/oracle server and Chrome processes must be
terminated using the same bounded cleanup policy. Timeout values and readiness
barriers remain unchanged unless a concrete pre-existing defect is separately
planned.

M007's operational-event timing barrier remains intact and must not be
simplified into sleeps.

No production process lifecycle code changes.

## 9. Compatibility and migration

No production migration.

Tooling compatibility includes:

- command path and CLI flags;
- environment variable names;
- oracle manifest/capture schemas;
- strict report JSON/Markdown shape;
- browser manifest fields;
- exit behavior;
- accepted-difference classification.

Internal Python import paths are not public unless tests/docs already rely on
them; keep the top-level script as the supported surface.

## 10. Required tests

Focused:

```bash
uv run ruff format --check scripts/ tests/tooling/
uv run ruff check scripts/ tests/tooling/
uv run pyright scripts/
uv run pytest tests/tooling/test_dashboard_parity_projection.py -q --tb=short --maxfail=1
uv run pytest tests/tooling/ -q --tb=short --maxfail=1
```

Compatibility/golden checks:

- `--help` output/options;
- representative strict report JSON schema/order;
- Markdown report sections;
- known accepted-gap classification;
- deliberate comparator failure cases;
- oracle capture/manifest hashes;
- M007 recovery-summary barrier;
- browser manifest schema and process cleanup.

Run the real strict qualification against the same frozen oracle after
refactor. Run the screenshot/interaction + shutdown/restart modes at least once
because those orchestration paths are being moved.

Repository gates, despite production code being unchanged:

```bash
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
```

Hosted CI must pass on the final implementation/closure head.

## 11. Documentation updates

- `tests/fixtures/dashboard-python-oracle/README.md`: internal tooling layout
  and regeneration/import ownership, without changing oracle authority.
- `.opencode/skills/development/SKILL.md`: keep the same supported command and
  update internal module/test pointers if useful.
- No architecture/runtime doc change is required unless it currently names the
  tooling file as a single implementation owner.

## 12. Acceptance criteria

1. M009 is closed before M010 begins.
2. The top-level qualification script is a thin stable facade.
3. Projection/comparison, oracle/fixture, process, browser, and reporting
   responsibilities live in explicit modules.
4. CLI flags/defaults/environment variables/exit behavior remain compatible.
5. Oracle fixtures, normalization, and strict comparator semantics are
   unchanged.
6. Accepted mismatch groups remain unchanged.
7. Representative JSON/Markdown/browser report schemas remain stable.
8. Real strict + screenshot/interaction + shutdown/restart qualification passes
   with the same accepted source-backed differences.
9. Ruff, Pyright, tooling tests, full Rust default/no-default gates, and hosted
   CI pass.
10. No production source or asset changes occur.

## 13. Stop conditions

Stop and report rather than broadening scope if:

- M009 is not closed;
- a module extraction requires changing normalization/comparator semantics;
- report/manifest schema must change for convenience;
- process/browser cleanup behavior becomes less deterministic;
- frozen oracle captures would need regeneration;
- a new external browser/test dependency is proposed;
- production Rust changes become necessary;
- unrelated CI failures appear.

## 14. Closure evidence required

The closure record must include:

- implementation commit(s);
- before/after file/function responsibility map;
- CLI `--help` compatibility evidence;
- representative pre/post JSON/Markdown schema comparison;
- oracle capture/manifest hash comparison;
- negative comparator test results;
- strict real qualification result and accepted-difference summary;
- browser/interaction/shutdown-restart result;
- Ruff/Pyright/tooling results;
- full Rust default/no-default results;
- hosted CI run ID/conclusion;
- security/secret-free and process-cleanup review;
- severity-tagged unresolved findings;
- registry/roadmap unblock audit and final disposition.

## 15. Handoff notes

Do not "clean up" comparator behavior while moving it. M010 exists because the
harness has become large, not because its strictness is wrong.

Preserve the top-level command as the stable operator/agent entry point.

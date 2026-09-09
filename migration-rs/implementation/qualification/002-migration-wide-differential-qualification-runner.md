# Q002 — Migration-Wide Deterministic Differential Qualification Runner

Status: ready for handoff

Source roadmap: `migration-rs/subsystems/qualification-roadmap.md`

Repository baseline: planning baseline `00dd27fa103e3c663968ecd95d9289c60fca0601`; implement against current main after accepted Q001.

Primary class: invariant/polish

Hard dependency: accepted Q001.

## Objective

Build one reproducible, bounded migration-wide qualification entry point over the existing Python/Rust oracle and focused Rust suites. Q002 should expose aggregate coverage and mismatches without duplicating subsystem logic or hiding differences behind broad normalization.

The intended result is a qualification runner/report that can be invoked deliberately before M10 closure and on representative targets. It is not a new runtime subsystem and is not automatically run in full on every commit.

## Existing evidence to compose

Reuse rather than rewrite:

- `tests/migration_rs/harness.py` and F002 differential launchers;
- F003/F004/F005/F006 tests;
- M4 provider transport fixtures;
- M5 routing/catalog/health tests;
- M6 request/codec/SSE cross-surface matrix;
- M7 coordinator finite/stream/failure/recovery matrices;
- M8 reload/generation/shutdown matrices;
- M9 command/control/backup/update/deploy matrices;
- Q001 manifest and normalization registry.

## Runner design

Add a small migration/qualification runner under `scripts/` or `tests/migration_rs/` that:

1. validates Q001 manifest completeness;
2. identifies the cells assigned to deterministic-local qualification;
3. executes named existing tests/suites and any new cross-boundary tests;
4. records pass/fail/skip/block/infrastructure-error distinctly;
5. writes a bounded JSON summary plus human-readable Markdown/table output;
6. includes candidate/Python identities and exact command lines;
7. exits non-zero for any mandatory deterministic cell that fails or is unowned.

Do not parse free-form test output as the primary contract when a direct structured observation can be produced.

## Required aggregate cross-boundary scenarios

Add only the scenarios not already represented end-to-end. At minimum:

### Fresh local deployment lifecycle

- isolated config/env/DB/runtime root;
- Python and Rust initialize equivalent config;
- migrate/start/readiness;
- deterministic local provider catalog;
- finite inference;
- streaming inference;
- runtime status;
- rehash of a live field;
- operator stats observation;
- graceful shutdown;
- DB durable-state comparison.

### Multi-account/provider failure path

- two deterministic accounts/providers;
- first attempt transport or pre-handoff provider failure;
- replacement ownership/failover;
- final finite success;
- compare attempt/reservation/routing/effect projections;
- assert no retry after downstream start in the streaming variant.

### Restart/recovery path

- create in-flight/pending durable state through reviewed fault hooks;
- restart Rust and Python references against their isolated DBs;
- reconcile without replay;
- compare terminal durable classifications and released reservations.

### Operational mutation path

- config mutation that is live-reloadable;
- mutation that is restart-required;
- backup and restore to a fresh isolated root;
- verify API/CLI state after restore.

## Observation discipline

Results should contain scalar/structural data, not raw provider bodies or secrets. For each mismatch identify:

- manifest cell id;
- Python observation;
- Rust observation;
- normalization rule used;
- first differing semantic field;
- owning subsystem/plan likely responsible.

If an existing historical normalization is too broad, tighten it and treat newly exposed mismatches as findings.

## CI posture

Do not replace the current small CI with the full runner by default. Q002 may add:

- a fast deterministic subset appropriate for current CI if it is demonstrably cheap;
- a manual `workflow_dispatch` or documented local command for the complete runner.

The closure must record duration/resource characteristics of the runner before deciding any CI placement.

## Required tests

- runner fails on an injected mandatory mismatch;
- runner distinguishes skip/block from pass;
- stale/missing manifest ids fail closed;
- Python and Rust launchers are distinct binaries/processes;
- observation normalization is rule-specific;
- result artifact size is bounded;
- secrets/API keys in fixture environment are redacted/omitted;
- cross-boundary lifecycle, failure, recovery, and mutation scenarios pass.

## Verification

Run:

```text
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --all-targets -- --test-threads=1
uv run pytest tests/migration_rs -q --tb=short --maxfail=1
# Q002 aggregate runner command defined by implementation
uv run pytest tests/smoke/ -q --tb=short --maxfail=1
uv run pyright src/ scripts/
uv run ruff format --check src/ tests/ scripts/
uv run ruff check src/ tests/ scripts/
git diff --check
```

If full serial Rust test execution is unavailable because of runner infrastructure, record the limitation and provide equivalent per-suite evidence; do not label an abandoned aggregate as pass.

## Non-goals

Q002 does not run live providers, rootful deployment, browser screenshots, physical SBC tests, or long-duration soak. Those have later owners.

## Closure evidence

Write `migration-rs/closure/qualification/002-status.md` containing:

- runner path/interface;
- Q001 deterministic cell count and coverage result;
- new cross-boundary scenarios;
- exact normalization rules exercised;
- aggregate commands/duration/results;
- any mismatches fixed during Q002 and regression evidence;
- CI placement decision;
- unresolved findings and registry transition.

## Acceptance criteria

Q002 closes only when every mandatory deterministic-local Q001 cell is passing or explicitly assigned to a later environment-specific plan, the aggregate runner is reproducible and secret-safe, and no high/medium deterministic compatibility finding remains.

Accepted Q002 promotes only Q003.

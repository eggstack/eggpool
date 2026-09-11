# P004 Closure — Oracle, Differential, Test, and Python Tooling Retirement

Status: accepted/closed 2026-09-11

Plan: [P004 — Oracle, differential, test, and Python tooling retirement](../../implementation/retirement/004-oracle-differential-test-and-python-tooling-retirement.md)

Implementation commit: `8980616cda2b92ef0a449369806688536e2b8e10`

Disposition evidence: [`004-disposition.json`](004-disposition.json)

## Result

The historical Python application is absent from the active test and CI graph.
The live Python/Rust launcher, application-backed migration fixtures, Python
application test suites, and app-only diagnostics were removed after their
contracts were mapped to Rust suites or frozen neutral fixtures. Release,
catalog, package-boundary, installer-transition, and Rust-only qualification
tools remain as bounded development tooling in `scripts/`; their retained
tests live under `tests/tooling/`.

The root `pyproject.toml` is now tooling-only: it has no `[project]`, build
backend, Python console script, historical application package, or production
dependency graph. `uv.lock` contains only the retained `pytest`, `ruff`, and
`pyright` tooling graph. Public historical Python wheels remain external,
immutable package-manager evidence; no wheel or source archive was copied or
modified.

Qualification config/TLS inputs moved from `tests/migration_rs/fixtures/` to
`migration-rs/fixtures/qualification/`. Existing schema, wire, coordinator,
routing, operations, provider-transport, dashboard, and runtime-lifecycle
observations remain under `migration-rs/fixtures/` and are consumed by named
Rust tests. The complete path-to-authority mapping is machine-readable in
`004-disposition.json`.

## Requirement evidence

| Requirement | Evidence |
|---|---|
| No active test/CI path imports or launches the Python application | `tests/` contains only `tests/tooling/` plus the schema fixture; CI runs Cargo and retained tooling checks; repository search found no `from eggpool`, `import eggpool`, Python server launch, or local oracle reference |
| Migration-only live differential machinery removed | `tests/migration_rs/harness.py`, application-backed fixture modules, dual-run tests, `qualification_runner.py`, `qualification_database.py`, and `qualification_dashboard.py` removed |
| Runtime contracts retain named authorities | `rust/tests/`, `rust/src/`, `rust/assets/runtime-manifest.json`, `migration-rs/fixtures/`, and accepted M4-M11 closure records; see `004-disposition.json` |
| Python remains tooling-only and bounded | root tool-only `pyproject.toml`, reduced `uv.lock`, `scripts/` validators/qualification tools, and `tests/tooling/` |
| Historical package compatibility remains external and explicit | K001 catalog, `scripts/qualify_cutover_transitions.py`, `scripts/install.sh`, and retained K005/K006/K007 tooling tests |
| Evidence is secret-free and recoverable | P001 manifest, immutable source commit `c6b5d2c25038a8ac155c71f68fd50afea03fa459`, neutral fixtures, and no active Python source archive |

## Verification

Passed:

```text
uv sync --frozen
uv run pytest tests/tooling/ -q --tb=short --maxfail=1
  76 passed

uv run ruff format --check scripts/ tests/tooling/
  43 files already formatted
uv run ruff check scripts/ tests/tooling/
  All checks passed
uv run pyright scripts/
  0 errors, 0 warnings, 0 informations

cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo test --manifest-path rust/Cargo.toml --all-targets -- --test-threads=1
  468 passed (52 suites)

uv run python scripts/check_cutover_catalog.py
  K001 catalog valid: 57 releases; cutover 0.8.0 published; 8 rollback-compatible
uv run python scripts/validate_release_workflow.py .github/workflows/release.yml
  pass; 35 actions, 9 jobs, 3 qualified targets
uv run python scripts/validate_m12_package_boundary.py --workflow .github/workflows/release.yml
  pass; Rust current runtime, packaging/pypi/pyproject.toml publication authority
uv run python scripts/validate_m12_retirement.py
  {"asset_count": 7, "migration_count": 54, "status": "pass"}
git diff --check HEAD
  passed
```

The required search command returned only two intentional `PYTHONPATH`
matches: the retirement validator's forbidden-pattern definition and
`scripts/install.sh` clearing `PYTHONPATH` while probing metadata from an
already-installed historical wheel. It returned no application imports,
Python server launchers, or local Python oracle paths.

`cargo clippy --all-targets -- -D warnings` was also checked and remains a
pre-existing P006 issue: 66 errors and 1 warning in unrelated runtime code.
P004 does not claim that gate; P006 must fix or explicitly review that baseline
before final M12 closure. The normal CI workflow therefore uses Rust format and
tests plus retained tooling checks, without introducing a known-failing Clippy
gate.

## Findings and handoff

No unresolved high- or medium-severity evidence-loss, coverage, packaging, or
secret-safety finding remains for P004. Low-severity follow-up is limited to
P005's repository/documentation consolidation and P006's existing Clippy and
post-retirement package-manager qualification.

P005 is promoted to the sole dependency-ready M12 plan. P006 remains queued
behind accepted P005. M12 remains open; only accepted P006 may close the
milestone.

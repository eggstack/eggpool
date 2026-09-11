# P004 — Oracle, Differential, Test, and Python Tooling Retirement

Status: accepted/closed 2026-09-11; see [closure record](../../closure/retirement/004-status.md)

Source roadmap: `migration-rs/subsystems/python-retirement-roadmap.md`

Primary class: invariant/polish

Hard dependencies: accepted P001-P003

## Objective

Retire migration-only live Python-oracle execution and the historical Python application test dependency graph while preserving the deterministic observations, fixtures and Rust-native contract tests that remain useful after the application source is gone.

P004 is not “delete all Python tests.” It is a classification-and-replacement pass driven by the accepted P001 manifest.

## Test disposition classes

Every Python test/helper under `tests/`, especially `tests/migration_rs/`, must end in one of four states:

1. **retain as tooling test** — tests a still-retained release/fixture/repository Python utility and does not import the retired application;
2. **replace with Rust-native test** — contract belongs to the current runtime and is better enforced in `rust/tests` or a Rust unit test;
3. **freeze as data fixture** — Python was only needed to produce a deterministic observation; retain the bounded JSON/text/DB/HTML fixture and its provenance/hash;
4. **remove as migration-only** — live dual-run launcher/oracle logic has no continuing value after its covered contract is represented elsewhere.

No item may be removed merely because the old application import no longer resolves.

## `tests/migration_rs` conversion

Audit the harness and fixture modules by subsystem. Preserve useful canonical request/wire, coordinator, routing, runtime-lifecycle, operations, dashboard and packaging observations as static fixtures where they still add coverage beyond Rust-native tests.

Replace tests whose only purpose is “launch Python, launch Rust, compare now” with one of:

- Rust against an accepted frozen fixture;
- Rust internal contract tests;
- a repository validator over accepted closure/manifests;
- removal when equivalent stronger Rust coverage is explicitly named.

The final suite must not require the historical `src/eggpool` application or a Python EggPool server to run.

## Development-only Python tooling boundary

Use the P001 inventory to reduce the Python dependency surface to tools that survive M12, such as bounded release/catalog/manifest validators or fixture processors. Remove application-only dependencies from the active development environment once no retained tool needs them.

The final tooling configuration must be clearly non-production:

- no `eggpool` Python console script;
- no build backend capable of publishing the historical application as a current package;
- no installation of `src/eggpool`;
- no service/runtime dependency on the tooling environment.

A root `pyproject.toml` containing only tool configuration is acceptable; alternatively move retained Python tool dependencies to a dedicated tooling manifest. Choose the smaller option supported by actual surviving dependencies rather than inventing a framework.

## Scripts and workflows

Classify scripts/workflows that reference Python or the former application:

- release/manifest validators may remain Python if tooling-only;
- live Python server launchers and oracle generators are removed after fixtures are frozen;
- qualification workflows are updated to run Rust-native/fixture-based gates;
- no normal CI job may silently recreate/install the historical application from Git history.

The M11 public package qualification workflow may still install **public historical PyPI wheels** when explicitly testing cross-era version compatibility. That is not a repository-local Python oracle dependency.

## Evidence preservation

Keep:

- P001 reference manifest and retained fixtures;
- accepted closure records and run reports;
- immutable source commit/tag identities;
- historical PyPI filename/hash identities;
- selected DB/config/dashboard/wire fixtures needed for regression.

Do not keep generated caches, redundant full source archives, provider secrets, or large duplicated release artifacts.

## Failure and rollback semantics

Remove tests/tools incrementally enough that each subsystem retains a named contract authority. If a Rust test does not actually cover a required behavior, add the missing bounded Rust regression before deleting the Python oracle check. If the mismatch reveals a real runtime defect, stop and create a corrective P-plan rather than updating the frozen expected fixture to match broken Rust output.

## Verification

At minimum:

```bash
rtk rg -n "from eggpool|import eggpool|PYTHONPATH|python.*server|python.*oracle" tests scripts .github
rtk cargo test --manifest-path rust/Cargo.toml --all-targets -- --test-threads=1
rtk uv run pytest <retained-python-tooling-tests> -q --tb=short --maxfail=1
rtk uv run python scripts/check_cutover_catalog.py
rtk uv run python scripts/validate_release_workflow.py .github/workflows/release.yml
rtk uv run ruff format --check <retained-python-paths>
rtk uv run ruff check <retained-python-paths>
rtk uv run pyright <retained-python-paths>
rtk git diff --check
```

The implementation must generate a machine-readable or Markdown disposition summary mapping removed test/oracle paths to retained fixture/Rust authorities. Store it with the P004 closure evidence.

## Acceptance criteria

P004 closes only when:

- no test/CI path needs the retired Python application source;
- all required migration contracts have named retained fixture/Rust authorities;
- retained Python is tooling-only and bounded;
- application-only Python dependencies/build metadata are gone from the active tooling environment;
- the full Rust suite and retained tooling tests pass;
- no high/medium evidence-loss or coverage regression remains.

Accepted P004 promotes P005.

## Non-goals

- no requirement to rewrite every useful repository script in Rust;
- no deletion of public historical Python releases;
- no removal of compatible cross-era transition tests;
- no provider/live-network qualification unless a changed tool specifically requires it;
- no new runtime feature;
- no M12 closure claim.

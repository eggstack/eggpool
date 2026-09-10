# K002 — Rust PyPI Binary-Wheel Packaging Substrate

Status: ready for handoff

Source roadmap: `migration-rs/subsystems/cutover-roadmap.md`

Primary class: infrastructure/capability

Hard dependency: accepted K001.

## Objective

Create the production packaging substrate that distributes the existing Rust `eggpool` executable through the existing PyPI project as a platform-specific wheel, without replacing or deleting the root Python oracle package.

Use Maturin's `bin` binding mode. This plan packages a standalone binary; it must not add PyO3 or convert EggPool into a Python extension.

## Packaging layout

Prefer a dedicated publication manifest, for example:

```text
packaging/pypi/
  pyproject.toml
  README.md or metadata reference
```

with the existing Rust crate selected through Maturin `manifest-path`.

The root `pyproject.toml` remains the historical Python/oracle build definition through M11. Do not make Python tests depend on the Rust publication manifest.

Required publication metadata:

- `[project] name = "eggpool"`;
- version dynamically sourced from Cargo or otherwise mechanically verified identical to Cargo;
- `requires-python = ">=3.11"` during M11 per ADR-0004;
- project description/license/authors/URLs/keywords aligned with the public project;
- classifiers updated for the Rust-backed distribution rather than claiming FastAPI/AsyncIO as the canonical runtime;
- no Python runtime dependencies such as FastAPI, Granian, HTTPX, aiosqlite, Pydantic or Click in the Rust wheel;
- no console-script wrapper pointing to `eggpool.cli`; the Maturin `bin` payload supplies the `eggpool` executable.

## Maturin configuration

Use an explicitly pinned/reviewed Maturin 1.x release at implementation time. Configure at minimum:

- `bindings = "bin"`;
- `manifest-path` to `rust/Cargo.toml`;
- Cargo lock enforcement;
- PyPI compatibility checking;
- stripping only if it does not break required diagnostics/self-checks;
- no sdist publication for Rust-backed M11 releases.

Do not enable PyO3/cffi/uniffi.

## Wheel contents contract

A built wheel must contain:

- exactly the native `eggpool` executable expected for the target;
- normal `.dist-info` metadata/RECORD;
- license/readme metadata needed by the public package;
- no copied Python runtime package or transitive Python application dependencies;
- no API keys, config files containing secrets, user state, local build paths or debug artifacts.

The existing dashboard assets are already embedded/packaged by the Rust build boundary and must continue working from the installed wheel binary without requiring repository checkout files.

## Version authority

K002 must consume the K001 version rule. A build must fail before wheel creation/publish if:

- Cargo version and publication metadata differ;
- version is not the selected candidate when a release candidate is requested;
- tag-derived version and package version differ in release mode.

Local development builds may use the current unreleased source version but still require internal metadata consistency.

## Installed runtime proof

Install the local wheel into fresh isolated environments using at least:

- ordinary pip/venv;
- uv tool;
- pipx when available on the development/qualification host.

Prove:

- `eggpool version` executes the Rust binary;
- no `eggpool.cli` Python entrypoint is needed;
- the environment has no required EggPool Python application dependency graph;
- `eggpool help` and `check-config` work;
- a bounded loopback `serve`/health request works;
- uninstall removes the wheel-owned executable cleanly;
- reinstall produces a valid manager-owned environment.

Use isolated temp roots and do not replace the developer's installed EggPool command.

## Runtime-Python independence test

Because the wheel still declares `Requires-Python >=3.11`, package installation uses a Python packaging environment. That is not the same as a runtime Python dependency.

Add a test/probe demonstrating that after installation the executable is a native platform binary and executes without importing/launching the wheel environment's Python interpreter. Suitable evidence includes process tree/exec observation or running the binary with the environment's Python executable temporarily unavailable in an isolated copied layout, provided the test itself does not corrupt the manager environment.

Do not make unsupported claims that the packaging manager itself needs no Python.

## Wheel metadata checks

Inspect built wheel metadata deterministically:

- project name normalized to `eggpool`;
- exact version;
- supported platform tag, never `py3-none-any` for Rust cutover wheels;
- expected `Requires-Python`;
- dependency list empty unless a new packaging-only dependency is explicitly justified;
- executable present in the proper script/data location;
- RECORD contains it;
- wheel size is bounded and below PyPI project/file limits;
- no sdist emitted by the production build command.

## Existing Python package preservation

K002 may add scripts/helpers to build historical Python wheels for K001 gap work, but it must not change the root Python runtime behavior or make the root `pyproject.toml` publish the Rust candidate.

The Python reference must remain installable in isolated tests from its historical source/tag as required by K005.

## Failure semantics

- unsupported host build target -> explicit build error;
- wheel tag not PyPI-compatible -> fail build;
- missing Rust binary -> fail build;
- duplicate/mismatched metadata -> fail build;
- attempt to produce public Rust sdist -> fail release validation;
- accidental Python dependencies in Rust wheel -> fail metadata test;
- missing license/readme/project identity -> fail package validation.

## Dependencies

Maturin is a build/release dependency only. Do not add a Rust runtime dependency for packaging.

If a dedicated Python dev dependency is needed for wheel inspection, prefer stdlib `zipfile`/`email.metadata` style tooling or the existing packaging toolchain before adding a library.

## Tests

Add focused K002 tests for:

- Maturin config shape;
- metadata identity/version;
- no Python application dependencies;
- no sdist in release output;
- native executable presence;
- wheel install/uninstall under isolated pip;
- uv tool install where available;
- pipx install where available;
- native runtime independence;
- dashboard/static asset availability from wheel-installed binary;
- unsupported target/tag rejection through deterministic fixtures.

## Verification

Run at minimum:

```text
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --all-targets -- --test-threads=1
# exact maturin build command frozen by K002
# focused wheel inspect/install tests
uv run pytest tests/migration_rs -q --tb=short --maxfail=1
uv run pytest tests/smoke/ -q --tb=short --maxfail=1
git diff --check
```

## Closure evidence

Write `migration-rs/closure/cutover/002-status.md` with:

- implementation commit(s);
- Maturin version and exact local build command;
- wheel filename/tag/hash/size;
- wheel metadata/content inventory summary;
- pip/uv/pipx isolated install results;
- native-runtime independence proof;
- dependency review;
- unresolved findings;
- registry transition.

## Acceptance criteria

K002 closes only when:

- the existing PyPI identity can be represented by a standards-compliant Rust binary wheel;
- the wheel contains the Rust executable and no Python EggPool runtime dependency graph;
- the root Python oracle packaging remains intact;
- installed CLI/server works from an isolated wheel;
- no Rust sdist fallback is part of the M11 production package contract;
- no new runtime framework/dependency is introduced;
- no unresolved high/medium packaging finding remains.

Accepted K002 promotes only K003.

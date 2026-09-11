# M12-P005 Closure — Repository, Installer, Release, and Documentation Consolidation

Status: accepted/closed 2026-09-11

## Implementation commits

- `c2f8fdd` — Consolidate Rust repository installer and documentation

## Outcome

P005 is complete. The current repository, installer, release workflow, and
operator/contributor documentation now describe one native Rust production
runtime. `rust/` owns the application and embedded assets, while
`packaging/pypi/` owns the current binary-wheel publication metadata. The root
Python project and retained Python scripts/tests are tooling-only.

The quick installer now checks the supported target classes before mutation,
uses the current Rust package path for source checkouts, and rejects explicit
historical versions outside the eight schema-compatible catalog entries. Its
deterministic qualification covers fresh installs, manager-owned historical
adoption, standalone adoption and rollback, source checkout, ownership and
path collisions, root refusal, unknown arguments, uncatalogued historical
targets, and unsupported platforms.

The deleted `scripts/install_prompt.py` helper and stale Python-current
architecture/module guidance are no longer active. Historical Python source
and package artifacts remain available only through immutable Git/PyPI
evidence and explicit exact-version compatibility paths. No public artifact,
PyPI file, GitHub release, or schema was changed by P005.

## Verification

All required P005 checks passed:

```text
rtk uv run python scripts/check_cutover_catalog.py
  K001 catalog valid: 57 releases; cutover 0.8.0 published; 8 rollback-compatible
rtk uv run python scripts/validate_cutover_docs.py
  pass; docs_checked=7; production release=0.8.0; targets=3
rtk uv run python scripts/validate_release_workflow.py .github/workflows/release.yml
  pass; actions=35; jobs=9; targets=3
rtk cargo test --manifest-path rust/Cargo.toml --test operations_o008 -- --test-threads=1
  10 passed
rtk cargo test --manifest-path rust/Cargo.toml --test operations_o009 -- --test-threads=1
  8 passed
rtk cargo test --manifest-path rust/Cargo.toml --all-targets -- --test-threads=1
  468 passed across 52 suites
rtk uv run python scripts/qualify_quick_installer.py
  14 deterministic cases passed
rtk uv run pytest tests/tooling/ -q --tb=short --maxfail=1
  76 passed
rtk uv run ruff format --check scripts/ tests/tooling/
  42 files already formatted
rtk uv run ruff check scripts/ tests/tooling/
  all checks passed
rtk uv run pyright scripts/
  0 errors, 0 warnings, 0 informations
rtk uv run python scripts/validate_m12_package_boundary.py --workflow .github/workflows/release.yml
  pass
rtk uv run python scripts/validate_m12_retirement.py
  pass; 7 assets and 54 migrations verified
rtk git diff --check
  pass
```

The requested stale-reference search has no matches in user-facing/current
paths. Its only matches in `scripts/` are the intentional retirement-validator
patterns that detect a reintroduced `src/eggpool` tree or import.

`cargo fmt --manifest-path rust/Cargo.toml -- --check` also passed. A strict
Clippy run was attempted and reports the pre-existing M11 Rust baseline
(66 errors and one warning in runtime code, including collapsible-if and
modernization lints); P005 introduced no Rust runtime changes. P006 explicitly
owns disposition of that known baseline and must not silently omit the gate.

## Retained and removed evidence

Retained evidence includes the K001 release catalog and its eight compatible
historical rollback entries, the M12 runtime-asset manifest and schema-54
migration checksums, current release/workflow validators, Rust operation tests,
and prior accepted M12/M11 closure records. These files were validated in
place; no historical observation or public artifact was rewritten.

Removed active material is limited to the deleted Python onboarding helper and
obsolete current-runtime references in repository guidance. No historical
source, package, fixture, migration, or public release file was removed.

## Unresolved findings

No P005-introduced high/medium packaging, installer, release, compatibility,
security, lifecycle, or evidence-loss finding remains. The pre-existing Rust
Clippy baseline is recorded above and remains a required P006 disposition, not
a P005 retirement regression.

## Registry transition

P005 is accepted/closed. Per the planning process,
`migration-rs/registry.md` and the M12 roadmap now mark P006 as the sole
`dependency-ready` plan. P005 does not close M12; only accepted P006 may make
that final closure claim.

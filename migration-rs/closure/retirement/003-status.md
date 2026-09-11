# P003 Closure — Python Application Source and Runtime-Asset Retirement

Status: accepted/closed 2026-09-11

Plan: [P003 — Python application source and runtime-asset retirement](../../implementation/retirement/003-python-application-source-and-runtime-asset-retirement.md)

## Implementation

Implementation commit: `39aec1c992b473651557c71914488f183b0aa855`

The complete tracked `src/eggpool/` application tree was removed. The source
remains recoverable at the immutable P001 reference commit
`c6b5d2c25038a8ac155c71f68fd50afea03fa459` (tree
`2887b6b6a3be38ad8b386781cda76f888d5ac0dc`) and was not copied into an active
archive or legacy tree.

Rust production ownership was made explicit before deletion:

| Runtime contract | Surviving owner | Proof |
|---|---|---|
| SQLite migrations 0001–0054 and checksums | `rust/assets/db/migrations/`, embedded by `rust/build.rs` | 54 files and every SHA-256 checksum validated |
| Default configuration and SBC template | `rust/assets/config/` | hash-locked runtime manifest and embedded mutation path |
| Provider onboarding templates | `rust/assets/providers/_templates.toml` | hash-locked runtime manifest and O004 Rust test |
| Wire-profile registry | `rust/assets/providers/_wire_profiles.toml` | hash-locked runtime manifest and Rust registry tests |
| Current/historical release catalog | `rust/assets/catalog/k001-installable-releases.json` | hash-locked runtime manifest and catalog validator |
| Dashboard static files and themes | existing `rust/assets/dashboard/` | 54-entry manifest validates every embedded asset hash |
| Historical compatibility observations | `migration-rs/fixtures/` and P001 manifest | retained neutral fixtures; Rust production no longer reads the fixture tree |

`rust/assets/runtime-manifest.json` records the P001 source identity and the
exact SHA-256 values for the Rust-owned runtime assets. No migration was added,
renumbered, reordered, or checksum-rewritten; schema semantics and canonical
config/data/runtime/backup/log/service paths are unchanged.

## Verification

Passed:

```text
rtk cargo build --manifest-path rust/Cargo.toml --locked
  Finished dev profile successfully

rtk cargo test --manifest-path rust/Cargo.toml --all-targets -- --test-threads=1
  468 passed (52 suites)

rtk cargo test --manifest-path rust/Cargo.toml --test database_compatibility -- --test-threads=1
  6 passed

rtk cargo test --manifest-path rust/Cargo.toml --test operations_o004 -- --test-threads=1
  6 passed

rtk cargo test --manifest-path rust/Cargo.toml --test build_manifest -- --test-threads=1
  4 passed

rtk cargo fmt --manifest-path rust/Cargo.toml --all -- --check
rtk uv run ruff format --check scripts/validate_m12_retirement.py
rtk uv run ruff check scripts/validate_m12_retirement.py
rtk uv run pyright scripts/validate_m12_retirement.py
  all passed; Pyright reported 0 errors, 0 warnings, 0 informations

rtk uv run python scripts/validate_m12_retirement.py
  {"asset_count": 7, "migration_count": 54, "status": "pass"}

rtk uv run python scripts/check_cutover_catalog.py
  K001 catalog valid: 57 releases; cutover 0.8.0 published; 8 rollback-compatible

rtk uv run python scripts/validate_release_workflow.py .github/workflows/release.yml
  pass; 35 actions, 9 jobs, 3 qualified targets

rtk uv run python scripts/validate_m12_package_boundary.py --workflow .github/workflows/release.yml
  pass; current runtime Rust and publication manifest packaging/pypi/pyproject.toml

rtk git diff --check
  passed
```

A local Maturin build from `packaging/pypi` produced
`eggpool-0.8.0-py3-none-macosx_10_12_x86_64.whl`. Its six members were the
native `eggpool` executable, four `.dist-info` metadata/license/SBOM files,
and `RECORD`; it contained no Python application modules or package
directory. The artifact is local verification only and does not qualify a
new target or publish a release.

The production/release boundary validator also passes its negative checks:
`src/eggpool` is absent, Rust/packaging/release paths contain no historical
application imports or launch commands, and the current publication manifest
does not expose the root Python console script.

## Findings and handoff

No unresolved high- or medium-severity runtime-asset, migration, packaging,
or data-loss finding remains. P004-owned migration-oracle tests and retained
Python diagnostic scripts still reference the historical application; they
are not current production/release dependencies and remain deliberately in
scope for P004’s disposition pass. P005 still owns consolidation of stale
user/developer documentation and the tooling-only root project metadata.

P004 is promoted to the sole dependency-ready M12 plan. P005 and P006 remain
serially blocked behind their direct predecessors. M12 remains open; only
accepted P006 may close the milestone.

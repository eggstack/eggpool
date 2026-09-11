# P005 — Repository, Installer, Release, and Documentation Consolidation

Status: dependency-ready after accepted P004 closure

Source roadmap: `migration-rs/subsystems/python-retirement-roadmap.md`

Primary class: capability/polish/invariant

Hard dependencies: accepted P001-P004

## Objective

Consolidate the post-retirement repository around one current Rust application and one Rust release path. Remove stale Python-current-runtime assumptions from installer/update/docs/workflows while preserving explicit compatible historical exact-version selection as defined by ADR-0005.

P005 prepares the tree for final M12 qualification. It must not change current API/runtime semantics or publish a release solely to prove documentation.

## Repository authority cleanup

After P003/P004, ensure the repository has a simple ownership layout:

- `rust/` owns the current EggPool application/runtime;
- `packaging/pypi/` owns current PyPI publication metadata;
- retained Python code is clearly tooling-only and cannot be imported as `eggpool` application code;
- `migration-rs/fixtures/retirement/` and accepted closure records own retained historical observations;
- historical source is referenced by immutable Git commit/tag, not a duplicated active application directory.

Remove stale package-data, build, lint/type-check, test, IDE or release configuration that only served the deleted Python application.

## Installer behavior

Review `scripts/install.sh` after source retirement and preserve these user-facing rules:

- no version specified → latest compatible current Rust release;
- `--version`/exact update may target a catalogued historical Python release if package-manager/Python/DB-config compatibility passes;
- existing historical Python installs can still be adopted/upgraded without cloning the old source tree;
- standalone Rust adoption remains explicit and collision-safe;
- no source-checkout or current-install path assumes the deleted Python application exists;
- unsupported platform and ambiguous ownership fail closed before destructive mutation.

A small metadata probe that executes the interpreter belonging to an **already-installed historical Python release** is allowed. It must inspect package metadata only and must not import repository-local Python application source.

## Update/catalog behavior

Re-run and tighten guards around:

- current/latest release era must be Rust;
- exact historical target is explicit, immutable and compatibility-gated;
- current Rust package-manager installs never receive direct raw-binary overwrite;
- standalone raw updater never attempts a Python target;
- automatic recovery restores the previous exact install and never changes eras opportunistically;
- historical files are never rebuilt/reuploaded as an M12 side effect.

The K001 catalog may retain all historical records. Mark current support/compatibility fields clearly enough that a future maintainer cannot confuse “artifact exists” with “safe for this database.”

## Release workflow

Prove `.github/workflows/release.yml` and release validators are independent of the removed Python application/root historical build backend. The workflow may use Python as a **build/validation tool** to run Maturin or scripts; that is not a production application dependency.

Required invariants:

- wheel payload is native Rust plus metadata/assets only;
- no current sdist is published;
- no `src/eggpool` path is read;
- target matrix remains Linux x86_64, Linux aarch64, macOS arm64 unless separately qualified;
- Trusted Publishing/OIDC, attestations, exact-bundle recovery and immutable artifact validation remain intact;
- a `workflow_dispatch` validation/rehearsal path can build the complete artifact set without publishing.

## Documentation

Update README and `docs/` so they accurately distinguish:

- current EggPool: native Rust executable distributed in wheels/raw assets;
- PyPI/Python requirement: package-management compatibility, not runtime execution;
- historical exact-version selection: supported only when the catalog/DB compatibility permits it;
- historical Python source: available from repository history, not a current source package;
- developer Python utilities: tooling-only;
- supported OS/architecture matrix;
- source-development flow for current Rust code.

Remove instructions that tell current contributors/users to run, install, import or modify `src/eggpool` as the active implementation.

## Failure and rollback semantics

P005 verification must be non-publishing by default. Any release-workflow rehearsal uses local artifacts or the existing `validate` destination. Do not modify public PyPI/GitHub release `0.8.0`, yank historical versions, delete files, or require a new stable version.

Installer/update deterministic fault tests must continue to prove manager failure, wrong version, ownership collision, config validation failure, restart failure and rollback failure categories without mutating a real user installation.

## Verification

At minimum:

```bash
rtk uv run python scripts/check_cutover_catalog.py
rtk uv run python scripts/validate_cutover_docs.py
rtk uv run python scripts/validate_release_workflow.py .github/workflows/release.yml
rtk cargo test --manifest-path rust/Cargo.toml --test operations_o008 -- --test-threads=1
rtk cargo test --manifest-path rust/Cargo.toml --test operations_o009 -- --test-threads=1
rtk cargo test --manifest-path rust/Cargo.toml --all-targets -- --test-threads=1
rtk rg -n "src/eggpool|python -m eggpool|historical Python oracle|current Python runtime" README.md docs scripts packaging .github
rtk git diff --check
```

Run local wheel/installer transition tests for at least:

- fresh current Rust install;
- exact-current repair/no-op;
- compatible historical Python exact target;
- incompatible historical target rejection before mutation;
- historical Python → current Rust adoption;
- standalone Rust target handling;
- unsupported target/no-wheel behavior.

## Acceptance criteria

P005 closes only when the repository/docs/release/installer surfaces tell one coherent story, current production/release behavior is independent of the deleted Python application, and compatible historical exact-version switching remains explicitly qualified rather than hidden or removed.

Accepted P005 promotes P006 as the sole final M12 closure plan.

## Non-goals

- no public release required;
- no new target support;
- no removal of historical PyPI files;
- no rewrite of release tooling into Rust for aesthetic reasons;
- no runtime feature work;
- no M12 closure claim.

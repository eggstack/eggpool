# Plan 171 — Migration Scaffold Retirement and Durable Release/Compatibility Consolidation

Date: 2026-09-11
Status: ready for handoff
Parent roadmap: `plans/168-rust-production-cleanup-roadmap.md`
Priority: P1 repository maintenance / ownership cleanup
Execution target: GPT-5.6 Luna/Sol or comparable implementation model

## Purpose

Remove the completed Python-to-Rust migration machinery from EggPool's active repository surface **without removing the durable contracts that were created during migration**.

M12/P007 is closed and no migration implementation plan remains dependency-ready. `migration-rs/` is therefore historical evidence, but it is not yet safely deletable: current release documentation and tooling still reference cutover-era manifests and validators under migration-specific names/paths. This plan first extracts those live contracts into neutral production-maintenance locations, verifies them, and only then removes historical migration scaffolding from the active tree/navigation.

The end state should make the repository read as a Rust application with bounded release/compatibility tooling, not as an application still undergoing a language migration.

## Current live dependencies on migration-era structure

Examples that must be resolved before deletion include:

- `docs/releasing.md` invokes `scripts/check_cutover_catalog.py`, `validate_cutover_release.py`, `validate_cutover_docs.py`, and `validate_m12_package_boundary.py`;
- release rehearsal consumes `migration-rs/closure/cutover/009-run.json`;
- post-publication verification consumes `migration-rs/closure/cutover/k003-release-manifest.json`;
- root `pyproject.toml` labels itself `project_role = "migration-tooling-only"`;
- retained scripts/tests use `cutover`, `m12`, and migration-era K/P identifiers for behaviors that are now ordinary exact-version/release/package contracts;
- `migration-rs/registry.md` still begins `Status: active` despite all migration milestones being closed.

Do not delete these paths until each current consumer has either been retired as one-time evidence or moved to a durable neutral authority.

## Governing constraints

1. Preserve exact-version update/downgrade compatibility for the currently supported historical Python window unless a separate product decision changes it.
2. Preserve package-manager ownership semantics for uv, pipx, pip/venv, standalone binary, and source workflows.
3. Preserve database/config compatibility prechecks, failed-update recovery, package publication validation, target/artifact integrity, and public-release verification.
4. Historical Python packages remain immutable external PyPI artifacts; do not vendor old Python application source back into the repository.
5. Git history is the complete migration archive. Do not copy `migration-rs/` wholesale into `docs/archive/` or another active directory.
6. Keep at most one concise current historical pointer if useful for locating final migration closure and rollback rationale.
7. Do not rename public CLI commands/config keys merely to remove migration terminology from internal tooling.
8. Script/fixture/test renames must preserve behavior and be verified before old names are deleted.
9. Do not port bounded Python release tooling to Rust solely for language purity.
10. Do not delete migration-era files still used as current release authority until source-equivalent neutral artifacts are committed and validated.
11. Do not create a new registry, closure system, or planning framework to replace `migration-rs/`.

## Workstream A — Build a live-reference inventory

Search the active tree for `migration-rs`, `cutover`, `M10`, `M11`, `M12`, K/P phase identifiers, `historical_python_version`, and `migration-tooling-only`. Classify each hit as live runtime, live release/package/rollback tooling, live test fixture/assertion, current operator/contributor docs, or purely historical evidence/navigation.

The inventory may be temporary. The directory cannot be removed until all live categories have neutral replacements or are intentionally retired with proof they are no longer needed.

## Workstream B — Establish neutral durable compatibility/release authorities

Move the small set of data files that remain executable release/rollback authority out of `migration-rs/`. Prefer simple current locations such as `packaging/compatibility/`, `packaging/release/`, `artifacts/release/`, or `tests/tooling/fixtures/` based on actual ownership; do not create all of them unnecessarily.

For every moved artifact:

- document whether it is current mutable authority, immutable fixture, or example;
- preserve checksums/source-version identity where relevant;
- update validators/tests before deleting the old path;
- keep current version authority in `rust/Cargo.toml` / `packaging/pypi/pyproject.toml`.

If K009/K003 JSON files are frozen examples, rename them semantically instead of carrying K-number terminology permanently.

## Workstream C — Rename or retire migration-named tooling

Audit retained scripts/tests. Rename only scripts enforcing current product/release behavior, for example:

- `check_cutover_catalog.py` -> neutral exact-version/release catalog naming;
- current artifact build/inspect/create helpers -> neutral native-release artifact naming;
- `qualification_cutover_rehearsal.py` -> neutral release/upgrade rehearsal naming if maintained;
- `validate_m12_package_boundary.py` -> neutral Rust package/runtime boundary naming;
- delete `validate_m12_retirement.py` if it only proves a completed one-time retirement invariant, or retain a much narrower neutral no-Python-runtime check only if it protects current behavior;
- rename cutover docs/release validators when they are ordinary release gates.

Do not blindly rename every historical helper. Delete one-time migration qualification scripts/tests once durable behavior is covered elsewhere. Update `tests/tooling/` alongside retained scripts and delete orphaned helpers/fixtures.

## Workstream D — Simplify root Python-tooling metadata

Update root `pyproject.toml` so it describes current repository tooling rather than migration. Replace `project_role = "migration-tooling-only"` with a neutral role such as `repository-tooling-only`, or remove the project-specific field if no live consumer needs it. Retain `current_runtime = "rust"` only if a live validator consumes it; remove closure-only fields; preserve `package = false`, Python tooling requirements, and Ruff/Pyright/Pytest configuration.

Update `uv.lock` only if dependencies actually change.

## Workstream E — Remove historical migration documentation/scaffold

After neutral authorities are green and a repository-wide scan proves no current consumer remains, remove `migration-rs/` from the active tree.

Before deletion, add at most one concise durable historical pointer if useful. It should record that migration completed with the Rust 0.8.0/M12 P007 lineage, provide a final closure commit/reference sufficient to recover the historical tree from Git, and point current historical-version policy at neutral release/upgrading docs.

Do not retain old registry/ADR/implementation/closure matrices or evidence JSON solely for archaeology. Git history preserves them. `plans/` remains the ordinary historical plan record; this plan does not mass-delete unrelated completed plans.

If a migration ADR still describes a current architectural rule (for example PyPI binary-wheel authority), restate the durable decision compactly in current architecture/release docs before deleting the migration copy.

## Workstream F — Update current documentation/navigation

Inspect and update at minimum:

- `README.md`;
- `AGENTS.md`;
- `docs/releasing.md`;
- `docs/upgrading.md`;
- relevant deployment docs;
- `architecture/README.md` and affected deep dives;
- `.opencode/skills/` guidance;
- release-workflow comments and validator invocations.

Use current-state wording: native Rust application, native wheel, historical exact-version compatibility. Avoid `candidate`, `cutover`, `M12`, or migration terminology for ordinary release operations unless discussing history explicitly.

Correct the existing release-document inconsistency: `AGENTS.md` says “Manual release procedure — no automated release workflow,” while `docs/releasing.md` identifies `.github/workflows/release.yml` as the production release authority. Final documentation must identify one truthful release model.

## Workstream G — Preserve/requalify exact-version compatibility

After path/script renames, retain deterministic coverage for:

- current Rust -> supported historical Python transition logic;
- historical Python -> current Rust return;
- uv/pipx/pip ownership selection;
- standalone Rust refusal of impossible Python downgrade paths;
- incompatible schema/config/version rejection before mutation;
- configuration/database path preservation;
- failed-update recovery behavior;
- current package/runtime boundary.

A full public cross-era installation rehearsal need not run on every CI push if intentionally release/manual qualification, but deterministic tooling tests must continue protecting decision logic.

## Workstream H — Final orphan/reference audit

Before deleting the scaffold, require zero unintended executable/current-doc references. A representative scan is:

```bash
rg -n "migration-rs|migration-tooling-only|validate_m12|cutover"
```

Historical wording may remain in CHANGELOG or this completed plan with an explicit reason. Current executable, documentation, workflow, packaging, installer, and test references must be zero or intentionally historical-only.

## Required verification

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --all-targets -- --test-threads=1
cargo build --manifest-path rust/Cargo.toml --locked --release
uv sync --frozen
uv run ruff format --check scripts/ tests/tooling/
uv run ruff check scripts/ tests/tooling/
uv run pyright scripts/
uv run pytest tests/tooling/ -q --tb=short --maxfail=1
git diff --check
```

Then run every retained release/catalog/artifact/package-boundary validator under its final neutral name and perform the orphan/reference scan. If `.github/workflows/release.yml` changes, validate its structure and run its non-publishing validation destination when practical before the next release.

## Acceptance criteria

- No current runtime, packaging, installer/updater, release workflow/procedure, test suite, or contributor navigation requires `migration-rs/`.
- All still-live manifests/fixtures are under neutral current locations with clear ownership.
- Migration-only scripts/tests are deleted; durable scripts/tests have neutral names and unchanged/stronger contracts.
- Root tooling metadata no longer describes the repository as undergoing migration.
- `migration-rs/` is removed rather than copied into another archive directory.
- One concise historical pointer exists only if useful; Git history is archival authority.
- Exact-version historical compatibility and current Rust packaging remain qualified.
- Release docs, AGENTS, workflow behavior, and package authority agree.
- Strict Clippy, Rust tests, tooling checks, and release validators are green.

## Handoff note

Perform this as extraction-then-deletion, not a mass rename/delete commit. A reviewer should be able to inspect the neutral release/compatibility authority while the old scaffold still exists, run both sets of checks, and only then accept deletion of the historical tree.

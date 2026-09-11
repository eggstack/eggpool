# P002 — Rust Production Package, Catalog, and Cross-Era Authority

Status: queued; promote only after accepted P001 closure

Source roadmap: `migration-rs/subsystems/python-retirement-roadmap.md`

Primary class: infrastructure/invariant

Hard dependencies: accepted P001; accepted ADR-0005

## Objective

Make the repository's package, version-catalog, updater, installer and release guards unambiguously describe one **current Rust production implementation** while preserving the already-qualified ability to select a compatible historical Python version explicitly through a package-manager-owned installation.

P002 changes authority and metadata. It does **not** remove `src/eggpool/` or migration-oracle tests; P003/P004 own those destructive actions.

## Production publication authority

`packaging/pypi/pyproject.toml` remains the sole current/future publication manifest for the `eggpool` project. Add deterministic guards so the release workflow and release-validation scripts fail if they attempt to build/publish the historical root Python package as a current release.

The release path must continue to enforce:

- Maturin `bindings = "bin"`;
- no Rust sdist fallback;
- the M10-qualified target set only;
- one Cargo/package/tag/manifest version authority;
- pinned build/publish tooling and existing OIDC/attestation boundary;
- wheel/raw artifact bytes produced from one source revision.

Do not publish a new release merely to close P002.

## Root Python package disposition

Use the accepted P001 manifest to mark root `pyproject.toml` as historical/development-only rather than a current EggPool distribution authority. P002 may move or split tooling configuration when necessary to prevent accidental publication, but must keep the Python application executable for P003/P004 evidence until those plans close.

Acceptable approaches are limited to a small, explicit development-tool manifest or non-build root tool configuration. Do not create a second public package name and do not introduce a wrapper package around the Rust executable.

## Historical exact-version contract

Update the embedded K001 catalog and validators so the distinction is explicit:

- current/future release era: Rust;
- historical Python releases: immutable historical artifacts;
- exact historical target: allowed only when artifact, Python environment and DB/config compatibility checks pass;
- `latest`: Rust-only;
- unsupported/incompatible historical target: fail before package mutation;
- no automatic Python fallback after Rust download/install/runtime failure.

The existing schema-54 compatibility window remains evidence-driven rather than hardcoded from prose. P002 must not widen compatibility for older releases simply because their wheels exist.

## `Requires-Python` decision

Retain `Requires-Python >=3.11` on current Rust wheels during M12. Record in package metadata/docs/tests that this is a package-manager compatibility requirement for historical exact-version transitions, not an interpreter dependency of the Rust process.

Add a regression proving the native installed `eggpool` process does not import or spawn the Python application in ordinary `version`, `help`, `check-config`, serve/health, or inference startup paths.

## Update/install authority

Preserve K004/K005 behavior:

- uv-tool installs use uv exact requirements;
- pipx installs use pipx exact requirements;
- pip/venv installs use that environment's Python/pip;
- standalone Rust uses verified raw GitHub assets and rejects Python targets;
- ambiguous/source-checkout provenance fails closed.

Review `scripts/install.sh` and the Rust updater for accidental assumptions that `src/eggpool/` remains in the current repository. The installer's bounded Python metadata probe for adopting an **existing historical Python install** may remain because it is package-transition tooling, not a current runtime dependency. It must not import repository-local Python application code.

## PyPI immutability rules

Add validation/docs that historical PyPI releases are consumed as immutable external artifacts. Do not plan backfills into releases older than PyPI's current 14-day file-addition window, reuse historical filenames, delete/reupload files, or derive current support claims from a mutable source branch.

## Failure and rollback semantics

P002 must be safe to abort before P003. If a catalog/release-guard change is invalid, the current Rust wheel/public release remains untouched. No database, config, service, installed package, PyPI release or GitHub release is mutated by normal P002 verification.

Any updater/catalog correction must retain K005's pre-mutation compatibility checks and exact rollback behavior. Do not weaken the manager ownership detector or introduce shell-string command construction.

## Verification

At minimum:

```bash
rtk uv run python scripts/check_cutover_catalog.py
rtk uv run python scripts/validate_cutover_docs.py
rtk uv run python scripts/validate_release_workflow.py .github/workflows/release.yml
rtk uv run pytest tests/migration_rs -q --tb=short --maxfail=1
rtk cargo test --manifest-path rust/Cargo.toml --test operations_o008 -- --test-threads=1
rtk cargo test --manifest-path rust/Cargo.toml --test operations_o009 -- --test-threads=1
rtk cargo test --manifest-path rust/Cargo.toml --all-targets -- --test-threads=1
rtk cargo fmt --manifest-path rust/Cargo.toml --all -- --check
rtk git diff --check
```

Add focused tests proving:

- latest resolution cannot choose a Python era;
- compatible exact historical target remains resolvable;
- incompatible historical state fails before manager mutation;
- standalone Rust rejects historical Python target;
- wheel-managed executable is never raw-overwritten;
- root Python project cannot be selected by the current release workflow.

## Acceptance criteria

P002 closes only when package/release/update authority is coherent, current Rust publication is unique, explicit compatible historical switching still works in deterministic tests, and no current production path depends on the Python application source.

No `src/eggpool/` deletion is authorized by P002 itself; accepted P002 promotes P003.

## Non-goals

- no Python application deletion;
- no live-oracle test deletion;
- no public release/yank/delete;
- no target-matrix expansion;
- no provider/runtime feature change;
- no DB schema change;
- no removal of historical exact-version support.

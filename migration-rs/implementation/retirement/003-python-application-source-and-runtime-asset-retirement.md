# P003 — Python Application Source and Runtime-Asset Retirement

Status: accepted/closed 2026-09-11; see [closure record](../../closure/retirement/003-status.md)

Source roadmap: `migration-rs/subsystems/python-retirement-roadmap.md`

Primary class: invariant/polish

Hard dependencies: accepted P001 and P002

## Objective

Remove the historical Python **application/runtime** from the active repository while proving that every asset, schema migration, default, template, static file and contract still needed by Rust has an independent Rust-owned or retained-fixture authority.

This is the first intentionally destructive M12 slice. It must be reversible by Git history and must stop rather than delete evidence when the P001 ownership map is incomplete.

## Removal boundary

The default intended removal target is the application tree under `src/eggpool/`, including Python runtime modules and Python-only application packaging material. The P001 manifest decides the exact per-path disposition.

Do not copy the whole Python application to `archive/`, `legacy/`, or another active source directory. Record the final reference commit/tree/hash in the M12 reference manifest and keep only bounded fixtures/assets that have ongoing regression value.

## Runtime assets and migrations

Before deleting an originating Python path, prove the surviving owner for:

- SQLite numbered migrations and migration checksums through schema 54;
- default configuration/template material;
- dashboard templates/static assets and any generated equivalents;
- provider/model static metadata used by Rust;
- package-facing documentation/assets required in the Rust wheel;
- sample configuration and deployment templates;
- any MIME/media/document fixtures that the Rust codecs/tests still consume.

For each asset category, add a deterministic test or build-time assertion that fails if the Rust-owned copy changes independently from the accepted retained hash where exact identity remains contractual.

If a required runtime asset exists only under `src/eggpool/`, move/copy it into an appropriate Rust-owned or neutral retained-fixture location **before** deleting the Python source. Do not leave Rust production reading from a `migration-rs/` fixture directory.

## Import/runtime independence audit

After removal, a repository-wide scan must show no production Rust build, executable, service, release workflow, installer latest path, or current package metadata depends on:

- `src/eggpool`;
- `python -m eggpool`;
- `PYTHONPATH` pointing at the former application tree;
- the root Python console-script entry point;
- dynamic import of the historical application.

Historical test/fixture references that P004 still owns may remain only if they resolve through the frozen manifest or retained fixture paths, not through a live application import.

## Database and state safety

No schema migration is created, reordered, renumbered, or checksum-rewritten merely because the Python source is removed. Use existing Q003/M11 evidence plus fresh Rust migration tests to prove a current schema-54 database remains unchanged in semantics.

The deletion must not alter canonical config, data, runtime, backup, log, or service paths.

## Failure and rollback semantics

Perform source/asset changes in one reviewable implementation commit or a small bounded series where every intermediate commit builds/tests. If an asset ownership check fails, restore the Python path and create a corrective P-plan; do not weaken the check or change runtime behavior to make deletion easier.

Because the public Rust release is already independent, P003 does not mutate installed services or publish packages. Git revert/history is the source rollback for the repository change.

## Verification

At minimum:

```bash
rtk rg -n "src/eggpool|PYTHONPATH|python -m eggpool|import eggpool|from eggpool" rust packaging scripts .github docs tests migration-rs
rtk cargo build --manifest-path rust/Cargo.toml --locked
rtk cargo test --manifest-path rust/Cargo.toml --all-targets -- --test-threads=1
rtk uv run python scripts/check_cutover_catalog.py
rtk uv run python scripts/validate_release_workflow.py .github/workflows/release.yml
rtk git diff --check
```

Run focused DB/config/dashboard asset tests named by P001. Build a local Maturin wheel from `packaging/pypi` and inspect its contents; it must contain the native binary and required metadata/assets but no Python application package.

Add a negative repository test that fails if a current production/release path reintroduces `src/eggpool` or the historical root console script.

## Acceptance criteria

P003 closes only when:

- the Python application source is absent from the active production tree;
- every required runtime asset/migration has an explicit surviving owner;
- Rust build/tests and a local production wheel succeed without the removed source;
- current service/config/DB paths are unchanged;
- P001 reference provenance still resolves to immutable Git history;
- no unresolved high/medium runtime-asset, migration, packaging or data-loss finding remains.

Accepted P003 promotes P004.

## Non-goals

- no broad deletion of Python-based developer tools;
- no deletion of retained deterministic fixtures;
- no historical PyPI artifact change;
- no updater feature removal;
- no Rust behavior redesign;
- no M12 closure claim.

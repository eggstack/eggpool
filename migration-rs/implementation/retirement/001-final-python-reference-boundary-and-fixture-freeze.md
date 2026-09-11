# P001 — Final Python Reference Boundary and Fixture Freeze

Status: accepted/closed 2026-09-11; see [closure record](../../closure/retirement/001-status.md)

Source roadmap: `migration-rs/subsystems/python-retirement-roadmap.md`

Primary class: invariant/infrastructure

Hard dependencies: accepted M11 closure; accepted ADR-0001 through ADR-0005

## Objective

Create the final reproducible map of the Python runtime/oracle and migration qualification boundary before M12 removes any production/runtime packaging or dual-run machinery. Freeze source identity and selected useful fixtures, classify every relevant path as retain/archive/replace/remove, and name the Rust-native replacement for every contract that must survive retirement.

P001 is deliberately non-destructive. It must not delete or alter the Python runtime, Rust runtime, package manifests, update behavior, public release metadata, or historical transition support.

## Authoritative Python reference

The inventory must identify and hash the final reference surfaces, including at minimum:

- `src/eggpool/` runtime modules, bundled config/assets, numbered SQLite migrations, templates/static files and `py.typed`;
- root `pyproject.toml`, `uv.lock`, `.python-version` when present, console-script metadata and Python application dependencies;
- Python unit/integration/contract/smoke tests and `tests/migration_rs/`;
- scripts that import `eggpool`, launch a Python server, set `PYTHONPATH`, inspect Python assets, or generate Python-side oracle observations;
- release/qualification workflows and public docs that still refer to the historical Python application;
- K001/K005/K012/Q012 and other accepted closure artifacts whose claims depend on the Python reference;
- the K001 installable-release catalog, public historical PyPI identities, and the K004/K005 cross-era transition tests.

The manifest must distinguish:

1. current production/runtime ownership;
2. historical public release compatibility;
3. retained development-only tooling;
4. migration-only oracle machinery;
5. immutable external evidence recoverable from PyPI/Git history.

## Required output

Create `migration-rs/fixtures/retirement/m12-reference-manifest.json` with at least:

- final Python source commit and repository tree identity;
- path classification: `retain`, `replace`, `remove`, or `external-history`;
- SHA-256 for retained machine-readable fixtures;
- named Rust test/fixture replacement for every removed runtime contract;
- Python module/test/script dependencies that must be eliminated before each destructive plan;
- SQLite migration/checksum and bundled-asset provenance mapping;
- approved exact/semantic normalization set inherited from M10/M11;
- historical PyPI/version-catalog identities that remain supported exact targets;
- explicit statement that full Python source is recoverable from immutable Git history and is not copied wholesale into a new archive tree.

The manifest must contain no credentials, provider response bodies, local absolute paths, environment dumps, mutable branch references, or host identity.

## Contract coverage map

For each Python-owned behavior that will disappear, name the surviving authority. At minimum map:

- config defaults/validation/path semantics → Rust config tests and frozen fixtures;
- CLI parsing/help/error/exit behavior → Rust CLI tests;
- SQLite schema/migrations/checksums → Rust migration/repository tests plus retained migration hashes;
- HTTP/API/auth/body-limit behavior → Rust server/inference tests;
- SSR/static/dashboard content → Rust dashboard/Q012 fixtures;
- provider/routing/wire/coordinator behavior → closed M4-M7 Rust suites and retained deterministic fixtures;
- runtime reload/shutdown/tasks → M8 Rust suites;
- operational CLI/backup/update/deploy → M9 Rust suites;
- package/cross-era transitions → M11 K004/K005/K012 tests/catalog;
- broad qualification observations → M10/M11 closure evidence.

“Covered by Rust” without a named test, fixture or closure authority is not sufficient.

## Historical version evidence

Do not download or duplicate every historical wheel into the repository. Record public PyPI filename/hash/version identities already frozen by K001 and treat those immutable files as external-history evidence. P001 must flag any catalog entry whose artifact identity is no longer resolvable or whose transition guarantee depends on source that M12 would delete.

Historical Python exact-version support remains part of the current package-management contract under ADR-0005. P001 therefore must preserve the fixtures/tests needed to distinguish artifact availability from DB/config compatibility.

## Failure and restart boundary

The capture process is deterministic and read-only with respect to production state. Interrupted generation may leave temporary output, but no partial manifest may replace the last accepted one. Use write-to-temp plus atomic rename in any generator added for the manifest.

No live provider traffic, service restart, database mutation, PyPI publication, package transition, or concurrent Python/Rust writer is required for P001.

## Verification

At minimum record:

```bash
rtk git status --short --branch
rtk git rev-parse HEAD
rtk git ls-files src/eggpool tests scripts packaging .github/workflows
rtk rg -n "from eggpool|import eggpool|PYTHONPATH|src/eggpool|python.*oracle|differential" src tests scripts .github docs migration-rs
rtk uv run python scripts/check_cutover_catalog.py
rtk uv run python scripts/validate_cutover_docs.py
rtk uv run pytest tests/migration_rs -q --tb=short --maxfail=1
rtk cargo test --manifest-path rust/Cargo.toml --all-targets -- --test-threads=1
rtk git diff --check
```

If the broad Python/Rust suite is not executable on the implementation host, record the environment blocker and run the deterministic subset needed to validate the manifest; P001 itself may not convert missing evidence into a pass.

## Acceptance criteria

P001 closes only when:

- the reference manifest is complete, deterministic and secret-free;
- every destructive P002-P005 target has a recorded disposition;
- every required runtime contract has a surviving Rust test/fixture/closure authority;
- migrations/assets needed after source deletion are identified explicitly;
- historical exact-version evidence and compatibility rules are retained;
- no production/runtime/package behavior changed;
- no unresolved high/medium evidence-loss finding remains.

## Non-goals

- no source/package deletion;
- no root `pyproject.toml` redesign;
- no release/update/catalog behavior change;
- no test-suite pruning;
- no PyPI/GitHub publication;
- no Rust feature or parity correction unless P001 discovers a contradiction, in which case stop and create a corrective plan;
- no M12 closure claim.

## Handoff result

Accepted P001 promotes P002 as the sole dependency-ready plan. P001 itself does not make M12 complete and does not authorize Python application removal.

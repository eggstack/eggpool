# K001 Closure — Cutover, Package, and Installable-Version Catalog Contract Freeze

Status: accepted; closed 2026-09-10

Plan: [K001 — Cutover, package, and installable-version catalog contract freeze](../../implementation/cutover/001-cutover-package-and-version-catalog-contract-freeze.md)

## Decision

K001 is accepted and closed. The cutover contract is frozen before any Rust
wheel, raw release asset, installer, updater, or production publication is
changed. No production release or package upload occurred.

Implementation commit:

- `47905d54307afb6c73ccc7403a7dfa8702c36c22` — K001 catalog, validator, and
  deterministic contract tests.

The closure/registry transition is recorded in the follow-up planning commit
that contains this record.

## Catalog and version authority

The machine-readable authority is
[`k001-installable-releases.json`](../../fixtures/cutover/k001-installable-releases.json),
validated by [`check_cutover_catalog.py`](../../../scripts/check_cutover_catalog.py).
Its schema is `k001.v1`; it is bounded, static, secret-free, and records the
capture time and authoritative repository/public metadata sources.

The selected `CUTOVER_VERSION` is **0.8.0**. It is valid in EggPool's accepted
release subset, strictly newer than the current stable 0.7.4, absent from the
live PyPI release inventory, absent from the live GitHub tag inventory, and
reserved for the first Rust-backed wheel set. The source-of-truth rule is:

- during the side-by-side window, the root `pyproject.toml` remains the
  historical Python oracle at 0.7.4 and `rust/Cargo.toml` remains the current
  Rust candidate at 0.7.4;
- after K002 materializes the release boundary, Cargo owns the Rust release
  version and the Rust wheel/tag/release manifest must all equal 0.8.0;
- the checker explicitly distinguishes these two authorities and rejects a
  disagreement; it does not require the historical Python payload to become
  the Rust wheel payload.

The live checks also confirmed `v0.8.0` has no GitHub release and 0.8.0 has no
PyPI release. Rust version output is sourced from `CARGO_PKG_VERSION` in
`rust/src/version.rs`; the current side-by-side binary therefore remains
0.7.4 until the later packaging plan changes it.

## Official release inventory

At capture time, the official GitHub tag and release APIs both contain 56
stable entries, `v0.1.0` through `v0.7.4`, each with an immutable tag commit in
the catalog. GitHub release asset presence is not treated as package
availability; the current releases contain no Rust cutover assets.

The public PyPI JSON inventory contains the same 56 normalized versions. Each
release has a public `eggpool-<version>-py3-none-any.whl` and
`eggpool-<version>.tar.gz`, with the captured SHA-256 digests recorded in the
catalog. No file is yanked, deleted, or unavailable in the captured inventory.

The initial planning note's 0.5.6 PyPI ceiling was stale at implementation
time. All official stable GitHub versions newer than 0.5.6 are now present on
PyPI, so the missing-version list is empty:

| Gap range | Disposition |
|---|---|
| 0.5.7 through 0.7.4 | No gap at capture; all are already public PyPI releases. No backfill or immutable fallback is scheduled. |

Any future gap requires a reviewed catalog correction. No historical source
is rebuilt with a changed version field, and no mutable branch is an approved
fallback.

## Install authority and provenance matrix

| Install class | Current authority | Exact update/downgrade authority | Safe signals frozen for K004 |
|---|---|---|---|
| uv tool | uv managed tool environment | uv exact reinstall | uv tool shim, optional `INSTALLER=uv`, distribution below uv tool environment |
| pipx | pipx managed virtual environment | pipx exact install/upgrade | pipx exposed shim, optional `INSTALLER=pipx`, distribution below pipx venv |
| ordinary pip in venv | owning environment's Python | environment `python -m pip` | executable and distribution share an environment, optional `INSTALLER=pip`, no index-install `direct_url.json` |
| standalone Rust binary | verified GitHub raw asset | O008 verified self-replacement | no owning distribution metadata, Rust self-check, writable standalone path |
| source checkout | explicit developer workflow | source workflow only | executable inside explicit checkout, source `direct_url.json` when installed |
| ambiguous/unmanaged executable | none trusted | fail closed with recovery instructions | no single owner for executable and distribution metadata |

PATH spelling alone is not sufficient. K004 owns the actual provenance detector
and transition engine; K001 freezes the signals and ownership boundary only.

## Exact transition and rollback contract

`eggpool update` resolves the latest stable release compatible with the current
supported target. `eggpool update X.Y.Z` and `eggpool update vX.Y.Z` normalize to
the same exact target. An older exact target is an authorized downgrade only
when the target is in the catalog and the DB/config rule permits it. The
catalog freezes these secret-free result categories:

`success`, `exact-current/no-op`, `target-not-found`,
`target-not-in-installable-catalog`, `target-yanked/unavailable`,
`unsupported-platform`, `incompatible-Python-environment`,
`DB/config rollback-incompatible`, `package-manager-unavailable`,
`install-provenance-ambiguous`, `package-manager-failure`,
`target-self-check-failure`, `service-restart/health failure`, and
`rollback-failure`.

The DB/config rollback window is schema 54 and is explicitly limited to Python
versions **0.6.7 through 0.7.4**. Repository history shows those versions carry
the schema-54 migration set, and Q003 proves the Python/Rust schema-54 state
boundary, backup, recovery, and durable reopening contract. Older Python
versions remain installable PyPI exact targets for compatible pre-schema-54
state, but they must fail before package mutation if asked to reopen state
written by the Rust candidate. Automatic database reset is never a rollback
strategy. K005 must qualify the real manager transitions and config behavior.

## Supported target matrix

This carries forward M10/Q005/Q008 exactly and freezes the K003 artifact
identities:

| Target class | Rust triple | Wheel platform tag | Classification |
|---|---|---|---|
| Linux x86_64 | `x86_64-unknown-linux-gnu` | `manylinux_2_17_x86_64` | supported |
| Linux aarch64 | `aarch64-unknown-linux-gnu` | `manylinux_2_17_aarch64` | supported |
| macOS arm64 | `aarch64-apple-darwin` | `macosx_11_0_arm64` | supported-development/non-root |
| Windows | none | none | unsupported |
| other Unix | none | none | not qualified |

The Linux floor is glibc 2.17/manylinux2014-compatible. macOS 11.0 is the
minimum development/runtime deployment target; no rootful macOS service claim
is made. Unsupported targets never appear in a release's supported target
list and receive no wheel tag.

## Verification

Focused K001 checks:

```text
uv run pytest tests/migration_rs/test_k001_catalog.py -q --tb=short --maxfail=1
# 10 passed
uv run python scripts/check_cutover_catalog.py
# K001 catalog valid: 56 releases; cutover 0.8.0 reserved; 8 rollback-compatible
```

Required repository gates:

```text
cargo test --manifest-path rust/Cargo.toml --all-targets -- --test-threads=1
# 445 passed (52 suites, 202.69s)
uv run pytest tests/migration_rs -q --tb=short --maxfail=1
# 180 passed, 3 skipped
uv run pytest tests/smoke/ -q --tb=short --maxfail=1
# 14 passed
uv run pyright src/ scripts/
# 0 errors, 0 warnings, 0 informations
uv run ruff format --check src/ tests/ scripts/
# 755 files already formatted
uv run ruff check src/ tests/ scripts/
# All checks passed
git diff --check
# pass
```

The live inventory comparisons used the official GitHub tags API and PyPI
JSON API, and matched all 56 source commit/file-hash records in the frozen
catalog. The 0.8.0 absence checks returned no GitHub tag/release and no PyPI
release.

## Findings, blockers, and registry transition

No unresolved high- or medium-severity K001 cutover-contract finding remains.
No package publication, installer behavior change, updater behavior change,
database migration, or Rust dependency change was made. Remaining work is
deliberately owned by the serial K002-K012 plans; K001 does not claim a Rust
artifact or public cutover.

The registry transition is:

- K001 moves from ready to accepted/closed with implementation commit
  `47905d54307afb6c73ccc7403a7dfa8702c36c22`;
- K002 is promoted to **ready for handoff** as the sole dependency-ready plan;
- K003-K012 remain queued behind their direct predecessors;
- M12 remains sequenced behind accepted K012 and is not unblocked by K001.


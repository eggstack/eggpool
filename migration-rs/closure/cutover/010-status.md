# K010 Closure — Public Metadata, Documentation, and Release-Candidate Freeze

Status: accepted; closed 2026-09-11

Plan: [K010 — Public metadata, documentation, and release-candidate freeze](../../implementation/cutover/010-public-metadata-docs-and-release-candidate-freeze.md)

## Decision

K010 is accepted and closed. The Rust wheel is now the documented canonical
public runtime and default package channel on the K001 supported targets. The
repository has one targeted validator for the public metadata/docs/release
boundary, and the production workflow is explicitly guarded until K011.

Per the cutover promotion rule, K011 is promoted to dependency-ready. K012
remains queued behind K011 and remains the only plan allowed to close M11.
M12 remains blocked behind accepted K012 and has not been authorized.

## Implementation commits

- `1d17fd1c4c597adf1161b813f5b958117370a500` — K010 implementation: Rust
  latest-catalog and standalone-update authority, public metadata, README and
  upgrade/deployment/release documentation, Python-reference labeling,
  release-workflow guard, and deterministic K010 validation.
- The closure transition commit records this status, registry promotion, and
  the K010-to-K011 handoff in the planning documents.

## Candidate identity and manifest evidence

The frozen candidate identity is version `0.8.0`, intended tag `v0.8.0`, with
Cargo and the Maturin publication manifest agreeing on the Rust version. The
root `pyproject.toml` remains `0.7.4` and is explicitly labeled as the Python
reference/oracle package through M11; it is not a Rust-era release authority.
The K001 installable catalog contains the same `0.8.0` Rust candidate and the
documented rollback window remains the compatible Python-era releases
`0.6.7` through `0.7.4`.

K009's immutable staged manifest remains the recorded pre-publication artifact
evidence:

- source commit: `e5fb9a52b8dcce1e02eddf406e7b0d29779738bf`;
- release manifest SHA-256:
  `3dcd16bc11674872a57cc42af04123684a4cd9602f0a40abac5cc9fd63c3cc4c`;
- Linux x86_64 wheel/raw: `aed054784a1619987457f332d18b354ab4c693f45a0b6dd6b70ad9d4edabd2d6` /
  `8664513c1634824d089a9b852f9e24792ae7743536a4b03f0aeec684fe5c3487`;
- Linux aarch64 wheel/raw: `b9b1827f442f1abf3c84116aa1a955770755ee5a4ac324ab11c5dee7065c260e` /
  `91eec9ab29556a30971ad94da7b42049c6c9f4b5fa1b041d1628527ec22dd9c8`;
- macOS arm64 wheel/raw: `ca51e31dcd56096e6cb08eca869c69c3bf3053f0f9ae38dfe17e3bc16151dd28` /
  `65deceda482f4dd9d8dc7e53fdab9846d0afe366a6966c6d6c2ff3d03df858db`.

The K010 implementation changes executable update/catalog authority after the
K009 build, so these hashes are not relabeled as K010-built artifacts. K011
must rebuild and reaggregate the complete supported artifact set from the
frozen K010 implementation commit before any public publication.

## Public metadata and documentation

- The Maturin `bin` package retains the `eggpool` identity, existing authors,
  URLs, license, and `Requires-Python >=3.11`; the Python-only classifier is
  removed and no Python application dependencies are declared.
- `README.md` leads with the installer, uv, and pipx package paths, documents
  the three supported targets and unsupported Windows/other targets, and
  points existing users to exact cross-era update/rollback guidance.
- `docs/upgrading.md` covers existing Python installs, manager-aware latest
  and exact transitions, database/config preservation, recovery, standalone
  raw GitHub authority, adoption, and unsupported-target behavior.
- `docs/deployment.md`, `docs/raspberry-pi.md`,
  `docs/rust-candidate-deployment.md`, and `docs/releasing.md` describe the
  native Rust process/package boundary without making Python the normal public
  runtime or sourcing arbitrary shell configuration as root.
- `CHANGELOG.md` describes the Rust runtime cutover, compatibility and
  rollback window, target policy, and Python-reference retention without
  claiming Python removal or unsupported benchmarks.
- `migration-rs/python-reference.md` records the root Python package and
  differential-oracle boundary through M11 and the separate M12 retirement
  authority.

## Target and release-guard consistency

The K001 target set is repeated consistently as Linux x86_64, Linux aarch64,
and macOS arm64. Windows, universal wheels, sdists, and hidden source fallbacks
are rejected by the documentation validator and are absent from the release
workflow. The workflow's `validate-release` job now runs
`validate_cutover_docs.py`; the release guard requires the Maturin manifest and
rejects root Hatchling-style `uv build`/`uv publish` publication paths.

Production PyPI/GitHub publication remains guarded until K011. No public
release or production upload was performed by K010.

## Staged latest/exact behavior

The Rust catalog now resolves package-managed `latest` to the Rust cutover
candidate rather than stopping at the Python-era release. Standalone Rust
`latest` remains on the verified GitHub raw-release authority; standalone
exact requests reject Python-era targets. Manager-owned uv/pipx/pip installs
continue to resolve through their package authority and preserve exact
cross-era compatibility checks. The K009 staged wheelhouse/TestPyPI rehearsal
and its accepted artifact matrix remain the staging evidence for the package
and installer paths.

## Verification

The following checks passed after the implementation changes:

- `cargo fmt --manifest-path rust/Cargo.toml --all -- --check`;
- `cargo test --manifest-path rust/Cargo.toml --all-targets -- --test-threads=1`
  — 463 tests across 52 suites;
- `uv run pytest tests/migration_rs -q --tb=short --maxfail=1` — 212 passed,
  3 skipped;
- `uv run python scripts/validate_cutover_docs.py`;
- `uv run python scripts/validate_release_workflow.py
  .github/workflows/release.yml`;
- `uv run python scripts/check_cutover_catalog.py`;
- `uv run python scripts/validate_cutover_release.py`;
- `uv run ruff check` on the changed validator/tests;
- `uv sync --frozen --extra ci`;
- `uv run ruff format --check src/ tests/ scripts/` and
  `uv run ruff check src/ tests/ scripts/`;
- `uv run pyright src/ scripts/` — 0 errors, 0 warnings, 0 informations;
- `uv run pytest tests/smoke/ -q --tb=short --maxfail=1` — 14 passed;
- `bash -n scripts/install.sh`;
- `git diff --check`.

The focused K002/K010 packaging/documentation tests passed, including the
updated Rust-wheel classifier contract. A strict full-workspace Clippy run is
not clean because it reports 66 pre-existing warnings across unrelated Rust
files; the changed Rust paths introduce no new Clippy-only cleanup scope for
K010, and Rust formatting/tests remain green.

## Residual risk and registry transition

There is no unresolved K010 high/medium documentation, metadata,
default-channel, unsupported-target, or manager-ownership finding. The
intentional remaining release prerequisites are owned by K011: rebuild the
artifact/hash matrix from the frozen K010 commit, perform the real PyPI/GitHub
publication and post-publication verification, and run the immediate public
rollback/re-upgrade drill. K010 did not claim those operations were complete.

K010 is accepted/closed. The registry now lists K011 as the sole
dependency-ready M11 plan; K012 remains queued/blocked behind K011, and M12
remains blocked behind accepted K012.

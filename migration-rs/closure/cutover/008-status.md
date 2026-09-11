# K008 Closure — Trusted Publishing, Attestations, and Release Supply Chain

Status: accepted; closed 2026-09-11

Plan: [K008 — Trusted publishing, attestations, and release supply chain](../../implementation/cutover/008-trusted-publishing-attestations-and-release-supply-chain.md)

## Decision

K008 is accepted and closed. The repository now has a dedicated release
workflow that builds the already-qualified K003 wheel/raw target set from an
immutable tag, validates the candidate and artifact manifest, uploads one
validated artifact bundle, and gives publication only the permissions required
by each destination. PyPI publication is Trusted Publishing/OIDC-only with
attestations enabled; no long-lived package token or provider credential path
exists.

This closure establishes release machinery. It does not claim that a live
PyPI/TestPyPI Trusted Publisher has been configured or that a public release
has been uploaded. K009 owns staged rehearsal and K011 owns the first
production publication.

## Implementation

- `6029e33` — dedicated K008 release workflow, immutable candidate/publication
  validators, deterministic supply-chain tests, closure evidence, and K009
  dependency promotion.
- `release.yml` — dedicated tag/manual workflow with separate validation,
  target build, aggregation, TestPyPI, PyPI, GitHub release, and post-publish
  verification jobs.
- `scripts/validate_release_workflow.py` — dependency-free static validator for
  trigger, permission, pinning, target, secret-isolation, dependency, and
  destination-separation contracts.
- `scripts/validate_cutover_release.py` — immutable tag/Cargo/Maturin/package
  identity gate using K001/K002 authorities.
- `scripts/verify_published_release.py` — bounded PyPI wheel-hash and GitHub
  raw-asset digest/stable-release verifier.
- `tests/migration_rs/test_k008_release_supply_chain.py` — deterministic
  workflow mutation, candidate identity, and public metadata contract tests.

## Workflow and permission diagram

```text
tag vX.Y.Z or manual rehearsal
              |
              v
      validate-release  [contents: read]
              |
     +--------+---------+----------------+
     v                  v                v
linux-x86_64      linux-aarch64     macos-arm64
[read only]       [read only]       [read only]
     +--------+---------+----------------+
              v
 aggregate-release-manifest [contents: read]
              |
       exact uploaded bundle
          /             \
         v               v
 TestPyPI [id-token:write]  PyPI [id-token:write, pypi env]
                              \
                               +--> GitHub release [contents:write]
                                      and stable public verification
```

Build and aggregation jobs cannot receive OIDC or content-write permission.
The GitHub release job cannot receive OIDC. Production PyPI and GitHub release
jobs are tag-push-only in the canonical `eggstack/eggpool` repository. Manual
workflow dispatch offers only `validate` and `testpypi`; it has no production
publication route.

## Candidate and target contract

The candidate gate requires the K001 cutover version (`0.8.0`), Cargo version,
Maturin publication metadata, `Requires-Python >=3.11`, `bindings = "bin"`,
`--locked`, and PyPI compatibility to agree. A production tag must be exactly
`v<catalogue-version>` and point at the checked-out commit.

| Product target | Rust target | Wheel policy | Raw asset |
|---|---|---|---|
| Linux x86_64 | `x86_64-unknown-linux-gnu` | manylinux2014 / glibc 2.17 | `eggpool-{version}-linux-x86_64` |
| Linux aarch64 | `aarch64-unknown-linux-gnu` | manylinux2014 / glibc 2.17 | `eggpool-{version}-linux-aarch64` |
| macOS arm64 | `aarch64-apple-darwin` | `macosx_11_0_arm64` | `eggpool-{version}-macos-aarch64` |

The workflow has no Windows/other-Unix target, universal wheel, or sdist step.
Each target builder invokes the existing K003 builder and wheel smoke
qualification. Aggregation creates a fresh `m11-release-manifest.v1` for the
current source commit, validates all wheel/raw hashes and target records, and
publishes only the downloaded wheels/raw assets/checksum sidecar/manifest.
No publish job runs Maturin or rebuilds source.

## Pinned release tooling

The release workflow uses immutable action revisions and explicit tool versions:

| Tool/action | Reviewed version or target | Immutable revision |
|---|---|---|
| `actions/checkout` | v4.3.1 | `34e114876b0b11c390a56381ad16ebd13914f8d5` |
| `actions/setup-python` | v5.6.0 | `a26af69be951a213d495a4c3e4e4022e16d87065` |
| `astral-sh/setup-uv` | v7.1.0 | `3259c6206f993105e3a61b142c2d97bf4b9ef83d` |
| `dtolnay/rust-toolchain` | Rust 1.85.1 | `d1031067263f94b142dd6c0ce24c5eb9d02d52a0` |
| `mlugg/setup-zig` | Zig 0.13.0 | `53fc45b17fe98b52f92ee5ea08ff48a85a3e7eb7` |
| `actions/upload-artifact` | v4.6.2 | `ea165f8d65b6e75b540449e92b4886f43607fa02` |
| `actions/download-artifact` | v4.3.0 | `d3f86a106a0bac45b974a628896c90dbdf5c8093` |
| `pypa/gh-action-pypi-publish` | v1.14.2 | `a892a5a61159132606e93a2fa6f4358831b04d26` |
| `softprops/action-gh-release` | v3.0.3 | `e598afbe1493e6b1bafb1f389cabb956eab91231` |
| Maturin | 1.14.1 | `maturin==1.14.1` via uv |

The action references were resolved from the upstream release/tag commits when
the workflow was implemented. A future dependency update must change the
immutable revision and the closure evidence together.

## Trusted Publisher and operational setup

Required one-time maintainer configuration, not verifiable from this checkout:

1. In the existing PyPI `eggpool` project, add a GitHub Trusted Publisher for
   owner `eggstack`, repository `eggpool`, workflow filename
   `.github/workflows/release.yml`, and environment `pypi`.
2. If K009 uses real TestPyPI publication, configure a distinct TestPyPI
   Trusted Publisher for the same workflow and `testpypi` environment.
3. Protect the `pypi` environment with the desired review/approval rule.
4. Do not add a PyPI API token, provider key, runtime key, proxy credential, or
   other repository/environment secret to make the workflow pass.

No live Publisher/OIDC or TestPyPI run was claimed by this closure. The
workflow will fail closed on missing OIDC authority; it has no token fallback.

## Attestation, provenance, and SBOM disposition

Both PyPI publication jobs set `attestations: true` on the official PyPA
publisher. The post-publication verifier checks the exact PyPI wheel SHA-256,
the GitHub release tag/draft/prerelease state, and the GitHub raw asset
`digest` values required by O008. `SHA256SUMS` and the bounded release
manifest are attached to the GitHub release as additional audit evidence.

No bespoke signing system or SBOM was introduced. SBOM generation remains an
optional hardening item because K001 did not make it mandatory and adding one
would not improve this release boundary without a deterministic dependency-graph
producer. K009/K011 may record standard index provenance and any future SBOM
identifier in their release evidence.

## Failure and partial-publication policy

- Candidate/tag/version mismatch fails before any build.
- Any missing target, wheel smoke failure, manifest mismatch, unsupported
  artifact, sdist, or secret reference prevents publication.
- Publish jobs consume only the exact downloaded bundle and cannot rebuild.
- OIDC/attestation failure fails the PyPI job; no credential fallback exists.
- PyPI filenames are immutable. A partial upload is recorded and reconciled by
  K009/K011 using PyPI’s recovery/yank/add-file rules; the workflow never
  overwrites an existing file.
- GitHub asset upload uses `overwrite_files: false`; a partial release is
  reconciled against the manifest before it can be considered complete.
- Post-publication verification rejects wrong hashes, missing O008 digests,
  source archives, draft/prerelease releases, and unexpected assets.

## Verification evidence

Passed locally:

```text
uv run ruff format scripts/validate_release_workflow.py scripts/validate_cutover_release.py scripts/verify_published_release.py tests/migration_rs/test_k008_release_supply_chain.py
uv run ruff check scripts/validate_release_workflow.py scripts/validate_cutover_release.py scripts/verify_published_release.py tests/migration_rs/test_k008_release_supply_chain.py
uv run pyright scripts/validate_release_workflow.py scripts/validate_cutover_release.py scripts/verify_published_release.py
uv run pytest tests/migration_rs/test_k008_release_supply_chain.py -q --tb=short --maxfail=1  # 9 passed
uv run python scripts/validate_release_workflow.py .github/workflows/release.yml  # pass; 33 pinned actions, 9 jobs
uv run python scripts/validate_cutover_release.py  # pass; candidate 0.8.0
rtk ruby -e 'require "yaml"; YAML.load_file(".github/workflows/release.yml")'  # YAML parse pass
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --lib -- --test-threads=1  # 43 passed
git diff --check
```

The publication tests use sanitized in-memory PyPI/GitHub metadata fixtures;
they verify exact hashes, raw digests, stable-release state, source-archive
rejection, and missing-digest rejection without contacting public services.
`actionlint` was not installed on this host, so YAML/action validation used the
repository's deterministic static validator. No live target build, OIDC
exchange, TestPyPI upload, PyPI upload, or GitHub release was claimed.

## Findings and registry transition

No unresolved high/medium workflow, artifact-integrity, permission-boundary,
secret-isolation, or release-authority finding remains in the implementation.
The maintainer-side Trusted Publisher/environment setup is an explicit
operational prerequisite for K009/K011, not a hidden implementation claim.

K008 is accepted/closed. K009 is now the sole future M11 plan unblocked by this
closure and is promoted to dependency-ready. K010 remains queued behind K009;
K011 remains queued behind K010 and explicit production publish authority; K012
remains queued behind K011. M12 remains blocked behind accepted K012.

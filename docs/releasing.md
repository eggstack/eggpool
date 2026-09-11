# Rust release procedure

M11 Rust releases are built and published by the pinned
.github/workflows/release.yml workflow. The workflow is the production
authority; it consumes the Maturin binary-wheel manifest and never selects the
root Hatchling project.

## Candidate checks

Before creating a release tag:

~~~bash
uv run python scripts/check_cutover_catalog.py
uv run python scripts/validate_cutover_release.py
uv run python scripts/validate_release_workflow.py .github/workflows/release.yml
uv run python scripts/validate_cutover_docs.py
git diff --check
~~~

The checks must agree on the K001 candidate (0.8.0 for this cutover), Cargo
version, package metadata, supported targets, changelog heading, and source
commit. The root pyproject.toml is deliberately the historical Python
reference at 0.7.4; it must not be built or uploaded for the Rust candidate.

The candidate release set is exactly:

- Linux x86_64 wheel/raw executable;
- Linux aarch64 wheel/raw executable;
- macOS arm64 wheel/raw executable.

There is no Rust sdist, universal wheel, Windows asset, or source-build
fallback.

## Staged rehearsal

Use K009's local wheelhouse/staged-index workflow before any public upload.
Staged commands must use explicit EGGPOOL_INSTALL_FIND_LINKS or
EGGPOOL_INSTALL_INDEX_URL together with
EGGPOOL_INSTALL_ALLOW_NONPRODUCTION_INDEX=1. Do not persist those variables
in operator configuration.

~~~bash
uv run python scripts/qualification_cutover_rehearsal.py \
  --manifest migration-rs/closure/cutover/009-run.json
~~~

The rehearsal is not a production publication and cannot make a missing
target artifact acceptable.

## Production workflow

K011 is the first operation authorized to publish. It must use a clean,
immutable vX.Y.Z tag, maintainer approval, the protected PyPI Trusted
Publisher environment, and the exact downloaded artifact bundle. The
production jobs build and qualify each target, aggregate the hashes, then
publish PyPI wheels and matching GitHub raw assets. They do not rebuild in a
publish job and do not consume a long-lived PyPI token.

After publication, verify the public metadata and release asset digests:

~~~bash
uv run python scripts/verify_published_release.py \
  migration-rs/closure/cutover/k003-release-manifest.json
~~~

Do not call a candidate released until all required targets are public and
the post-publication verifier passes. A partial immutable upload is stopped,
recorded, and recovered by a new reviewed release; filenames are never reused.

If a tag run builds and attaches the exact GitHub assets but the PyPI publisher
fails before uploading any wheel, a maintainer may use the manual
`pypi-resume` destination with that failed run ID. The recovery job downloads
that run's immutable bundle, verifies the matching tag, source commit, release
manifest, and sidecar hashes, and publishes only those wheels through the
protected PyPI environment. This is a recovery path for the same release, not
a normal release trigger or a substitute for the post-publication verifier.

## Python reference packaging

The root Hatchling project and src/eggpool remain available through M11 for
differential tests and exact rollback targets. The canonical production build
is packaging/pypi/pyproject.toml, whose Maturin bin backend packages the Rust
executable with no Python application dependencies. Python source and
packaging retirement belong to M12.

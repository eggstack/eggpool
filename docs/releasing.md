# Current Rust release procedure

Current EggPool releases are built and published by the pinned
`.github/workflows/release.yml` workflow. The workflow is the production
authority; it consumes `packaging/pypi/pyproject.toml` and packages only the
Rust executable plus distribution metadata/assets.

## Release checks

Before creating a release tag:

~~~bash
uv run python scripts/check_release_catalog.py
uv run python scripts/validate_release_identity.py
uv run python scripts/validate_release_workflow.py .github/workflows/release.yml
uv run python scripts/validate_release_docs.py
uv run python scripts/validate_runtime_package_boundary.py
git diff --check
~~~

The checks must agree on the release catalog's native version (0.8.0 for the current public
release), Cargo version, package metadata, supported targets, changelog
heading, and source commit. The root `pyproject.toml` is tooling-only and
cannot be built or uploaded as an EggPool release. `Requires-Python >=3.11` on
the Rust wheel exists for package-manager compatibility when an operator
explicitly selects a historical Python target, not because the Rust process
imports or spawns Python.

The release set is exactly:

- Linux x86_64 wheel/raw executable;
- Linux aarch64 wheel/raw executable;
- macOS arm64 wheel/raw executable.

There is no Rust sdist, universal wheel, Windows asset, or source-build
fallback.

## Staged rehearsal

Use release rehearsal's local wheelhouse/staged-index workflow before any public upload.
Staged commands must use explicit EGGPOOL_INSTALL_FIND_LINKS or
EGGPOOL_INSTALL_INDEX_URL together with
EGGPOOL_INSTALL_ALLOW_NONPRODUCTION_INDEX=1. Do not persist those variables
in operator configuration.

~~~bash
uv run python scripts/qualification_release_rehearsal.py \
  --manifest packaging/release/upgrade-rehearsal.json
~~~

The rehearsal is not a production publication and cannot make a missing
target artifact acceptable.

## Production workflow

The workflow uses a clean, immutable vX.Y.Z tag, maintainer approval, the
protected PyPI Trusted Publisher environment, and the exact downloaded
artifact bundle. The production jobs build and qualify each target, aggregate
the hashes, then publish PyPI wheels and matching GitHub raw assets. They do
not rebuild in a publish job and do not consume a long-lived PyPI token.

After publication, verify the public metadata and release asset digests:

~~~bash
uv run python scripts/verify_published_release.py \
  packaging/release/release-manifest.json
~~~

Do not call a release complete until all required targets are public and
the post-publication verifier passes. A partial immutable upload is stopped,
recorded, and recovered by a new reviewed release; filenames are never reused.

If a tag run builds and attaches the exact GitHub assets but the PyPI publisher
fails before uploading any wheel, a maintainer may use the manual
`pypi-resume` destination with that failed run ID. The recovery job downloads
that run's immutable bundle, verifies the matching tag, source commit, release
manifest, and sidecar hashes, and publishes only those wheels through the
protected PyPI environment. This is a recovery path for the same release, not
a normal release trigger or a substitute for the post-publication verifier.

## Historical exact-version compatibility

Historical Python releases remain immutable external PyPI artifacts. They may
be selected only by an explicit exact version when the release catalog, Python
environment, and database/config compatibility checks allow it. They are not
rebuilt, uploaded, or used as a current source package. The root
`pyproject.toml` is tooling-only and contains no EggPool application package;
the current publication authority is `packaging/pypi/pyproject.toml`.

Use a non-publishing rehearsal before any upload:

```bash
uv run python scripts/qualification_release_rehearsal.py \
  --manifest packaging/release/upgrade-rehearsal.json
```

Manual `workflow_dispatch` with `destination: validate` builds and validates
the complete supported artifact set without publishing. `testpypi` is an
explicit staged destination, while `pypi-resume` is reserved for recovery of
one failed immutable tag run. Never modify, yank, delete, or re-upload files
belonging to an existing historical release.

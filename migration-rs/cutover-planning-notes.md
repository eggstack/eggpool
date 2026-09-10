# M11 Cutover Planning Notes

Status: research/planning evidence

Repository baseline reviewed: `aec794a060f6a9e71853a515fe50992446c41ebd` (accepted Q012 / M10 closure).

Research date: 2026-09-10

These notes support `subsystems/cutover-roadmap.md` and ADR-0004. They are not themselves implementation authority; `registry.md` and the registered K-plans control handoff.

## Repository findings

### Existing public package and version split

- Root `pyproject.toml` defines PyPI project `eggpool`, version `0.7.4`, `requires-python >=3.11`, Hatchling, a pure-Python package under `src/eggpool`, and `eggpool = "eggpool.cli:main"`.
- Rust `rust/Cargo.toml` is also version `0.7.4`, `publish = false`, and builds the completed Rust CLI/server.
- GitHub release history is at least through `v0.7.4`; those releases currently have no Rust binary assets.
- The public PyPI project page currently shows `0.5.6` as the newest published PyPI release, with a universal `py3-none-any` wheel and sdist. Therefore GitHub-tagged versions after 0.5.6 are not automatically package-manager-installable from PyPI today.
- Existing quick install uses pipx when available, otherwise uv tool; normal remote installation still resolves the `eggpool` PyPI project.

### Existing updater

M9/O008 implemented a good standalone Rust updater:

- exact/latest GitHub release resolution;
- target-specific raw executable naming;
- size and SHA-256 verification;
- trusted redirect allowlist;
- private sibling staging;
- staged and installed `eggpool version` self-check;
- rollback rename and restart recovery;
- exact-version downgrades.

That implementation is not suitable as the canonical updater for a wheel-managed install because direct executable replacement bypasses package-manager ownership/metadata. M11 must route package-managed installs through their owning environment and retain O008 only for standalone binaries.

### Current supported targets from M10

Frozen M10 target authority:

- Linux x86_64 — supported, including rootful deployment qualification;
- Linux aarch64 — supported, including physical Raspberry Pi 5 evidence;
- macOS arm64 — supported-development/non-root runtime; no rootful service-deployment claim;
- other Unix — not qualified;
- Windows — unsupported.

M11 wheel publication must match this matrix rather than imply portability from what Cargo can compile.

### Compatibility boundary already available

M10/Q003 and prior migration closures prove the current SQLite/config/path compatibility window. This is what makes Python <-> Rust package version switching viable: the package payload changes, but canonical config/data/database locations and schema semantics do not.

## Packaging research

### Maturin binary wheels

Maturin's official `bin` binding mode packages a Rust binary into the wheel's scripts area. After installation the binary is exposed on `PATH` like another Python-package CLI. A Python extension module or PyO3 bridge is not required.

Relevant sources:

- https://www.maturin.rs/bindings.html
- https://www.maturin.rs/distribution.html
- https://www.maturin.rs/config.html
- https://www.maturin.rs/metadata.html
- https://www.maturin.rs/project_layout.html

Important current guidance:

- configure `bindings = "bin"` explicitly;
- use `manifest-path` to point at the Rust crate;
- use `project.dynamic = ["version"]` when Cargo owns the version;
- on Linux enforce an explicit manylinux baseline; Rust's current minimum supports manylinux2014/glibc 2.17;
- use `--compatibility pypi` and `--locked` for publishing;
- `maturin-action` supports x86_64/aarch64 manylinux build containers and recommends pinning both the action and maturin version.

### Wheel/install semantics

The Wheel/PyPA specifications allow distribution files to install executable scripts through the wheel data/scripts area. Installed distributions carry `.dist-info` metadata including `METADATA`, usually `RECORD`, and optionally `INSTALLER`; direct URL installs additionally record `direct_url.json`.

Relevant sources:

- https://packaging.python.org/en/latest/specifications/binary-distribution-format/
- https://packaging.python.org/en/latest/specifications/recording-installed-packages/
- https://packaging.python.org/en/latest/specifications/direct-url/

This makes package-manager ownership observable enough to distinguish an installed wheel from a standalone binary without heuristics based solely on PATH spelling.

### Exact version changes

`pip`, `uv`, and `pipx` all accept package requirement specifications. Exact `eggpool==VERSION` requirements can therefore select a Python-era wheel or a Rust-era platform wheel under the same project name.

Relevant sources:

- https://pip.pypa.io/en/stable/cli/pip_install/
- https://docs.astral.sh/uv/concepts/tools/
- https://docs.astral.sh/uv/guides/tools/
- https://pipx.pypa.io/stable/reference/cli.html

Current behavior useful to M11:

- `uv tool install 'package==version'` can recreate/replace a managed tool at the exact spec;
- uv tool executables include binary scripts supplied by wheels;
- pipx `install --upgrade PACKAGE_SPEC` explicitly supports upgrading **or downgrading** an existing package when the installed version does not satisfy the supplied spec;
- ordinary pip accepts exact requirements and can reinstall a different version into the same environment.

### PyPI immutability and historical gaps

PyPI does not allow a previously used distribution filename to be reused, even if deleted. A project version can consist of multiple distribution files, but M11 must not attempt to overwrite an already published artifact.

Relevant source:

- https://pypi.org/help/

For GitHub versions not currently represented on PyPI, M11 must freeze an explicit policy. The preferred outcome is to publish reproducibly rebuilt historical Python wheels for the missing version numbers when those filenames have never existed and the source tag/commit is immutable and qualified. If that is not feasible for a specific release, the installable-version catalog can use a pinned immutable VCS/archive fallback, but such a fallback must be called out as non-PyPI and tested before M11 promises it.

### Trusted Publishing and attestations

PyPI recommends Trusted Publishing via GitHub Actions OIDC. Its guidance recommends a dedicated release workflow and strongly encourages a GitHub environment; `id-token: write` should be job-scoped. The official `pypa/gh-action-pypi-publish@release/v1` action supports Trusted Publishing.

Relevant sources:

- https://docs.pypi.org/trusted-publishers/
- https://docs.pypi.org/trusted-publishers/using-a-publisher/
- https://docs.pypi.org/trusted-publishers/security-model/

The release workflow should build/test artifacts before the publish job; the publish job itself receives only the distributions to upload and OIDC permission. Index-hosted attestations should remain enabled. PEP 770 SBOM inclusion is useful hardening if the release build can produce one without introducing runtime complexity, but it is not an M11 functional blocker.

## Recommended cutover architecture

### Public distribution

Keep `eggpool` as the one PyPI package identity.

From the M11 cutover release onward:

- publish wheel-only Rust packages using Maturin `bin` bindings;
- build Linux x86_64 manylinux2014, Linux aarch64 manylinux2014, and macOS arm64 wheels only;
- install one native `eggpool` executable from the wheel;
- keep `Requires-Python >=3.11` through M11 to preserve the cross-era rollback environment even though the native process itself does not invoke Python;
- do not publish a Rust sdist as an automatic unsupported-platform build fallback;
- keep the historical root Python Hatchling package available for oracle/reference work until M12.

A dedicated M11 packaging manifest is safer than converting the root `pyproject.toml` immediately because the root definition is still needed to run the final Python reference and test suite.

### Release artifacts

A release candidate should produce one immutable artifact set from one source revision:

- supported PyPI wheels;
- the corresponding raw Rust binaries for standalone users/O008;
- hashes/manifest metadata linking version, source commit, target, binary digest, and wheel digest;
- optional SBOM/provenance metadata;
- no sdist for Rust-backed public releases during M11.

### Update/install authority

Install provenance decides the transition mechanism:

- uv-tool install -> uv tool exact reinstall;
- pipx install -> pipx exact install/upgrade path;
- ordinary pip/venv install -> that environment's Python `-m pip` exact install;
- standalone raw Rust executable -> O008 verified GitHub asset replacement;
- ambiguous/unmanaged paths -> fail closed with an actionable command instead of guessing.

`eggpool update` with no version remains latest-stable behavior. `eggpool update 1.2.3` and `eggpool update v1.2.3` normalize to the same exact target. An older target is an intentional downgrade, not an error.

### Cross-era rollback

The acceptance matrix needs at least:

```text
historical Python wheel
    -> first Rust wheel
    -> historical Python wheel
    -> Rust wheel
```

and should cover uv tool, pipx, and an ordinary isolated pip environment on the applicable host. The same config and DB must survive. If the service was running, transition logic stops it safely, changes the package, validates the target runtime/config/DB, restarts it, checks health, and restores the previous exact package version if verification/restart fails.

### Quick installer

The public quick installer can become substantially smaller:

- do not clone the repository for a normal public install;
- prefer uv's standalone bootstrap + `uv tool install 'eggpool==VERSION'`;
- allow pipx when already present and compatible;
- add explicit `--version VERSION` support;
- preserve existing config/data/database paths exactly;
- recognize an existing managed EggPool install and use the same transition service rather than layering a second environment over it;
- source-checkout installation remains an explicit developer path.

## Open questions frozen for K001 resolution

K001 must turn these into deterministic tables rather than leaving them implicit:

1. exact first Rust cutover version (`CUTOVER_VERSION`), which must be greater than every already-published/tagged stable release;
2. exact set of official historical versions supported for package-manager version switching;
3. which post-0.5.6 Python-era tags can be backfilled to PyPI as reproducible wheels without modifying historical source semantics;
4. exact package-manager provenance signals for uv, pipx, and pip environments on the M10-supported targets;
5. exact wheel tags and minimum macOS deployment target for the accepted macOS arm64 development target;
6. whether SBOM inclusion is mandatory for the first public cutover or an immediately-following hardening item;
7. whether PyPI is the default update metadata authority for package-managed installs while GitHub remains the raw-binary authority, or whether one signed release manifest indexes both. The recommended design is one static EggPool release catalog that links both channels while leaving actual installation to the owning package manager.

No M11 implementation should publish a real release before these tables are accepted.
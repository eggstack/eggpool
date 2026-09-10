# K010 — Public Metadata, Documentation, and Release-Candidate Freeze

Status: queued; blocked on accepted K009

Source roadmap: `migration-rs/subsystems/cutover-roadmap.md`

Primary class: capability/polish

Hard dependency: accepted K009.

## Objective

Prepare the repository and public documentation for the actual Rust-backed release without publishing it yet. Freeze one release candidate whose version, package metadata, installer behavior, updater semantics, target support and rollback documentation all agree.

K010 is the last pre-production cutover gate. It may change public docs and default installer code to the Rust-wheel path only when K009 has proven those paths against staged artifacts, but the production PyPI/GitHub release remains K011.

## Part A — release candidate identity

Use the K001 `CUTOVER_VERSION` and one immutable candidate commit.

Verify and freeze:

- Cargo version;
- Maturin/PyPI package version;
- `eggpool version` output;
- intended Git tag/release name;
- release manifest version;
- changelog heading;
- installable-release catalog entry;
- updater latest/exact fixture expectations.

No two version sources may disagree.

If the repository root Python `pyproject.toml` retains an older/reference package version, documentation and scripts must clearly distinguish it as the Python reference packaging definition; do not make it the M11 release-version authority.

## Part B — PyPI metadata

Update/finalize the Rust-wheel publication metadata to reflect the canonical runtime:

- description remains EggPool's existing product identity;
- license/authors/project URLs unchanged unless independently corrected;
- remove or replace Python-runtime-specific classifiers such as FastAPI/AsyncIO if they would falsely describe the Rust wheel;
- retain `Requires-Python >=3.11` during M11 per ADR-0004;
- target/platform metadata and documentation reflect Linux x86_64, Linux aarch64, and macOS arm64 development support only;
- no Python application dependency list in Rust wheel;
- no claim of importable Python library API unless one actually exists and is supported.

The PyPI project description must explain succinctly that the package now installs a native Rust `eggpool` executable while preserving exact-version rollback to supported Python-era releases.

## Part C — README quick start

Update README only after staged installer/package evidence is accepted.

The primary quick start should remain simple, e.g.:

```text
curl .../scripts/install.sh | bash
# or
uv tool install eggpool
# or
pipx install eggpool
```

Do not tell new users to clone the repository or install the Python app stack for normal operation.

Document:

- supported targets;
- normal latest install;
- exact version install where useful;
- `eggpool update` latest;
- `eggpool update X.Y.Z` exact upgrade/downgrade;
- config path and data preservation;
- systemd deployment path;
- how to identify current version/install method;
- explicit unsupported-target behavior.

Avoid cluttering the quick-start section with migration internals; link to a focused upgrade/rollback document.

## Part D — upgrade and rollback guide

Add/update a dedicated document such as `docs/upgrading.md` covering:

### Existing Python installs

- no config/database relocation needed;
- run ordinary `eggpool update` or public installer upgrade path;
- package manager replaces Python wheel with Rust wheel;
- service stops/restarts as documented;
- verify version/health.

### Exact downgrade

Examples:

```text
eggpool update 0.5.6
# or another exact version in the installable release catalog
```

State clearly:

- exact downgrade is supported only for catalogued compatible versions;
- Python-era target requires the manager environment's compatible Python;
- DB is not reset/downgraded;
- incompatible target is rejected;
- package manager provenance is respected.

### Return to Rust

Document exact/latest re-upgrade after rollback.

### Standalone binaries

Explain that standalone Rust installs use GitHub raw assets/O008 and are distinct from package-managed PyPI installs. Document optional adoption into canonical wheel management.

### Recovery

Document the manual recovery commands emitted when automatic rollback cannot complete. Do not require users to delete their database.

## Part E — deployment documentation

Update `docs/deployment.md` and relevant examples so:

- service installation resolves the package-managed Rust executable correctly;
- production deployment's chosen package-vs-standalone authority from K007 is explicit;
- root/personal install boundaries remain clear;
- no instructions source arbitrary shell rc files as root;
- cron/logrotate/backup commands remain valid after version switch;
- Python reference deployment is documented only as rollback/reference, not the normal new install.

## Part F — update command documentation

Document install-aware behavior:

- wheel-managed uv -> uv authority;
- pipx -> pipx authority;
- pip/venv -> owning pip authority;
- standalone Rust -> verified GitHub asset authority;
- source checkout -> explicit source update path;
- ambiguous provenance -> fail closed.

The user does not need to know every internal detection detail, but the command must explain why an installation cannot be updated automatically when ownership is ambiguous.

## Part G — release notes/changelog

Prepare release notes that cover:

- Rust implementation becomes canonical runtime;
- preserved config/DB/API/CLI/dashboard behavior;
- lower process/runtime dependency footprint as characterization, not an unsupported benchmark claim;
- PyPI remains package channel through platform wheels;
- exact upgrade/downgrade support;
- supported targets and unsupported Windows status;
- rollback window to supported Python releases;
- Python source remains in repo temporarily for M12/reference.

Do not claim Python is removed in M11.

## Part H — installer/update default-channel freeze

At the K010 candidate:

- public installer code should default to the Rust-wheel PyPI path when given an index containing the cutover release;
- `eggpool update` package-managed latest should resolve the cutover candidate through the package authority/catalog;
- standalone updater should resolve K003 GitHub raw assets;
- tests must use staging/local index overrides until K011 publishes production artifacts.

Ensure there is no hidden fallback to the old Python/source install when the Rust wheel is missing on a supported target; missing artifact is a release blocker.

## Part I — Python reference labeling

M11 still needs Python for rollback/oracle tests. Avoid confusing users/developers by making the root Python package look like the canonical release path after K010.

Add focused developer documentation explaining:

- `src/eggpool` is the final Python reference until M12;
- root Python tooling is still used by differential tests;
- canonical production package build uses the Maturin manifest;
- historical Python wheels are installed only via exact rollback/version tests;
- do not publish the root Hatchling package under the cutover version accidentally.

Add a release guard that prevents the wrong build backend/artifact set from being uploaded for a Rust-era release.

## Part J — documentation tests

Add deterministic checks for:

- quick-start commands use canonical package channel;
- no normal install step requires repo clone;
- supported target list equals K001;
- `eggpool update VERSION` examples normalize valid targets;
- Python removal is not claimed;
- no stale "Granian/FastAPI is the runtime" language in public operational docs;
- no stale raw-self-update claim for uv/pipx/pip installs;
- no Windows support claim;
- release candidate version consistent across required authorities;
- root Hatchling artifact cannot be selected by production release workflow.

Do not build an elaborate prose-lint framework; targeted string/structured checks are enough.

## User-visible compatibility

Keep command names, config paths, API URLs and deployment workflow stable. The packaging backend is an implementation detail; users should primarily notice that the installed binary is Rust and updates can move exactly between supported versions.

## Verification

Run K001-K009 focused tests plus full Rust/migration/smoke, installer dry-run/staged install, release workflow validation and docs link/command checks.

Re-run the M10 deterministic aggregate if K010 changes executable-visible metadata/help or installer/deployment sources included in Q001/Q002.

## Closure evidence

Write `migration-rs/closure/cutover/010-status.md` with:

- implementation commits;
- release candidate version/commit/manifest hashes;
- public metadata diff summary;
- quick-start/update/rollback documentation matrix;
- supported target/docs consistency;
- production-release guard proof;
- staged latest/exact install behavior;
- Python-reference preservation proof;
- verification results;
- unresolved release blockers;
- registry transition.

## Acceptance criteria

K010 closes only when:

- one release candidate version is consistent across all release authorities;
- public metadata/docs correctly describe Rust wheel installation and exact cross-era switching;
- normal new-install docs no longer route to Python/source runtime;
- deployment and update docs respect package-manager ownership;
- Python reference remains available and cannot be accidentally published as the Rust cutover version;
- supported/unsupported target claims match M10/K001;
- production release remains unpublished;
- no unresolved high/medium documentation/metadata/default-channel finding remains.

Accepted K010 promotes only K011.
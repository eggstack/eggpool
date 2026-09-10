# K001 — Cutover, Package, and Installable-Version Catalog Contract Freeze

Status: ready for handoff

Source roadmap: `migration-rs/subsystems/cutover-roadmap.md`

Repository baseline: `aec794a060f6a9e71853a515fe50992446c41ebd` (accepted Q012 / M10 closure).

Primary class: invariant/infrastructure

Hard dependencies: accepted M10/Q012 closure and ADR-0004.

## Objective

Freeze the exact public cutover, packaging, release, and cross-era version-switch contract before any release artifact or updater behavior is changed.

K001 converts the current implicit distribution state into a deterministic, machine-readable authority covering:

- first Rust cutover version;
- official release/tag inventory;
- PyPI release/file inventory;
- Python-versus-Rust implementation era;
- package-manager and standalone installation authorities;
- exact target/platform compatibility;
- DB/config rollback compatibility;
- unavailable/yanked/historical gaps;
- expected exact-version update/downgrade outcomes.

No production release is published in K001.

## Why this must precede packaging

The repository says `0.7.4`, GitHub release history reaches `v0.7.4`, while the public PyPI project currently exposes releases only through `0.5.6`. Existing `eggpool update VERSION` semantics already accept exact targets, but no truthful cross-era guarantee exists until the install source for each target is known.

Publishing a Rust wheel first and deciding historical semantics later would create avoidable ambiguity around downgrades and could strand package-managed users.

## Authoritative sources

Inspect current repository and public package evidence at implementation time. At minimum:

- `pyproject.toml`;
- `rust/Cargo.toml`;
- `scripts/install.sh`;
- `rust/src/operations/update.rs`;
- `src/eggpool/cli.py` and Python update/install helpers;
- O008/O009/O010 closure records;
- Q003/Q005/Q006/Q008/Q012 closure records;
- GitHub releases/tags for `eggstack/eggpool`;
- public PyPI `eggpool` release/file history;
- ADR-0004 and `cutover-planning-notes.md`.

Do not infer artifact availability from a Git tag alone.

## Part A — freeze release identity

### A1. Cutover version

Choose `CUTOVER_VERSION` satisfying all of:

- valid under EggPool's accepted PEP-440-compatible release subset;
- strictly newer than every stable version already published to PyPI or GitHub;
- identical in Cargo package version, Rust `version` output, PyPI metadata, Git tag/release and release manifest;
- not already used on PyPI;
- reserved for the first public Rust-backed wheel set.

Do not hard-code a suggested version from this plan; inspect current tags immediately before implementation.

### A2. Version-source consistency checker

Add a deterministic script/test that fails when candidate version authorities disagree. During the side-by-side window it must distinguish historical root Python project metadata from the Rust cutover packaging metadata instead of requiring both build systems to publish the same payload.

## Part B — installable release catalog

Create a bounded machine-readable catalog such as:

`migration-rs/fixtures/cutover/k001-installable-releases.json`

Each supported target release entry must contain at least:

- normalized version;
- implementation era: `python` or `rust`;
- immutable source tag and commit SHA;
- public release status;
- PyPI presence;
- known wheel/sdist filenames and hashes when public;
- Python requirement;
- supported target classes;
- DB/config compatibility classification;
- canonical package-manager requirement/source;
- expected `eggpool version` identity;
- rollback suitability;
- yanked/deleted/unavailable status;
- reason when exact switching is not supported.

Do not store credentials, signed temporary URLs, local paths, environment dumps or mutable branch names.

## Part C — historical Python-gap resolution policy

Enumerate every official stable Python-era GitHub release newer than the newest existing PyPI release.

For each missing version decide one of:

1. **PyPI backfill candidate** — immutable source can build the historically intended wheel, filename/version is unused, metadata is coherent, and a bounded smoke can qualify it;
2. **immutable fallback candidate** — package-manager can install a pinned immutable Git/archive source and the resulting historical runtime is qualified;
3. **not supported for exact switching** — explain the blocker explicitly.

The preferred outcome is a PyPI wheel backfill because it keeps the same exact `eggpool==VERSION` UX. However, never alter historical source semantics merely to force a backfill. PyPI does not permit replacing a used filename; any already-published file remains immutable.

K001 must also define whether backfilled historical wheels will be published during K008/K011 or earlier in a reviewed release-preparation step. Production upload remains outside K001.

## Part D — package/install authority matrix

Freeze expected provenance and transition ownership for:

| Install class | Current authority | Update/downgrade authority |
|---|---|---|
| uv tool | uv managed tool env | uv exact reinstall |
| pipx | pipx managed venv | pipx exact install/upgrade |
| ordinary pip in venv | environment pip | environment Python `-m pip` |
| standalone Rust binary | raw GitHub release asset | O008 verified self-replacement |
| source checkout | developer/source | explicit source workflow only |
| ambiguous/unmanaged executable | none trusted | fail closed with instructions |

K001 must record the exact safe signals available to later K004 provenance detection. `.dist-info/INSTALLER` is useful but optional; `direct_url.json`, environment paths and uv/pipx manager metadata may be combined. PATH shape alone is not enough.

## Part E — exact version transition contract

Freeze these user-level semantics:

```text
eggpool update
  -> latest stable release compatible with the current supported target

eggpool update X.Y.Z
eggpool update vX.Y.Z
  -> same exact normalized target
```

An exact target may be older than the current version and is therefore an authorized downgrade.

Required outcome categories include:

- success;
- exact-current/no-op;
- target-not-found;
- target-not-in-installable-catalog;
- target-yanked/unavailable;
- unsupported-platform;
- incompatible-Python-environment;
- DB/config rollback-incompatible;
- package-manager-unavailable;
- install-provenance-ambiguous;
- package-manager-failure;
- target-self-check-failure;
- service-restart/health failure;
- rollback-failure.

Error categories are secret-free and must not expose arbitrary subprocess output unboundedly.

## Part F — rollback compatibility rule

Use Q003 and current schema history to freeze which Python-era releases may safely open state after the Rust candidate has run.

If all current supported historical releases share the compatible schema, state that explicitly with evidence. Otherwise the release catalog must carry a minimum/maximum rollback window and K004/K005 must refuse an incompatible downgrade before package mutation.

Do not use automatic DB reset as a downgrade strategy.

## Part G — target matrix

Carry forward M10 exactly unless a new qualification explicitly changes it:

- Linux x86_64: supported;
- Linux aarch64: supported;
- macOS arm64: supported-development/non-root;
- Windows: unsupported;
- other Unix: not qualified.

Define exact Rust target triples and wheel platform tags to be implemented in K003. Linux baseline may not silently move newer than the accepted M10 compatibility floor.

## Part H — test fixtures

Add deterministic tests for catalog parsing/validation:

- duplicate versions rejected;
- mutable/non-immutable fallback rejected;
- missing source commit rejected;
- Rust entry without required supported wheels rejected once artifact stage is active;
- invalid/yanked state represented explicitly;
- Python/Rust era transition matrix generated deterministically;
- leading-`v` normalization;
- unsupported target never appears as supported;
- no secret-bearing fields allowed.

## Non-goals

K001 does not:

- build or publish Rust wheels;
- upload historical backfills;
- change public installer behavior;
- change `eggpool update` production behavior;
- alter DB schema;
- remove Python source/package metadata;
- add a broad release framework;
- create a second product/package name.

## Verification

Run at minimum:

```text
cargo test --manifest-path rust/Cargo.toml --all-targets -- --test-threads=1
uv run pytest tests/migration_rs -q --tb=short --maxfail=1
uv run pytest tests/smoke/ -q --tb=short --maxfail=1
uv run pyright src/ scripts/
uv run ruff format --check src/ tests/ scripts/
uv run ruff check src/ tests/ scripts/
git diff --check
```

Add focused K001 catalog/version tests and run them explicitly.

## Closure evidence

Write `migration-rs/closure/cutover/001-status.md` containing:

- implementation commit(s);
- selected `CUTOVER_VERSION` and source-of-truth rule;
- complete official GitHub/PyPI version inventory summary;
- list of missing historical PyPI versions and disposition for each;
- install authority/provenance matrix;
- DB/config rollback window;
- exact supported target/triple/tag table;
- test/verification results;
- unresolved blockers;
- registry transition.

## Acceptance criteria

K001 closes only when:

- release/version state is machine-readable and reproducible;
- every advertised exact-switch target has an immutable installation route or is explicitly classified unsupported;
- the first Rust cutover version is unambiguous and unused;
- package-manager versus standalone update ownership is frozen;
- cross-era rollback compatibility is explicit;
- target matrix matches M10;
- no production publication occurred;
- no unresolved high/medium cutover-contract finding remains.

Accepted K001 promotes only K002.
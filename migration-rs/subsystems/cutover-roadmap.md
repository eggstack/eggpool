# M11 Rust Cutover, Packaging, Release, and Cross-Era Versioning Roadmap

Status: active implementation; K011 blocked on production PyPI Trusted Publisher configuration; K001-K010 closed

Repository baseline for planning: `aec794a060f6a9e71853a515fe50992446c41ebd` (accepted Q012 / M10 closure).

Canonical sources: `../000-long-term-specification.md`, `../001-terminology-and-domain-model.md`, `../002-long-term-roadmap.md`, `../003-planning-process.md`, accepted ADR-0001 through ADR-0004, closed M4-M10 roadmaps, and accepted Q012 closure.

Research notes: `../cutover-planning-notes.md`.

## Purpose

M11 makes the already-qualified Rust implementation the canonical public EggPool runtime while preserving the product's existing package identity, filesystem/config/database contracts, operator workflows, and a controlled path back to compatible Python-era releases.

The cutover is a distribution and ownership transition, not another server rewrite. M4-M10 have already qualified the Rust runtime. M11 must establish trustworthy artifacts, package/install/update provenance, cross-era exact-version transitions, public installer/release workflows, deployed-service rollback, and the actual first Rust-backed public release.

M11 does **not** remove Python source or the final Python oracle. That remains M12 after the Rust cutover has stabilized.

## Current cutover block

The first `v0.8.0` GitHub release is public, but PyPI publication did not
complete. K013 corrected the workflow and proved exact-bundle recovery through
artifact validation; PyPI then rejected the OIDC exchange because the
`eggstack/eggpool` Trusted Publisher is not configured. K014 owns that external
configuration and recovery. K012 remains blocked until K011 is accepted.

## Packaging decision

ADR-0004 is authoritative:

- the PyPI project remains `eggpool`;
- Rust-backed releases are platform-specific Maturin `bin` wheels containing the native `eggpool` executable;
- the Rust wheel remains `Requires-Python >=3.11` during M11 to preserve a package-managed rollback environment, although the native EggPool process itself does not invoke Python;
- Rust-backed M11 releases are wheel-only on supported targets; no Rust sdist fallback is published;
- package-manager-owned installs are updated/downgraded through their owning package manager;
- O008 raw GitHub executable replacement remains only for clearly standalone Rust installs;
- Python packaging/source stays available as reference material until M12.

## Current distribution state

At the planning baseline:

- repository/Cargo/project version is `0.7.4`;
- GitHub has official tags/releases through `v0.7.4` with no current Rust binary assets;
- PyPI's newest visible `eggpool` release is `0.5.6`, distributed as a universal Python wheel and sdist;
- `scripts/install.sh` clones/uses the repository but ultimately installs through pipx or uv tool;
- O008 expects future GitHub raw assets named `eggpool-{version}-{os}-{arch}` and performs verified rollback-capable replacement;
- Q001/M10 support Linux x86_64, Linux aarch64, and macOS arm64 development/runtime; Windows and other Unix targets are not qualified.

M11 must reconcile the PyPI/GitHub version gap before promising arbitrary exact-version switching.

## M11 invariants

1. **One public package identity.** PyPI project name remains `eggpool`; do not create `eggpool-rs` as the normal upgrade path.
2. **Native process after cutover.** Installing the cutover wheel exposes the Rust `eggpool` executable directly; normal `eggpool` execution does not import/launch the Python runtime.
3. **Package-manager ownership is respected.** A wheel-managed executable is never directly overwritten by O008's raw updater.
4. **Exact version transitions are direction-neutral.** Newer, older, Python-backed, and Rust-backed targets use the same user-facing `eggpool update [VERSION]` intent when supported.
5. **No false historical promise.** M11 claims only versions present in the frozen installable release catalog with a tested immutable installation route.
6. **PyPI artifacts are immutable.** Never replace or mutate a previously published filename/version artifact; backfills use only filenames/releases PyPI will accept and source tied to immutable history.
7. **Existing config/data survive.** Package transitions never relocate, rewrite, or reset the canonical config, `.env`, SQLite DB, runtime paths, backups, or operator-created deployment files merely because implementation era changes.
8. **Database rollback window stays explicit.** Rust->Python downgrade is allowed only to release/catalog entries whose DB/config compatibility is accepted; incompatible targets fail before destructive mutation.
9. **Version authority is single-source.** A release candidate has one version across Cargo, Rust output, PyPI metadata, Git tag/release, release catalog, and docs.
10. **Supported target matrix is inherited from M10.** No Windows/other-Unix wheel or public support claim appears without qualification.
11. **No unsupported source fallback.** Missing wheel on an unsupported platform fails clearly instead of silently building Rust from sdist.
12. **Artifact provenance is bounded and verifiable.** Wheel/raw binary hashes and source commit are recorded; publish uses Trusted Publishing/OIDC; credentials are never stored in repo artifacts.
13. **Release build and publish are separated.** Build/test jobs produce immutable artifacts; a narrow publish job receives those artifacts and the minimum OIDC permission.
14. **Public release is reversible operationally.** Before M11 closure, a deployed Python install must cross to Rust, back to Python, and to Rust again without DB/config loss.
15. **Failure leaves a known install.** Failed package transition, binary validation, service restart, or health check restores or preserves the previous version when feasible and emits actionable recovery state.
16. **No double-manager install.** uv, pipx, pip/venv, and standalone provenance are distinguished; transition code does not install a second EggPool over an existing manager's exposed executable.
17. **M11 keeps Python.** Python source/package definition/oracle tests remain in the repository through cutover; M12 owns deletion/simplification.
18. **Normal CI remains bounded.** Release-target build workflows may be matrixed over the three accepted target classes, but M11 does not expand into a general all-platform CI program.
19. **Public docs match reality.** README/install/deploy/update documentation changes only when corresponding public artifact/install behavior exists.
20. **No cutover by documentation alone.** M11 closes only after the first real Rust-backed public release is installed and rollback-tested through the documented paths.

## Implementation sequence

```text
M10 Q012 accepted
  |
  v
K001 cutover/package/version-catalog contract freeze
 -> K002 Rust PyPI binary-wheel packaging substrate
 -> K003 supported wheel + raw release artifact matrix/catalog
 -> K004 install provenance + package-manager transition engine
 -> K005 cross-era exact upgrade/downgrade and rollback matrix (closed)
 -> K006 quick installer + existing-install adoption cutover (closed)
 -> K007 deployed-service cross-era transition and recovery (ready)
 -> K008 trusted publishing, attestations, and supply-chain release workflow
 -> K009 local wheelhouse/TestPyPI staged release rehearsal
 -> K010 public metadata/docs/default-channel release-candidate freeze
 -> K011 first Rust-backed public release + immediate rollback drill
 -> K012 aggregate M11 cutover qualification/closure
  |
  v
M12 planning eligibility only after accepted K012 closure
```

Only `../registry.md` authorizes implementation. K011 is the sole dependency-ready plan after accepted K010 closure.

## K001 — Cutover/package/version-catalog contract freeze

Freeze the actual release/version inventory before building artifacts. Enumerate GitHub tags/releases, PyPI releases/files, Python constraints, source commits, schema/config compatibility, target compatibility, and install mechanism. Choose the first Rust cutover version and define which historical versions are guaranteed switch targets.

Resolve the post-PyPI-0.5.6 gap explicitly. Prefer tested backfilled Python wheels for official missing versions where immutable source can be reproduced and PyPI accepts the new version/files; otherwise define an immutable package-manager fallback and label it clearly. Do not promise an untestable “any version.”

Exit: a machine-readable installable-release catalog and exact transition/error contract exist before packaging or publishing.

## K002 — Rust PyPI binary-wheel packaging substrate

Add a dedicated Maturin packaging boundary for `eggpool` using `bindings = "bin"` and the existing Rust Cargo manifest. Build/install local wheels without changing the root Python oracle package. Prove installed `eggpool` is the Rust executable and does not import Python at runtime.

Exit: local platform wheel is standards-compliant, metadata-correct, wheel-only, and installable by pip/uv/pipx tooling.

## K003 — Supported wheel/raw artifact matrix and release catalog

Build the accepted release target set: Linux x86_64 manylinux, Linux aarch64 manylinux, and macOS arm64. Produce matching raw GitHub executables for O008 standalone installs. Generate one bounded manifest linking version, commit, target, wheel filename/hash/tag, raw asset/hash, and installability.

Exit: every supported target has a verified candidate wheel and raw asset; unsupported targets are explicitly absent.

## K004 — Install provenance and package-manager transition engine

Teach Rust update/install logic to distinguish uv-tool, pipx, ordinary pip/venv, standalone Rust, source checkout, and ambiguous/unmanaged states using installed metadata and manager-owned state rather than PATH guesses. Package-managed installs delegate exact requirements to their manager; standalone installs retain O008.

Exit: update authority is deterministic and package metadata cannot be desynchronized by direct overwrite.

## K005 — Cross-era exact version transitions and rollback

Exercise and harden exact upgrades/downgrades across the Python/Rust boundary. Required core path: Python -> Rust -> Python -> Rust using the same config/database. Cover latest, exact, leading `v`, exact-current, downgrade, nonexistent/unavailable target, incompatible platform/Python, package-manager subprocess failure, target self-check failure, and rollback failure reporting.

Exit: every catalog-guaranteed transition has deterministic evidence for its owning installation method.

## K006 — Quick installer and existing-install adoption cutover

Replace the current clone-heavy public quick-install behavior with a small package-channel bootstrap. Prefer uv tool; use an existing compatible pipx installation where appropriate. Add exact `--version`. Detect and adopt an existing EggPool tool rather than layering a second manager environment. Preserve config/data/database and retain an explicit source-checkout developer path.

Exit: fresh install and existing Python-installed upgrade both end at the Rust wheel via the public installer without changing product state paths.

## K007 — Deployed-service cross-era transition and recovery

Qualify package changes while EggPool is daemon/systemd-managed. Safely stop/observe running state, transition package, validate target/config/DB, restart, health-check, and recover previous exact version on failure. Ensure service `ExecStart` points at a stable manager-exposed path and deployment files need no rewrite solely because package era changes.

Exit: a real disposable Linux deployment can Python -> Rust -> Python -> Rust with preserved state and deterministic service recovery.

## K008 — Trusted publishing, attestations, and release supply chain

Create the dedicated release workflow. Pin release actions/tooling, build from tags, use Maturin `--locked`/PyPI compatibility and explicit manylinux baselines, separate build/publish permissions, use PyPI Trusted Publishing/OIDC with a protected environment, retain standard attestations, and publish raw GitHub assets from the same source revision. Optional SBOM support may be added if bounded.

Exit: the workflow can produce and stage the complete immutable artifact set without a long-lived PyPI token.

## K009 — Local wheelhouse/TestPyPI staged release rehearsal

Before touching production PyPI, run the complete artifact/install/version-switch flow against local wheelhouse and TestPyPI-equivalent staging where possible. Validate wheel selection, unsupported targets, metadata, uv/pipx/pip behavior, standalone raw assets, exact-version switching, service restart and rollback.

Exit: release mechanics are proven independently of the production package index.

## K010 — Public metadata/docs/default-channel release-candidate freeze

Update project metadata/docs/install/deploy/update guidance to describe the actual Rust-wheel architecture, supported target matrix, package-manager ownership, exact-version commands, rollback window, and standalone binary path. Freeze the release candidate version/changelog and verify no stale Python-runtime claim remains in public installation docs while the Python reference is still documented for rollback/debugging.

Exit: the release candidate is internally consistent and documentation no longer points new users to the old Python runtime by default.

## K011 — First Rust-backed public release and immediate rollback drill

Publish the first real Rust-backed `eggpool` PyPI wheel set plus matching GitHub raw assets from the frozen candidate. Install it from the public channel on required supported targets, verify the executable is Rust, run bounded health/inference/operator checks, then execute at least one real cross-era rollback/re-upgrade through a package-managed installation. Record PyPI/GitHub immutable artifact identities and any yanks/blockers rather than hiding release defects.

Exit: public `pip`/`pipx`/`uv tool` resolution selects the Rust wheel on supported targets and the rollback path works on preserved state.

## K012 — Aggregate M11 qualification and cutover closure

Aggregate K001-K011 plus fresh M10 evidence, verify public install/update/deploy/docs behavior, ensure no supported command still assumes Python package internals, confirm no public unsupported-target fallback, and decide whether Rust can be called canonical while Python remains a reference implementation.

Exit: Rust is the canonical public runtime/distribution on supported targets, package-managed exact transitions are safe, the documented rollback window works, and no unresolved high/medium cutover/security/data-loss/packaging/compatibility finding remains. M12 becomes eligible for separate planning only after accepted K012 closure.

## Installable release catalog

K001 should add a bounded machine-readable catalog, for example under `migration-rs/fixtures/cutover/`, with one record per supported transition target:

- normalized version;
- era: `python` or `rust`;
- immutable source commit/tag;
- distribution authority: PyPI wheel or explicitly approved immutable fallback;
- package filename/hash when frozen/available;
- `Requires-Python` constraint;
- DB/config compatibility floor/ceiling;
- supported target classes;
- exact install requirement/source;
- expected `eggpool version` output;
- rollback classification;
- yanked/unavailable/deprecated status.

The catalog is not a second package index. It is a compatibility/decision artifact used by transition validation and tests.

## Version semantics

M11 keeps O008's PEP-440-compatible release subset and optional leading `v` normalization. Exact target selection means the requested release identity, not “nearest compatible.”

For package-managed installs:

```text
eggpool update            -> latest stable catalog/PyPI target
eggpool update 1.2.3      -> exact 1.2.3
eggpool update v1.2.3     -> exact 1.2.3
```

An older exact target is an authorized downgrade. A missing, yanked-without-policy, incompatible, unsupported, or non-catalog target fails before package mutation with an actionable category.

Do not implement `eggpool update` by shelling out through an untrusted command string. Use fixed executable/argument vectors, bounded output/timeouts, validated package/version strings, and inherited environment allowlists appropriate to the manager.

## Release target matrix

Starting M11 matrix, inherited from Q001/Q005/Q008:

| Target | Public Rust wheel | Raw GitHub asset | M11 service claim |
|---|---|---|---|
| Linux x86_64 | required | required | yes |
| Linux aarch64 | required | required | yes/non-root + SBC; deployment according to M10 Linux contract |
| macOS arm64 | required | required | development/non-root only |
| Windows | no | no | unsupported |
| other Unix | no | no | not qualified |

Linux wheels use an explicit manylinux baseline no newer than the accepted M10 deployment floor unless qualification proves otherwise. Maturin/PyPI compatibility checks must pass before publish.

## Release provenance and security

The release workflow should:

- trigger only from an intentional version/tag/release event after deterministic gates;
- verify tag/version/catalog/Cargo/PyPI metadata agreement;
- pin build/publish actions and Maturin version;
- build wheels/raw binaries in target-specific jobs with Cargo lock enforcement;
- generate hashes and a bounded manifest;
- smoke-install each wheel before upload;
- publish to PyPI using Trusted Publishing with job-scoped `id-token: write` and a protected `pypi` environment;
- publish GitHub assets from the same candidate commit;
- retain index-hosted attestations from the standard publishing action;
- never expose provider API credentials to release-build jobs;
- never grant write permissions to matrix build jobs merely to simplify uploads.

M11 may include PEP 770 SBOM data in the wheel/release manifest if it can be generated deterministically without expanding the runtime dependency graph. SBOM is recommended hardening, not a prerequisite for basic package switching.

## Historical Python release gap policy

Current PyPI public state stops before current GitHub version history. K001 must not guess that all GitHub tags are installable via `eggpool==VERSION`.

For each official Python-era release missing from PyPI:

1. verify the tag resolves to an immutable commit and the package version matches;
2. build the historical wheel in an isolated/reproducible environment;
3. run at least the frozen installation/version/config-open smoke applicable to that release;
4. determine whether PyPI will accept a new release/file for that unused version filename;
5. prefer publishing the tested historical wheel if this can be done without modifying the historical payload/metadata semantics;
6. otherwise record a pinned immutable fallback or classify the version unsupported for direct switching.

M11's user-facing promise is “any version in the installable release catalog,” not every tag ever created regardless of artifact availability.

## Failure and rollback posture

A package transition is a small state machine:

```text
identify current install/provenance
 -> resolve exact target/catalog compatibility
 -> record current exact version + running/service state
 -> stop service if required
 -> invoke owner package manager or standalone updater
 -> verify target `version` + config + DB/migrations
 -> restart if previously running
 -> health check
 -> commit success
```

If mutation succeeds but verification/restart fails, use the same owner to restore the previous exact release and then restore prior running state. Config/DB are never deleted as rollback. If automatic rollback itself fails, retain explicit recovery instructions and avoid claiming success.

## CI/release posture

M11 adds a release-specific target matrix because distribution correctness requires it. It does not convert normal PR CI into that full matrix. Keep ordinary development CI lean; release candidate and manual qualification workflows own expensive cross-target packaging.

## Non-goals

M11 does not:

- delete `src/eggpool` or the Python oracle package;
- remove migration/differential tests;
- publish Windows/other-unqualified target wheels;
- introduce a Python/Rust hybrid runtime process;
- create a bootstrap downloader inside the wheel;
- add PyO3 merely to package the CLI;
- publish a Rust sdist as an unqualified fallback;
- change DB schema for release metadata;
- redesign config, CLI, dashboard, routing, or provider semantics;
- make PyPI credentials available to normal CI;
- implement fleet/auto-update orchestration;
- require a cloud signing service or custom package protocol when standard PyPI/GitHub provenance suffices;
- retire Python before M12.

## M11 closure

K012 may close M11 only when:

- first Rust-backed wheels exist publicly for every required M10-supported target;
- fresh public install resolves to Rust on those targets;
- package-managed Python -> Rust -> Python -> Rust exact transitions are demonstrated on the required compatibility path;
- standalone raw Rust update remains verified and does not run for wheel-managed installs;
- existing config/data/database/deployment state survives transitions;
- public installer/docs point new supported users to the Rust-backed package;
- release hashes/provenance/Trusted Publishing evidence are recorded;
- unsupported targets fail cleanly;
- no high/medium release, package ownership, update, rollback, compatibility, security, or data-loss finding remains;
- Python reference source remains intact for M12.

Accepted K012 makes M12 eligible for its own planning review; it must not remove Python as part of closure.

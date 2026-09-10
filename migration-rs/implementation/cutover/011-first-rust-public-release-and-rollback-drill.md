# K011 — First Rust-Backed Public Release and Immediate Rollback Drill

Status: queued; blocked on accepted K010

Source roadmap: `migration-rs/subsystems/cutover-roadmap.md`

Primary class: capability/invariant

Hard dependency: accepted K010.

Operational dependencies: maintainer-approved production PyPI Trusted Publisher/environment, GitHub release authority, and supported-target hosts for post-publication validation.

## Objective

Execute the first real public Rust cutover: publish the frozen Rust-backed `eggpool` wheels to the existing PyPI project, publish matching GitHub raw executables/manifest, verify public package resolution on supported targets, and perform an immediate real package-managed Python -> Rust -> Python -> Rust rollback drill on preserved state.

K011 is intentionally operational. If production publication cannot be performed, K011 remains blocked; staged evidence from K009 cannot be relabeled as a public release.

## Pre-publication gate

Before any production upload, require all of:

- K001-K010 accepted;
- source tree clean at the frozen release candidate commit;
- selected tag/version unused on PyPI and GitHub stable release history;
- Cargo/package/tag/catalog/changelog versions identical;
- all K003 supported wheel/raw artifacts rebuilt or retrieved as the exact frozen bytes approved by K009/K010;
- full artifact hashes/manifest verified;
- full required Rust/migration/smoke/release checks green;
- PyPI Trusted Publisher configuration verified for the exact release workflow/environment;
- no unresolved high/medium cutover finding;
- explicit maintainer approval for the production release event.

Do not publish one target early while another required artifact is still failing.

## Historical Python gap/backfill ordering

If K001 chose to backfill missing official Python-era versions to PyPI, complete and verify those reviewed uploads before the cutover release is declared complete.

Rules:

- each backfill is built from its immutable historical commit/tag;
- upload only filenames/versions confirmed unused;
- verify public PyPI hash/metadata after upload;
- install each newly public backfill at least once through the intended manager path;
- update the installable release catalog with immutable public file hashes;
- do not modify/delete an existing PyPI release to make the history look cleaner.

If a selected historical fallback remains non-PyPI, document it clearly in the public rollback matrix.

## Production PyPI publication

Use the K008 dedicated release workflow/Trusted Publishing job.

Publish only the accepted wheel files for `CUTOVER_VERSION`:

- Linux x86_64;
- Linux aarch64;
- macOS arm64.

No Rust sdist. No universal wheel. No Windows/unqualified platform wheel.

After upload, verify through PyPI public metadata/index:

- exact version exists;
- file list exactly matches intended set;
- file hashes equal release manifest;
- `Requires-Python` and project metadata are correct;
- no Python application dependencies are present;
- Trusted Publishing/attestation state is present as expected;
- the stable release is resolver-visible.

PyPI files are immutable. A bad uploaded file is handled by stopping/yanking/releasing a corrected newer version, not overwriting the filename.

## Production GitHub release

Create/finalize the matching stable GitHub release/tag with:

- raw Rust executable for each supported target;
- release manifest/hashes;
- release notes from K010;
- optional SBOM/provenance artifacts accepted by K008;
- no draft/prerelease flag once normal latest update should see it.

Verify O008 latest/exact standalone resolution finds the intended raw asset and digest evidence.

## Public fresh-install validation

From clean supported target environments, install from production PyPI without staging overrides.

Mandatory:

### Linux x86_64

- uv tool fresh install;
- `eggpool version` exact cutover version;
- prove native Rust executable;
- `help`, `check-config`, foreground serve/health;
- bounded local finite/stream inference;
- dashboard route;
- uninstall/reinstall.

### Linux aarch64

Use the representative physical/real environment available from Q008 or equivalent:

- uv tool or canonical SBC manager install from public PyPI;
- native aarch64 Rust executable;
- version/config/serve/health/local inference;
- no source build/toolchain fallback.

### macOS arm64

- public wheel install;
- native Rust executable;
- version/config/serve/health/local inference;
- no source build.

Pipx and ordinary pip production-index install should also be tested on at least one supported host each if they are retained as public supported package managers.

## Existing Python-install upgrade validation

Create/use an actual package-managed historical Python installation from production PyPI/catalog, with preserved config/database state. Execute:

```text
eggpool update
```

or the exact cutover version if required by the frozen test.

Verify:

- manager remains authoritative;
- public resolver selects Rust wheel;
- native Rust executable replaces Python CLI;
- config and DB stay at same paths;
- package manager metadata and CLI version agree;
- service state can be restored;
- no raw GitHub replacer touched the manager-owned binary.

## Immediate public rollback drill

On one disposable but realistic Linux package-managed deployment:

1. begin from a public historical Python release;
2. seed/preserve representative config and DB;
3. update through public channel to Rust cutover release;
4. run request/operator/backup/health checks and produce Rust-era DB writes;
5. exact-downgrade through `eggpool update <PYTHON_VERSION>` to the catalogued Python rollback release;
6. verify Python package/CLI metadata and same DB/config;
7. run the allowed Python read/serve/health checks and confirm post-Rust state readability;
8. exact/latest update back to Rust public release;
9. verify final service/inference/DB state;
10. record package-manager metadata and sanitized state hashes at each leg.

This is mandatory. A hypothetical rollback is insufficient for cutover.

## Standalone public updater validation

Install/use one K011 raw Rust binary in a standalone location and verify:

- latest/exact O008 resolution selects K011 GitHub asset;
- digest/size/self-check succeeds;
- exact downgrade between available raw Rust releases works if at least two Rust raw versions exist; otherwise exact-current/latest behavior is proven and future raw downgrade remains qualified by O008 fixture until a second Rust release;
- package-managed detection does not route this standalone install through pip/uv/pipx.

Do not use standalone raw path as a substitute for PyPI wheel validation.

## Unsupported-target public behavior

From resolver simulation or a real unsupported environment where practical, prove:

- production PyPI has no compatible Rust wheel;
- no Rust sdist exists;
- package manager returns a no-matching-distribution/unsupported result;
- existing installation is not mutated by `eggpool update` after compatibility precheck;
- docs accurately mark the target unsupported/not qualified.

## Production failure decision tree

If publication/validation fails:

### Before any file upload

Abort release; no public state changed.

### Partial PyPI file upload

Stop further publication. Compare uploaded files to manifest. Do not overwrite. Decide whether the incomplete release can be completed safely with the remaining exact approved files or must be yanked and superseded by a new version. Record actual public state.

### All files uploaded but runtime defect found

Yank affected release/files where appropriate, keep immutable forensic hashes, restore README/install latest guidance if necessary, and release a corrected new version only through a new plan/corrective pass. Do not delete/reupload under same filename.

### GitHub asset mismatch

Do not mark stable release complete/latest until raw assets match manifest. If a bad immutable public asset has been consumed, create corrective release/version policy rather than silently swapping content without audit.

### Rollback drill fails

M11 remains open. Do not declare Rust canonical merely because fresh installs work.

## Metrics/evidence

Record public, non-secret facts:

- PyPI version/file names/hashes/tags/sizes;
- Trusted Publishing/attestation presence;
- GitHub release/tag/raw asset hashes;
- target/package-manager install results;
- exact transition versions/eras;
- config/DB semantic hashes or bounded row summaries;
- service/health results;
- artifact/source commit identity.

Never record user credentials, provider API keys, `.env`, raw request bodies, private host identifiers or OIDC tokens.

## Release announcement scope

K011 may update release notes/public project description as frozen in K010. Avoid broad performance claims not supported by Q008/Q009 characterization. The central claim is compatibility-preserving Rust runtime cutover, not benchmark marketing.

## Tests/verification

Before production trigger, rerun:

- K001 catalog/version guards;
- K002/K003 artifact validation;
- K004/K005 manager transition suites;
- K006 installer suite;
- K007 deployed rollback suite or freshness-confirmed equivalent;
- K008 workflow validators;
- K009 staging aggregate;
- K010 docs/version guards;
- full Rust tests;
- migration oracle suite;
- smoke suite;
- formatting/lint/type checks.

After production upload, run the public-index target/rollback matrix above.

## Closure evidence

Write `migration-rs/closure/cutover/011-status.md` containing:

- production source/tag/version;
- PyPI wheel table with hashes/tags/attestation status;
- GitHub raw asset/manifest table;
- historical backfill publication results if applicable;
- fresh install results by target/manager;
- real Python -> Rust -> Python -> Rust public rollback drill;
- standalone updater public result;
- unsupported-target result;
- any yank/partial-publication events;
- exact verification commands/results;
- unresolved findings;
- registry transition.

## Acceptance criteria

K011 closes only when:

- the first Rust-backed stable `eggpool` release is genuinely public on PyPI for every required supported target;
- matching verified GitHub raw assets are public;
- supported public fresh installs run the Rust binary;
- at least one real public package-managed Python -> Rust -> Python -> Rust rollback cycle succeeds on preserved state;
- existing Python install upgrade works through its manager;
- unsupported targets do not fall back to source builds;
- artifact/public hashes match the frozen manifest;
- production publish uses Trusted Publishing and expected provenance;
- no unresolved high/medium release/rollback/data-loss/security finding remains.

Accepted K011 promotes only K012. K011 alone does not remove Python source.
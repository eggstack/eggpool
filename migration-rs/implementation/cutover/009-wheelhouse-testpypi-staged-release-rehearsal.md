# K009 — Local Wheelhouse and TestPyPI Staged Release Rehearsal

Status: dependency-ready after accepted K008

Source roadmap: `migration-rs/subsystems/cutover-roadmap.md`

Primary class: invariant/polish

Hard dependency: accepted K008.

Operational dependencies: target runners from K003 and optional TestPyPI Trusted Publisher/environment for the existing/rehearsal project identity if feasible.

## Objective

Run the complete M11 package/install/update/release flow in environments that behave like public distribution without making the Rust runtime canonical on production PyPI yet.

K009 catches resolver, wheel-tag, manager, publish-workflow and rollback defects before K011 is allowed to publish the first Rust-backed production release.

## Two-layer rehearsal

### Layer 1 — local wheelhouse/index substitute (mandatory)

Use immutable local artifacts from K003 and package-manager index/find-links options to reproduce exact install transitions without external package-index uncertainty.

This layer must be fully deterministic and run on all required target classes.

### Layer 2 — TestPyPI or equivalent real index (strongly preferred; mandatory if feasible)

Publish a uniquely versioned rehearsal artifact or use the accepted TestPyPI workflow mode to prove:

- wheel upload;
- platform file discovery;
- public-style resolver behavior;
- uv/pipx/pip download/install;
- Trusted Publisher/OIDC path if TestPyPI is configured;
- index metadata/hashes.

Do not pollute production PyPI with rehearsal-only versions merely to test index mechanics.

If TestPyPI cannot support the existing project identity/Trusted Publisher setup cleanly, closure may use the mandatory local wheelhouse plus a workflow-level TestPyPI dry-run only if the limitation and production residual risk are explicit. K011 remains responsible for first real production proof.

## Candidate identity

Use a cutover release candidate version that cannot be mistaken for the final production version if uploaded to TestPyPI. Do not consume the final stable PyPI version filename during rehearsal.

The source code/artifact shape should otherwise match the intended production release as closely as possible.

## Target rehearsal matrix

Mandatory:

- Linux x86_64 wheel install/update;
- Linux aarch64 wheel install/update on real or accepted representative runner/SBC where feasible;
- macOS arm64 wheel install/update;
- unsupported-target resolution negative case;
- raw standalone Rust artifact update path on at least one Linux target and macOS target where available.

The package-manager cross-era matrix from K005 must be rerun at least on Linux x86_64 and macOS arm64, with Linux aarch64 using the primary manager path expected for SBC deployment.

## Package-manager cases

For uv tool, pipx, and ordinary pip/venv where supported:

- fresh Rust wheel install;
- Python rollback release -> Rust candidate;
- Rust candidate -> Python rollback release;
- Python -> Rust again;
- exact current target;
- invalid/nonexistent target;
- unsupported wheel target;
- manager collision/ambiguous provenance;
- uninstall/reinstall;
- no raw executable overwrite on manager-owned install.

Use the exact K001 catalog and K004 transition service, not a special rehearsal-only update path.

## Installer rehearsal

Run the K006 public installer against the staged artifact authority using explicit test hooks/index configuration that cannot point accidentally to production.

Prove:

- normal fresh install does not clone repo;
- `--version` selects exact candidate or historical target;
- existing Python install is adopted in place;
- config/data paths remain unchanged;
- source-checkout developer mode remains local;
- no unsupported-target sdist fallback.

Any test-index override must be explicit and impossible to retain accidentally in generated user configuration.

## Release workflow rehearsal

Trigger the K008 release workflow in non-production mode and verify:

- tag/version validation;
- all supported target artifacts built;
- wheel install smoke happens before publish;
- aggregate manifest generated from exact artifacts;
- build jobs have no production OIDC/release write authority;
- staging publish uses only staging destination;
- production publish job is disabled/gated;
- artifacts downloaded by publish job match build hashes.

## Historical Python backfill rehearsal

If K001 selected PyPI backfills for missing historical versions:

- build from immutable historical commit;
- install through local wheelhouse and, if practical, TestPyPI under a safe rehearsal version/project setup;
- verify Python CLI/config/DB compatibility;
- compare metadata to historical project identity;
- record whether production PyPI upload is technically possible without filename conflict.

Do not alter the historical wheel payload solely to make current tests easier.

## Unsupported-target proof

At minimum test a resolver/tag set representing Windows and one unqualified Unix target. Expected behavior:

- no compatible Rust wheel;
- no sdist/source fallback;
- package manager returns no matching distribution/unsupported target;
- existing installed EggPool remains unchanged during update attempt;
- `eggpool update` translates the failure into a bounded actionable category where it controls the transition.

## Artifact/index consistency

For any real staging upload, compare:

- local wheel SHA-256;
- index-reported file hash;
- downloaded wheel SHA-256;
- installed executable SHA-256;
- release manifest entry.

Likewise for raw GitHub rehearsal assets where a draft/rehearsal release is used.

## Failure injection

Exercise:

- one artifact missing from workflow aggregation;
- corrupted wheel after build-before-publish;
- index upload failure;
- index returns wrong/missing file;
- package manager download timeout;
- partial manager install failure;
- staged target self-check failure;
- service restart failure after staged install;
- local wheelhouse contains unsupported extra version/target;
- attempt to activate production publish from rehearsal trigger.

The rehearsal must fail closed and leave production authorities untouched.

## Environment evidence

Record sanitized:

- OS/architecture;
- manager versions;
- Python version used only for package management/rollback;
- wheel tag selected;
- exact candidate/release manifest hash;
- source commit;
- result categories.

Do not store TestPyPI OIDC tokens, package index credentials, proxy auth or user home paths.

## Existing M10 evidence freshness

Re-run the M10 deterministic aggregate against the final K009 candidate if packaging/update/installer code touches runtime-visible behavior. Full live-provider/SBC/rootful evidence is not automatically repeated; record source-freshness justification or run targeted follow-up only when those surfaces changed.

## Tests

Add a K009 qualification runner/report that can be executed locally and on target hosts. It should orchestrate existing K003-K007 tests rather than duplicate their internal logic.

Required machine evidence:

- target;
- manager;
- source/target version and era;
- selected wheel/raw artifact;
- artifact hash checks;
- install/update/rollback result;
- unsupported-target results;
- workflow rehearsal result;
- bounded error category.

## Closure evidence

Write `migration-rs/closure/cutover/009-status.md` with:

- implementation/rehearsal commits;
- candidate identity;
- local wheelhouse matrix;
- TestPyPI/staging disposition and exact evidence;
- package-manager cross-era results;
- installer rehearsal results;
- workflow permission/publish gating proof;
- artifact hash consistency table;
- unsupported-target negative evidence;
- historical backfill readiness if applicable;
- unresolved production-release risks;
- registry transition.

## Acceptance criteria

K009 closes only when:

- the complete supported wheel set resolves/installs correctly from a package-index-like authority;
- package-manager exact upgrade/downgrade paths use the same code intended for production;
- installer/adoption behavior works against staged artifacts;
- production publish cannot run from rehearsal mode;
- unsupported targets do not build from source or mutate existing installs;
- artifact hashes remain consistent build -> index -> download -> install;
- any TestPyPI limitation is explicit and does not conceal a high/medium production risk;
- no unresolved high/medium release-mechanics finding remains.

Accepted K009 promotes only K010.

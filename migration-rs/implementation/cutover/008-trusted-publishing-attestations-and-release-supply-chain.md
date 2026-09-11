# K008 — Trusted Publishing, Attestations, and Release Supply Chain

Status: queued; dependency-ready after accepted K007

Source roadmap: `migration-rs/subsystems/cutover-roadmap.md`

Primary class: infrastructure/invariant

Hard dependency: accepted K007.

Operational dependency: maintainer configuration of the existing PyPI `eggpool` project's Trusted Publisher and GitHub `pypi` environment where required.

## Objective

Create the narrow production release workflow that builds the K003 artifact set from an immutable version tag, validates it, and publishes through standard trusted package/release channels without long-lived PyPI credentials.

K008 establishes release machinery and may perform TestPyPI/staging publication. The first canonical production Rust cutover release remains K011.

## Workflow structure

Prefer one dedicated `.github/workflows/release.yml` with clearly separated jobs:

```text
validate-release
  -> build-wheel-linux-x86_64
  -> build-wheel-linux-aarch64
  -> build-wheel-macos-arm64
  -> build/raw artifact checks
  -> aggregate-release-manifest
  -> staged/test publication gates
  -> production publish jobs (only K011/tag/release policy)
```

Do not grant release/publish permissions to normal CI or build jobs.

## Trigger policy

Freeze exact trigger semantics. Recommended:

- version tag matching the accepted release format;
- optional manual rehearsal mode that cannot publish production artifacts;
- production PyPI/GitHub publication only from the canonical repository and reviewed release/tag event;
- protected `pypi` GitHub environment with manual approval if available/desired.

A pull request or arbitrary branch push must never publish to production PyPI.

## Permission model

Default workflow permissions should be read-only.

Production PyPI publish job receives only the permissions it needs, including job-scoped:

```text
id-token: write
```

for Trusted Publishing/OIDC. GitHub release upload job receives the minimum content/release write permission separately.

Build/test jobs do not receive OIDC publish permission or provider credentials.

## Trusted Publishing

Use the official PyPI Trusted Publishing mechanism for the existing `eggpool` project:

- repository owner/name fixed to `eggstack/eggpool`;
- workflow filename fixed to the dedicated release workflow;
- GitHub environment fixed if configured;
- no PyPI API token secret stored in repository/action configuration;
- official `pypa/gh-action-pypi-publish@release/v1` or current PyPA-recommended equivalent, pinned/reviewed according to repo policy;
- attestations remain enabled unless a documented compatibility problem requires a separate decision.

Document the one-time maintainer action required in PyPI project settings. The implementation agent cannot truthfully claim Trusted Publisher configuration succeeded without evidence from a real staged/production publish.

## Build action/tool pinning

Pin:

- checkout/setup actions used in the release path;
- Maturin action by immutable commit SHA where practical;
- explicit Maturin version;
- Rust toolchain/version compatible with Cargo `rust-version`;
- any QEMU/cross build setup action used for aarch64.

Maturin's own hardening guidance recommends explicit manylinux versions, `--compatibility pypi`, `--locked`, and pinned tool/action versions. Follow it unless current tooling evidence requires an explicit deviation.

Do not use `latest` for production release-build tooling.

## Build once / publish exact artifacts

Production publish jobs must not rebuild source.

Required sequence:

1. checkout exact release commit/tag;
2. validate version/tag/catalog consistency;
3. build wheel/raw artifact in target job;
4. run target-appropriate install/smoke checks;
5. hash artifact;
6. upload as workflow artifact;
7. aggregate hashes into K003 release manifest;
8. publish the exact downloaded artifacts from the validation job to PyPI/GitHub.

The artifact passed to publish must be byte-identical to the one smoke-tested.

## GitHub raw assets

Create/attach raw executables required by O008/K003 plus the bounded release manifest.

Do not depend only on GitHub's source tarball/zip for standalone updater behavior. The exact target assets and hash/digest metadata expected by O008 must exist.

If GitHub's API asset `digest` field is not consistently available for the workflow-produced asset at implementation time, publish a signed/hashed manifest or adjacent SHA-256 file and update O008 descriptor parsing behind its existing integrity boundary. Do not invent a proprietary crypto/signing system unless standard GitHub/PyPI provenance is insufficient.

## PyPI wheel publication

Publish only the supported M11 Rust wheels for the candidate version. Do not upload:

- a universal `py3-none-any` cutover wheel;
- a Rust source distribution;
- Windows/other-unqualified target wheels;
- historical Python files whose source/version provenance has not passed K001/K003/K005 qualification.

If historical PyPI backfills are part of K001, publish them through a separate reviewed mode/job tied to the immutable historical commit and exact version. Never rebuild an old wheel from current source with a downgraded version field.

## Attestation/provenance checks

Record and, where APIs permit, verify:

- PyPI file SHA-256 equals built artifact hash;
- GitHub raw asset SHA-256 equals release manifest;
- source tag commit equals release manifest commit;
- package metadata version equals tag;
- published files have expected platform tags;
- PyPI Trusted Publishing/attestation is present;
- GitHub release is not draft/prerelease when selected by normal latest updater.

## SBOM

Maturin supports adding SBOM material under `.dist-info/sboms`. Evaluate a minimal deterministic release SBOM generated from Cargo/package metadata.

Requirements if included:

- release/build-only tooling;
- no secrets/local paths;
- bounded size;
- references the exact artifact/source dependency graph;
- included consistently in wheel hashes before smoke/publish.

SBOM support is recommended but not a K008 closure blocker unless K001 made it mandatory. Do not delay the cutover solely to build a bespoke SBOM service.

## Historical backfill security

For any Python release backfill:

- use isolated build from immutable commit/tag;
- verify version metadata exactly matches target;
- inspect wheel/sdist contents for unintended credentials/local files;
- do not sign/publish an artifact reconstructed from uncommitted working-tree state;
- retain the backfill's source commit and artifact hash in the K001 catalog.

## TestPyPI/staging mode

The workflow should support K009 rehearsal without production permissions. TestPyPI uses a distinct environment/publisher if configured. Local wheelhouse rehearsal must not require OIDC.

A manual staging run must not accidentally fall through to production PyPI if TestPyPI configuration is missing.

## Release workflow failure semantics

- version mismatch -> fail before build/publish;
- one target build missing -> no partial production release by default;
- wheel smoke failure -> no publish;
- manifest/hash mismatch -> no publish;
- attestation/OIDC failure -> publish job fails, no token fallback;
- PyPI partial upload -> stop, record immutable partial state and follow PyPI recovery/yank/add-file rules; do not overwrite files;
- GitHub partial asset upload -> reconcile against manifest before marking release complete;
- historical backfill filename already used -> fail, never overwrite.

K011 owns the actual production-release rollback/yank decision tree.

## Secret isolation

Release build jobs must not receive:

- provider API keys;
- EggPool runtime/server API keys;
- proxy credentials;
- arbitrary repository/environment secrets.

Trusted Publishing should eliminate long-lived PyPI tokens. If GitHub release publication uses the default workflow token, scope it narrowly.

## Normal CI separation

Do not move the full release target matrix into `.github/workflows/ci.yml`. Normal PR CI remains lean. Release workflow validation can have a cheap non-publishing lint/dry-run test without building every target on every PR.

## Tests

Add deterministic workflow/config validators for:

- trigger filters;
- job permission boundaries;
- pinned actions/Maturin;
- supported target set exactly matches K001;
- production publish depends on all required artifacts;
- no sdist step;
- no secret-token PyPI credential path;
- no provider secret exposure;
- artifact manifest hash verification;
- production versus TestPyPI destination cannot be confused;
- historical backfill path requires explicit immutable source/version.

Use actionlint/yaml parsing only if already available or as bounded dev tooling; do not add a runtime dependency.

## Closure evidence

Write `migration-rs/closure/cutover/008-status.md` with:

- implementation commits;
- workflow/job/permission diagram;
- pinned actions/tool versions;
- target matrix;
- Trusted Publisher setup status and required maintainer action;
- staged dry-run/TestPyPI evidence if available;
- artifact hash/provenance checks;
- SBOM disposition;
- failure/partial-publish policy;
- unresolved operational blockers;
- registry transition.

## Acceptance criteria

K008 closes only when:

- a dedicated minimally privileged release workflow exists;
- supported target wheels/raw assets are built once and publish jobs consume exact validated bytes;
- PyPI production path uses Trusted Publishing/OIDC with no long-lived token fallback;
- release tooling/action versions are pinned;
- no unsupported wheel/sdist can be published by the normal release path;
- GitHub raw asset integrity remains compatible with O008;
- partial failure behavior is explicit and immutable-artifact-safe;
- no provider/user secrets reach release jobs;
- no unresolved high/medium supply-chain/release-authority finding remains.

Accepted K008 promotes only K009.

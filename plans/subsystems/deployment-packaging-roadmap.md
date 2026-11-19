# Deployment and Packaging Roadmap

Status: active

Long-term references:

- `plans/000-long-term-specification.md#1-product-definition`
- `plans/000-long-term-specification.md#2-end-state-invariants-normative`
- `plans/000-long-term-specification.md#3-ownership-boundaries-normative`
- `plans/002-long-term-roadmap.md#phase-4--operations-integrations-and-deployment`

Related ADRs:

- plans/adrs/ADR-0001-binary-first-quick-install-authority.md
- A new ADR is required before M011 chooses durable Eggup crates, acquisition
  transport, or update transaction ownership; it has not been drafted or
  accepted.

## 1. Purpose and ownership boundary

This subsystem owns the path from a qualified EggPool release artifact to a
safe, attributable installed command and the release/package contracts that
make that path reproducible.

Primary authorities are:

- `.github/workflows/release.yml` for immutable tag-build aggregation and
  publication;
- `scripts/build_release_artifacts.py`,
  `scripts/create_release_manifest.py`,
  `scripts/validate_release_artifacts.py`,
  `scripts/verify_published_release.py`, and related tooling for release
  identity and verification;
- `scripts/install.sh` plus `scripts/qualify_quick_installer.py` for the
  one-shot personal installer;
- `rust/src/operations/provenance.rs`,
  `rust/src/operations/catalog.rs`, and `rust/src/operations/update.rs`
  for installed-owner detection and native transition authority;
- `docs/releasing.md`, `docs/upgrading.md`,
  `docs/rust-release-deployment.md`, and `README.md` for operator-facing
  distribution contracts.

This subsystem consumes the native EggPool executable but does not own runtime
request behavior, provider transport, database semantics, server transport, or
client-integration behavior.

## 2. Work classification

### Invariants

- A current native release is installed only from a qualified supported target
  artifact.
- Unsupported OS/architecture combinations fail before mutation.
- Existing uv, pipx, pip/venv, standalone, and source-checkout ownership is not
  silently reclassified.
- Ambiguous ownership and path collisions fail closed.
- Quick install and update preserve config, database, and `.env` state.
- A fresh current-native quick install does not require Python, uv, pipx, pip,
  Cargo, or a source checkout.
- The supported proxy target matrix remains Linux x86_64, Linux aarch64, and
  macOS arm64 unless changed by a separate release decision.
- Release publication builds candidate bytes once; publish steps do not rebuild.

### Capabilities

- One-command personal installation of the latest supported native release.
- Exact-version installation/transition within the supported catalog and
  ownership constraints.
- Owner-aware native updates and rollback.
- Actionable diagnostics for unsupported, unverifiable, or ambiguous installs.

### Infrastructure

- Release manifest/checksum generation and validation.
- Raw artifact naming and target mapping.
- Deterministic disposable installer qualification.

### Polish

- Concise install output that identifies version, source authority, ownership,
  command path, and preserved config path.
- Documentation that clearly distinguishes the default raw-binary channel from
  optional PyPI/crates.io channels.

## 3. Non-goals

- Adding Windows proxy/server support.
- Reintroducing a Python runtime fallback.
- Building from source in the quick installer.
- Retiring PyPI wheels or crates.io publication.
- Changing database/config compatibility policy.
- Giving standalone Rust installs a direct historical Python-era downgrade
  mechanism.
- Reworking `eggpool-connect` desktop bootstrap behavior.
- Changing system/root production deployment ownership without a separate
  milestone.

## 4. Current state

M001 is closed at `plans/closure/deployment-packaging/001-status.md`.
Fresh current-native quick installs are binary-first: `scripts/install.sh`
selects the supported raw GitHub release asset from `SHA256SUMS`, binds
latest selection to the exact release version, verifies digest/version/native
identity, and installs a standalone Rust command without requiring Python, uv,
pipx, pip, Cargo, or a source checkout.

Existing native installations preserve ownership by delegating ordinary
transitions to `operations/update.rs`: standalone remains standalone and
uv/pipx/pip-managed installs retain their manager. Historical Python-era exact
targets remain an explicit package-manager compatibility path.

The deterministic quick-installer qualification now covers the original
Linux/aarch64 stale-pipx/no-Python regression plus checksum, target, collision,
owner, historical, lock, and rollback cases.

Post-closure review found two narrower gaps in the shell transaction boundary:
fresh `--force` could replace an unowned regular destination file when that
file was not classified as an EggPool owner, and a first-time `init-config`
failure after executable commit could return failure while leaving the newly
installed executable in place. M002
(`plans/closure/deployment-packaging/002-status.md`) corrects both: fresh
`--force` now refuses unowned destinations byte-for-byte, and config-seeding
failure rolls back the executable committed by that transaction while guarding
executable identity. Deterministic qualification is now 46 cases including the
M002 regressions.

Post-M002 review found one remaining ownership ambiguity: config rollback still
inferred that a regular final config file belongs to the installer merely because
the path was absent at preflight. An unrelated process can create that path
while `init-config` is running, after which rollback may delete operator state.
M003 corrects it: first-time config is generated only in transaction-owned
staging and published with true no-clobber (hard-link) semantics so the final
config path is never written directly and never deleted by rollback. Deterministic
qualification is now 60 cases including concurrent-writer preservation,
publish-race winner preservation, staged success/failure cleanup, symlink/special
refusal, no-clobber, signal staging cleanup, executable-rollback preservation,
and existing-owner safe staging.


The roadmap reopened on repository evidence gathered at baseline `e6822d8f78`.
Two qualification workflows still pin the EggPool release version as a literal
rather than deriving it the way `release.yml` does, so they no longer match
the version the build produces; the release workflow validator that exists for
this purpose runs only inside the release workflow, which means it never runs
in ordinary CI; the repository's `/artifacts/qualification/` ignore rule is
inert because the retained qualification artifacts are already tracked; and
the dashboard parity tooling's default inputs live under an untracked path.
The historical M001–M005 records remain closed and immutable.

## 5. Target architecture

The distribution path has one explicit dispatch boundary:

```text
curl installer
  |
  +-- no existing EggPool owner
  |     |
  |     +-- current/latest Rust target -> verified GitHub raw asset
  |     +-- explicit historical Python target -> compatibility package path
  |
  +-- verified standalone Rust
  |     -> standalone authority / native EggPool updater
  |
  +-- verified uv/pipx/pip owner
  |     -> preserve manager authority / native owner-aware updater
  |
  +-- source checkout or ambiguous owner
        -> fail closed
```

For a fresh current-native install, the shell script performs only bootstrap
work: target detection, release-sidecar resolution, bounded download, digest
verification, candidate self-check, safe filesystem placement, initial config
seeding, and concise completion output. It must not reproduce the runtime's
full update state machine.

The installed binary becomes `standalone-rust`; subsequent ordinary updates
use the native updater. Existing package-managed installations remain
package-managed.

The stable `SHA256SUMS` release asset is the shell-friendly discovery
surface. The latest path can select the unique supported raw asset entry and
derive the version without jq or Python. Exact Rust versions use the same
contract under their explicit release tag.

### Proposed Eggpack / Eggup release-update boundary (draft)

The existing release catalog and install provenance remain EggPool policy. Eggpack is a producer-side contract/build/qualification/manifest tool; Eggup is a consumer-side staged file transaction. Neither selects a release, target, origin, destination, install owner, or authenticity policy for EggPool.

Proposed mapping (candidate product IDs; confirm against the selected Eggpack contract):

| Candidate product/channel | EggPool target class | Canonical Rust triple | Existing asset shape | Consumer |
|---|---|---|---|---|
| eggpool raw | linux-x86_64 | x86_64-unknown-linux-gnu | eggpool-{version}-linux-x86_64 | Eggup direct, one member |
| eggpool raw | linux-aarch64 | aarch64-unknown-linux-gnu | eggpool-{version}-linux-aarch64 | Eggup direct, one member |
| eggpool raw | macos-arm64 | aarch64-apple-darwin | eggpool-{version}-macos-aarch64 | Eggup direct, one member |
| eggpool-wheel | linux-x86_64 | x86_64-unknown-linux-gnu | eggpool-{version}-<python>-<abi>-<manylinux platform>.whl | package managers only |
| eggpool-wheel | linux-aarch64 | aarch64-unknown-linux-gnu | eggpool-{version}-<python>-<abi>-<manylinux platform>.whl | package managers only |
| eggpool-wheel | macos-arm64 | aarch64-apple-darwin | eggpool-{version}-<python>-<abi>-macosx_11_0_arm64.whl | package managers only |
| eggpool-connect | connect-linux-x86_64 | x86_64-unknown-linux-gnu | eggpool-connect-{version}-linux-x86_64 | Separate helper product |
| eggpool-connect | connect-linux-aarch64 | aarch64-unknown-linux-gnu | eggpool-connect-{version}-linux-aarch64 | Separate helper product |
| eggpool-connect | connect-macos-arm64 | aarch64-apple-darwin | eggpool-connect-{version}-macos-aarch64 | Separate helper product |
| eggpool-connect | connect-windows-x86_64 | x86_64-pc-windows-msvc | eggpool-connect-{version}-windows-x86_64.exe | Separate helper; not a proxy target |
| connect-bootstrap | no target | — | eggpool-connect.sh and eggpool-connect.ps1 | Static assets; no proxy updater |

The raw/runtime triples and names come from scripts/inspect_release_raw.py;
the wheel platform tags come from scripts/inspect_release_wheel.py; helper
triples and filenames come from scripts/inspect_connect_artifact.py. Eggup
consumes only the three raw EggPool rows in this milestone. Wheels are
alternate installation forms, not additional members of the same transaction.
The helper and bootstrap identities stay separate from the proxy identity.

All product manifests share the release_id and source revision resolved from
EggPool's existing version authority/tag. Map aliases only for display;
consumer selection requires one exact canonical target triple, with no
nearest-target fallback. Eggpack ReleaseManifest is a distinct wire contract
from EggPool's current aggregate release-manifest.v1. Keep SHA256SUMS and the
current aggregate manifest during a parallel-parity period. Preserve build and
qualification evidence as Eggpack evidence or separate release evidence; do
not add unrecognized fields to the closed manifest schema.

Consumer flow for a standalone Rust update:

EggPool owner/provenance and ReleaseCatalog select one exact release and target; fetch that release manifest from the trusted HTTPS origin; the Eggup Eggpack adapter validates the selected product/release/target mapping; Eggup acquisition fetches the exact caller-bound asset under byte/time bounds; Eggup core stages, checks exact size and SHA-256, and validates the candidate; EggPool proves current executable ownership and commits one file under lock; EggPool verifies the live version and completes the existing service restart; finalize on success, otherwise restore and verify the prior executable.

EggPool retains restart/service-manager ownership. The eggup-service crate is not part of this integration unless a separate decision transfers that boundary.

## 6. Dependency graph

- Release raw-artifact publication -> installer M001: **interface**. The v0.8.1
  raw asset names and `SHA256SUMS` contract already exist and are validated.
- Standalone provenance/update -> installer M001: **interface**.
  `InstallProvenance::StandaloneRust` and raw self-update are already
  production behavior.
- Historical release catalog -> installer M001: **interface**. It remains the
  compatibility gate for exact Python-era requests.
- GitHub release availability -> installer M001: **operational**. Network
  failure must be cleanly recoverable and cannot partially install.
- Artifact attestations / immutable release enforcement -> future hardening:
  **soft**. Useful provenance strengthening, not required to remove Python from
  the fresh native path.
- Repository version authority -> M006: **interface**. The qualification
  workflows derive the release version from the same authority `release.yml`
  uses; a reintroduced literal fails validation in ordinary CI.
- M006–M008 are closed; M009 dependency advisory remediation is closed (plans/closure/deployment-packaging/009-status.md).
- Eggpack CLI/manifest contract -> M010: interface. Pin an immutable reviewed tool revision and prove it can represent the current release without a second, conflicting build/publish authority.
- M010 Eggpack manifest mapping -> M011 Eggup consumer: hard. The consumer must not be implemented against a guessed or unstable product/target map.
- Accepted ADR for Eggup's exact published crate graph, acquisition adapter, and update-transaction ownership -> M011: hard. The ADR must resolve the synchronous commit versus EggPool's asynchronous restart/rollback seam.
- EggPool install provenance, release catalog, service lifecycle, and SHA256SUMS installer contract -> M010/M011: interface; remain the application-owned authorities unless an accepted ADR says otherwise.

## 7. Milestones

### Milestone 1 — Binary-first quick installer and ownership cleanup

Class: capability

Objective:

Make the public curl installer use the qualified GitHub raw executable for
fresh current-native installs while preserving all existing owner-aware update
and compatibility boundaries.

Dependencies:

- Interface dependencies above are satisfied at the current repository
  baseline.
- No hard dependency is open.

Deliverable boundary:

One coherent installer/release-tooling/documentation pass covering the
binary-first fresh path, owner dispatch, deterministic qualification, and
public documentation. No proxy runtime behavior changes outside operations
provenance/update seams needed to expose or verify the existing ownership
contract.

User or operator value:

A supported SBC or desktop can install EggPool with only a POSIX shell, curl,
and a platform hash utility; an old distro Python or pipx cannot block a native
install.

Exit conditions:

- Fresh current-native Linux/aarch64 install passes with no Python and with a
  stale/incompatible pipx present.
- Linux x86_64 and macOS arm64 select only their target artifact.
- The candidate is verified before execution and committed only after
  self-check.
- Existing package-manager ownership is preserved.
- Existing standalone ownership remains standalone.
- Historical exact compatibility behavior remains explicit and bounded.
- Config/database/`.env` preservation, collision refusal, rollback behavior,
  and source-checkout refusal are regression-covered.
- Public install/update/release docs agree on the new authority.

Deferred work:

- GitHub artifact attestations and release immutability enforcement.
- System-owned/root production distribution authority changes.
- Additional proxy targets.

### Milestone 2 — Installer transaction and collision corrective

Class: invariant

Objective:

Correct the two post-M001 findings without changing the binary-first
architecture: make `--force` refuse unowned regular-file destinations and
make first-time config-seeding failure roll back the executable committed by
that fresh-install transaction.

Dependencies:

- M001 is closed and supplies the binary-first authority and qualification
  baseline.
- ADR-0001 already defines the required ownership and failed-install
  invariants.
- No hard dependency is open.

Deliverable boundary:

A narrow `scripts/install.sh` + deterministic qualification + planning/docs
corrective. Existing updater, release publication, target matrix, historical
compatibility, and production/root deployment remain unchanged.

User or operator value:

`--force` cannot destroy an unrelated file, and a failed first-time install
does not misleadingly leave a newly installed executable behind.

Exit conditions:

- unowned regular destination + `--force` fails closed byte-for-byte;
- verified standalone/package-managed repair still works without owner change;
- injected first-time `init-config` failure restores the pre-install
  executable/config state;
- rollback never deletes a destination changed by a concurrent actor;
- all M001 regressions remain green;
- roadmap/registry/closure evidence accurately records why M001 verification
  missed the two cases.

Deferred work:

- Independent artifact signing/attestation.
- Broader installer transaction framework extraction.
- System/root deployment authority changes.

### Milestone 3 — Config publication ownership corrective

Class: invariant

Objective:

Remove the remaining config-path ownership race by generating first-time config
into transaction-owned staging and publishing it with true no-clobber
semantics. Rollback must never delete the final config path.

Dependencies:

- M001 and M002 are closed and provide the binary-first installer, owner
  preservation, install locking, and guarded executable rollback.
- ADR-0001 already requires unrelated user state to be preserved.
- No hard dependency is open.

Deliverable boundary:

A narrow installer/qualification/planning corrective. Prefer one shared safe
config-staging helper for fresh and existing-owner first-time config creation.
No release, runtime, schema, target, or production/root deployment changes.

User or operator value:

A concurrent operator/process creating `config.toml` cannot have that file
deleted or overwritten by a failing installer transaction.

Exit conditions:

- fresh config generation never writes directly to the final config path;
- concurrent final config creation is preserved byte-for-byte;
- staged publication cannot overwrite a concurrent winner;
- final symlink/special-file boundaries fail closed;
- rollback removes only transaction-owned staging, never final config;
- M002 executable rollback remains intact;
- existing-owner first config uses equivalent safe semantics;
- all prior installer regressions remain green.

Deferred work:

- Independent artifact signing/attestation.
- General installer transaction framework extraction.
- System/root deployment authority changes.

### Milestone 4 — Shell command discovery and activation corrective

Class: capability (with owner/collision security invariants)

Objective:

Make an installed EggPool command discoverable in future supported interactive
shells automatically and in the already-running shell through the documented
one-line install command; repair the hidden-but-owned canonical executable
discovery gap without changing native binary-first installation authority.

Dependencies:

- Hard: M001–M003 closed, including native release qualification and guarded
  executable/config publication and rollback.
- Soft/reference: Gregg Plans 130/131 define bounded PATH profile persistence
  and a documented parent-shell activation pattern; no Gregg code change or
  downstream release dependency is required.

Deliverable boundary:

One coherent `scripts/install.sh`, disposable installer qualification,
README/install documentation, and planning corrective. Resolve PATH-hidden
canonical EggPool owners by verifiable provenance without weakening foreign
collision/`--force` refusal. Persist guarded, idempotent user-shell PATH
integration only after successful installation, with custom destination and
unsafe-profile handling; do not mutate startup files on failed installs.
Document one invocation that runs the installer and then activates its bin
directory in the **invoking** shell, since a piped child cannot export into
its parent. Keep system shell files and all Rust/runtime/protocol/storage
surfaces unchanged.

User or operator value:

macOS arm64 zsh and Linux Bash users can paste one command and invoke
`eggpool` immediately in that shell; future shells also resolve it without
manual PATH configuration. A hidden earlier install is repaired/updated as a
verified owner rather than rejected as a fresh unowned collision.

Exit conditions:

- Native macOS zsh and Linux Bash shells resolve the CLI immediately after
  the documented parent-shell one-liner and in subsequent sessions.
- Bare pipeline semantics are described truthfully: persist future-shell
  config, but no impossible parent-process mutation guarantee.
- Managed startup integration is append-only, idempotent, fails safely on
  symlink/special/unwritable targets and reuses working Gregg/user PATH
  entries without duplication; opt-out and custom bin location are covered.
- An off-PATH canonical verified EggPool owner is recognized; hidden unowned
  files and visible conflicting executables still fail closed.
- All M001–M003 checksum, provenance, no-clobber, rollback, and manager
  invariants remain green; deterministic and real shell startup evidence
  are recorded.

Implementation plan:

- `plans/archive/implementation/deployment-packaging/004-shell-command-discovery-and-activation-corrective.md`

Deferred work:

- A universal shell integration mechanism (fish/nu/PowerShell).
- Privileged/system-scope PATH changes and a general installer transaction
  abstraction.
- Changes to Gregg's completed Plans 130/131 without a demonstrated
  separate defect.

### Milestone 5 — Active PATH integration detection corrective

Class: polish (bounded detector correctness; preserves profile/installer safety invariants)

Objective:

Prevent an unnecessary EggPool-managed profile append when a **functioning
preexisting Gregg guarded PATH block** or **zsh tied `path=(...)` array**
already exposes the verified installed directory.

Dependencies:

- Hard: M004 closed at `plans/closure/deployment-packaging/004-status.md`;
  73-case installer harness, Linux Bash and macOS arm64 zsh shell qualification
  and native parent-shell activation are accepted.
- Gregg Plans 130/131 are **source-backed fixture references**, not executable
  sibling dependencies. No pending interface or upstream change.

Deliverable boundary:

Correct only `scripts/install.sh` `profile_has_active_bin()` and the
disposable qualification coverage in `scripts/qualify_quick_installer.py`,
plus focused comments/docs/planning if needed. Recognize complete working
Gregg `case` guards and real zsh `path=(...)` assignments with static
read-only validation of the actual installed directory, not broad text or
malformed syntax. Preserve profiles byte-for-byte when already integrated;
conservatively append the existing safe guarded EggPool block when not.

The M004 ownership/provenance, hidden canonical owner classification,
collision/`--force` refusal, checksum/release, success-only profile edits,
no-clobber config/rollback, explicit opt-out, custom bin location, and
parent-shell activation contract are unchanged. No Rust/runtime, database,
protocol, release-target, or new CI workflow changes.

Exit conditions:

- Gregg-managed functional guard and zsh array fixtures (with real zsh
  startup proof where available) yield `already active` without profile edit.
- Comment-only, disabled/incomplete, unrelated, quoted-tilde, subpath and
  wrong-directory cases never falsely suppress a needed append.
- Existing EggPool-managed and normal Bash PATH forms remain recognized;
  rerun is idempotent and the baseline 73 cases stay green.
- Existing ordinary CI and native shell qualification are recorded, with a
  new `plans/closure/deployment-packaging/005-status.md` before closure.

Implementation plan:

- `plans/archive/implementation/deployment-packaging/005-active-path-integration-detection-corrective.md`

Deferred work:

- Arbitrary shell parsing or evaluating profiles.
- Broad installer, manager, update, runtime, or Gregg changes.

### Milestone 6 — Release artifact version derivation and repository artifact hygiene

Class: polish

Objective: derive the release artifact version in the qualification workflows
from the repository's version authority instead of a literal, run the release
workflow validator in ordinary CI, and reconcile the repository's ignored
paths with the artifact and fixture files it actually tracks.

Dependencies: none. Every defect is a literal-versus-derived mismatch or an
ignore-rule-versus-tracked-state mismatch.

Deliverable boundary: no release-related workflow contains a hardcoded
EggPool version literal; `scripts/validate_release_workflow.py` runs in
`.github/workflows/ci.yml` and covers the qualification workflows; no path in
the repository is simultaneously ignored and tracked; the dashboard parity
tooling's default inputs exist in a clean clone. The release artifact
format, the publication gate, and the installed-owner detection are
unchanged.

Exit conditions: the derivation matches `rust/Cargo.toml`; a reintroduced
literal fails validation in ordinary CI; the retained qualification artifacts
still resolve for the closure records that cite them.

Deferred work: deleting the retained qualification artifacts, which the
closure records need as evidence.

### Milestone 7 — Release action-pin enforcement and guard correction

Class: invariant

Objective:

Hold the qualification workflows to the immutable-SHA action-pin rule the
release workflow already follows, and make the milestone's own guards assert
the properties its closure record claimed for them.

Dependencies: none. Every defect is an unenforced rule or a guard that does
not read the repository it claims to describe.

Deliverable boundary:

- Every `uses:` in a covered workflow is pinned to an immutable SHA.
- The release workflow validator enforces the pin rule on the qualification
  contract as well as on the release contract.
- The version-glob guard reads the covered workflows, the release catalog, and
  the Cargo version instead of comparing hardcoded strings.
- The artifact-hygiene guard derives the retained evidence set from the
  ignore rule rather than from a hardcoded list.

User or operator value:

A release workflow cannot be changed to pull an action by a mutable tag
without the ordinary CI validator failing.

Exit conditions:

- A validator test reintroduces a tag-style pin into each covered
  qualification workflow and asserts the validator rejects it.
- A validator test removes the derived glob from a build job's artifact
  selection and asserts the guard fails.
- The tracked artifact corpus is unchanged in count and no path is
  simultaneously tracked and ignored.

Deferred work:

- Making generated run output inside a retained evidence tree ignored. That
  needs a per-tree ignore rule and is an explicit operator decision.

### Milestone 8 — CI dependency-lock discipline

Class: invariant

Objective:

Make the CI environment be built from the committed lockfile, or fail — and
never silently re-resolve.

Dependencies:

- None. The change is to the command that installs the Python environment, not
  to the dependencies it selects.

Deliverable boundary:

- The four CI gates that install the project environment with
  `uv sync --frozen` use `uv sync --locked` instead.
- The release-docs guard test runs its validator under the test suite's own
  interpreter instead of shelling out through `uv`.
- A guard keeps the workflows on the flag that detects lock drift.
- `uv.lock` and `pyproject.toml` are unchanged. The release artifact, the
  publication gate, the action pins, and the release version derivation are
  unchanged.

User or operator value:

A `pyproject.toml` that has drifted from the committed lockfile fails the CI
job at the environment-install step, with a message naming the remedy, instead
of the first `uv run` silently re-resolving and rewriting the lockfile so the
job qualifies a resolution the repository never committed.

Exit conditions:

- A drifted lockfile is detected by the gate, demonstrated in a sandbox.
- No workflow installs the project environment with `uv sync --frozen`.
- A guard fails if any of the four gates regresses to `--frozen`.
- The committed `uv.lock` and `pyproject.toml` are byte-identical before and
  after the change.
- The tooling suite, the linters, and the release-documentation validator pass.

Deferred work:

- Adding a lock flag to the repository's `uv run` invocations. The gate in
  front of them already guarantees the lockfile is current, so each of them
  installs the committed resolution.

### Milestone 10 — Eggpack release contract and manifest producer

Class: infrastructure

Objective:

Add a reproducible Eggpack producer contract for EggPool releases and prove a manifest mapping for raw executable targets without changing the installed updater or publication authority.

Dependencies:

- M009 is closed.
- Review the Eggpack ReleaseManifest v1 contract and pin an immutable CLI revision.
- Map the existing Maturin wheel/raw derivation, eggpool-connect Cargo artifacts, and static bootstraps. If Eggpack cannot consume the already-qualified outputs, stop rather than silently rebuilding or replacing the current build-once release path.

Deliverable boundary:

A producer-side contract, policy/bindings, local scratch qualification, and parallel manifest output. The existing release workflow remains the production publication authority; the existing updater, SHA256SUMS, wheel distribution, helper artifacts, and current aggregate manifest continue to work unchanged. No runtime Eggup dependency is added in this milestone.

User or operator value:

Release bytes gain a schema-checked, target-specific manifest and reproducible qualification evidence without interrupting current installation/update paths.

Exit conditions:

- Raw runtime targets map exactly to supported canonical triples; product/release/source identities derive from current version authority and the tag commit.
- Per-target filename, size, and SHA-256 agree with finalized raw bytes; no alias/target ambiguity or unsupported extra asset is accepted.
- Wheels remain alternative distributions, not members of the raw update set; eggpool-connect and its bootstraps remain distinct from proxy artifacts.
- Eggpack output and qualification evidence match the existing release inventory and release validators; aggregation/publication never rebuilds artifact bytes.
- Generated workflow checks preserve immutable action pins, least privilege, publication separation, and existing release rollback gates.
- Existing SHA256SUMS consumers and the current aggregate manifest pass unchanged during the parallel period.
- Every published pre-Eggpack native updater (currently tags v0.8.0–v0.8.3) resolves a newer Eggpack-enabled release through the unchanged GitHub Release API contract: `tag_name`, exact raw filename, `browser_download_url`, `digest`, and `size`. Additional Eggpack manifest assets do not shadow raw-asset selection. Prove this with tagged-version fixtures and a complete disposable standalone update; never use an operator installation or production data.
- The published-release verifier recognizes the explicitly named Eggpack manifest asset while remaining fail-closed for unrelated assets; retain existing raw/helper names and GitHub digest metadata.

Deferred work:

- Eggup runtime integration (M011).
- Signature or provenance authentication. M006 remains a separate decision; SHA-256 remains integrity evidence only.
- Windows proxy support, root/system deployment changes, and manager-owner migration.

### Milestone 11 — Eggup staged standalone update and rollback

Class: capability

Objective:

Use Eggpack manifest inputs and Eggup's staged transaction for the existing standalone Rust self-update path while preserving EggPool release selection, owner detection, package-manager routes, service control, and recovery behavior.

Dependencies:

- M010 is closed and its product/release/canonical-target mapping is stable.
- A separate ADR is accepted for exact Eggup crate versions, the acquisition/manifest adapters, and the update-transaction ownership boundary.
- A bounded technical proof demonstrates that Eggup can retain rollback evidence through EggPool's asynchronous post-replacement validation/restart without blocking or nesting the current-thread Tokio runtime. If not, use an approved upstream async-compatible seam or retain EggPool's replacement owner.
- Published crate archives, exact-pin compatibility, advisories, feature/license/source policy, and default/no-default resolution are audited before selection. Do not rely on a moving branch or assume newer transitive fixes are picked up.

Deliverable boundary:

One direct raw executable member for StandaloneRust only. EggPool continues to select latest/exact versions through its catalog/release policy and continues to dispatch uv/pipx/pip owners to their existing managers. EggPool keeps restart/service-manager ownership; no Eggup service crate is added. Archive extraction and helper self-update are out of scope.

User or operator value:

Standalone updates use a reusable, auditable staged transaction while retaining the current promise that failed validation or restart restores the previous working binary or reports concrete recovery evidence.

Exit conditions:

- The caller selects one catalogued release and exact supported target before acquisition; manifest parsing cannot select an origin, version, destination, or fallback target.
- Only HTTPS release origins and exact caller-bound asset URLs are used; redirects cannot escape the approved origin policy.
- Staging enforces exact artifact size and SHA-256, then validates executable version and target before any live mutation.
- Eggup re-proves exact current executable identity/ownership under its mutation lock. Unknown/foreign state, malformed lock state, or changed identity fails closed.
- Restart/validation success finalizes the transaction; every failure before finalization restores and verifies the old executable. RecoveryRequired retains real backup/lock evidence and is never reported as success.
- Lock contention, stale-lock recovery, cancellation, process restart, and concurrent update attempts have deterministic tests; no timeout/sleep guess is used as a synchronization proof.
- Existing StandaloneRust, uv, pipx, pip/venv, historical exact-version, ambiguous-owner, and source-checkout behavior remains regression-covered.
- Disposable supported-host qualification proves successful update and failure/rollback without touching production paths or data.

Deferred work:

- Updating eggpool-connect, changing package-manager owners, or changing the supported proxy target matrix.
- M006 signing authority, key custody, and signature verification.
- Any change to managed service deployment authority.

## 8. Cross-cutting requirements

Storage/migration: no schema or data migration. Installer state/lock/temp files
must live outside config/database content and be removed or left as bounded
recovery evidence.

Protocol/compatibility: the one-shot install command remains stable. Direct uv,
pipx, and cargo installation remain supported explicit alternatives.

Security/auth: release downloads use HTTPS; checksums are validated before
execution; unexpected names/digests/targets fail closed. No credentials are
read or emitted by the installer.

Concurrency/recovery: concurrent installers for the same user/path must not
silently interleave. Commit must be same-filesystem atomic where practical.
Failed download/verification/self-check leaves the prior command untouched;
failed replacement of a known standalone install restores its rollback copy or
delegates to the native updater's rollback semantics.

Observability: output identifies selected target, release version, resulting
ownership class, command path, and config path without leaking environment or
credentials.

Performance: installer work is bounded by the release artifact size and must
not clone/build the repository or provision a Python environment for current
native installs.

Docs/ops: deployment docs must distinguish personal quick-install ownership
from explicit system-owned production deployment.

Release compatibility: eggpack-manifest is one-product release evidence; the
current EggPool manifest is an aggregate inventory. Maintain a tested
projection/aggregate boundary, preserve additional evidence outside the closed
Eggpack schema, and keep legacy consumers working until an explicit cutover.
Never model alternative wheel/raw install channels as one transactional bundle.

Transaction/restart: Eggup's local commit API is synchronous while EggPool's
restart callback is asynchronous. Do not use nested block_on, block the
current-thread runtime, or discard the backup before restart acceptance. If a
supported composition cannot preserve rollback through restart, stop and keep
the current EggPool updater as authority pending an upstream/API decision.

Trust: Eggpack/Eggup SHA-256 values prove byte integrity only. This integration
must not claim publisher authenticity, signed provenance, or solve security
hardening M006.

## 9. Verification strategy

Deterministic tests use fake curl/release fixtures and temporary home/bin/state
roots. They must cover success and failure without touching real package
managers, the network, or contributor configuration.

Focused tooling qualification:

```bash
uv run python scripts/qualify_quick_installer.py
uv run pytest tests/tooling/test_installer.py -q
uv run pytest tests/tooling/test_release_artifacts.py tests/tooling/test_release_packaging.py tests/tooling/test_release_supply_chain.py -q
uv run python scripts/check_release_catalog.py
uv run python scripts/validate_release_workflow.py .github/workflows/release.yml
uv run python scripts/validate_release_docs.py
```

Any Rust operations change additionally runs the focused provenance/update
tests and full serial workspace/no-default verification required by the
development skill.

For M010, exercise Eggpack in a disposable release checkout and compare its
manifest/qualification output with current version, target, asset and checksum
validators. Run the generated-workflow drift check; native target qualification
must run on a matching supported host. Keep artifact creation separate from
aggregation so publication cannot rebuild.

For M011, add offline manifest/acquisition fixtures for valid and malformed
identities, wrong target, wrong size/digest, origin/redirect violation, owner
change, lock contention, candidate validation failure, restart failure,
rollback failure, cancellation, and retained-recovery evidence. Run the focused
operations::update tests, release-artifact tooling tests, default and
no-default dependency checks, the serial workspace suite, strict Clippy,
cargo deny, and the locked release build. Live smoke qualification is limited
to disposable supported hosts and isolated install/data/service paths.

A physical Le Potato or comparable Linux/aarch64 host is valuable operational
evidence for closure but is not required to make deterministic host tests pass.

## 10. Risks and decision points

- Shell JSON parsing would recreate unnecessary fragility; prefer the stable
  checksum sidecar contract.
- Reimplementing the Rust updater in shell would duplicate restart/rollback
  logic; existing native installs should delegate whenever possible.
- An installation target under a manager-owned shim must never be overwritten
  as if it were standalone.
- Historical Python compatibility can reintroduce interpreter requirements only
  when explicitly requested; it must not become a fallback for native install
  failure.
- Attestation verification in the default shell path would add external tool
  requirements. Treat it as separate hardening unless a dependency-free
  verification design is established.
- A production/root binary authority change crosses a separate operational
  ownership boundary and must not hitchhike on M001.
- Eggpack's producer pipeline may not reproduce the existing Maturin-derived
  wheel/raw pair or qualification evidence. A second build can break byte
  identity and the release build-once invariant; prove an ingest/handoff path
  before changing release publication.
- The custom EggPool aggregate manifest and Eggpack's one-product manifest are
  not wire-compatible. Define product identities for raw runtime, wheel
  distribution, helper binaries, and bootstraps without claiming the helper is
  a proxy target.
- Eggup's manifest adapter does not choose latest/exact releases or authorize
  URLs/roots. Keep those policies in EggPool and bind them explicitly.
- Eggup's synchronous transaction/post-commit API may not safely encompass
  EggPool's async service restart. This is a hard stop, not an invitation to
  block the runtime or weaken rollback.
- Eggup adapters use exact crate pins and source/registry states can diverge.
  Audit the exact published crate graph and available fixes before choosing
  versions; never patch around advisories with deny exceptions.
- Neither tool adds authenticity. M006's signature-authority work remains
  separate and is not implied by manifest adoption.

## 11. Completion definition

The roadmap may close when the default fresh native quick installer, existing
ownership transitions, release verification, deterministic qualification, and
operator documentation have one coherent authority with no Python dependency
on the normal Rust path and no medium-or-higher unresolved correctness or
security findings; and when every release-related workflow derives the release
version from the repository authority, the release workflow contract is checked
in ordinary CI, the repository's ignore rules and tracked state agree, and the
CI environment is built from the committed lockfile or the job fails.

If M010/M011 are approved, closure additionally requires Eggpack manifests to
match finalized supported release bytes and existing distribution channels,
and the Eggup consumer to preserve owner-aware selection, staged validation,
rollback-through-restart, recovery receipts, and legacy install/update
compatibility. The roadmap remains open if the Eggup async/restart seam or any
medium-or-higher integrity/ownership finding is unresolved.


## 12. Milestone status

| Milestone | Status | Implementation plan | Closure record | Blockers |
|---|---|---|---|---|
| 1 | closed | `plans/implementation/deployment-packaging/001-binary-first-quick-installer.md` | `plans/closure/deployment-packaging/001-status.md` | none |
| 2 | closed | `plans/implementation/deployment-packaging/002-installer-transaction-and-collision-corrective.md` | `plans/closure/deployment-packaging/002-status.md` | none |
| 3 | closed | `plans/implementation/deployment-packaging/003-config-publication-ownership-corrective.md` | `plans/closure/deployment-packaging/003-status.md` | none |
| 4 | closed | `plans/archive/implementation/deployment-packaging/004-shell-command-discovery-and-activation-corrective.md` | `plans/closure/deployment-packaging/004-status.md` | none |
| 5 | closed | `plans/archive/implementation/deployment-packaging/005-active-path-integration-detection-corrective.md` | `plans/closure/deployment-packaging/005-status.md` | none; M004 closed |
| 6 | closed | `plans/implementation/deployment-packaging/006-release-artifact-version-derivation-and-artifact-hygiene.md` | `plans/closure/deployment-packaging/006-status.md` | none; closed through M007 |
| 7 | closed | `plans/implementation/deployment-packaging/007-release-action-pin-enforcement-and-guard-correction.md` | `plans/closure/deployment-packaging/007-status.md` | none |
| 8 | closed | `plans/implementation/deployment-packaging/008-ci-dependency-lock-discipline.md` | `plans/closure/deployment-packaging/008-status.md` | none |
| 9 | closed | `plans/implementation/deployment-packaging/009-dependency-advisory-remediation.md` | `plans/closure/deployment-packaging/009-status.md` | RustSec audit, default/no-default serial suites and locked release build passed |
| 10 | proposed | — | — | Eggpack contract/tool revision and artifact-ingest compatibility proof; no release authority change before review |
| 11 | blocked | — | — | M010 closed; accepted ADR for Eggup crate/API/ownership choice; sync commit must preserve async restart rollback |

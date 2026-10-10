# Deployment and Packaging Roadmap

Status: active

Long-term references:

- `plans/000-long-term-specification.md#1-product-definition`
- `plans/000-long-term-specification.md#2-end-state-invariants-normative`
- `plans/000-long-term-specification.md#3-ownership-boundaries-normative`
- `plans/002-long-term-roadmap.md#phase-4--operations-integrations-and-deployment`

Related ADRs:

- `plans/adrs/ADR-0001-binary-first-quick-install-authority.md`

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

- `plans/implementation/deployment-packaging/005-active-path-integration-detection-corrective.md`

Deferred work:

- Arbitrary shell parsing or evaluating profiles.
- Broad installer, manager, update, runtime, or Gregg changes.

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

## 11. Completion definition

The roadmap may close when the default fresh native quick installer, existing
ownership transitions, release verification, deterministic qualification, and
operator documentation have one coherent authority with no Python dependency
on the normal Rust path and no medium-or-higher unresolved correctness or
security findings.

## 12. Milestone status

| Milestone | Status | Implementation plan | Closure record | Blockers |
|---|---|---|---|---|
| 1 | closed | `plans/implementation/deployment-packaging/001-binary-first-quick-installer.md` | `plans/closure/deployment-packaging/001-status.md` | none |
| 2 | closed | `plans/implementation/deployment-packaging/002-installer-transaction-and-collision-corrective.md` | `plans/closure/deployment-packaging/002-status.md` | none |
| 3 | closed | `plans/implementation/deployment-packaging/003-config-publication-ownership-corrective.md` | `plans/closure/deployment-packaging/003-status.md` | none |
| 4 | closed | `plans/archive/implementation/deployment-packaging/004-shell-command-discovery-and-activation-corrective.md` | `plans/closure/deployment-packaging/004-status.md` | none |
| 5 | ready | `plans/implementation/deployment-packaging/005-active-path-integration-detection-corrective.md` | pending `plans/closure/deployment-packaging/005-status.md` | none; M004 closed |

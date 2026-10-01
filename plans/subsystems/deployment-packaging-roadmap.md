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

The v0.8.1 release pipeline already publishes matching raw EggPool executables
for Linux x86_64, Linux aarch64, and macOS arm64 alongside a stable
`SHA256SUMS` asset and versioned release manifest. Post-publication validation
checks GitHub asset digests against the aggregated release metadata.

The Rust runtime already distinguishes package-managed and standalone
installations. `operations/update.rs` can resolve GitHub raw release assets,
verify release-provided SHA-256 evidence, stage/self-check the candidate,
replace a standalone executable, and attempt rollback/restart recovery.

The mismatch is the fresh quick installer. `scripts/install.sh` currently
chooses uv when present, otherwise pipx when present, and bootstraps uv only
when neither is available. The native wheel declares `Requires-Python >=3.11`
for package-manager compatibility. Consequently a supported Linux/aarch64 host
with an old pipx interpreter can fail package resolution before EggPool's native
binary is installed.

The qualification harness models manager invocation but does not model an
incompatible pipx interpreter, so it does not catch that class of SBC failure.

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

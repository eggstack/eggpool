# Security Hardening Milestone 006 — Release artifact signature authority

Status: blocked

Repository baseline: `dcbd820e9b978d47bd43d237eded1f488d3449ee`

Source roadmap:

- `plans/subsystems/security-hardening-roadmap.md#milestone-6--release-artifact-signature-authority`

Long-term requirements:

- `plans/000-long-term-specification.md#2-end-state-invariants-normative` —
  item 9, tooling is not a runtime fallback; a release artifact must be
  verifiable before execution
- `plans/002-long-term-roadmap.md#phase-4--operations-integrations-and-deployment`

Applicable ADRs:

- `plans/adrs/ADR-0001-binary-first-quick-install-authority.md` — binary-first
  install, checksum verification, dependency-light installer, and atomic
  staging remain authoritative.
- A new ADR (the next global number) is a hard dependency. It must decide the
  signature mechanism, key custody, rotation, the installer prerequisite
  question, and the unsigned-transition rule.
- Stop before implementation if the ADR is not accepted.

Primary class: capability

## 1. Objective

Make release artifacts verifiable independently of the HTTPS origin that
served them, so a compromised GitHub account, release, or TLS chain alone is
not enough to install a different binary — without weakening ADR-0001's
dependency-light installer.

## 2. Why this milestone is ready

Not ready at this baseline; the milestone is blocked on the signature-authority
ADR named in §14. The evidence below is why the plan is registered rather than
handed off.

`eggpool update` verifies a SHA-256 digest resolved from the GitHub release API
(`rust/src/operations/update.rs`); `scripts/install.sh` verifies a checksum
sidecar from the same origin. Both trust the origin. An independent signature
is the missing authority.

Choosing the mechanism is an architecture decision, not an implementation
detail:

- The installer must remain dependency-light (ADR-0001). A Rust-only verifier
  solves `eggpool update` but not the initial shell install.
- A shell verifier implies an external tool (`minisign`, `openssl`, or
  `ssh-keygen -Y`), which changes the installer's prerequisites.
- Sigstore keyless verification adds network and identity dependencies and a
  different failure model.
- Key custody (offline signing key, release environment secret, or hardware
  token) is a maintainer operational decision.

Until the ADR is accepted, this plan defines the boundary and the evidence; it
does not prescribe a winner.

## 3. Current implementation evidence

- `rust/src/operations/update.rs` — `DEFAULT_RELEASE_API` is the GitHub
  releases API; the updater stages a downloaded artifact and compares its
  SHA-256 with the release-provided digest before replacement.
- `scripts/install.sh` — downloads the raw asset and its checksum sidecar over
  HTTPS, validates the sidecar shape, and refuses a mismatch. It also
  bootstraps `uv` through a remote shell script on the explicit package-manager
  path.
- `.github/workflows/release.yml` — builds artifacts once, aggregates a
  versioned manifest, writes `SHA256SUMS`, and publishes through separate
  privileged jobs. Actions are SHA-pinned; publish jobs use OIDC with
  environment-scoped `id-token: write`.
- `packaging/release/release-manifest.json` records SHA-256 digests for raw,
  wheel, and connect artifacts.
- `docs/upgrading.md` and `docs/deployment.md` describe checksum verification
  only.

## 4. Invariants that must not regress

- ADR-0001's binary-first fresh install, ownership-class detection, atomic
  staging, rollback copy, and no source build remain unchanged.
- A checksum mismatch is still a hard refusal; signature verification is
  additive and never lowers the existing bar.
- The installer still needs no Python, Rust, Cargo, jq, or source checkout.
  If the ADR admits a new verifier tool, its absence must have an explicit,
  documented behavior (see the ADR), never a silent downgrade presented as
  verified.
- The updater verifies before staging, not after replacement.
- No secret key enters the repository, the build logs, or an artifact.
- Release job permissions and the OIDC trusted-publishing path are unchanged
  except for the minimal additional privilege the chosen signing method needs.

## 5. Scope

### In scope (after the ADR is accepted)

- A detached signature over `SHA256SUMS` or over each artifact, as the ADR
  decides.
- Signing in a dedicated release job with the key material supplied by the
  release environment.
- Verification in `rust/src/operations/update.rs` before staging.
- Verification in `scripts/install.sh`, with the ADR-defined behavior when the
  verifier is unavailable.
- Public-key distribution: embedded in the binary and the installer source,
  identified by key id and rotation epoch.
- Failure-path tests and operator documentation.

### Explicitly out of scope

- Changing which artifacts exist, their names, or their target matrix.
- Signing Windows helper binaries beyond the existing artifact set unless the
  ADR includes them.
- Transparency-log or attestation infrastructure.
- Rotating the existing repository or PyPI credentials.

## 6. Required production changes

After the ADR is accepted:

- Release workflow: a signing job or step after the aggregate manifest, with
  the signature published beside `SHA256SUMS`; no signature is produced from
  an unvalidated bundle.
- `rust/src/operations/update.rs`: parse the published signature, verify with
  the embedded public key, and refuse before staging on absence, mismatch, or
  an unknown key id. Reuse the existing error taxonomy; do not add a new
  retry path.
- `scripts/install.sh`: verify with the ADR-selected tool; fail closed on a
  present-but-invalid signature; follow the ADR's explicit rule for an absent
  verifier.
- `rust/Cargo.toml`/`Cargo.lock` only if the ADR selects a Rust verification
  crate; that selection is itself the ADR's dependency decision.
- No schema or configuration change. A key id may be a compiled constant.

## 7. Ordered work packages

### Work package A — Decision (hard dependency)

Intent: settle mechanism, custody, rotation, and installer behavior.

Required changes: write and accept the ADR before code. Record the rejected
options and why, including the installer prerequisite each implies.

Acceptance evidence: an accepted ADR referenced from this plan and the
roadmap.

### Work package B — Release signing

Intent: publish a signature that is reproducible and independent of the
artifact bytes' origin.

Required changes: sign the manifest/digest file in a job that cannot alter the
validated bundle; publish the signature with the exact asset name the clients
expect; keep the key out of logs.

Acceptance evidence: a rehearsal release produces a signature that verifies
locally; a modified bundle fails verification.

### Work package C — Runtime updater verification

Intent: refuse a tampered update before it replaces the executable.

Required changes: verify the signature after download and before staging;
map every failure to a typed updater error; keep the existing checksum check.

Acceptance evidence: a tampered artifact and a tampered signature each fail
with a typed error and no executable replacement; a valid signature updates.

### Work package D — Installer verification and docs

Intent: make a fresh install verifiable under the ADR's constraints.

Required changes: implement the ADR-selected verification; keep the checksum
check; document key rotation and the failure messages; update the release
manifest if the ADR adds a signature record.

Acceptance evidence: installer fixtures for valid, missing, and invalid
signature behavior; docs validation; a disposable-host install.

## 8. Failure, cancellation, restart, contention semantics

Verification is a pure operation over downloaded bytes and a public key. A
failure aborts before any filesystem mutation; the staged temporary file is
removed. Interrupted downloads are retried by the existing updater logic, not
by the verifier. Concurrent updates are already serialized by the update lock.

## 9. Compatibility and migration

Existing releases without a signature follow the ADR's transition rule. The
updater must not treat an old release as verified; it may keep the existing
checksum-only path only where the ADR explicitly allows it, and the operator
must see which authority was used. No database or config migration.

## 10. Required tests

- Unit — signature parse, key-id selection, malformed signature, wrong key,
  truncated file.
- Updater integration — valid signature installs; modified artifact refuses;
  modified signature refuses; missing signature follows the ADR rule.
- Installer — fixture server for valid/missing/invalid/oversized signature;
  no execution of an unverified candidate.
- Negative — a signature from a rotated-out key is refused with a rotation
  message.
- Release rehearsal on the supported target matrix.

## 11. Required verification commands

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --test operations_o006 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test operations_o007 -- --test-threads=1
uv run python scripts/qualify_quick_installer.py
uv run python scripts/validate_release_workflow.py .github/workflows/release.yml
uv run pytest tests/tooling/test_installer.py tests/tooling/test_release_supply_chain.py -q
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
```

The exact updater/installer targets are confirmed against the repository at
implementation time; the two `operations_o00*` targets above are the existing
update/install coverage and may be adjusted without weakening the evidence.

## 12. Documentation updates

- `docs/upgrading.md`, `docs/deployment.md`, `docs/releasing.md` — signature
  verification, key rotation, failure behavior.
- `architecture/deep-dive-deployment.md` — verification authority and the
  pre-staging boundary.
- `SECURITY.md` (from M002) — how to report a signature or key problem.
- `plans/registry.md` — blocked-to-ready and closure transitions.

## 13. Acceptance criteria

- A published release carries a signature that verifies against the embedded
  public key.
- A byte-modified artifact or signature is refused before staging by both the
  installer and `eggpool update`; the installed executable is unchanged.
- A missing verifier follows the ADR exactly and the operator can tell which
  authority was used.
- Key rotation is documented and an old-key signature fails with a clear
  message.
- No signing key appears in the repository, logs, or artifacts.

## 14. Stop conditions

Stop and report if:

- the ADR is not accepted;
- the chosen mechanism requires a runtime dependency that ADR-0001 forbids and
  the ADR has not amended it;
- the installer would need to execute an unverified verifier;
- key custody cannot be established without exposing the key to a job that
  also builds or uploads artifacts.

## 15. Closure evidence required

- The accepted ADR.
- A rehearsal release with a valid and an invalid signature.
- Updater and installer refusal evidence with the installed executable
  unchanged.
- The key-rotation exercise.
- A statement that checksum verification, artifact names, and ownership
  classes are unchanged.

## 16. Handoff notes

Do not start with code. The ADR is the gate. If the ADR selects a shell
verifier, quantify the prerequisite against the supported targets before
accepting it; a tool that is absent on a supported SBC makes the fresh install
unverifiable, not secure.

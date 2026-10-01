# ADR-0001: Binary-first quick-install authority

Status: accepted

Date: 2026-10-01

Decision owners: project maintainers

Related specification sections:

- `plans/000-long-term-specification.md#1-product-definition`
- `plans/000-long-term-specification.md#2-end-state-invariants-normative`
- `plans/000-long-term-specification.md#3-ownership-boundaries-normative`
- `plans/000-long-term-specification.md#6-non-goals`
- `plans/002-long-term-roadmap.md#phase-4--operations-integrations-and-deployment`

Affected subsystem roadmaps:

- `plans/subsystems/deployment-packaging-roadmap.md`

## Context

EggPool's production runtime is native Rust, but the current one-shot installer still
routes a fresh install through a Python package manager. It prefers an already
available `uv`, then an already available `pipx`, and otherwise bootstraps
`uv`. The current Rust wheel declares `Requires-Python >=3.11` for
package-manager compatibility even though the installed EggPool process neither
imports nor requires Python.

That distinction is observable on older SBC distributions. A host with an
existing `pipx` backed by Python older than 3.11 can pass EggPool's supported
Linux/aarch64 platform gate and still fail before the native executable is
installed because pip filters every published EggPool release by
`Requires-Python`.

The release pipeline already publishes one qualified raw EggPool executable for
each supported proxy target, plus `SHA256SUMS` and the versioned release
manifest. The Rust runtime already has a standalone ownership class and a
GitHub-release update path in `rust/src/operations/update.rs` that resolves
supported raw assets, checks release-provided SHA-256 evidence, stages and
self-checks the candidate, replaces the executable, and attempts rollback on
post-mutation failure.

The installer therefore has two authorities for the same native runtime:
Python package management for initial quick install, and GitHub raw assets for
standalone updates. That split is unnecessary for fresh current-native installs.

## Decision drivers

- A supported native install must not require a compatible Python interpreter.
- Linux/aarch64 SBCs must follow the same supported artifact contract as other
  release targets rather than a distro-dependent pipx interpreter contract.
- Initial install and subsequent standalone updates should use one release
  authority and one artifact identity.
- Existing uv, pipx, pip/venv, source-checkout, and standalone ownership must not
  be silently reclassified.
- Config, database, `.env`, deployment, and service state must remain outside
  installer replacement semantics except where the existing native updater
  already owns restart/rollback behavior.
- The quick installer must remain dependency-light and must not require jq,
  Python, Rust, or a source checkout.
- Historical Python-era compatibility must remain explicit and bounded rather
  than forcing Python into the normal Rust installation path.

## Considered options

### Option A — Keep package-manager-first installation and improve Python checks

The installer could probe the interpreter behind uv/pipx/pip, reject old
versions earlier, or force pipx to use a newer Python.

This improves diagnostics but preserves Python as a prerequisite for a native
binary and leaves behavior dependent on package-manager and distro versions.

### Option B — Always bootstrap uv for fresh installs

The installer could ignore an old pipx and bootstrap uv, relying on uv's Python
management to satisfy the wheel metadata.

This fixes the observed failure but still downloads and manages a Python
environment solely to install a Rust executable. It also leaves initial install
and standalone self-update on different authorities.

### Option C — Install verified GitHub raw binaries by default

Fresh installs of current Rust-era releases download the already-qualified raw
asset for the detected supported target, verify it before execution, install it
as a standalone Rust binary, and let EggPool's existing native update machinery
remain its update authority.

Existing package-managed installations retain their package manager. Historical
Python-era transitions remain an explicit compatibility path.

### Option D — Retire PyPI and crates.io distribution

This would reduce channel count but would break supported explicit installation
and developer workflows without solving a requirement of the quick installer.

## Decision

Select Option C.

The public `curl .../scripts/install.sh | bash` path is binary-first for a
fresh current-native installation.

For Linux x86_64, Linux aarch64, and macOS arm64, the installer resolves the
matching versioned `eggpool-<version>-<os>-<arch>` GitHub release asset from
the stable `SHA256SUMS` release sidecar, downloads it over HTTPS, verifies the
expected SHA-256 before execution, performs a staged self-check, and commits it
to the user-owned executable location atomically.

Latest resolution may fetch
`releases/latest/download/SHA256SUMS`, select exactly one supported raw EggPool
entry, derive the immutable versioned asset name, and then fetch that asset from
the same release. Exact Rust versions resolve under `releases/download/vX.Y.Z/`.
The installer must validate the selected filename, version, digest syntax,
platform mapping, size bound, and self-reported version before mutation.

A fresh normal native install MUST NOT invoke uv, pipx, pip, Python, Cargo, or a
source build.

Ownership rules are:

1. Fresh current-native install: create a `standalone-rust` installation.
2. Existing verified `standalone-rust`: remain standalone. Ordinary
   latest/exact Rust transitions should delegate to the native
   `eggpool update` machinery where possible rather than reimplementing its
   restart/rollback protocol in shell.
3. Existing uv/pipx/pip-owned native install: preserve the detected owner.
   The quick installer may delegate the transition to the existing native
   updater or the verified owning-manager path; it MUST NOT silently replace the
   manager-exposed command with a standalone file.
4. Existing source checkout or ambiguous ownership: fail closed.
5. An explicitly requested catalogued Python-era target is a compatibility
   exception, not a native quick-install path. It may use the existing
   package-manager compatibility machinery only when the requested transition
   and Python environment are valid. A standalone install does not acquire a
   direct Python-era downgrade path.
6. PyPI wheels and `cargo install eggpool` remain supported explicit
   alternatives. They are no longer the authority of a fresh default curl
   install.

The existing `--adopt-standalone` behavior must not be silently repurposed.
If retained, it remains an explicit opt-in migration from standalone ownership
to package-manager ownership and is documented as an advanced compatibility
operation, not part of the normal binary-first flow.

The supported proxy target matrix is unchanged. This ADR does not add a Windows
proxy artifact.

## Consequences

### Positive

- Fresh supported installs have no Python version dependency.
- Le Potato and other Linux/aarch64 hosts consume the same release executable
  already qualified and published for that target.
- Initial installation and normal standalone update share the GitHub raw-asset
  authority.
- The installer becomes smaller conceptually: native users no longer need a
  package manager solely as an executable copier.
- Existing manager ownership and historical compatibility remain available
  without defining the default path.

### Negative

- The quick installer now owns safe filesystem placement and initial
  replacement semantics that package managers previously supplied.
- Standalone fresh installs cannot directly downgrade to historical
  Python-era releases.
- Documentation and qualification currently written around a package-channel
  default must be updated.
- GitHub release availability becomes an operational dependency of the default
  quick-install path.

### Neutral or deferred

- PyPI publication remains part of the release contract.
- crates.io publication remains unchanged.
- GitHub artifact attestations and immutable-release enforcement are desirable
  supply-chain hardening, but they are not required to remove Python from the
  native quick-install path and may be planned separately.
- Production/root deployment authority may be revisited separately; this ADR
  changes the personal quick-install default and does not silently rewrite a
  system-owned deployment.

## Compatibility and migration

No config, database, schema, API, or wire migration is introduced.

Existing package-managed installations retain their owner and update semantics.
Existing standalone installations retain standalone semantics. Fresh installs
become standalone and use the already-supported native update path thereafter.

The one-shot command remains stable. Direct `uv tool install eggpool`,
`pipx install eggpool`, and `cargo install eggpool` remain documented
explicit choices.

## Security and reliability implications

The installer must fail before mutation on unsupported platform, malformed or
ambiguous checksum selection, checksum mismatch, oversized artifact, download
failure, wrong executable version, unsafe target path, ownership ambiguity, or
pre-existing unrelated command collision.

Download into a private temporary location, verify before execution, then stage
a candidate in the destination filesystem before atomic replacement. Never
execute an unverified downloaded candidate. Preserve a rollback copy when
replacing a verified standalone executable outside the native updater and
restore it on failed post-install validation.

Release URLs must be HTTPS in production. Test/non-production release origins
must require an explicit opt-in and must not become persistent user
configuration.

The shell installer remains part of the GitHub trust boundary. SHA-256 sidecars
protect artifact consistency; stronger provenance such as GitHub artifact
attestations is additive future hardening rather than a reason to retain
Python-based installation.

## Verification

Conforming implementation must demonstrate:

- fresh Linux/aarch64 install succeeds with no usable Python and with a stale
  or incompatible pipx present;
- fresh Linux/x86_64 and macOS/arm64 select only their matching raw artifact;
- checksum mismatch, malformed sidecar, wrong version, unsupported target, and
  collision fail without committing a command;
- a verified fresh binary reports `standalone-rust` provenance;
- existing uv/pipx/pip installs retain their owner;
- existing standalone installs remain standalone across latest/exact Rust
  updates;
- source-checkout and ambiguous provenance still fail closed;
- config/database/`.env` bytes are preserved;
- installer qualification has no network dependency and does not touch the
  contributor's real home or package managers.

## Supersession

None.

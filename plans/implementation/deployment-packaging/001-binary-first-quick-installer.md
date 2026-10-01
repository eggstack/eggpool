# Deployment and Packaging Milestone 001 — Binary-first quick installer

Status: active

Repository baseline: `5dc8aeccf06c0f012d6fae81410a805586c9d9d3`

Source roadmap:

- `plans/subsystems/deployment-packaging-roadmap.md#milestone-1--binary-first-quick-installer-and-ownership-cleanup`

Long-term requirements:

- `plans/000-long-term-specification.md#1-product-definition`
- `plans/000-long-term-specification.md#2-end-state-invariants-normative`
- `plans/000-long-term-specification.md#3-ownership-boundaries-normative`
- `plans/002-long-term-roadmap.md#phase-4--operations-integrations-and-deployment`

Applicable ADRs:

- `plans/adrs/ADR-0001-binary-first-quick-install-authority.md`

Primary class: capability

## 1. Objective

Replace the package-manager-first behavior of the public one-shot installer
with a verified GitHub raw-binary path for fresh current-native installs,
while preserving existing installation ownership, rollback, compatibility, and
state-preservation contracts.

A successful fresh install on a supported target must not depend on Python,
uv, pipx, pip, Cargo, or a source checkout.

## 2. Why this milestone is ready

There are no open hard dependencies.

The release workflow already publishes exactly one qualified raw EggPool
executable for each supported proxy target and a stable `SHA256SUMS` file.
The current v0.8.1 GitHub release exposes:

- `eggpool-0.8.1-linux-x86_64`;
- `eggpool-0.8.1-linux-aarch64`;
- `eggpool-0.8.1-macos-aarch64`;
- `SHA256SUMS`;
- the versioned release manifest.

The runtime already has a production standalone ownership/update contract in
`rust/src/operations/provenance.rs` and
`rust/src/operations/update.rs`. Standalone updates already use GitHub raw
assets with release digest verification, staging, self-check, replacement,
restart handling, and rollback.

The observed Le Potato failure is therefore not a missing target artifact or
runtime incompatibility. It is a bootstrap-policy defect: `scripts/install.sh`
can select an existing pipx whose Python is older than the wheel's
`Requires-Python >=3.11`, causing pip to reject every available EggPool
release before the native binary is installed.

## 3. Current implementation evidence

### Release production

- `.github/workflows/release.yml` builds wheel/raw pairs for Linux x86_64,
  Linux aarch64, and macOS arm64, aggregates the exact bytes, produces
  `SHA256SUMS`, and attaches raw assets/checksums/manifest to the GitHub
  release without rebuilding.
- `scripts/build_release_artifacts.py` owns raw/wheel extraction.
- `scripts/create_release_manifest.py` records release identity/digests.
- `scripts/validate_release_artifacts.py` and
  `scripts/verify_published_release.py` check the published bundle and GitHub
  asset digest metadata.
- `packaging/pypi/pyproject.toml` declares `Requires-Python >=3.11`; repo
  documentation explicitly describes this as package-manager compatibility,
  not a Rust runtime dependency.

### Installer

- `scripts/install.sh` supports Linux x86_64/aarch64 and macOS arm64.
- Fresh selection is uv -> pipx -> bootstrap uv.
- Existing package-managed ownership is inferred and retained.
- Standalone ownership currently requires explicit `--adopt-standalone`
  because the script's destination model is a wheel-managed install.
- Source checkout and ambiguous ownership fail closed.
- Config is created only when absent and existing config is preserved.

### Native ownership/update

- `rust/src/operations/provenance.rs` classifies native executables without
  distribution metadata as `StandaloneRust` and exposes bounded
  `install-provenance --shell` output.
- `rust/src/operations/update.rs` maps supported raw asset names from platform
  + version, enforces HTTPS, bounds metadata/artifact sizes, verifies SHA-256,
  stages/self-checks, replaces safely, and supports standalone rollback.
- `docs/upgrading.md` already documents standalone GitHub raw assets as an
  update authority.

### Test gap

- `scripts/qualify_quick_installer.py` uses fake managers and validates that
  manager commands are invoked, but does not model an old pipx interpreter
  filtering all wheels by `Requires-Python`.
- `tests/tooling/test_installer.py` therefore cannot catch the reported fresh
  Linux/aarch64 failure.

## 4. Invariants that must not regress

- Supported proxy targets remain Linux x86_64, Linux aarch64, macOS arm64.
- No Windows proxy/server support is implied.
- Fresh native install never falls back to source compilation.
- Existing package-manager ownership remains package-manager ownership unless
  the user explicitly requests a migration.
- Existing standalone ownership remains standalone on normal install/update.
- Source checkouts remain developer-owned and are never overwritten.
- Ambiguous provenance, unsafe links, and command-path collisions fail closed.
- Config, database, `.env`, backup state, and deployment files are preserved.
- Historical Python packages remain immutable external artifacts.
- Exact historical transitions remain bounded by the release catalog and
  Python/database/config compatibility.
- No release candidate is executed before integrity and version verification.
- Non-production release origins require explicit opt-in and do not persist.
- Release publication still builds exact candidate bytes once; this milestone
  does not weaken release validation to make installation easier.

## 5. Scope

### In scope

- Refactor `scripts/install.sh` around an explicit ownership/target dispatch.
- Add a dependency-free fresh native GitHub raw-binary install path.
- Keep exact Rust-version installation on the same raw-binary path.
- Preserve an explicit historical Python-era compatibility path.
- Delegate existing native owner-aware transitions to EggPool's Rust updater
  where this avoids duplicating transition/restart/rollback logic.
- Preserve explicit package-manager install options and migration semantics.
- Add deterministic binary-download/checksum/path/rollback qualification.
- Update public release/install/upgrade/deployment documentation and static
  validators/guards affected by the authority change.

### Explicitly out of scope

- Proxy/runtime request-path changes.
- New release targets.
- Windows proxy support.
- Removing PyPI or crates.io publication.
- Rebuilding historical releases.
- Changing database schema/config compatibility.
- Redesigning `eggpool update`.
- Reworking `eggpool-connect`.
- Changing root/system production ownership from its current explicit flow.
- Requiring GitHub CLI, jq, Python, or Sigstore tooling in the default quick
  installer.
- Enabling GitHub artifact attestations or immutable release policy as a hidden
  prerequisite; record these as follow-up hardening if desired.

## 6. Required production changes

### Installer authority dispatch

Restructure `scripts/install.sh` so fresh current-native installation is not
selected through manager discovery.

The dispatch order should be explicit:

1. validate arguments, user/root policy, OS/architecture, and non-production
   overrides;
2. inspect an existing `eggpool` command if present;
3. classify ownership;
4. choose exactly one transition authority;
5. perform the transition;
6. verify resulting ownership/version;
7. seed config only when absent;
8. print bounded next steps.

Expected authorities:

- no existing command + latest/current Rust target -> raw GitHub binary;
- no existing command + explicit Rust target -> raw GitHub binary;
- no existing command + explicit catalogued Python-era target -> historical
  package compatibility path, with Python compatibility checked before
  mutation;
- native existing standalone -> native standalone update authority;
- native existing uv/pipx/pip -> native owner-aware update authority;
- legacy Python-era package install -> existing owner/package compatibility
  transition;
- source checkout/ambiguous/collision -> refuse.

Do not let the mere presence of pipx or uv alter the authority of a fresh
current-native install.

### Shell-native release resolution

Add small shell functions for:

- supported platform normalization;
- release base/tag URL construction;
- downloading the stable `SHA256SUMS`;
- selecting exactly one raw EggPool asset for the target;
- extracting and validating the version from the selected filename;
- exact-version filename/tag agreement;
- SHA-256 verification;
- artifact size bound;
- staged self-check and destination commit.

Latest must be resolvable without parsing GitHub JSON. Prefer:

```text
https://github.com/eggstack/eggpool/releases/latest/download/SHA256SUMS
```

Then select the single line matching the supported raw target and fetch:

```text
https://github.com/eggstack/eggpool/releases/latest/download/<selected-file>
```

or the equivalent version-pinned tag URL after extracting the version.

For exact versions:

```text
https://github.com/eggstack/eggpool/releases/download/v<version>/SHA256SUMS
https://github.com/eggstack/eggpool/releases/download/v<version>/<selected-file>
```

Do not accept wheel or `eggpool-connect` checksum entries as proxy candidates.

### Integrity and candidate validation

Before executing a downloaded binary:

- require a syntactically valid 64-hex SHA-256;
- require exactly one expected filename match;
- reject unexpected path separators/whitespace/filename forms;
- enforce the existing updater-scale artifact bound (128 MiB is the current
  Rust updater maximum unless release authority changes it intentionally);
- verify with `sha256sum` on Linux and `shasum -a 256` on macOS, with a
  deliberate portable fallback only if equally strict;
- `chmod` only after the download is complete;
- run staged `version` and require the selected release version;
- run staged `install-provenance --shell` or another bounded native identity
  check and require native Rust identity;
- never execute the candidate after checksum/version validation fails.

Production release URLs must remain HTTPS. Any fixture/test origin override
must require an explicit non-production opt-in analogous to the current
package-index guard.

### Destination, collision, and atomicity

For a fresh personal install, use one canonical user command location
consistent with existing PATH guidance, expected to be
`${HOME}/.local/bin/eggpool` unless the current repository path authority
defines a better existing standard.

Requirements:

- create the destination directory safely;
- refuse a pre-existing unrelated file/symlink/collision;
- use a private temp directory and a same-filesystem staged destination file;
- make the final commit atomic via rename;
- verify the committed command path and version;
- clean temporary files on exit/signal;
- do not mutate config/data before the command is committed and verified.

If replacing a known standalone command without delegating to
`eggpool update`, retain a rollback copy and restore it if post-commit
verification fails. Prefer delegation to the Rust updater for existing native
installs so service restart, transition locking, self-check, and rollback are
not duplicated in shell.

### Existing-owner behavior

Existing native installs should normally use their own binary to execute
`eggpool update [VERSION]`, because that path already re-detects provenance
and preserves manager/standalone authority.

Preserve a safe repair story for `--force`: same-version repair must still
be possible without changing owner. If the native updater intentionally treats
the same version as a no-op, the shell may perform an owner-specific repair
only with the same safety and verification guarantees.

Legacy Python-era commands that do not expose the Rust
`install-provenance`/update protocol may continue through the bounded
manager-detection compatibility logic already present.

The existing `--adopt-standalone` flag must not change meaning silently. It
may remain as an explicit advanced migration to package-manager ownership or
be deprecated with a compatibility period, but ordinary standalone curl
updates must no longer require it.

### Historical exact-version behavior

Preserve the catalog gate for pre-native exact targets. The fresh native path
must not attempt to download a nonexistent raw Python-era artifact.

If a user explicitly requests a catalogued Python-era version:

- require the historical compatibility path;
- establish a compatible Python environment before mutation;
- fail with an explicit historical-target diagnostic when compatibility cannot
  be satisfied;
- do not use the historical path as fallback after any current-native download,
  checksum, or GitHub failure.

### Release/supply-chain consistency

Update validation tooling as needed so the installer-facing raw asset and
`SHA256SUMS` contract is machine-checked.

Do not introduce an installer-specific unvalidated alias or duplicate binary
build.

If release artifact attestations are added later, attest the released binaries
or manifest/checksum authority in the existing tag build; do not require
`gh` solely to make the default installer function.

## 7. Ordered work packages

### Work package A — Freeze installer authority and fixture contract

Intent:

Make ownership and target selection testable before replacing manager logic.

Required changes:

- Refactor installer selection into small shell functions with one explicit
  authority result.
- Add deterministic fixture hooks for release origin/download without relaxing
  production HTTPS policy.
- Extend `scripts/qualify_quick_installer.py` to fake curl/release assets,
  supported platforms, hash tools, existing owners, and legacy manager cases.
- Add a regression fixture representing Linux/aarch64 with pipx present but an
  unusable Python.

Acceptance evidence:

- The regression case selects the raw-binary lane and never invokes pipx.
- Existing uv/pipx/pip ownership cases still select their owner.
- Source/ambiguous ownership remains refusal.

### Work package B — Implement verified fresh raw-binary install

Intent:

Install one exact qualified raw executable without Python or a package manager.

Required changes:

- Implement latest/exact checksum-sidecar resolution.
- Enforce target/name/version/digest/size constraints.
- Download, hash, staged-self-check, and same-filesystem atomic commit.
- Create/verify the canonical user command path.
- Seed config only after verified command installation.

Acceptance evidence:

- Linux x86_64, Linux aarch64, and macOS arm64 fixture installs succeed.
- Resulting command reports expected version and `standalone-rust`.
- No manager/Python command is invoked on the fresh native path.

### Work package C — Reconcile existing-owner transitions and repair

Intent:

Remove duplicated bootstrap/update behavior without regressing ownership.

Required changes:

- Delegate existing native updates to `eggpool update` where compatible.
- Keep legacy Python package transition support.
- Define and test `--force` repair per owner.
- Preserve or deliberately deprecate `--adopt-standalone` semantics without
  silently changing its meaning.
- Preserve running-service restart/rollback behavior by using the native
  updater rather than shell duplication where possible.

Acceptance evidence:

- Existing standalone remains standalone.
- Existing uv/pipx/pip remains the same manager kind.
- Exact Rust update works for both standalone and package-managed owners.
- Historical Python target is rejected from standalone and remains gated for
  compatible package owners.
- Failed repair/update preserves or restores the previous command.

### Work package D — Harden negative paths and concurrency

Intent:

Ensure a one-line installer cannot leave an ambiguous or partially installed
command.

Required changes:

- Add malformed/multiple/missing checksum entry rejection.
- Add wrong-version/wrong-platform candidate rejection.
- Add oversized/truncated download rejection.
- Add target symlink/special-file/unrelated collision refusal.
- Add private temp cleanup and signal handling.
- Add a simple per-user install/transition lock or an equivalent race-proof
  revalidation before atomic commit.
- Ensure post-install validation failure restores known standalone state.

Acceptance evidence:

- Every negative fixture fails before final mutation or proves rollback.
- Concurrent/racing fixture cannot silently overwrite a changed target.
- No failure modifies config/database/`.env`.

### Work package E — Documentation, guards, and release qualification

Intent:

Make the public contract match the implementation.

Required changes:

- Update `README.md` Quick Start to describe raw binary as the one-shot
  default and uv/pipx as explicit alternatives.
- Update `docs/upgrading.md`, `docs/releasing.md`, and
  `docs/rust-release-deployment.md`.
- Update `.opencode/skills/deployment/SKILL.md` and any static release-doc
  validators that encode package-manager-first wording.
- Keep production/root deployment wording explicit and unchanged unless this
  milestone directly proves a safe binary-owned replacement.
- Add a short SBC note that current-native curl install does not require
  Python >=3.11.

Acceptance evidence:

- `scripts/validate_release_docs.py` passes.
- No current document calls PyPI wheel management the default one-shot owner.
- Direct uv/pipx/cargo commands remain documented as supported alternatives.

## 8. Failure, cancellation, restart, contention semantics

Download or checksum failure: no destination mutation.

Candidate self-check failure: no destination mutation.

Fresh install commit failure: no config mutation; temporary candidate is
removed; any unrelated pre-existing target is untouched.

Existing standalone/package-managed update: use native updater lifecycle
semantics when available, including transition locking, running-service
restart, and rollback.

Legacy manager transition: preserve the current manager rollback behavior and
verify resulting ownership/version before config work.

Signal/cancellation: traps remove private temp files and must not remove an
existing installed command or user state.

Concurrent invocations: one invocation must fail or observe/revalidate the
other's commit; neither may silently replace a target whose identity changed
after preflight.

Checksum sidecar ambiguity: fail closed. Never select first-match from multiple
eligible raw entries.

GitHub latest changes between sidecar and binary request: bind selection to the
version parsed from the sidecar and prefer the version-tagged asset URL after
selection so a moving latest pointer cannot cross release versions.

## 9. Compatibility and migration

The public one-shot command stays unchanged:

```bash
curl -fsSL https://raw.githubusercontent.com/eggstack/eggpool/main/scripts/install.sh | bash
```

Its resulting ownership changes for fresh native installs from package-managed
wheel to standalone raw binary, as authorized by ADR-0001.

Existing installations are not silently migrated.

Direct package installs remain valid:

```bash
uv tool install eggpool
pipx install eggpool
cargo install eggpool
```

Historical exact targets remain compatibility-only. No database/config
migration occurs.

The installer must preserve existing config path precedence and continue to
create the canonical config only when absent.

## 10. Required tests

Extend `scripts/qualify_quick_installer.py` and
`tests/tooling/test_installer.py` with at least:

- fresh Linux/aarch64 native install with no Python;
- fresh Linux/aarch64 with stale pipx present: raw path selected, pipx unused;
- fresh Linux/x86_64 success;
- fresh macOS/arm64 success;
- exact Rust version success;
- unsupported platform refusal;
- missing `SHA256SUMS`;
- zero matching raw entries;
- multiple matching raw entries;
- malformed digest;
- checksum mismatch;
- oversized artifact;
- truncated/failed download;
- wrong target filename;
- staged command reports wrong version;
- staged command not recognized as native EggPool;
- destination symlink/special file/unrelated collision;
- interrupted/cancelled install cleanup;
- target changes between preflight and commit;
- existing standalone delegates/retains standalone ownership;
- existing uv ownership retained;
- existing pipx ownership retained;
- existing pip/venv ownership retained;
- source checkout refusal;
- ambiguous provenance refusal;
- explicit historical exact target uses compatibility path only;
- standalone historical downgrade refusal;
- `--force` repair retains owner;
- existing config bytes preserved;
- absent config seeded only after command commit;
- non-production origin override requires explicit opt-in.

Release tests should assert that every supported proxy raw artifact appears in
`SHA256SUMS` exactly once and that helper/wheel entries cannot satisfy the
installer's raw selector.

If Rust provenance/update code changes, run its focused unit/integration tests
covering `InstallProvenance`, raw update resolution, transition rollback, and
ownership preservation.

## 11. Required verification commands

Tooling-focused:

```bash
uv run python scripts/qualify_quick_installer.py
uv run pytest tests/tooling/test_installer.py -q
uv run pytest   tests/tooling/test_release_artifacts.py   tests/tooling/test_release_packaging.py   tests/tooling/test_release_supply_chain.py   tests/tooling/test_release_catalog.py   tests/tooling/test_release_docs.py -q
uv run python scripts/check_release_catalog.py
uv run python scripts/validate_release_identity.py
uv run python scripts/validate_release_workflow.py .github/workflows/release.yml
uv run python scripts/validate_release_docs.py
uv run python scripts/validate_runtime_package_boundary.py
uv run ruff format --check scripts/ tests/tooling/
uv run ruff check scripts/ tests/tooling/
uv run pyright scripts/
git diff --check
```

If any Rust code changes:

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
```

Operational closure evidence should additionally include a fresh supported
Linux/aarch64 device run when available, preferably the reported Le Potato,
showing platform/Python/pipx versions, selected artifact identity, resulting
EggPool version/provenance, config preservation, and successful
`eggpool check-config`. Do not include credentials.

## 12. Documentation updates

Required:

- `README.md`
- `docs/upgrading.md`
- `docs/releasing.md`
- `docs/rust-release-deployment.md`
- `.opencode/skills/deployment/SKILL.md`
- `architecture/deep-dive-deployment.md` if it states package-manager-first
  authority
- release/installer validators whose assertions encode the previous default

Document these distinctions explicitly:

- quick curl install = verified standalone GitHub raw binary;
- uv/pipx = explicit package-managed alternatives;
- cargo = source build/install alternative;
- existing owner remains authoritative;
- Python >=3.11 matters only to package/historical compatibility paths, not a
  fresh current-native curl install;
- system/root production deployment remains its documented explicit authority.

## 13. Acceptance criteria

1. A fresh supported current-native install succeeds without Python, uv, pipx,
   pip, Cargo, or a source checkout.
2. A Linux/aarch64 host with stale/incompatible pipx cannot reproduce the
   reported package-resolution failure because pipx is not consulted.
3. Fresh installs consume only the qualified raw artifact matching the detected
   target and verify SHA-256 before execution.
4. Latest resolution is version-bound after checksum selection and cannot mix
   checksum data from one release with a binary from another moving latest.
5. Exact Rust versions use the same raw authority.
6. Resulting fresh installs report `standalone-rust` and use native
   `eggpool update` thereafter.
7. Existing uv/pipx/pip owners remain unchanged across installer-driven update
   or repair.
8. Existing standalone owners remain standalone unless an explicit migration
   flag is used.
9. Source-checkout, ambiguous, symlink/special-file, and unrelated command
   collisions fail closed.
10. Historical Python-era targets are never an implicit fallback for native
    install failure.
11. Config, database, `.env`, and unrelated deployment artifacts are bytewise
    preserved by install/update transitions.
12. Failed download, verification, self-check, replacement, or post-check does
    not leave an unverified command active.
13. Deterministic qualification covers the old-pipx Linux/aarch64 regression
    without real network/package-manager/user-state access.
14. Public docs and static validators agree on binary-first quick-install
    authority.
15. No supported proxy target, API surface, runtime capability, or package
    publication channel is removed.

## 14. Stop conditions

Stop and report rather than improvise if:

- current GitHub release assets do not provide exactly one qualified raw binary
  per supported target;
- the checksum sidecar cannot be used without ambiguous parsing;
- safe atomic placement would require overwriting a manager-owned shim;
- existing native updater behavior cannot preserve manager/standalone ownership;
- same-version repair requires changing ownership;
- a proposed historical compatibility change would rebuild or mutate old PyPI
  artifacts;
- a change would alter system/root production ownership outside this milestone;
- release verification would need to be weakened to make the installer pass;
- repository state has moved enough that ADR-0001's assumptions no longer hold.

## 15. Closure evidence required

The closure record must include:

- implementation commit(s);
- requirement-to-evidence matrix for all acceptance criteria;
- deterministic installer qualification output and focused pytest results;
- release catalog/workflow/docs validator results;
- Rust focused/full/no-default results if Rust changed;
- fixture evidence that stale pipx on Linux/aarch64 is ignored for fresh native
  install;
- fixture evidence for checksum mismatch, moving-latest binding, collision,
  ownership preservation, rollback/cleanup, and historical-path separation;
- documentation diff summary;
- exact statement of whether physical Le Potato evidence was collected
  (`measured` or `not measured`, never implied);
- severity-tagged unresolved findings;
- disposition: closed, conditionally closed, corrective pass required, or
  blocked;
- registry update and unblock audit.

## 16. Handoff notes

Treat installer ownership and verification as correctness boundaries, not
presentation polish.

Do not solve the observed failure by merely forcing a newer Python. The point of
ADR-0001 is that the normal current-native path must not have a Python
dependency at all.

Prefer using existing native update/provenance logic for already-installed Rust
commands. Shell should bootstrap a fresh trusted executable, not become a
second implementation of the Rust transition engine.

Keep tests disposable. Do not invoke the contributor's real package managers,
touch the real `~/.config/eggpool`, or perform a public install as part of the
deterministic suite.

# O008 — Update, Version Resolution, and Update-Checker Background Task

Status: closed; closure accepted

Implementation commit: `c9b63d5562cbfa515ae4dfb6cfa7ba688f82b14e`

Closure: [O008 status](../../closure/operations/008-status.md)

Source roadmap: `migration-rs/subsystems/operational-cli-lifecycle-roadmap.md`

Primary class: capability/invariant

Hard dependency: accepted O007.

## Objective

Implement a Rust-native update service behind `update [VERSION]`, preserve version normalization/check semantics, and replace the R008 deferred `update_checker` with a real callback owned by the existing M8 task supervisor.

M11—not O008—makes Rust releases the canonical public installation path. O008 must nevertheless make the Rust binary's update machinery complete, safe, and testable so M11 is a cutover/release action rather than a late implementation project.

## User-visible contract

Preserve O001 semantics:

- `version` reports the running Rust package version;
- `update` with no version resolves latest release;
- exact version accepts `0.6.5` and `v0.6.5` equivalently;
- nonexistent/invalid exact version is a clear non-zero error;
- `--check` performs no replacement;
- already-current target is a no-op success;
- config/database are never overwritten by update;
- if the server was running before an applied update, restart behavior matches the frozen operational contract;
- download/metadata/protocol/verification/replacement errors are distinguished and secret-free.

Do not shell out to pip/pipx/uv from the Rust binary as a migration fallback. Python package-manager command construction remains historical oracle evidence, not the Rust implementation architecture.

## Part A — release/version model

Implement small typed helpers for:

- normalize optional leading `v`;
- parse/compare supported EggPool release versions using the repository's actual versioning rules;
- current version from Cargo metadata;
- release target (`Latest` or `Exact`);
- release metadata and compatible artifact descriptor;
- platform/architecture identity needed to choose an artifact.

Avoid a broad package-manager library. If semver crate support is not already present and EggPool versions are SemVer-compatible, a small direct `semver` dependency is acceptable only if justified; otherwise implement the limited validated syntax needed by the current project.

Pre-release behavior must follow O001/current release policy explicitly.

## Part B — release metadata client

Use the existing Hyper/Rustls stack for HTTPS. Do not add Reqwest.

The production metadata source should be the intended Rust release authority for M11 (normally GitHub Releases/tags/assets unless current repo planning specifies another source). Keep source URLs centralized and injectable for deterministic local tests.

Client requirements:

- bounded connect/read/overall timeouts;
- bounded response size;
- redirect policy limited to trusted release/download flows;
- typed HTTP/status/JSON errors;
- no automatic retry storm;
- optional cache only if Python/current config requires it;
- user-agent identifying EggPool/version without host/user identifiers.

No provider/account proxy routing is required unless current network config explicitly applies to update checks; freeze this from O001 and document the decision.

## Part C — artifact identity and integrity

Define the future Rust release asset naming contract narrowly enough for M11 to publish without changing O008 code. At minimum account for supported OS/arch tuples represented by the current build target.

Require integrity evidence before replacement. Preferred order:

1. release-provided SHA-256 manifest/signature already planned by the repository;
2. exact expected digest carried in release metadata;
3. if no integrity metadata exists yet, O008 must fail apply and record a cutover prerequisite rather than silently trust an arbitrary downloaded executable.

Do not invent a bespoke cryptographic signing scheme in M9. M11 may strengthen release signing; O008 should expose a verifier interface with SHA-256 minimum if that is the accepted repository standard.

Downloads go to a private temp file, enforce a configured/reasonable maximum, and never execute before validation.

## Part D — staged executable replacement

Self-update must be crash-conscious and local:

1. resolve/validate target and artifact;
2. determine current executable path and whether it is a supported managed layout;
3. download to sibling/private staging path;
4. verify digest/metadata;
5. set reviewed executable mode;
6. optionally execute `eggpool version` or a narrow self-check against the staged binary before replacement if safe;
7. determine whether server is running and perform O003 graceful stop at the correct point;
8. atomically rename old binary to a rollback path and staged binary into place where filesystem semantics allow;
9. verify installed version;
10. restart previously-running service/process according to O001;
11. remove rollback copy only after successful verification/restart policy.

On any failure after old-binary rename, restore the old executable when possible and retain actionable paths otherwise. Never replace config/database/provider templates as part of binary update.

If the current executable comes from an unsupported/read-only/source checkout layout, return an explicit install-method error; do not modify unrelated package-manager environments.

## Side-by-side migration behavior

Before M11 public Rust release assets exist, `update --check` and exact/latest resolution may be testable/usable if metadata exists, while apply may report `no compatible Rust artifact` explicitly. O008 is accepted based on deterministic local artifact tests and code completeness, not on already flipping public distribution.

M11 is responsible for publishing matching artifacts and changing public install/update documentation to make the Rust path canonical.

## Part E — `update_checker` background task

Implement the R008 deferred callback using the same metadata/version service in check-only mode.

Requirements:

- exact enable/interval/current config semantics from O001;
- M8 supervisor singleton/non-overlap only;
- no automatic install from background task unless the Python contract explicitly already does so (expected: check/diagnostic only);
- bounded network timeout/response;
- failures isolated to task diagnostics;
- no notification spam/state growth; retain bounded latest check result/version/error category;
- reload task-spec changes through R007;
- no provider runtime shutdown on metadata failure.

After O008 closure the `update_checker` capability is no longer deferred.

## Tests

Use a loopback fake release HTTP service and fake executable files. Cover:

- version normalization with/without `v`;
- invalid/exact missing/latest/current/newer/older/pre-release policy;
- `--check` never writes;
- bounded malformed/oversized/slow/redirect/status metadata;
- OS/arch artifact selection and no-compatible-artifact;
- digest match/mismatch/missing integrity;
- staged binary version mismatch/non-executable;
- read-only/unsupported install path;
- replacement fault before download, after download, after verification, after old rename, after new rename, after version verify, during restart;
- old binary restored or retained as documented;
- config/database byte hashes unchanged across every case;
- previously stopped service remains stopped; previously running service restart behavior;
- concurrent update invocation serializes/refuses safely;
- update checker enable/disable/reload/failure/non-overlap/shutdown;
- no unbounded task history or downloaded-temp leakage.

Do not hit GitHub/PyPI in normal tests.

## Security review

Explicitly review:

- HTTPS/redirect trust boundary;
- artifact path/name sanitization;
- integrity verification;
- executable permission/ownership;
- symlink/hardlink replacement hazards;
- TOCTOU around current executable;
- temp directory permissions;
- hostile metadata strings in logs/terminal;
- downgrade/exact-version semantics.

## Non-goals

- public Rust release pipeline/cutover;
- automatic background upgrades unless already contractual;
- package-manager abstraction for pip/homebrew/apt;
- broad cross-platform artifact CI;
- custom signing PKI.

## Verification

Run fmt/Clippy, focused O008 tests, O003 lifecycle regressions, R006/R007 task tests, aggregate Rust, Python update/version oracle tests, migration suite, static checks, and `git diff --check`.

## Closure evidence

Write `migration-rs/closure/operations/008-status.md` with user-level parity matrix, artifact/integrity contract, replacement fault matrix, config/DB immutability hashes, task registration proof, dependency/security review, and unresolved M11 release prerequisites.

## Acceptance criteria

O008 closes only when Rust latest/exact/check/update behavior is implemented with staged verified rollback-capable replacement, unsupported/no-asset states fail explicitly, config/database survive all faults, and `update_checker` is a real bounded singleton M8 task. Public Rust-default release availability is not required until M11.

Accepted O008 promotes only O009.

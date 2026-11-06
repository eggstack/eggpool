# Deployment and Packaging M009 — Dependency advisory remediation

Status: implemented

Repository baseline: `73eac4c6` on `plan/optional-tools-privategpt-parity-20261103`

Source roadmap: `plans/subsystems/deployment-packaging-roadmap.md#12-milestone-status`

Original plan and closure context: `plans/implementation/deployment-packaging/008-ci-dependency-lock-discipline.md`; `plans/closure/deployment-packaging/008-status.md`.

Long-term requirements: `plans/000-long-term-specification.md`; `plans/002-long-term-roadmap.md`; `plans/003-planning-process.md`.

Applicable ADRs: none required; dependency compatibility remains Cargo-owned.

Primary class: infrastructure.

## 1. Objective

Restore the repository's RustSec dependency gate by upgrading affected
dependencies, retaining backup/restore compatibility, and propagating ZIP
member-name decoding errors safely.

## 2. Why this milestone is ready

The dependency audit workflow rejected the committed graph with RustSec
advisories. The vulnerable archive dependency is used by the native backup
service. Fixed releases are available and the application has bounded
member-by-member restore logic rather than extracting to a directory.

## 3. Current implementation evidence

`rust/Cargo.toml` and `rust/Cargo.lock` define the graph. `rust/src/operations/backup.rs`
uses `ZipArchive::by_index`, rejects directories/special files and enforces
per-member limits before reading. Existing archive compatibility tests live in
`rust/tests/database_compatibility.rs` and `rust/tests/on_prem_budgets.rs`.

## 4. Invariants that must not regress

- Restore remains byte-exact, bounded, atomic, and rejects unexpected,
  duplicate, encrypted, directory, and special-file entries.
- No `ZipArchive::extract` call or archive-controlled filesystem path is
  introduced.
- The RustSec gate remains authoritative; do not add advisory ignores for
  findings that have fixed releases.
- Default and no-default builds remain supported.

## 5. Scope

### In scope

- Fixed compatible Tokio, rustls-webpki, webpki-roots, crypto-bigint, ctutils,
  and ZIP dependency resolutions.
- ZIP 9 fallible member-name API handling in backup restore.
- Existing backup/restore compatibility verification.

### Explicitly out of scope

- Backup format changes, database migrations, compression-policy changes,
  transport architecture changes, or advisory-policy exceptions.

## 6. Required production changes

Update the locked dependency graph to releases that resolve current RustSec
advisories. Adapt `entry.name()` to handle `ZipError` before applying the
existing exact filename allowlist. Keep all other restore bounds and
publication behavior unchanged.

## 7. Ordered work packages

### Work package A — Resolve advisories

Intent: make `cargo deny` pass using fixed upstream releases.

Acceptance evidence: `cargo deny --manifest-path rust/Cargo.toml check` passes
with no new `deny.toml` exception.

### Work package B — Preserve backup compatibility

Intent: adapt the ZIP API and prove existing archives still validate and
restore.

Acceptance evidence: workspace compile, focused backup/restore targets, serial
default/no-default suites, and locked release build pass.

## 8. Failure, cancellation, restart, contention semantics

No runtime transition or persistence semantics change. Invalid ZIP member-name
metadata becomes a typed archive error. Restore remains staged and atomic.

## 9. Compatibility and migration

No config or database migration. Existing archive format and stored members
remain unchanged; this changes only the dependency API and lockfile.

## 10. Required tests

- `rust/tests/database_compatibility.rs` backup validation and restore tests.
- Budget backup coverage in `rust/tests/on_prem_budgets.rs`.
- Full serial default and no-default workspace suites.

## 11. Required verification commands

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
cargo deny --manifest-path rust/Cargo.toml check
cargo build --manifest-path rust/Cargo.toml --locked --release
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- --test-threads=1
```

## 12. Documentation updates

Update the deployment-packaging roadmap/registry and closure record. No
operator-facing backup format change is expected.

## 13. Acceptance criteria

The audit reports no current advisories, backup restore compiles and retains
existing behavior, no advisory exceptions are added, and all required tests
and release verification pass.

## 14. Stop conditions

Stop if a fixed dependency release changes backup format or restore semantics,
if safe archive handling requires broad redesign, or if dependency updates
break the supported Rust toolchain.

## 15. Closure evidence required

Record exact resolved versions, RustSec result, ZIP API change, backup tests,
default/no-default suites, locked release build, migration/security review,
and any remaining external evidence.

## 16. Handoff notes

Do not use `ZipArchive::extract`. Archive members are untrusted; only the
existing basename allowlist and bounded byte reads are supported.

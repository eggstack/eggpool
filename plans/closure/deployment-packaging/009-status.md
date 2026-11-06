# Deployment and Packaging Milestone M009 — Closure Status

Status: closed

Source implementation plan:

- `plans/implementation/deployment-packaging/009-dependency-advisory-remediation.md`

Source subsystem roadmap:

- `plans/subsystems/deployment-packaging-roadmap.md#12-milestone-status`

Repository baseline reviewed: `73eac4c6` plus the implementation commit recorded below.

Implementation commits:

- `32f5c66a` — remediate RustSec advisories and adapt backup ZIP member
  parsing.

## 1. Executive finding

Fixed dependency releases are selected and `cargo-deny` now passes without
advisory exceptions. ZIP restore now propagates the newer fallible member-name
decoder's error. The full suites and locked release build passed.

## 2. Requirement-to-evidence matrix

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| Remove current RustSec advisories | `cargo deny --manifest-path rust/Cargo.toml check` | pass | No ignored advisories were added. |
| Preserve ZIP restore's bounded member handling | `rust/src/operations/backup.rs::prepare_restore` | pass | Uses `by_index`; decodes each member name fallibly, then applies the existing exact allowlist and size bounds. Never calls extraction-to-directory APIs. |
| Backup/restore compatibility | `rust/tests/database_compatibility.rs`, `rust/tests/on_prem_budgets.rs` | pass | Default and no-default serial workspace suites pass. |
| Database migration behavior | No migration change | pass | v65 and all migration assets unchanged. |

## 3. Production implementation evidence

The lockfile upgrades ZIP 2.4.2 to 9.4.1 and resolves the audited Tokio,
rustls-webpki, webpki-roots, crypto-bigint, and ctutils advisories with fixed
releases. `prepare_restore` maps fallible ZIP member-name decoding failures
through `BackupError::Archive` before any path or member allowlist decision.

## 4. Verification executed

### Commands run

```bash
cargo deny --manifest-path rust/Cargo.toml check
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets
```

### Results

- Cargo deny passed after the dependency updates.
- Workspace all-target check passed with ZIP 9.4.1.
- Strict Clippy and both serial workspace suites passed. The locked release
  build completed successfully.

## 5. Invariant review

Restore still rejects unexpected names and special members before reading
payloads, and checks per-member and total archive bounds. It does not extract
archive paths to disk. Existing staged atomic replacement logic is untouched.

## 6. Failure and recovery review

Malformed ZIP name metadata returns a typed archive error. No new write or
restart state was added. Existing restore rollback and final publication paths
remain unchanged; focused archive compatibility is covered by the completed
serial workspace suites.

## 7. Migration and compatibility review

No schema, backup format, config, or migration change. Archive input remains
bounded and output still writes stored members. Migration v65 is untouched.

## 8. Security review

The dependency gate identified process-abort and symlink-path advisories in
ZIP 2.4.2, plus fixed upstream issues in Tokio, rustls-webpki, webpki-roots,
crypto-bigint, and ctutils. ZIP 9.4.1 and the fixed lock entries resolve the
current audit. Restore does not call `ZipArchive::extract`; it rejects
special-file metadata and reads only allowlisted basenames into bounded
buffers. No new advisory exceptions were introduced.

## 9. Documentation and operations

No operator-facing backup format change. The deployment-packaging roadmap and
registry identify M009 as closed after the test/release checks passed.

## 10. Unresolved findings

| Severity | Finding | Impact | Required action |
|---|---|---|---|
| — | No unresolved M009 findings | Dependency and backup compatibility gates passed | Continue normal release qualification. |

## 11. Roadmap disposition

Closed after serial default/no-default suites, strict CI, Cargo deny, and the
locked release build passed. There is no physical SBC gate for this
correctness/security fix; SBC testing remains required only for
device-specific footprint claims.

## 12. Registry updates

M009 is registered as closed in `plans/registry.md` and
`plans/subsystems/deployment-packaging-roadmap.md`.

# On-Prem User Access Milestone 001 — Closure Status

Status: conditionally closed

Source implementation plan:

- `plans/implementation/on-prem-user-access/001-admin-bootstrap-and-credentials.md`

Source subsystem roadmap:

- `plans/subsystems/on-prem-user-access-roadmap.md#m001--secure-admin-bootstrap-and-client-credentials`

Repository baseline reviewed: `0d6ebcd9` (task branch baseline); roadmap/ADR authority `81beec4e`

Implementation commits:

- `ecdc313` — add local admin bootstrap, per-user credentials, staged auth migration, tests, and docs.
- `1a2d329` — move M001 into closure after implementation landed.
- `b4f769b` — activate M001 and move it from the blocked registry into active work.

## 1. Executive finding

M001's schema, local administrator bootstrap/recovery, personal agent tokens,
legacy compatibility gate, and documentation are implemented. The initial
admin and token are committed atomically; passwords are Argon2id-hashed
outside SQLite's gate with fixed bounded parameters and at most two concurrent
hash workers. The legacy global server key authenticates only while the
database remains `legacy_unmigrated`; after identity bootstrap, agent routes
require a personal token. M001 does not provide browser login or multi-user
web authorization.

This milestone is conditionally closed because the physical Le Potato/Pi
password-hashing latency and memory qualification required by the source plan
could not be performed on the available Darwin x86_64 host. This limits target
hardware sizing claims; it does not block M002's credential/schema interface.

## 2. Requirement-to-evidence matrix

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| Append-only identity schema and checksum | Migration `0055_user_identity.sql`; migration runner and database compatibility suite; SHA-256 `352224c8e2505509efa8fad0017c9aaac74ef8338771b35388ef22cf77cd8a62` | pass | Prior migration files/checksums remain unchanged; runtime manifest now records 55 migrations. |
| Atomic first-admin creation and race safety | `rust/tests/on_prem_identity.rs`: atomic bootstrap and concurrent first-run tests | pass | Exactly one initial administrator is committed. |
| Password storage, reset and token revocation | Identity integration tests; passwords are never stored in plaintext; reset revokes personal credentials; explicit revoke is covered | pass | Argon2id v19, m=19456 KiB, t=2, p=1; maximum two concurrent hash workers. |
| High-entropy personal agent token and one-way storage | Bootstrap/token integration tests; selector plus SHA-256 digest stored; no `Debug` implementation for plaintext token | pass | Token is emitted once and is withheld by default when stdout is not a TTY. |
| Legacy key is staged and cannot cross cutover | Middleware lifecycle test; full server transport suite | pass | Configured global key works only in `legacy_unmigrated`; personal tokens are limited to agent surfaces. |
| Fresh headless CLI flow and secret-output posture | Disposable release-binary smoke: `users bootstrap --password-stdin`, then `users list` | pass | Random password supplied through stdin; non-TTY output contained no `epk1_` token. |
| Existing DB/config and restore compatibility | `database_compatibility` suite and full serial workspace suite | pass | Upgrade is forward-only and transactional; no upstream account/provider topology was repurposed. |
| Strict build/test parity and dependency audit | Commands below | pass | All checks are local; no hosted CI result is claimed. |
| Physical Le Potato/Pi Argon2 qualification | Not available on this host | partial | Carry exact qualification into M007; do not infer SBC sizing from desktop results. |

## 3. Production implementation evidence

Migration 55 adds downstream `users`, `user_credentials`, and singleton
`user_access_state` tables, including a unique active-admin constraint and
`legacy_unmigrated`, `fresh_uninitialized`, `admin_initialized`, and `managed`
states. The schema does not modify upstream `accounts` or provider config.

`rust/src/identity.rs` owns password hashing, credential issuance/revocation,
token verification, and access-state operations. The local CLI supports
bootstrap, list, password reset, token issue, and token revocation. Bootstrap
accepts confirmed TTY passwords or one bounded stdin line; no password CLI
argument exists. Plaintext token output requires a TTY or explicit
`--show-secret`. Fresh keyless server startup is rejected until bootstrap.

The ingress middleware keeps the shared configured key within the staged
legacy state and limits personal credentials to inference/model discovery and
the integration profile. No browser session or web-admin authority was added.

## 4. Verification executed

### Commands run

```bash
rtk cargo fmt --manifest-path rust/Cargo.toml --all -- --check
rtk cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
rtk cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
rtk cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
rtk cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
rtk cargo test --manifest-path rust/Cargo.toml --test on_prem_identity -- --test-threads=1
rtk cargo test --manifest-path rust/Cargo.toml --lib server::middleware::tests::global_server_key_is_valid_only_before_identity_cutover -- --exact
rtk cargo build --manifest-path rust/Cargo.toml --locked --release
rtk cargo deny --manifest-path rust/Cargo.toml check
rtk cargo tree --manifest-path rust/Cargo.toml -e features
rtk uv run python scripts/validate_release_docs.py
rtk uv run python scripts/validate_runtime_package_boundary.py
rtk git diff --check
```

### Results

- Final full serial workspace run: 939 passed, 1 ignored, 73 suites.
- Focused identity run: 3 passed; legacy-key lifecycle unit test: 1 passed.
- Strict default Clippy: pass. No-default check and strict Clippy: pass.
- Locked release build: pass. Release binary: 31,811,040 bytes; SHA-256 `5b448d86506388a95680f51ffa76fed9717203b2e2f3d7a341aec4f89c0882e1`.
- `cargo deny`: advisories, bans, licenses, and sources passed; existing duplicate-version warnings were reported.
- Release-doc and runtime-package-boundary validators: pass. `git diff --check`: pass.
- Disposable release-binary bootstrap/list smoke: pass; token plaintext was withheld on captured stdout.
- One earlier full run reported `operations_o008::verified_replacement_self_checks_and_preserves_other_files` as `SelfCheckFailed`. The isolated full `operations_o008` target then passed all 10 tests, and the final full workspace run passed. No change was made to update self-check behavior.
- Local resource observation: `rtk /usr/bin/time -l cargo test --manifest-path rust/Cargo.toml --test on_prem_identity -- --test-threads=1` completed with 13.76 s wall and 72,011,776-byte maximum RSS for the Cargo/test process tree (three tests, including concurrent password hashing). Host: Darwin 25.6.0 x86_64. This aggregate desktop measurement is not SBC or per-hash calibration evidence.

## 5. Invariant review

- One SQLite worker/gate remains the only database writer; password hashing
  occurs before the database transaction and uses bounded blocking workers.
- Migration history is append-only and old migration checksums are unchanged.
- Upstream provider accounts/configuration and inference wire shapes remain
  owned by their existing modules.
- Personal tokens do not grant dashboard/admin access; the legacy global key
  is rejected after identity cutover. Database-state errors do not enable a
  keyless bypass.
- Secrets are excluded from ordinary diagnostics and password/token plaintext
  is not persisted.
- `--no-default-features` compilation and strict Clippy pass.

## 6. Failure and recovery review

The admin row and initial credential share one transaction; concurrent
bootstrap produces one winner. Password hashing failure occurs before the
transaction and cannot leave a partial administrator. Password reset and
credential revocation are transactional. Credential verification is
fail-closed on database errors and revocation has no positive token cache.
Failed token digests are bounded to 256 entries with a one-second TTL.

Recovery remains local through `eggpool users reset-password`; it never
reveals the old password and revokes that user's existing API credentials.
Before downgrading to a binary that predates schema 55, retain a protected
pre-upgrade DB backup: migration is forward-only, and older binaries may reject
the newer migration ledger. Restore through the existing staged backup/restore
validation path.

## 7. Migration and compatibility review

Fresh databases begin `fresh_uninitialized`; existing databases receive the
additive migration seeded as `legacy_unmigrated`. Existing shared-key and
dashboard behavior therefore continues until local identity bootstrap. The
bootstrap transition commits the admin and initial token together and changes
the state to `admin_initialized`; after that, the configured global key no
longer authenticates requests. The migration does not update request history,
provider credentials, or upstream account rows. There is no down migration.

## 8. Security review

Passwords require 12–1024 bytes and are hashed with Argon2id using a fresh
OS-random salt. Parameters are fixed and PHC parameters are checked before
verification; hashing concurrency is capped at two. Personal tokens contain a
high-entropy random secret, are stored only as selector plus digest, and are
compared at fixed digest length. Token plaintext is one-time output only.
This is an infrastructure milestone: cookie sessions, login throttling, CSRF,
and browser route authorization belong to M002.

## 9. Documentation and operations

Updated `architecture/deep-dive-security.md`,
`architecture/deep-dive-database.md`, `docs/deployment.md`,
`docs/cli-reference.md`, `docs/backup-restore.md`, and
`docs/agent-configuration.md`. Release-doc link/phrase validation passed.
The disposable CLI smoke used an isolated `HOME`, config, and data directory;
it did not start a listener or contact provider services.

## 10. Unresolved findings

| Severity | Finding | Impact | Required action |
|---|---|---|---|
| medium | No physical Le Potato/Pi Argon2 latency, RSS, or concurrent-login measurement was available. | The fixed 19 MiB / two-worker bound has no demonstrated memory/latency margin on the supported SBC class. | In M007, run the documented qualification on physical Le Potato/Pi hardware: single and two concurrent password hashes, wall time and peak RSS, under representative service load; record model, OS, storage, command, repetitions, p50/p95, peak RSS, and pass/fail against safe memory headroom. Adjust parameters only through a reviewed corrective if the bound fails. |

## 11. Roadmap disposition

M001 is conditionally closed. Production/schema/bootstrap and credential
contracts are complete and M002 can proceed against them. SBC hash calibration
remains an explicit M007 operational condition; no SBC performance claim is
made here.

## 12. Registry updates

The same closure commit marks implementation plan M001 `implemented`, updates
the roadmap M001 to `conditionally closed`, marks M002 `ready`, promotes M002
to dependency-ready, removes it from blocked work, and records this closure in
the registry. M003–M007 remain blocked on their named hard predecessors;
physical qualification remains assigned to M007.

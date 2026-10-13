# On-Prem User Access Milestone 001 — Admin bootstrap and client credentials

Status: implemented

Repository baseline: `main` at `9a0a903c9944b69a6c1ae9fa969bb8603d5c65d9` (architecture/roadmap authority committed on `plans/on-prem-small-team-user-access` at `81beec4e215e8991880d407ed2752d01ff88d58a`)

Source roadmap:
- `plans/subsystems/on-prem-user-access-roadmap.md#m001--secure-admin-bootstrap-and-client-credentials`

Long-term requirements:
- `plans/000-long-term-specification.md#2-end-state-invariants-normative`
- `plans/001-terminology-and-domain-model.md#identity-and-lifecycle-terms`
- `plans/002-long-term-roadmap.md#phase-5--optional-on-prem-small-team-access-and-budgeting`

Applicable ADRs:
- `plans/adrs/ADR-0002-on-prem-user-identity-and-policy-boundaries.md` (accepted)

Primary class: infrastructure

## 1. Objective and boundary

Provide an atomic, local, non-default administrator bootstrap and distinct
per-user API credentials on **fresh** installations, plus safe staged
compatibility for **existing** ones. This milestone is an infrastructure
foundation, not a claim that interactive multi-user web management works.

No hard predecessor beyond ADR-0002 and existing native lifecycle/storage
contracts. This phase must not depend on internet access or a web login.

## 2. Baseline evidence

`rust/src/cli.rs` exposes `Onboard`, `Newkey`, `Getkey`;
`rust/src/runtime.rs` implements their workflows.
`rust/src/server/middleware.rs` checks one constant-time configured key,
and keyless loopback is accepted. `rust/src/config.rs` defaults LAN bind and
public dashboard. `rust/src/db/migrations.rs` embeds checksum-validated
migrations through 0054 and `rust/src/db/connection.rs` is the one worker/gate;
`rust/src/server/mod.rs` builds Axum/EggServe routes. Upstream `accounts`
in `0001_initial.sql` must not become human accounts.
Existing `rust/tests/cli_contract.rs`, `server_transport.rs`,
`database_compatibility.rs` and `operations_o005.rs` are regression owners.

### Always-preserved invariants
- A single on-prem instance; TOML/environment retains upstream provider topology/credentials, model routing, and operator-owned settings. SQLite owns only downstream users, sessions, grants, profiles, budget reservations, consumption and audit.
- No SaaS organizations, hosted signup, embedded reverse proxy/VPN, enterprise identity dependency, separate service/database, provider credential web editor, or assertion of legal authority to share personal subscription seats.
- All legacy inference wire paths and return shapes (Chat Completions, Messages, Responses, Responses compact), streaming/terminal semantics, redaction, configuration lifecycle, and operator CLI remain compatible unless an explicit migration contract states otherwise.
- Respect `GenerationLease`, coordinator publication/finalization compensation, one SQLite gate/worker with WAL/NORMAL, `config_reload_policy.rs` as reload authority, and `--no-default-features` compilation.
- In-flight resource ownership remains deterministic. No database locks span provider I/O or expensive password hashing. No raw prompt, token, password, session, provider key, or cache key reaches diagnostics or ordinary request storage.

### Shared repository verification
Run on implementation and report actual results in the closure record, not the plan:
```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
```
For dependency changes also run locked release build, `cargo deny` and
`cargo tree -e features`; for user-facing release-doc edits run
`uv run python scripts/validate_release_docs.py`. Ensure `git diff --check`.

## 3. Required production changes

### A — Immutable user identity and migration
Create `users` with stable opaque user IDs, unique normalized usernames,
explicit administrator role, active/disabled state, first-login/password
change requirement and password-hash metadata. Add `user_credentials`
(per-user label, scope, irreversible HMAC/digest, lookup selector,
created/last-used/expiry/revoked state); reserve naming space for later
`web_sessions`, policies, and audit. Prefer Argon2id
with per-password salt, independently reviewed bounded parameters calibrated
for Le Potato/Pi class, an explicit hash-algorithm/parameter version, and
a strict parallelism/rate limit. A zero-byte/plain password is forbidden.
Use OS cryptographic RNG for high-entropy, prefix-identifiable client secrets;
display only at creation and hash at rest. Do not use upstream `accounts`.

Add checksum-verified, append-only `0055_*.sql` migration and update
`rust/assets/db/migrations/checksums.json` via the existing build process
(not hand-rewriting old entries). Extend backups, restores, migration and
permissions qualifications; schema upgrades are all-or-nothing.

### B — Local administrator bootstrap
Extend `eggpool onboard` and the local operations/CLI boundary to create
one admin using interactive TTY username/password+confirmation; provide
non-echo stdin or FD input for unattended installation, no argv password,
default `admin` username allowed but **no default password**. Initial
setup must be idempotent and race-safe: only one initial admin transaction,
no remote bootstrap endpoint, no bootstrap flag in GET requests, no
network-unlocked empty DB transition. Add a local `eggpool users` CLI
entry for inspection and protected password reset/recovery, preserving
local config/data-dir ownership and symlink refusal. A recovery reset
invalidates credentials/sessions according to an explicit documented
security choice; it must not reveal existing passwords.

### C — Runtime personal API key authentication
Implement the credential validator at the existing ingress middleware,
accepting `Authorization: Bearer` / `x-api-key`, supporting constant-time
verification, bounded lookup caches and negative-rate controls. API keys
are inference/read-only integration credentials, **never** web-admin grants.
Create a token for the first admin through protected CLI output/TTY; keep
`EGGPOOL_API_KEY` environment semantics (a per-user token fits).
No password-to-inference fallback. Maintain current configured global server
key as a **scoped legacy inference credential only** during explicit
migration. No extra auth server or HTTP stack.

### D — Upgrade staging and feature state
Define a tiny explicit auth lifecycle (`legacy_unmigrated`,
`admin_initialized`, later `managed`) with startup checks. Pre-existing
configs/databases must start with their existing credentials and dashboard
semantics until a documented local migration command is executed; avoid
unattended systemd lockout. Fresh installations must not start in
unauthenticated loopback or expose an admin/default credential. Until M002
adds browser sessions, no extra users or mutating web-admin routes are
enabled. Do not turn `dashboard.public=true` into a write privilege.
`getkey/newkey` retain legacy semantics only while legacy mode remains
valid, with guided replacement and explicit no-global-key behavior after
cutover. Do not automatically rotate unrelated upstream provider secrets.

## 4. Ordered work packages

1. Finalize credentials/roles/scheme and append migrations; prove zero
   destructive changes on fixtures and restored backups.
2. Add local bootstrap/recovery operations and CLI; prove fresh vs upgrade
   behavior, stdin secrecy and concurrent first-run exclusivity.
3. Add personal token auth + legacy scoped key verification; prove agent
   surface compatibility, no server-admin privilege or secret logging.
4. Wire single-user fresh installation and migration diagnostics into
   onboard/init, installer qualification, operations/docs; keep dormant user
   API/browser features for M002.

## 5. Failure, cancellation, restart and contention

Bootstrap commits identity and password hash together; interrupted stdin
cannot leave an empty-password admin. Password hashing happens outside DB
critical sections with bounded memory concurrency. Concurrent bootstrap
attempts return one committed admin. Token creation and revocation are atomic;
cache entries cannot outlive revocation once managed in later milestones
(design invalidation contract now). Failure to read identity state in
managed/fresh configuration fails closed, not keyless loopback.
No locks held during provider I/O. A daemon restart retains token hashes
but never resurrects revoked keys.

## 6. Compatibility and migration

No changes to provider TOML or upstream `accounts`.
Legacy inference key remains usable only before supervised cutover and cannot
gain admin powers; legacy user identity/usage separation is specified but
attribution lands in M003. Document upgrade rollback limits for auth schema,
protected DB backup, recovery from lost administrator password, and how
existing `configremote` desktop profiles obtain per-user tokens.

## 7. Required tests and verification

Create `rust/tests/on_prem_identity.rs`. Extend
`rust/tests/cli_contract.rs`, `server_transport.rs`,
`database_compatibility.rs`, `operations_o005.rs`, and installer tests.
Cover no password defaults, password equality/entropy rules, Argon2 verification,
bounded malicious-login concurrency, header precedence/shape/constant-time
checks, token expiration/revocation, restored legacy keys, keyless-loopback
fresh rejection, ephemeral `HOME` with non-interactive stdin,
simultaneous bootstraps, interrupted writes and no sensitive diagnostics.
Run focused:
```bash
cargo test --manifest-path rust/Cargo.toml --test on_prem_identity -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test server_transport -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test database_compatibility -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test cli_contract -- --test-threads=1
```
Also run the shared matrix above. New password hashing dependency must
qualify Rust binary size/RSS and `--no-default-features`; no network services.

## 8. Documentation, acceptance, stop and closure

Update `architecture/deep-dive-security.md`,
`architecture/deep-dive-database.md`, `docs/deployment.md`,
`docs/cli-reference.md`, `docs/backup-restore.md`, and
`docs/agent-configuration.md`. Do not imply browser login exists.

Accept only when fresh admin setup, agent key, legacy upgrade,
password reset, restart and schema restore work without a default
credential or external dependency. Stop if migration would lock out an
unattended existing installation or alter provider auth/topology.
Closure file `plans/closure/on-prem-user-access/001-status.md` must include
commits, exact migration/checksum fixture evidence, actual commands/results,
local adversarial tests, SBC password-hashing measurements, legacy upgrade,
rollback/recovery notes, and registry unblock audit. Never call M001
multi-user web authorization.


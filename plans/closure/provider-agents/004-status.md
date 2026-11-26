# Provider Agents Milestone 004 — Closure Status

Status: conditionally closed

Source implementation plan:

- `plans/implementation/provider-agents/004-retire-direct-oauth-custody.md`

Source subsystem roadmap:

- `plans/subsystems/provider-agents-roadmap.md#7-milestones`

Repository baseline reviewed: `bead2b63` (Conditionally close the provider agents
origin and health milestone)

Implementation commits or pull requests:

- `f64f74a4` — Retire direct provider OAuth custody

## 1. Executive finding

The M004 capability boundary is complete and verified. EggPool no longer holds a
provider OAuth credential at all. The `[oauth]` configuration, the
`AccountAuthMode::OAuth` account kind, the server-side credential store, the
Web UI and desktop CLI login surfaces, the `oauth_credentials_write` identity
scope, the archive's OAuth members, and the ADR-0015 owner-scoped refresh retry
are removed. A legacy configuration now fails closed with a typed error that
names the remediation command; a legacy credential directory is detected,
reported, and preserved; and one explicit, confirmed, idempotent command removes
only the legacy OAuth surfaces and reports every account and its owner. No
OAuth account is converted into an agent account, because a provider grant
cannot be transferred to an agent without the user's local login.

The milestone is **conditionally closed**, not closed, for the reasons recorded
in §10: the repository-wide dependency-audit gate is red on two pre-existing
`russh` advisories that M004 neither introduced nor can resolve, and the live
ticket producer in `eggstack/eggpass#10` does not exist, so no user can yet
produce the real artifact whose absence the migration must explain. M004's own
plan names the live producer as a stop condition and a fixture-only path is
explicitly insufficient to retire a user capability.

## 2. Requirement-to-evidence matrix

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| A legacy configuration fails closed with a typed error | `rust/src/config.rs` `ConfigError::LegacyOAuth { surface }` | pass | The error names `eggpool accounts migrate-oauth` |
| The check runs before any generic field error | `rust/src/config.rs` `from_toml_bytes` | pass | The legacy check precedes the `deny_unknown_fields` deserialization, so the typed error wins |
| Every legacy configuration shape is detected | `rust/src/config.rs` `legacy_oauth_surface` | pass | A top-level `[oauth]` table, a top-level `[[accounts]]` entry, and a `providers.<id>.accounts` entry, each with `auth_mode = "oauth"` |
| Detection and removal cannot diverge | `rust/src/config.rs` `strip_legacy_oauth_surfaces` | pass | One shared account predicate; the migration command delegates to it |
| A legacy credential directory is reported and preserved | `rust/src/config.rs` `legacy_oauth_directory` | pass | Detected at startup, reported, never read, moved, or deleted implicitly |
| The legacy directory does not block startup | `rust/tests/oauth_removal.rs` | pass | The directory alone leaves the configuration loadable |
| The migration command is explicit | `rust/src/cli.rs` `migrate-oauth`; `rust/src/runtime.rs` | pass | `eggpool accounts migrate-oauth`; confirmation, live-server refusal, and a machine-readable summary |
| The migration reports every account and its owner | `rust/src/operations/account_migration.rs` | pass | Provider, account, and owner, with no secret material |
| The migration never converts a grant into an agent credential | `rust/src/operations/account_migration.rs` | pass | Only OAuth accounts and OAuth files are removed |
| The migration is idempotent | `rust/src/operations/account_migration.rs` | pass | A second run reports `nothing_to_do` |
| The migration is atomic per file and compare-and-swap on the configuration | `rust/src/operations/account_migration.rs` | pass | A configuration changed after the preview aborts the run |
| The migration refuses to run against a live server | `rust/src/operations/account_migration.rs` | pass | PID file and control-socket probe; `--allow-live-server` only after coordination |
| The migration requires confirmation | `rust/src/operations/account_migration.rs` | pass | A non-terminal run without `--yes` is refused |
| The backup recommendation is presented in both output modes | `MigrationSummary.backup_recommendation` | pass | Carried inside the JSON summary so stdout stays one document |
| New backups carry no OAuth member | `rust/src/operations/backup.rs` `write_archive` | pass | Only `META`, `config.toml`, `.env`, and `usage.sqlite3` are written |
| The backup format is version 2 | `rust/src/operations/backup.rs` `BACKUP_FORMAT_VERSION` | pass | A version-2 archive carrying an OAuth member is rejected |
| A legacy archive restores safely | `rust/src/operations/backup.rs` | pass | The OAuth members are discarded and the omission is reported |
| A legacy archive whose configuration carries OAuth material restores | `rust/src/operations/backup.rs`; `rust/src/config.rs` | pass | The archived configuration is restored byte-exact and the leftover legacy material is reported |
| The restored legacy configuration still fails closed | `rust/src/config.rs` | pass | The live loader is unchanged; the operator is told to run the migration |
| The live credential directory survives a restore | `rust/src/operations/backup.rs` | pass | Restore never clears the live directory |
| The OAuth 401 refresh retry is gone | `rust/src/coordinator/` | pass | No refresh producer remains anywhere in the tree |
| The Agent 401 terminalization is unchanged | `rust/src/coordinator/mod.rs` `agent_401_for_account` | pass | The same gate, now on `AccountAuthMode::Agent` |
| The static-key 401 policy is unchanged | `rust/src/coordinator/mod.rs` | pass | Asserted by the coordinator unit test |
| The published failure vocabulary did not move | `rust/src/coordinator/failure.rs` | pass | `RetryScope` and `NextAction` are unchanged and `Wire` is still produced |
| The `eggpass-auth` dependency is removed | `rust/Cargo.toml`, `rust/Cargo.lock` | pass | The lock diff removes only `eggpass-auth` and adds nothing |
| No dependency pin moved | `rust/Cargo.toml` | pass | Eggfetch `=0.4.0`, Eggress `=1.0.12`, and EggServe `=0.4.0` are unchanged |
| `rpassword` and `nix` are retained | `rust/Cargo.toml` | pass | Unchanged |
| The removed surfaces cannot return | `rust/tests/oauth_removal.rs` | pass | Modules, symbols, routes, the CLI command, the scope, and the config key are all guarded |
| `--no-default-features` parity | the full suite in both feature configurations | pass | |
| The reload authority is unchanged | `rust/src/config_reload_policy.rs` | pass | Only the `oauth.enabled` disposition was removed; `classify_transition` remains the sole authority |

## 3. Production implementation evidence

Landed ownership:

- `rust/src/config.rs` — `ConfigError::LegacyOAuth`, `legacy_oauth_surface`,
  the shared `strip_legacy_oauth_surfaces` helper, and
  `from_toml_bytes_allowing_legacy_oauth`, the archive-only tolerant parse.
  The live loader is unchanged and still fails closed.
- `rust/src/operations/account_migration.rs` — the migration: the
  secret-free preview, the owner reconciliation between the credential file and
  the durable binding, the comparison-and-swap on the configuration, the
  per-file removal with progress journalling, the atomic configuration
  replacement, the idempotent no-op path, the machine-readable summary, and the
  redacted `Debug`.
- `rust/src/operations/backup.rs` — `BACKUP_FORMAT_VERSION = 2`, the
  archive-only tolerant validation, the omitted-member report, and the
  restored-legacy-configuration report.
- `rust/src/runtime.rs` — the `migrate-oauth` command, the migration's
  confirmation and live-server handling, the JSON summary, and the restore
  operator message.
- `rust/src/cli.rs` — the `accounts migrate-oauth` command.
- `rust/src/identity.rs` — the `oauth_credentials_write` scope and its
  route mapping removed.
- `rust/src/coordinator/` — the ADR-0015 refresh producers removed and the
  Agent 401 terminalization retained.

Removed: `rust/src/oauth.rs`, `rust/src/server/oauth_login.rs`,
`rust/src/server/oauth_credentials.rs`, `rust/src/operations/oauth_login.rs`,
`rust/src/coordinator/oauth_integration_tests.rs`, and
`rust/tests/provider_oauth.rs`.

Added: `rust/src/operations/account_migration.rs` and
`rust/tests/oauth_removal.rs`.

Dependency change: `eggpass-auth` removed. Nothing else changed; no advisory
waiver was added.

## 4. Verification executed

### Commands run

All commands below were run LOCALLY on 2026-11-26 in the authorized worktree.
No commit was pushed and no hosted CI run was executed for this change, so no
CI evidence is claimed.

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- --test-threads=1
cargo build --manifest-path rust/Cargo.toml --locked --release
cargo deny --manifest-path rust/Cargo.toml check
uv run pytest tests/tooling/ -q --tb=short --maxfail=1
uv run python scripts/validate_release_docs.py
git diff --check
```

### Results

| Check | Result |
|---|---|
| `cargo fmt --check` | pass |
| strict Clippy, default features | pass, zero warnings |
| `cargo check --no-default-features` | pass |
| strict Clippy, `--no-default-features` | pass, zero warnings |
| full serial workspace suite, default features | 1335 passed, 0 failed, 2 ignored (98 result blocks) |
| full serial workspace suite, `--no-default-features` | 1336 passed, 0 failed, 2 ignored (98 result blocks) |
| `cargo build --locked --release` | pass |
| `cargo deny check` | **advisories FAILED**; bans ok, licenses ok, sources ok — see §10 |
| tooling suite | 267 passed, 1 skipped |
| `scripts/validate_release_docs.py` | pass |
| `git diff --check` | clean |

The `cargo deny` result is a genuine failure of the advisory gate and is not
reported as a pass. Its scope is established in §10.

### Independent review and the corrective pass

An independent verifier audited the uncommitted implementation before closure.
It returned one HIGH, one MEDIUM, and two LOW findings plus three test-coverage
gaps. All were resolved:

- HIGH — a pre-removal backup whose configuration carried the removed OAuth
  material could not be restored, because the archive's staged configuration was
  validated with the new fail-closed loader and so was rejected before any file
  was written, even though the archive's OAuth members were already discarded.
  A restore is a fidelity operation, so the archived configuration is now
  validated against a view with the legacy surfaces removed, restored
  byte-exact, and reported; the live loader is unchanged and still fails
  closed, so the operator is told to run the migration before restarting.
  The new archive-only tolerant parse shares one removal authority with the
  migration command, so a legacy shape cannot be recognised in one place and
  missed in the other.
- MEDIUM — the provider-oauth roadmap and the registry still described the
  direct OAuth path as active. This was a records gap, not a code defect, and
  is resolved in this closure: the roadmap is `superseded` with an
  append-only note, and the registry records the supersession.
- LOW — the migration's backup recommendation was printed only in the
  human-readable mode, so `--json --yes` could remove configuration entries
  and credential files without ever presenting it. The recommendation is now
  carried inside the JSON summary, so stdout stays exactly one document and the
  recommendation is always presented.
- LOW — the migration plan derived `Debug`, so formatting it could print the
  raw configuration bytes, which may include a plaintext server key. The
  derived implementation is replaced with a manual one that reports only safe
  facts.
- TEST GAPS — the v1 restore fixture now carries a legacy configuration; the
  live loader's continued fail-closed behaviour is asserted against the same
  bytes; the `--json --yes` output is asserted to be exactly one document
  carrying the recommendation; and the migration plan's `Debug` is asserted
  to contain neither the configuration bytes nor a secret sentinel.

Two further defects were found by the parent during this pass and fixed here:
the generated default configuration still emitted an `[oauth]` section, which
the new fail-closed loader correctly refused to load, and the hash-locked
runtime asset manifest and the CLI command census still recorded the pre-removal
state.

## 5. Invariant review

- **No server-held provider credential.** No code path reads, writes, stores,
  logs, projects, or transitively depends on a provider OAuth credential.
- **Owner binding unchanged.** The owner gate, grants, routing, quota,
  budgets, and accounting model are untouched.
- **Dispatch identity unchanged.** Agent and static-key accounts reach their
  provider exactly as before; the origin projection and the offline exclusion
  from M003 are unaffected.
- **Fail closed, never silently downgraded.** A legacy configuration is
  rejected with a typed error and a named remediation; it is never partially
  loaded, auto-migrated, or silently ignored.
- **No implicit deletion.** A legacy credential directory is detected,
  reported, and preserved. Only the explicit, confirmed migration command
  removes it, and it is atomic per file with a compare-and-swap on the
  configuration.
- **No laundering across owners.** The migration reconciles the owner recorded
  in the credential file with the owner recorded in the durable binding and
  refuses to run when they conflict. It never converts a grant into an agent
  credential, so no credential can move between users.
- **Reload authority unchanged.** `classify_transition` remains the only
  reload-versus-restart authority, and only the `oauth.enabled` disposition
  was removed.
- **The published failure vocabulary did not move.** `RetryScope` and
  `NextAction` keep their public variants, and the `Wire` scope is still
  produced by live paths. The two labels the removed refresh producer invented
  were in-memory only and were never persisted or parsed by anything, so no
  historical row can fail to parse.
- **Performance boundaries unchanged.** The single SQLite gate, the streaming
  mpsc bridge, the `current_thread` Tokio runtime, and the routing selection
  lock are untouched.
- **Closed milestones not regressed.** M001's tunnel ownership, shutdown model,
  owner gate, and terminal Agent 401 are unchanged; M002's ticket decoder,
  enrollment registry, enrollment and revocation routes, and atomic
  publication path keep their behaviour; M003's origin projection, discovery
  branch, and availability snapshot are unchanged.

## 6. Failure and recovery review

- **A legacy configuration at startup.** EggPool refuses to start with the
  typed error naming the migration command; nothing is published and no
  credential is lost.
- **A legacy credential directory at startup.** Startup proceeds; the
  directory is reported and preserved.
- **A live server during migration.** The migration refuses and writes a
  `blocked_live_server` report; nothing is changed.
- **A configuration changed between preview and apply.** The run aborts with
  `ConfigChanged`; the operator re-runs the preview.
- **An interrupted migration.** The report file records the progress made, and
  the configuration is either rewritten atomically or not at all.
- **An unexpected entry in the credential directory.** The migration refuses
  with `UnsafeCredentialDirectory` and changes nothing.
- **A failed restore step.** The existing transactional rollback restores the
  original files; a failed rollback is reported.
- **A legacy archive whose configuration carries OAuth material.** The archive
  restores, the credential members are discarded, the configuration is restored
  byte-exact, and the operator is told to run the migration before restarting.

## 7. Migration and compatibility review

- No storage migration and no schema change. The persisted model, quota,
  health, and binding tables are untouched.
- This milestone intentionally breaks the OAuth configuration and credential
  format. The contract is: detect, report, require confirmation, remove only
  the OAuth-specific artifacts, and document the agent alternative.
- No OAuth account has an automatic conversion, because a provider grant
  cannot be transferred to an agent without the user's local login.
- The backup format moves from 1 to 2. Version-1 archives remain restorable:
  their OAuth members are discarded, the omission is reported, and a
  configuration that carries legacy OAuth material is restored byte-exact and
  reported rather than rejected.
- Existing static-key and agent configurations load unchanged; the new
  configuration keys are additive.

## 8. Security review

- **Secret hygiene.** No credential, token, raw body, or cache key enters
  SQLite, a log, a diagnostic, a projection, an archive member, an error
  message, or a `Debug` output. The migration's `Debug` was a latent path to
  that and is now redacted.
- **The migration summary is secret-free.** It reports provider, account, and
  owner identifiers only.
- **The archive never carries legacy secret bytes.** The OAuth members are
  discarded at parse time and are never written to disk.
- **Privilege bounds.** The migration removes only the accounts it was asked
  to remove, and it refuses to run when the owner evidence conflicts.
- **The operator surface is bounded.** Every reported value is a fixed field
  or an identifier; no endpoint, credential, or free-form configuration text
  is serialized.

## 9. Documentation and operations

- `docs/providers.md` — the agent model, the legacy configuration rejection,
  the preserved credential directory, and the migration command.
- `docs/api-reference.md` — the removed routes and the current agent
  surfaces.
- `docs/cli-reference.md` — `eggpool accounts migrate-oauth`, its
  confirmation and live-server rules, and the machine-readable summary.
- `docs/backup-restore.md` — format 2, the legacy-archive omission report,
  the restored legacy configuration, and the preserved credential directory.
- `docs/deployment.md` — the upgrade path from a legacy OAuth install.
- `architecture/deep-dive-providers.md`, `deep-dive-retry.md`,
  `deep-dive-on-prem-user-access.md`, `deep-dive-runtime.md`,
  `deep-dive-catalog.md`, and `architecture/overview.md` — the post-removal
  ownership, the module inventory, and the route census.
- `AGENTS.md` — the Layout module list.
- `rust/config.example.toml` — the generated default configuration no longer
  carries an `[oauth]` section.

## 10. Unresolved findings

| Severity | Finding | Impact | Required action |
|---|---|---|---|
| high | The live ticket producer in `eggstack/eggpass#10` does not exist. | No user can yet produce the real artifact, so the agent path that replaces OAuth cannot be exercised by a real user, and the migration has no live counterpart to point at. M004's own plan names the live producer as a stop condition and states that a fixture-only path is not sufficient to retire a user capability. | Land the `eggpass eggpool` ticket producer with the agreed `epagent1` format, then re-run the upgrade journey against a live producer. |
| high | The repository-wide dependency-audit gate is red: `cargo deny check` reports advisories FAILED for RUSTSEC-2026-0492 and RUSTSEC-2026-0495 against `russh 0.62.7`, reached through `eggress-transport-ssh 1.0.12` → `eggress-outbound 1.0.12`, which the default `ssh` feature enables. | The `dependency-audit` workflow gates on `rust/Cargo.toml`/`rust/Cargo.lock`/`deny.toml`, so it will fail for this branch. The condition is a pre-existing SSH-transport advisory, not an Agent-path defect. | Owned by Security hardening M008. Resolution requires an Eggress release permitting `russh >= 0.65`, or an accepted ADR for a vendored patched `0.62.x`, or an accepted ADR removing the default `ssh` feature. No advisory waiver is acceptable and none was added. |
| low | The migration's owner evidence treats an unreadable durable database as an empty owner snapshot rather than as an error, so an account whose credential file carries no owner is reported with an unknown owner. | The report is less precise for that account. No credential can move between owners, because the migration removes the OAuth account rather than transferring it, so this is a reporting limit and not a custody hole. | Bound the database read and report the degraded owner evidence explicitly when the durable snapshot is unavailable. |
| low | The mTLS branch of the reverse server's client-certificate handling is still not proven end-to-end, carried forward from M001's and M002's closure records. | The branch is configured and validated but not exercised by an automated test. | Prove the mTLS branch when a per-agent client certificate is issued. |

Scope evidence for the high dependency-audit finding: the identical
`cargo deny check` run against the pre-M001 commit `3b90434b` in a detached
worktree fails with the same two advisories, and M004 removes a dependency
rather than adding one, so the finding is pre-existing, repo-wide, and neither
introduced nor worsened by M004.

## 11. Roadmap disposition

Conditionally closed with named outstanding conditions: the live ticket producer
in `eggstack/eggpass#10` must exist, and the dependency-audit gate must report
`advisories ok` again, before this branch lands on `main`. Neither is an M004
interface defect, and M004 is the subsystem's terminal milestone, so nothing is
promoted.

M004's own capability boundary is complete: no partially implemented removal
path, no unrun required test, no unimplemented security or migration
requirement, and intact `--no-default-features` parity.

## 12. Registry updates

Applied in the same commit as this record:

- `plans/registry.md` — the Provider OAuth roadmap row records
  `superseded`; the Provider agents roadmap row records M001–M004
  conditionally closed and the roadmap as `closing`; the M004 row moves from
  the dependency-ready table to the recently-closed table; a dated audit note
  records the closure, the conditions, and the supersession.
- `plans/subsystems/provider-oauth-roadmap.md` — the status becomes
  `superseded` and an append-only supersession note records the retirement.
  Its M001–M004 closure records are not rewritten.
- `plans/subsystems/provider-agents-roadmap.md` — the milestone status table
  records M004 as conditionally closed, and the roadmap status becomes
  `closing`.
- `plans/implementation/provider-agents/004-retire-direct-oauth-custody.md` —
  the header status becomes `implemented`.

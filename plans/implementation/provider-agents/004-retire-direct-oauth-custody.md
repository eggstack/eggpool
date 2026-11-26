# Provider Agents Milestone 004 — Retire Direct OAuth Custody

Status: implemented

Repository baseline: `c4d774aeb883307f28ef94f6143dc688b3098946`

Source roadmap:

- `plans/subsystems/provider-agents-roadmap.md#7-milestones`

Long-term requirements:

- `plans/000-long-term-specification.md#2-end-state-invariants-normative`
- `plans/000-long-term-specification.md#3-ownership-boundaries-normative`

Applicable ADRs:

- `plans/adrs/ADR-0016-provider-agent-upstreams.md` (accepted; supersedes the custody and refresh decisions)
- `plans/adrs/ADR-0012-provider-oauth-account-custody-and-lifecycle.md` (historical after this milestone)
- `plans/adrs/ADR-0014-provider-oauth-web-ui-and-desktop-flow-boundary.md` (historical after this milestone)
- `plans/adrs/ADR-0015-provider-oauth-owner-scoped-credential-refresh-retry.md` (historical after this milestone)
- `plans/adrs/ADR-0013-provider-oauth-eggfetch-compatibility.md` (dependency pin remains in force; a separate review may revisit it)

Primary class: capability

## 1. Objective

Remove EggPool's server-held provider OAuth path — code, configuration, routes,
CLI, credential files, backup members, and the 401 refresh retry — once agent
accounts replace the capability, with a documented and tested migration that
never silently loses a user's credential or strands an account.

## 2. Why this milestone is ready

Ready.

- Hard: satisfied. M002 and M003 are implemented and conditionally closed
  through `plans/closure/provider-agents/002-status.md` and
  `plans/closure/provider-agents/003-status.md`. Their outstanding conditions
  are external to this milestone — the repository-wide dependency-audit gate
  owned by Security hardening M008, and the live ticket producer in
  `eggstack/eggpass#10` — and neither is an interface dependency of the
  removal, the migration command, or the backup format here, so neither blocks
  implementation.
- Hard: the migration evidence this milestone requires — that every existing
  OAuth account has a documented destination (agent enrollment or static key)
  and that removal leaves no user without a path — is this milestone's OWN
  deliverable, produced by its migration command and its restore report.
- Hard: ADR-0016 acceptance is satisfied (2026-11-24).
- Hard: ADR-0016 acceptance is satisfied (2026-11-24).
- Operational: the live Eggpass ticket producer exists (`eggstack/eggpass#10`);
  a fixture-only path is not sufficient to retire a user capability.

## 3. Current implementation evidence

Remove-inventory at baseline:

- Custody and refresh: `rust/src/oauth.rs`; the OAuth branch in
  `rust/src/catalog/refresh.rs`; `rust/src/coordinator/mod.rs`
  (`oauth_401_for_account`, `refresh_rejected_oauth_credential`,
  `healed_credential_refresh_effects`), `rust/src/coordinator/failure.rs`
  (`terminalize_oauth_401`), and their call sites in
  `rust/src/coordinator/finite.rs` and
  `rust/src/coordinator/streaming/coordinator.rs`.
- Login surfaces: `rust/src/server/oauth_login.rs`,
  `rust/src/server/oauth_credentials.rs`, `rust/src/operations/oauth_login.rs`,
  the `oauth-login` command in `rust/src/cli.rs` and its dispatch in
  `rust/src/runtime.rs`, and the routes in `rust/src/server/mod.rs`
  (`/api/user/oauth/*`, `/api/user/oauth-credentials`,
  `/account/tokens/oauth`).
- Configuration: `OAuthConfig` and `[oauth]` in `rust/src/config.rs`;
  `AccountAuthMode::OAuth`; the `oauth.enabled` transition in
  `rust/src/config_reload_policy.rs`.
- Identity scope: the `oauth_credentials_write` scope and its token-creation
  route in `rust/src/identity.rs` and the dashboard.
- Backup: OAuth members and validation in `rust/src/operations/backup.rs`.
- Tests: `rust/tests/provider_oauth.rs`, `rust/src/coordinator/oauth_integration_tests.rs`,
  the OAuth cases in `rust/tests/on_prem_admin_ui.rs` and
  `rust/tests/on_prem_identity.rs`.
- Records: `plans/closure/provider-oauth/001-status.md` through `004-status.md`,
  the M004 closure audit text, and `plans/subsystems/provider-oauth-roadmap.md`.

## 4. Invariants that must not regress

- Static-key and agent accounts behave exactly as before removal.
- The owner binding, grants, routing, quota, budgets, and accounting model is
  unchanged.
- Historical plans, closure records, and accepted ADRs are not rewritten; the
  roadmap gains an append-only supersession note and registry row.
- No credential file is deleted implicitly. A destructive step is explicit,
  reported, and preceded by a backup recommendation.
- A legacy configuration or credential directory fails closed with a migration
  message, never a silent reset or ignored secret.
- Backup restore remains safe for both new and legacy archives.
- The removal does not entangle a new dependency or Eggfetch-pin decision.

## 5. Scope

### In scope

- Deletion of every OAuth code path, route, CLI command, configuration key,
  credential store, backup member, and identity scope listed above.
- A migration report/command and startup diagnostics for legacy configuration and
  credential directories.
- Backup format versioning and legacy-archive behavior.
- Removal of the ADR-0015 retry producers, with the durable-vocabulary
  disposition recorded.
- Documentation, guards, roadmap/registry supersession.

### Explicitly out of scope

- Rewriting historical closure records or accepted ADRs.
- Revisiting the Eggfetch `0.4.0` pin (a separate dependency review).
- Changing agent enrollment, transport, or model projection.
- Deleting user data without an explicit operator action.

## 6. Required production changes

**Configuration.** Remove `OAuthConfig`, `[oauth]`, `AccountAuthMode::OAuth`,
and the `oauth.enabled` reload key. Add a targeted legacy diagnostic: a
configuration containing `[oauth]` or an `auth_mode = "oauth"` account fails to
load with an error naming the removed surface and the migration document, rather
than a generic unknown-field error. Update
`config_reload_policy::classify_transition` and its tests.

**Code deletion.** Remove the modules and symbols in the inventory. Remove the
`oauth_eggfetch_client` helper if no caller remains. Remove the
`oauth_credentials_write` scope and its dashboard action. Remove the OAuth
branch from catalog refresh and the OAuth cases from the coordinator tests.

**Coordinator.** Delete the refresh producers and the `terminalize_oauth_401`
path. If any frozen compatibility fixture or durable row still carries the
`same_account_refreshed_credential` / `refresh_credential_same_account` labels,
retain the vocabulary constants as producer-less and documented, following the
routing-selection M009 precedent, so historical observations still parse.

**Credential files and migration.** Provide
`eggpool agents migrate-oauth` (name illustrative) that reports every OAuth
account and owner, requires explicit confirmation, refuses to run against a live
server without coordination, removes only `.eggpool-oauth/` files and OAuth
configuration entries, and prints a machine-readable summary. It must be
idempotent and must never touch agent or static accounts. Without the explicit
command, a legacy directory is detected and reported but not deleted.

**Backup.** Bump the backup metadata/format version so new archives exclude OAuth
members. A legacy archive containing OAuth members restores without them and
reports the omission. Backup validation and restore tests cover both.

**Docs and records.** Update provider, CLI, backup, architecture, and AGENTS.md
references. Append a supersession note to the provider-oauth roadmap (status
`superseded`) and update the registry in the same change.

## 7. Ordered work packages

### Work package A — Inventory and guards

Intent: make removal verifiable.

Required changes: a guard test that asserts no OAuth route, config key, CLI
command, module, or scope remains; a baseline inventory list.

Acceptance evidence: the guard fails on a deliberately retained OAuth route/key
and passes after removal.

### Work package B — Code and configuration removal

Intent: delete the path.

Required changes: modules, symbols, routes, CLI, scope, tests; reload-policy
update; legacy configuration diagnostic.

Acceptance evidence: default and no-default builds; static-key and agent tests
unchanged; a legacy config test produces the migration error.

### Work package C — Credential and backup migration

Intent: leave no secret stranded and no backup broken.

Required changes: migration command/report, legacy directory detection, backup
format bump, legacy-archive behavior, docs.

Acceptance evidence: migration on a fixture removes only OAuth files/config and
is idempotent; a legacy archive restores without OAuth members and reports it; a
legacy directory without the command is reported and preserved.

### Work package D — Coordinator and vocabulary cleanup

Intent: remove the retry without breaking historical data.

Required changes: delete producers and tests; retain only the vocabulary that
historical rows require and document it as producer-less.

Acceptance evidence: agent and static 401 behavior is unchanged; no
`oauth_401_for_account`/refresh symbol remains; frozen fixtures still parse.

### Work package E — Records and documentation

Intent: make the supersession explicit.

Required changes: roadmap append-only note and status, registry row, provider
and architecture docs, AGENTS.md, CLI reference, backup docs.

Acceptance evidence: the plan-status guard passes; links resolve; no doc claims
an OAuth route exists.

## 8. Failure, cancellation, restart, contention semantics

- The migration command is atomic per file and idempotent; an interruption leaves
  a report and no partially rewritten configuration.
- A live server refuses an uncoordinated destructive migration.
- A legacy config fails startup with a typed error; it is never silently
  downgraded.
- Backup restore is transactional as today; an omitted OAuth member is reported,
  not treated as corruption.
- Removing the refresh producers must not change any other retry classification
  or durable row.

## 9. Compatibility and migration

This milestone intentionally breaks the OAuth configuration and credential
format. The migration contract is: detect, report, require confirmation, remove
only OAuth-specific artifacts, and document the agent alternative. Existing OAuth
accounts have no automatic conversion because a provider grant cannot be
transferred to an agent without the user's local login.

## 10. Required tests

- `rust/tests/oauth_removal.rs`: no route/config/CLI/module/scope remains; legacy
  configuration error; legacy directory detection.
- Migration tests: report, confirmation, idempotence, agent/static isolation,
  interrupted run.
- `rust/tests/backup_restore.rs` or the existing backup target: new format, legacy
  archive omission, transactional failure.
- Coordinator tests: agent and static 401 unchanged; no refresh producer.
- `rust/tests/provider_agents` fixtures still pass.
- Default and no-default serial suites; strict Clippy; `cargo deny`; locked
  release build.

## 11. Required verification commands

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --test oauth_removal -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test backup_restore -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- --test-threads=1
cargo deny --manifest-path rust/Cargo.toml check
cargo build --manifest-path rust/Cargo.toml --locked --release
```

## 12. Documentation updates

- `docs/providers.md`: remove OAuth custody; describe agent and static accounts.
- `docs/cli-reference.md`: remove `accounts oauth-login`; keep personal tokens.
- `docs/backup-restore.md`: new format and legacy behavior.
- `architecture/deep-dive-providers.md`, `deep-dive-retry.md`,
  `deep-dive-on-prem-user-access.md`: post-removal ownership.
- `AGENTS.md`: remove OAuth ownership entries; add `rust/src/agents/`.
- `plans/subsystems/provider-oauth-roadmap.md`: append-only supersession note and
  status.
- `plans/registry.md`: roadmap and plan status in the same change.

## 13. Acceptance criteria

- No OAuth route, config key, CLI command, credential store, backup member, or
  refresh producer remains.
- A legacy configuration or credential directory is detected and reported with a
  migration path; no secret is silently deleted or ignored.
- Static-key and agent accounts pass their full behavior suites unchanged.
- New backups exclude OAuth members; legacy archives restore safely with a
  reported omission.
- The provider-oauth roadmap is `superseded` and its closure records remain
  historical.

## 14. Stop conditions

Stop and report if: M002/M003 are not closed; the live Eggpass producer is absent;
no safe migration path exists for an existing OAuth account; removal would
require a dependency decision (for example the Eggfetch pin); a historical record
would have to be rewritten; or backup/restore cannot remain safe.

## 15. Closure evidence required

- Removal guard output and the final inventory.
- Migration report/confirmation/idempotence evidence.
- Legacy configuration and directory behavior.
- Backup new-format and legacy-archive evidence.
- Coordinator no-refresh and static/agent parity evidence.
- Full matrix results, including no-default tests, `cargo deny`, and the locked
  release build.
- Registry and roadmap supersession references.

## 16. Handoff notes

This is a destructive milestone. Make the migration explicit and reversible via
backup, not implicit. Do not rewrite historical plans or ADRs. Do not fold the
Eggfetch pin review into this work. Rust tests run serially.

# On-Prem Member Access Milestone 004 — Personal upstream custody and routing

Status: implemented

Repository baseline: `9da5e2d383c73035850c37f8125c145b8b7ff9ff`

Source roadmap:

- `plans/subsystems/on-prem-member-access-roadmap.md#milestone-4--personal-upstream-custody-and-routing`

Long-term requirements:

- `plans/000-long-term-specification.md#2-end-state-invariants-normative`
- `plans/000-long-term-specification.md#3-ownership-boundaries-normative`
- `plans/001-terminology-and-domain-model.md#identity-and-lifecycle-terms`
- `plans/002-long-term-roadmap.md#phase-5--optional-on-prem-small-team-access-and-budgeting`

Applicable ADRs:

- `plans/adrs/ADR-0010-personal-upstream-account-custody.md` (accepted)
- `plans/adrs/ADR-0002-on-prem-user-identity-and-policy-boundaries.md`

Primary class: infrastructure

## 1. Objective

Let a local administrator attach a bundled-provider upstream secret to one
user, keep the secret out of SQLite, and make routing select that account
only for that user. Provide the CLI consumer in the same milestone so the
binding is observable.

## 2. Why this milestone is ready

It is not ready. ADR-0010 is proposed. Start only after a maintainer marks
that ADR accepted and the acceptance change includes the canonical sentence
amendments the ADR names. Those amendments are work package A below; they
are not permission to invent a different custody design.

No code dependency on milestones 001–003.

## 3. Current implementation evidence

`AccountConfig` in `rust/src/config.rs` holds an optional inline `api_key`
and an `api_key_env`. `AccountIdentity` in `rust/src/accounts/registry.rs`
is credential-free and has no owner. `CredentialStore` keeps secrets out of
routing identity objects. Eligibility in `rust/src/routing/eligibility.rs`
filters on enabled, health, catalog, and quota. It does not see a user id.

`classify_transition` treats account topology as live-reloadable and
redacts secret paths. `connect_with_transition` in
`rust/src/operations/config_mutation.rs` writes operator accounts.
`rust/src/operations/paths.rs` resolves the config-adjacent `.env`.

The migration chain is v1–v65. v65 is the withdrawn tool-grant tables.
The next migration is v66. ADR-0002 forbids moving upstream secrets into
the database. Invariant 8 forbids credentials in persistence, logs, and
diagnostics.

Bundled provider ids live in
`rust/crates/eggpool-provider-profile/assets/_provider_profiles.toml`.
`opencode-go` is present. Amazon Bedrock is not.

## 4. Invariants that must not regress

- One SQLite writer. No per-request topology mutex.
- `ProviderClientPool` still publishes an immutable topology and closes it
  atomically.
- `classify_transition` remains the only reload authority. No new parallel
  key list.
- Secrets stay out of SQLite, logs, audit, reload diagnostics, and
  `AccountIdentity`.
- An unbound account stays shared. A bound account is never shared because
  a file write was only half finished.
- Model policy, user budgets, provider health, and provider quota still
  apply. Selection stays load-based.
- `--no-default-features` still compiles. This milestone adds no SSH
  feature.
- v1–v65 files and checksums stay byte-identical.

## 5. Scope

### In scope

- Canonical amendments named by the accepted ADR.
- v66 binding and grant tables.
- CLI `eggpool users upstream`.
- TOML plus `.env` transaction and generation snapshot.
- Eligibility filter before scoring.
- Audit rows that name the account and not the secret.

### Explicitly out of scope

- Browser forms. Milestone 005.
- A new provider profile, including Amazon Bedrock.
- Member-supplied URLs, proxies, headers, or weights.
- Pinning the personal account ahead of a shared account the same user
  may also use.
- Letting a personal API token perform the mutation.

## 6. Required production changes

Work package A edits only the sentences ADR-0010 names in the long-term
documents and adds the supersession pointer on ADR-0002. It does not
rewrite ADR-0002's Decision.

v66 adds:

- `personal_upstream_grants`: singleton instance default `deny`, and
  per-user rows of `allow`, `deny`, or `inherit`.
- `personal_upstream_bindings`: `provider_id`, `account_name`, `user_id`,
  `state` of `active` or `withdrawn`, actor, timestamps. Unique on
  `(provider_id, account_name)`. No secret column.

The CLI, from the machine that owns the data directory:

- `eggpool users upstream default allow|deny`
- `eggpool users upstream grant USER allow|deny|inherit`
- `eggpool users upstream add USER --provider PROVIDER --name NAME`
- `eggpool users upstream list [USER]`
- `eggpool users upstream remove USER --provider PROVIDER --name NAME`

The secret is read with the same hidden TTY or `--secret-stdin` pattern as
passwords. Argv is rejected. `--provider` must be a bundled profile id.
The endpoint written into TOML is copied from that profile. The account
stanza sets `api_key_env` to a new variable. The variable's value is
appended to the config-adjacent `.env` with mode `0600` on the containing
directory's existing secret practice.

Add order, which fails closed:

1. Reject a name that already exists in config or in a binding.
2. Commit an `active` binding while the account is still absent from
   config. An absent account is ineligible, not shared.
3. Publish the TOML stanza and the `.env` line by the existing atomic
   config write.
4. Rehash through `classify_transition`.
5. On publication or rehash failure, restore the previous TOML and `.env`
   bytes and delete the binding.

Remove order:

1. Set the binding to `withdrawn`. A withdrawn account is ineligible for
   every principal, including the owner.
2. Publish a config and `.env` that omit the account, then rehash.
3. Delete the binding only after that publication succeeds.
4. On failure, leave the binding `withdrawn` and report that the account
   is not routable and the files need operator repair. Do not delete the
   binding while the stanza remains.

The generation snapshot copies active bindings. Eligibility, before score,
drops an account whose snapshot binding is `withdrawn` or whose owner is
a different user id. No binding means shared. A legacy shared-key
principal has no user id and cannot select a bound account. Ambiguous
ownership excludes the account.

Disabling a user withdraws that user's active bindings in the same
transaction that disables the user, then the next publication excludes
those accounts. Re-enabling the user does not by itself reactivate a
withdrawn binding.

Default `deny` means an instance that never calls the CLI has no new
rows that change routing. Shared `eggpool connect` accounts remain the
solo path.

## 7. Ordered work packages

### Work package A — Acceptance wording

Intent: The long-term sentences and ADR-0002 pointer match the accepted
ADR before runtime code depends on them.

Required changes: The edits ADR-0010 lists. Stop if the ADR status is not
`accepted`.

Acceptance evidence: The diff of `plans/000-long-term-specification.md`,
`plans/001-terminology-and-domain-model.md`,
`plans/002-long-term-roadmap.md`, and ADR-0002 is limited to those
sentences and the supersession pointer.

### Work package B — Schema and CLI grant

Intent: The grant and binding exist, and the default does nothing to
existing accounts.

Required changes: v66, checksum, repositories, and the grant/list/remove
commands. `add` may land in work package C if that keeps the file
transaction together; the grant commands must exist before `add` is
callable.

Acceptance evidence: Migration from a v65 database preserves accounts and
adds the default deny. A second migrate is a no-op. `list` shows no
personal accounts. Secrets cannot be inserted by the repository API.

### Work package C — File transaction and eligibility

Intent: `add` publishes a personal account that only the owner can use,
and a failed reload cannot leave it shared.

Required changes: The order in section 6, the generation snapshot, and
the filter in `rust/src/routing/eligibility.rs`. Pass the principal user
id from the admitted request into selection. Do not take it from the body.

Acceptance evidence: Owner traffic can select the account. Another member
and the legacy key cannot. A forced rehash failure restores the previous
files and leaves no active shared account under that name. Reload
diagnostics for the change contain the env var name and not the secret.
SQLite backups of the new tables contain no secret. A withdrawn account
is selected for nobody.

## 8. Failure, cancellation, restart, contention semantics

Two concurrent `add` commands for the same name: the unique index rejects
the second. A crash after the binding commit and before the file publish
leaves an inert binding and no routable account. A crash after the file
publish and before rehash is repaired on the next process start by the
snapshot: the binding is active, so the account is personal, not shared.
A crash during remove leaves `withdrawn`, which is ineligible. Restart
rebuilds the snapshot from SQLite plus config. In-flight requests keep
the generation they acquired. Cancellation of the CLI before commit
leaves no partial binding.

## 9. Compatibility and migration

v66 is append-only. Existing accounts have no binding. Existing `.env`
keys are preserved. Config that already uses an inline `api_key` for a
shared account is unchanged. Personal accounts use `api_key_env` only.
The public inference body is unchanged. `/v1/models` for a member lists
models the member's policy allows; it does not need to reveal which
account is personal beyond the account remaining unselectable by others.

## 10. Required tests

- Migration test in the database compatibility target: v65 upgrade, rerun,
  and absence of secret columns.
- `rust/tests/on_prem_identity.rs` for grant default, admin add, collision,
  disable-withdraws, and secret absence from audit.
- Routing tests for owner, other user, legacy principal, withdrawn, and
  missing-config binding.
- A reload-failure test that restores file bytes.
- `--no-default-features` check and clippy.

## 11. Required verification commands

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo test --manifest-path rust/Cargo.toml --test on_prem_identity -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
uv run python scripts/validate_release_docs.py
```

Add the focused routing and database targets once the new tests have
names, and record those names in the closure. If `Cargo.lock` or the
migration checksum list changes, also run the locked release build
required by `AGENTS.md` for that class of change. A migration checksum
update is expected. A dependency change is not.

## 12. Documentation updates

- `docs/cli-reference.md` for the new subcommands.
- `docs/providers.md` for the personal-account relationship to `eggpool connect`.
- `architecture/deep-dive-on-prem-user-access.md` and
  `architecture/deep-dive-routing.md` for the binding and the filter.
- `docs/backup-restore.md` to state that `.env` holds the secret and the
  database binding does not.

## 13. Acceptance criteria

- Before any grant or add, routing of existing accounts is unchanged.
- An administrator can add an `opencode-go` account for one user. The
  secret is only in `.env`. The TOML stanza points at the env var. The
  database row has no secret.
- That user can be routed to the account. Another user and the legacy
  shared key cannot.
- A failed rehash does not leave the account shared.
- A withdrawn account is routed to nobody.
- Self-service default is deny. The CLI can set allow, deny, and inherit.
- Amazon Bedrock is not added.

## 14. Stop conditions

Stop if ADR-0010 is not accepted. Stop if the implementation needs a
secret column, a member-supplied URL, a second SQLite writer, or a new
reload key list. Stop if fail-closed remove cannot be done without a
window where the account is unbound and still present in the published
generation. Stop if the bundled profile for the requested provider does
not exist; report that as a provider-profile gap rather than inventing
the provider here.

## 15. Closure evidence required

ADR acceptance SHA. Canonical diff stat limited to the named sentences.
Migration id and checksum. The routing matrix, the failed-reload fixture,
and a database dump assertion that the secret string is absent. Commands
and results, including no-default clippy. Registry promotion of milestone
005 only in that closure commit, after confirming its interface matches
the shipped binding.

## 16. Handoff notes

This is infrastructure plus a CLI consumer. Do not describe it as the
member-facing capability; milestone 005 is that capability. Do not reuse
v65 tool-grant tables. Do not log the stdin secret on a parse error.
Serial tests only. The eligibility filter must use the authenticated
principal already on the request, the same principal model policy uses.

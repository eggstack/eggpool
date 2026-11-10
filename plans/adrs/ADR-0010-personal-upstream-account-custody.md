# ADR-0010: Personal upstream account custody

Status: accepted

Date: 2026-11-10

Decision owners: project maintainers (explicit product direction, 2026-11-10)

Related specification sections:

- `plans/000-long-term-specification.md#1-product-definition`
- `plans/000-long-term-specification.md#2-end-state-invariants-normative`
- `plans/000-long-term-specification.md#3-ownership-boundaries-normative`
- `plans/001-terminology-and-domain-model.md#identity-and-lifecycle-terms`
- `plans/002-long-term-roadmap.md#phase-5--optional-on-prem-small-team-access-and-budgeting`

Affected subsystem roadmaps:

- `plans/subsystems/on-prem-member-access-roadmap.md`
- `plans/subsystems/on-prem-user-access-roadmap.md` (closed; not reopened)

## Context

EggPool is one on-prem instance. Operator `eggpool connect` accounts are
shared by every principal whose model policy allows the model.
`rust/src/routing/` does not consult the caller. The `accounts` table stores
provider capacity facts and an environment-variable name, never a person and
never a secret value.

Accepted ADR-0002 keeps upstream secrets in operator TOML and environment
files, forbids a provider-secret web editor, and says ordinary users must not
write provider configuration. The closed on-prem user-access roadmap lists
custom per-user provider secrets as a non-goal. That design is what shipped.

The 2026-11-10 audit recorded the resulting gap. A member can see their own
usage, limits, models, and client credentials. A member cannot add an
upstream credential that only they can spend, and an administrator cannot
allow or deny that ability. The requested examples are an individual
OpenCode Go key and an individual Amazon Bedrock account. OpenCode Go is a
bundled provider profile. Amazon Bedrock is not.

Long-term invariant 8 still requires credentials to stay out of SQLite,
logs, and diagnostics. The `accounts` table must continue to describe
provider capacity rather than downstream people. This ADR does not weaken
either rule. It proposes a narrow, grant-gated way for the server process
to accept one upstream secret and keep it in the operator secret file.

## Decision drivers

- Solo install and shared operator accounts remain the default.
- A small local team needs per-person upstream keys without sharing those
  keys across the team.
- An administrator must be able to deny the ability for the instance, or
  allow or deny it for one user.
- The member must not gain a shell, a config path, or a write to arbitrary
  upstream URLs.
- A crash or a failed reload must not turn a personal account into a shared
  account.
- Existing live reload, one SQLite writer, and generation publication stay
  the only topology path.

## Considered options

### Option A — Keep every upstream account shared

Members keep using operator keys, limited by model policy and budgets.
This preserves ADR-0002 and needs no secret-handling change. It leaves the
requested personal-key path closed.

### Option B — Store the upstream secret in SQLite beside the user

A member form writes the key into a user-owned table. Routing reads it per
request. This puts a credential in persistence, which invariant 8 forbids,
and it makes the `accounts` row describe a person.

### Option C — Operator-mediated file custody and a secret-free binding

The process writes the secret only to the config-adjacent environment file
and writes a non-secret binding row that names the owner. Shared accounts
have no binding. Eligibility consults the binding. Self-service exists only
when an administrator grant allows it.

## Decision

Option C, if this ADR is accepted. Until then, no milestone may implement it.

1. **Default.** The instance self-service grant is `deny`. Existing shared
   accounts stay shared. Solo onboarding does not ask for a personal
   upstream key.
2. **Grant.** An instance default of `allow` or `deny`, and a per-user
   value of `allow`, `deny`, or `inherit`. The user value wins when it is
   not `inherit`. The grant controls self-service only. The active
   administrator may register or remove a personal upstream for any user
   while that user's self-service grant is `deny`.
3. **Custody.** The plaintext secret is read from a hidden TTY or stdin,
   never from argv. The process writes it to the config-adjacent `.env`
   resolved by `rust/src/operations/paths.rs`, under a new variable name.
   The account stanza stores `api_key_env` only. SQLite, audit rows, reload
   diagnostics, logs, and dashboard HTML do not receive the secret.
4. **Binding.** A new table binds one `(provider_id, account_name)` to one
   `user_id`. It stores no secret. No row means the account is shared.
   An `active` row means only that user can be routed to it. A `withdrawn`
   row means nobody can be routed to it. A missing config account does not
   become shared. Two bindings for one account are a failed migration, not
   a choice.
5. **Publication.** The binding snapshot is fixed in the published
   generation. In-flight work keeps its generation. The next publication
   uses `config_reload_policy.rs::classify_transition`. Account topology
   stays live-reloadable. This ADR adds no second reload list.
6. **Selection.** The filter runs before score, health, and quota choice.
   It does not pin an account, bypass model policy, or bypass provider
   health and quota. A legacy shared-key principal has no user id and
   cannot select a bound account.
7. **Surface.** Self-service uses a browser session, the existing CSRF
   check, and the existing local-or-trusted-HTTPS gate. Personal API
   tokens do not authorize it. The member supplies an account name and a
   secret for a bundled provider id. The server copies that provider's
   endpoint from the bundled profile. The member does not supply a base
   URL, proxy, or header.
8. **Providers.** Any bundled profile id is eligible, including
   `opencode-go`. Adding Amazon Bedrock, or any other new provider, is a
   separate provider-profile change and is not authorized here.

On acceptance, amend only the sentences this ADR names:

- `plans/000-long-term-specification.md` product definition: personal
  upstream secrets remain operator-process files; a granted member may
  submit one through the server; SQLite stores the binding only.
- `plans/000-long-term-specification.md` ownership boundaries: the
  `accounts` table still does not describe a person; a separate binding
  may restrict selection. Ordinary users still cannot write TOML or `.env`
  directly.
- `plans/001-terminology-and-domain-model.md`: add the term for a personal
  upstream binding. A user remains distinct from a provider account.
- `plans/002-long-term-roadmap.md` Phase 5: record this granted path and
  keep the solo shared-account default.
- `plans/adrs/ADR-0002-on-prem-user-identity-and-policy-boundaries.md`
  Supersession: state that this ADR supersedes Decision items 1 and 7 only
  for the granted path. Do not rewrite the ADR-0002 Decision text.

Invariant 8 is unchanged. Acceptance does not authorize a secret column.

## Consequences

### Positive

- One person can spend their own provider key without exposing it to the
  team.
- The administrator can turn self-service off for the instance or for one
  person.
- Shared operator accounts and the solo install stay the default.

### Negative

- The server process becomes a writer of upstream secrets. A bug in the
  file transaction can leak a key into a diagnostic or leave a withdrawn
  account in config.
- Personal keys enlarge the generation's account topology. Health and quota
  stay per account, so a bad personal key must not be allowed to suppress a
  shared account.

### Neutral or deferred

- Amazon Bedrock and any other provider that is not already bundled.
- Member-supplied base URLs, proxies, and headers.
- Preferring a personal account over a healthier shared account.
- A second administrator.

## Compatibility and migration

Append one new migration after v65. Do not edit v1–v65. Existing accounts
gain no binding and remain shared. Existing `.env` entries stay. A personal
account name that collides with a configured account is rejected. Backup of
SQLite continues to exclude the secret; operators who back up the config
directory already back up `.env` and must treat that file as secret.

## Security and reliability implications

The secret crosses the browser only on the existing authenticated,
same-origin, CSRF-checked, transport-gated form, and only for a principal
whose effective grant is `allow` or for the active administrator. Failed
publication restores the previous config and `.env` bytes and removes or
withdraws the binding so the account cannot be selected as shared. Reload
diffs use the existing secret-path redaction. Disabling a user withdraws
eligibility of that user's active bindings on the next publication.

## Verification

A member without a grant receives no form and a rejected mutation. A member
with a grant can add a bundled provider account and is the only principal
who can be routed to it. Another member, the legacy shared key, and an
unauthenticated caller cannot. Revocation and a failed reload leave the
account ineligible. Tests assert the secret is absent from SQLite, audit
rows, and reload diagnostics. Default and `--no-default-features` serial
suites pass.

## Supersession

Accepted. Supersedes ADR-0002 Decision items 1 and 7 only for this granted
path. The remainder of ADR-0002 stays in force, including one active
administrator, no public signup, and no organization id.

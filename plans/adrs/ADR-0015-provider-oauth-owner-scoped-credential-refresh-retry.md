# ADR-0015: Provider OAuth Owner-Scoped Credential Refresh Retry

Status: accepted

Date: 2026-11-24

Decision owners: project maintainers

Related specification sections:

- `plans/000-long-term-specification.md#1-product-definition`
- `plans/000-long-term-specification.md#2-end-state-invariants-normative`
- `plans/001-terminology-and-domain-model.md#identity-and-lifecycle-terms`

Affected subsystem roadmaps:

- `plans/subsystems/provider-oauth-roadmap.md`

Related decisions:

- `plans/adrs/ADR-0012-provider-oauth-account-custody-and-lifecycle.md`
- `plans/adrs/ADR-0013-provider-oauth-eggfetch-compatibility.md`
- `plans/adrs/ADR-0014-provider-oauth-web-ui-and-desktop-flow-boundary.md`

## Context

ADR-0012 approved a strict no-replay rule for owner-bound OAuth credentials. It
stated that provider transport stays neutral and a submitted request is not
replayed after HTTP 401 (`ADR-0012:35`), that "HTTP 401 never triggers
automatic refresh-and-replay" (`ADR-0012:58`), and that "No 401 replay or
cross-account fallback is permitted" (`ADR-0012:88`). ADR-0013 and ADR-0014
retained that clause.

That rule was correct about what it protected. An owner-bound credential is not
a shared secret, a rejected credential must not be replayed against another
account, and a refresh token that rotates must never be spent twice. Those
protections remain in force.

The rule was too strong about one thing. A provider routinely rejects an
expired access token, and the owner then had no way to serve the request even
though a valid refresh token was on disk. The rejection also recorded an
authentication failure, and an authentication failure disables the account
(`rust/src/health/health_manager.rs:346-351`), so a routine expiry produced a
user-visible failure plus a disabled account that only a new login could
repair.

The reference implementation EggPool already depends on does not behave that
way. The eggpass CLI refreshes the credential and retries the same request once
after an upstream 401 (`grokproxy/src/upstream.rs:206-225`), and the shared
`eggpass-auth` crate exposes `TokenManager::force_refresh` for exactly this
compare-and-refresh case: it refreshes only when the stored access token is
still the one the provider rejected, and otherwise returns the newer stored
token (`eggpass-auth/src/manager.rs:111-124`). The owner has decided that the
reference behavior is the better product behavior, and that the divergence
should be closed.

## Decision drivers

- Keep owner binding absolute. The refreshed credential is the same owner's,
  for the same provider account. No other account, provider, or user may be
  substituted for a rejected credential.
- Never spend a rotated refresh token twice. The refresh must be
  compare-and-refresh, not blind re-authentication.
- Bound the recovery. One refresh and one retry per request, never a loop.
- Keep the account health evidence honest. A credential that was stale and then
  healed is not an account fault; a credential that is still rejected is.
- Keep the operator gate absolute. Disabled OAuth blocks the refresh exactly as
  it blocks login, routing, and proactive refresh.
- Keep the failure classifier and its recorded vocabulary unchanged. The
  compatibility oracle for the 401 classification is a frozen contract.

## Considered options

### Option A — Keep the strict no-replay rule

Every upstream 401 on an OAuth account stays terminal, and the account is
disabled for an authentication failure. This preserves the existing records
exactly and keeps the request path free of a credential-rotation step, but a
routine credential expiry still fails the request and disables the account
until the owner logs in again.

### Option B — Refresh the owner's credential and retry the same account once

On an upstream 401 for an account whose auth mode is OAuth, EggPool refreshes
that owner's credential for that account and retries the same request against
the same account exactly once. The refresh uses the crate's compare-and-refresh
primitive, so a credential another process already rotated is reused instead of
rotated again. The account health evidence is applied only when the refresh
fails or the refreshed credential is rejected again.

### Option C — Refresh and retry against any eligible account

Refresh the rejected credential, then let ordinary routing pick another
eligible account for the retry. This would keep a request alive when the
owner owns several accounts, but it is a cross-account fallback, which ADR-0012
prohibits and which the owner-bound custody model exists to prevent.

### Option D — Refresh and retry without a bound

Refresh whenever a 401 arrives and keep retrying while the budget allows. This
maximizes request survival but turns a rejected credential into repeated token
requests against the provider.

## Decision

Select Option B.

- On an upstream HTTP 401 for an account whose auth mode is OAuth, EggPool
  refreshes that owner's credential for that account and retries the same
  request against the same account exactly once.
- The refresh is compare-and-refresh. When the stored access token is no
  longer the token the provider rejected, the newer stored credential is used
  without contacting the provider, so a rotated refresh token is never spent
  twice.
- The retry holds the account constant. It never reselects, never substitutes
  another account, provider, or user, and never crosses an owner boundary.
- The account health evidence is applied only when the refresh fails or the
  refreshed credential is rejected again. A credential that was healed is not
  recorded as an account fault.
- The operator gate blocks the refresh. A disabled OAuth service refuses the
  refresh exactly as it refuses login, routing, and proactive refresh.
- The failure classifier is unchanged. The 401 classification, its evidence
  class, and its recorded labels stay exactly as the compatibility oracle
  records them. The recovery is coordinator-owned and adds its own scope and
  action labels for the healed attempt.
- A static-key account's 401 behavior is unchanged: the classifier's ordinary
  retry and account-fallback policy still applies.
- At most one credential refresh occurs per request, and the ordinary attempt
  budget still bounds the request.

## Consequences

### Positive

- A routine credential expiry heals without a user-visible failure and without
  disabling the account.
- The request path matches the reference implementation EggPool already
  depends on, so the divergence between the two OAuth implementations closes.
- A genuinely rejected credential still fails closed. The refresh fails, or the
  refreshed credential is rejected again, and the account is disabled for an
  authentication failure exactly as before.

### Negative

- One additional upstream attempt and one additional token request per
  rejected OAuth credential, bounded to one each per request.
- A provider that rejects a credential for a reason a refresh cannot repair
  now costs one extra token request before the request fails.
- The durable failure vocabulary gains one scope label and one action label for
  the healed attempt.

### Neutral or deferred

- Live provider behavior is not established by this decision. Local stubs prove
  the dispatch semantics, not provider support or legal compliance.
- The reference CLI refreshes and retries on any 401. EggPool keeps its
  classifier's account-disable semantics for a second rejection, which is
  stricter than the reference and remains deliberate.
- Any future support for refreshing across providers, or for retrying a
  rejected credential against a different account, requires a separate
  decision.

## Compatibility and migration

No stored-credential migration is needed. Credential files keep their format,
their owner binding, their permissions, and their file names. No configuration
key is added, removed, or reclassified, so the reload and restart boundary is
unchanged. No public API, route, or wire contract changes. The behavior change
is confined to the request path: an upstream 401 on an owner-bound OAuth
account now recovers instead of terminating.

## Security and reliability implications

The owner binding is unchanged and is re-checked on the refresh path: the
refresh goes through the same configured-account check, the same active-owner
check, and the same operator gate as the proactive resolution, so no user can
refresh or use another owner's credential.

The refresh runs under the existing per-account OS lock and commits atomically
before the lock is released, so a concurrent process cannot spend the same
rotated refresh token or observe a partially written credential. The refresh
transaction is detached from request cancellation, so a cancelled request
cannot leave a credential half-rotated.

Secrets stay where ADR-0012 put them. The rejected access token is compared in
memory only; it never enters the failure observation, diagnostics, logs, audit
rows, SQLite, or Debug output, and the credential file keeps its private mode.
The failure classifier's recorded vocabulary is unchanged, so the compatibility
oracle and the durable attempt rows keep their existing shape.

Reliability is bounded. One refresh and one retry per request, inside the
ordinary attempt budget, with the account held constant. A provider that keeps
rejecting a credential disables the account and the request fails closed, which
is the pre-existing outcome.

## Verification

- Verify that an upstream 401 on an owner-bound OAuth account refreshes the
  credential and retries the same account exactly once, with the retry carrying
  the refreshed token.
- Verify that a second rejection of the refreshed credential terminalizes the
  request and disables the account, with no second refresh.
- Verify that a static-key account's 401 keeps the existing classifier retry
  and account-fallback behavior.
- Verify that the operator gate refuses the refresh and that a non-owner
  cannot refresh another owner's credential.
- Verify that the refresh is compare-and-refresh: an already rotated credential
  is reused without a second token request.
- Verify that the 401 classification and its recorded labels are unchanged
  against the frozen compatibility observation fixture.
- Verify that no access or refresh token appears in logs, diagnostics, SQLite,
  or the durable attempt rows.

## Supersession

This ADR supersedes only ADR-0012's no-replay clause: the sentence "Keep
provider transport neutral and do not replay a submitted request after HTTP
401" (`ADR-0012:35`), the sentence "HTTP 401 never triggers automatic
refresh-and-replay" within ADR-0012's refresh-and-dispatch rule
(`ADR-0012:58`), and the sentence "No 401 replay or cross-account fallback
is permitted" (`ADR-0012:88`), and it supersedes that clause only insofar as
it forbids the owner-scoped credential refresh and the single same-account
retry this ADR approves.

It also supersedes the retention of that clause in ADR-0013 (`ADR-0013:51`,
`ADR-0013:77`) and ADR-0014 (`ADR-0014:54`, `ADR-0014:80`), again only as
to that clause.

ADR-0012 remains an immutable historical record. Its account custody,
ownership, lifecycle, secret-handling, gate, refresh coordination, and
backup/restore decisions remain in force, and its prohibitions on pooling and
cross-account fallback remain in force: this ADR approves a refresh of the
owner's own credential and a retry of the same account, not a fallback to a
different account. ADR-0013's dependency-compatibility decision and ADR-0014's
flow boundary are otherwise unaffected.

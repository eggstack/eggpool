# Deep Dive: On-Prem User Access

Back to [Architecture](README.md). See also the review index in
[overview.md](overview.md), [Database](deep-dive-database.md),
[Security](deep-dive-security.md), [Dashboard](deep-dive-dashboard.md), and
[Request lifecycle](deep-dive-request-lifecycle.md).

This deep dive covers the shipped on-prem user-access stack: local downstream
identities, their credentials, the browser session boundary, the per-user and
instance-global budget intersection, and the versioned model policy. It
describes what the shipped runtime does; it does not restate plan or closure
history.

## Ownership boundary

The subsystem is five flat runtime modules plus two HTTP adapter surfaces:

| Path | Owns |
|---|---|
| `rust/src/identity.rs` | Local user records, password hashes, personal API credentials, the administrative audit trail, and the request-attribution types |
| `rust/src/web_session.rs` | Opaque browser sessions, the CSRF digest, and bounded local login throttling |
| `rust/src/model_policy.rs` | Versioned, user-specific model authorization (profiles, exact rules, per-user overrides, instance denials) |
| `rust/src/user_budget.rs` | Durable per-user budget rules, admission holds, attempt usage, and settled usage windows |
| `rust/src/instance_budget.rs` | The instance-global budget scope intersected with the per-user rules |
| `rust/src/server/auth.rs` | Browser login, session cookies, CSRF forms, and forwarded-HTTPS validation |
| `rust/src/server/dashboard/admin.rs` | The admin and member HTML surfaces and their form handlers |

The boundary is deliberately narrow:

- `rust/src/db/` owns the SQLite connection, the gate, the transactions, and
  the embedded migration chain. The subsystem owns no schema of its own and
  opens no second writer; every read and write goes through `Database`.
- `rust/src/coordinator/` owns request admission, durable publication, and
  terminal settlement. The subsystem supplies the *facts* (an authenticated
  principal, a policy snapshot, budget limits) and never dispatches upstream.
- `rust/src/routing/`, `rust/src/quota/`, and `rust/src/health/` keep owning
  upstream provider accounts, provider quota, and provider health. Downstream
  user budgets are independent of provider quota and never share a counter.
- `rust/src/server/` owns HTTP credential extraction, the browser session and
  CSRF boundary, and the route-role gates. The subsystem owns no router.
- `rust/src/operations/` and `rust/src/runtime.rs` own the local CLI surface
  (`eggpool users …`), which renders and dispatches only.

There is no provider-secret web editor, no public signup, no organization or
tenant identifier, and no external IAM. The upstream provider accounts in
`accounts/` remain operator-owned and are never exposed to a downstream user.

## Identity and credential model

`identity.rs` owns the local downstream identity. Usernames are 1–64 ASCII
letters, digits, dots, underscores, or hyphens, stored both as entered and
as a lowercase `normalized_username` with a unique index. Passwords are 12–1024
bytes.

Password hashing is Argon2id with fixed parameters (`m = 19456`, `t = 2`,
`p = 1`) and a per-password salt. `verify_password` rejects any stored hash
whose algorithm, version, or parameters differ from the pinned values, so a
weakened or foreign hash cannot be accepted. Hashing runs on a bounded
two-slot semaphore *before* the SQLite gate is acquired, so a slow hash never
holds the single database gate. An unknown username is verified against a
lazily initialized dummy hash, so a missing user and a wrong password take the
same work.

Personal API credentials are high-entropy `epk1_<selector>_<secret>` tokens.
Only the SHA-256 digest is stored; the row is found by the non-secret
selector and the final digest comparison is constant-time. Credential labels
are 1–80 bytes, and the default scope set is
`inference,discovery,integrations`. The plaintext token is shown exactly
once at issue time and is never recoverable afterwards.

The access-state singleton in `user_access_state` has exactly four states:

- `legacy_unmigrated` — an existing deployment that still authenticates with
  the configured shared server key;
- `fresh_uninitialized` — a brand-new schema with no administrator yet;
- `admin_initialized` — the first administrator exists but the shared-key
  path has not been retired;
- `managed` — managed access is in force.

`enable_managed` moves the singleton to `managed` and is the explicit,
audited step that retires legacy access. `bootstrap_admin` creates the sole
initial administrator and its first inference-only personal credential in one
transaction. A partial unique index (`users_one_active_admin`) makes a second
active administrator impossible at the schema level, and `LastAdminProtected`
refuses to disable the last active administrator.

Every authenticated request carries an `AuthenticatedPrincipal`
(`user_id`, `credential_id`, `role`, `scopes`, `auth_epoch`, and a
`PrincipalKind` of `agent_token`, `browser_session`, or `legacy_shared_key`).
The same principal produces a `RequestAttribution`, which is snapshotted into
the request row at publication time. `auth_epoch` is bumped on credential
revocation and password reset, so a snapshot taken before the change can no
longer be presented as current.

## Browser session and CSRF boundary

`web_session.rs` owns browser sessions. A session token is
`eps1_<selector>_<secret>`; only its SHA-256 digest and the CSRF digest are
persisted. Session lookup is by selector followed by a constant-time digest
comparison, so a stolen selector alone is not a session.

Session rows carry a 7-day expiry, a 12-hour idle-use window, and an explicit
revocation timestamp. `last_used_at` is refreshed at most once every 15
minutes. Both cookies are `HttpOnly; SameSite=Lax`, and both gain `Secure`
when the request is determined to be secure. The CSRF cookie is deliberately
not `Strict`: a `Strict` cookie is withheld on a cross-site top-level
navigation, so a deep-linked operator's page would arrive without it, and the
account page derives its hidden CSRF field from the cookie alone. `Lax` still
withholds the cookie on a cross-site unsafe request, so the server-side
same-origin check stays the authoritative defense and the double-submit token
stays defense in depth.

`server/auth.rs` owns the transport side of the boundary. A login form is
refused unless the connection is local or is fronted by a trusted HTTPS proxy;
the refusal page names the SSH-port-forward and reverse-proxy paths instead
of asking for a password over an unsafe transport. Unsafe methods require
same-origin, and every browser-originated mutation additionally carries a CSRF
form field verified against the session's CSRF digest.

The sign-in page, the account page, the account-error page, the transport
refusal, the member landing page, and the member refusal all render through one
document builder, so a browser never meets an unstyled fragment. Every sign-in
failure — transport, origin, throttle, and unavailable service alike — renders
that document with a `role="alert"` message. Every sign-in response carries
`Cache-Control: no-store`, because `/login` is an unconditionally public path
that the middleware returns from before it applies that header.

Login throttling is local, bounded, and unpersisted: a 15-minute window with 10
failures per account, 30 per peer IP, 30 personal-token attempts per peer IP,
and a 2048-bucket capacity. The account bucket is keyed on the normalized
username alone, so the ten-failure budget belongs to the account rather than to
an (account, source address) pair: an attempt from a new address does not reset
it. The account check is a hard refusal consulted before the credential check,
so an exhausted account budget is not broken by a correct password. A
successful sign-in clears the account bucket only; the per-peer bucket
deliberately survives, because clearing it would let any valid account reset an
address's cross-account failure budget and multiply that address's brute-force
throughput without bound. Failed token digests are remembered for one second so
a repeated wrong token is rejected before it reaches SQLite.

`web_session::SessionFence` is the transaction-time fence for browser-originated
mutations. It binds a mutation to the exact session and privilege snapshot
that middleware admitted, so a revocation, password reset, role change, or
disablement that lands before the commit makes the write fail closed instead
of racing through.

## Model policy

`model_policy.rs` owns versioned, user-specific model authorization. The
durable state is a singleton `policy_state` row carrying a monotonic
`revision`, plus `policy_profiles`, `profile_model_rules`,
`user_profile_assignments`, `user_policy_overrides`, and
`instance_model_denials`.

Policy mutations advance the revision in the same SQLite transaction as the
mutation, and SQLite triggers advance it when user state, role, or auth epoch
changes. The bounded process cache (256 users) is checked against the revision
before a new request is served, and the request then holds an immutable
`ModelPolicySnapshot` for its whole lifetime — a mid-request policy change
cannot alter an already-admitted decision.

The effective decision is ordered: an exact instance denial wins, then an
exact per-user override, then the assigned profile's exact rule, and finally
the profile's default effect. With no profile, the role-safe default applies:
an administrator is allowed and a member is denied. Catalog and integration
identifiers carry the provider as a suffix, so a rule may match the exact
`provider/model` pair or the bare model. A profile may also name a single
permitted client recommendation; a preferred model that the profile does not
allow is rejected rather than silently widened.

Denial precedes upstream dispatch. The policy also filters the model catalog
and the authorized virtual-model ids a member can see, so a denied model is
not discoverable either.

## Budget intersection

`user_budget.rs` and `instance_budget.rs` share one contract. A rule names a
window (`day` or `month`, on UTC calendar boundaries), an optional
`model_id`, and up to four ceilings (`max_requests`, `max_tokens`,
`max_microdollars`, `max_concurrent`). `model_id = NULL` means every model;
a rule with a `model_id` matches that exact model.

Each rule carries an explicit enforcement mode:

- `estimated` (the default) holds the request-size token projection and the
  fallback microdollar estimate;
- `hard` reserves the admission worst case — the input bound plus the
  caller's output cap, converted at the operator-configured
  `[providers.<id>.budget]` tariff — and records the bound and the price
  provenance on the reservation row.

`hard` enforcement fails closed. An unbounded output, an unknown tariff, or an
arithmetic overflow denies with `BudgetNotEnforceable` before any upstream
submission. Pre-existing rules stay `estimated` until an administrator
explicitly promotes them.

The per-user scope and the instance-global scope are intersected, not
composed additively: a request must satisfy both. Checks and holds run in the
same publication SQLite transaction as the request/attempt/provider
reservation, so a concurrent request for the last remaining slot cannot pass
on a stale preflight. A retry extends the existing logical hold without
incrementing the request count. Attempt facts are retained idempotently, and
terminal finalization settles exactly one daily and one monthly usage bucket
in the transaction that terminalizes the request. Unknown usage consumes the
remaining estimate and increments explicit unknown counters rather than
being reported as zero.

There is exactly one instance-wide scope and no second database writer.
Instance tables carry no user reference, so the global counters survive a user
being disabled or removed and also cover unattributed (legacy shared-key)
requests. When no instance rule exists, publication takes no instance hold and
writes no instance rows, so a single-administrator workload is unchanged apart
from the measured lookup overhead.

## Admin and member surfaces

`server/dashboard/admin.rs` owns the HTML surfaces and their form handlers.
Every handler is a thin adapter: it validates the CSRF form field, resolves
the actor, and delegates to `identity.rs` or `model_policy.rs`.

Admin surfaces: `/users`, `/profiles`, `/usage`, `/admin/audit`, and the
`/admin/users/*` action family (create, state, password, token, revoke
tokens, profile assignment, override add/remove, budget), the
`/admin/budget/instance*` family, and the `/admin/profiles/*` family (create,
default, update, delete, rule add/remove, preference).

Member surfaces: `/login`, `/logout`, `/account`, `/account/landing`,
`/account/password`, `/my-account`, `/my-account/models`, `/account/tokens`,
`/account/tokens/revoke`, `/usage`, and `/api/user-budget`.

A member who signs in lands on `/account/landing`, which links the member
destinations (`/my-account`, `/my-account/models`, `/usage`) and exposes no
administrative route and no administrative datum, so the member surface is
reachable without knowing a URL. An administrator who signs in lands on
`/account`, which links back to the dashboard overview.

The route-role gate lives in `server/middleware.rs`, not here. A non-administrator
browser principal is confined to the member surfaces; an administrator-only
path answers `403` with a page that names the refusal and points at the member
surface instead of leaking its existence. A principal that must change its
password is redirected to the account surface rather than to the surface it
asked for. The administrative audit trail (`user_admin_audit`)
records the actor, the action, the target kind, and the target id only —
never a password, a token, or any other secret.

The local CLI surface is `eggpool users`, whose subcommands are `bootstrap`,
`add`, `disable`, `enable`, `show`, `list`, `reset-password`, `token`,
`revoke`, `enable-managed`, `policy`, `budget`, `global-budget`, and
`defaults`.

## Durable tables

The subsystem's tables are migrations 55–63 of the embedded chain:

| Migration | Tables |
|---|---|
| 55 `user_identity` | `users`, `user_credentials`, `user_access_state` |
| 56 `web_sessions` | `web_sessions` |
| 57 `request_attribution` | `users.auth_epoch`, `requests.user_id`, `requests.credential_id`, `requests.auth_epoch`, `requests.principal_kind`, `requests.request_kind`, `requests.parent_proxy_request_id` |
| 58 `model_policy` | `policy_state`, `policy_profiles`, `profile_model_rules`, `user_profile_assignments`, `user_policy_overrides`, `instance_model_denials` |
| 59 `user_budgets` | `user_budget_rules`, `user_budget_reservations`, `user_budget_attempt_usage`, `user_budget_usage`, `user_budget_rule_audit` |
| 60 `user_admin_audit` | `user_admin_audit` |
| 61 `user_budget_enforcement` | enforcement column and worst-case hold/price-provenance columns on the budget tables |
| 62 `instance_budgets` | `instance_budget_rules`, `instance_budget_reservations`, `instance_budget_usage`, `instance_budget_rule_audit` |
| 63 `default_member_profile` | `policy_state.default_member_profile_id` |

`requests.user_id` is a nullable column without a foreign key, so disabling
or removing a local user cannot erase request attribution. Internal semantic
selector rows carry a parent proxy request id and are excluded from logical
user-history queries; `identity::recent_user_requests` uses the
`(user_id, started_at DESC, id DESC)` index, clamps reads to 500 rows, and
returns content-free request facts.

No table stores a plaintext password, a plaintext token, a prompt, a raw
body, or a cache key. Backups therefore carry identity records and
non-recoverable credential digests only.

## Invariants

- Every admitted request retains a trustworthy principal; denial precedes
  upstream dispatch, and aliases, selectors, and cross-protocol requests
  cannot bypass a model restriction.
- Managed access never inherits the public-dashboard or loopback shortcuts.
- A browser mutation that races a revocation, password reset, role change, or
  disablement fails closed at commit time.
- A policy snapshot is immutable for the request that admitted it.
- Downstream user budgets and upstream provider quota remain independent.
- The single SQLite connection/gate/worker, WAL/NORMAL, the generation lease
  model, and the publication/finalization ownership are unchanged by this
  subsystem.

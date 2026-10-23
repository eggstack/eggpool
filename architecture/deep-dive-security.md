# Deep Dive: Security and Redaction

Back to [Architecture](README.md). See also the review index in
[overview.md](overview.md) (§12): §12 is the birds-eye summary, this file
is the security authority.

The native runtime applies request limits, API-key authentication
(constant-time `Bearer`/`x-api-key` check in `rust/src/server/middleware.rs`,
valid key shape 8..=512 `[A-Za-z0-9_-]`, loopback exemption for
`localhost`/the full `127.0.0.0/8` range/`::1` including IPv4-mapped
loopback), header filtering, credential redaction, safe
filesystem permissions, and metadata-only
diagnostics. Configuration and deployment operations preserve secret values in
the environment or adjacent `.env`; they do not echo them into logs or
structured events.

## Authentication

`verify_api_key()` accepts `Authorization: Bearer <key>` (the scheme token is
matched case-insensitively, so `bearer` and `BEARER` name the same scheme)
or the `x-api-key` header, pads both sides to fixed
512-byte buffers, and folds the full pads before combining with the shape
bits — so shape validity never short-circuits the compare. Provided and
expected values must both satisfy `valid_key_shape()`. A shared server key is
required for a non-loopback `legacy_unmigrated` installation; malformed
configured keys fail startup. After local identity initialization, keyless
listeners are allowed because protected routes use a personal agent token or
browser session. Keyless loopback legacy mode retains its explicit
unauthenticated warning. Legacy route behavior remains unchanged: health,
readiness, and static assets are public; sensitive agent/operational routes
require the shared key; ordinary dashboard pages follow
`[dashboard].public`.

After local identity initialization, the configured global key is not
accepted on any route. Personal credentials are checked against their
selector and stored digest on each agent request. A new empty schema is marked
`fresh_uninitialized`, and a keyless listener refuses to start until local
bootstrap. In `admin_initialized` and `managed`, the public-dashboard setting
does not expose dashboard or `/api/*` data. Browser login/account routes are
session-only; agent credentials cannot access them. `/account/landing`, the
member landing page a member reaches after a successful sign-in, is one of
those session-only routes and exposes no administrative route. Health,
readiness, and static assets remain public. Unknown `/api/*` routes require an
administrator session before normal 404 handling.

Downstream human identity is separate from provider `accounts`. Migration 55
adds local users, credentials, and access state; migration 56 adds opaque
browser sessions with only token and CSRF digests stored. Passwords use
bounded Argon2id hashes, and personal `epk1` credentials and `eps1` sessions
store a SHA-256 digest plus a random selector. Password hashing runs in a
bounded blocking worker pool outside the SQLite gate. Personal credentials
authenticate only inference, `/v1/models`, and the scoped integration-profile
endpoint; sessions authenticate dashboard administration and account routes
but never inference. `eggpool users reset-password` revokes API credentials
and browser sessions. The configured shared key stops authenticating when the
first local identity is created. Upgraded schemas remain `legacy_unmigrated`
until that identity is initialized.

Sessions have a 12-hour idle bound and 7-day absolute expiry. Both cookies —
`eggpool_session` and `eggpool_csrf` — are HttpOnly, SameSite=Lax, root-path
scoped, and Secure when the accepted connection is HTTPS. The CSRF cookie is
not `Strict`: a `Strict` cookie is withheld on a cross-site top-level
navigation, which is exactly the request that already carries the session
cookie, so the account page would render without the token its hidden field is
derived from. `Lax` still withholds the cookie on a cross-site unsafe request,
so the same-origin check below stays the authoritative defense and the
double-submit token stays defense in depth. Unsafe browser forms require
same-origin Origin/Referer and a session-bound CSRF token. A same-origin
browser form POST presents `Origin` and `Referer` together, so
`server::auth::same_origin` requires every provenance header that is present
to agree with the request `Host` rather than exactly one of them; duplicated
headers and userinfo-bearing authorities still fail closed.
Remote login requires HTTPS or a trusted proxy peer listed as a literal IP in
`security.trusted_proxies` that sends one `X-Forwarded-Proto: https` value.
For browser-login and failed personal-token throttling, one literal client IP
in `X-Forwarded-For` is used when received from that exact trusted peer; the
proxy must replace this header. Missing, duplicated, malformed, or chained
values fall back to the proxy peer. No other forwarded header establishes
trust.
`security.allow_insecure_web_auth` is an explicit development override and
emits a startup warning.

Browser sign-in throttling is process-local and unpersisted: ten failures per
account and thirty per peer address inside a fifteen-minute window. The
account budget is keyed on the normalized username alone and is a hard refusal
consulted before the credential check, so a new source address does not reset
it and an exhausted budget is not broken by a correct password. A successful
sign-in clears the account budget only; the per-peer budget deliberately
survives, because clearing it too would let any valid account reset an
address's cross-account failure budget.

The local operator CLI uses the data-directory owner's filesystem authority.
`users add` commits a member, an explicit or configured default initial profile,
the transition from
`admin_initialized` to `managed`, and audit entries in one transaction; a
failed profile assignment leaves the original state intact. The explicit
`users enable-managed` path uses the same transition helper and records its
own audit event. A remaining configured shared key is rejected after
identity initialization, and `dashboard.public` cannot expose managed routes;
removing those stale config values is cleanup, not a precondition for the
atomic cutover. Default member profile selection is audited and revisioned,
affects only future member creation, and does not alter existing assignments.
Clearing restores deny-by-default for future unassigned members. No budget
template is copied; budget scopes remain independently configured. Disabling a user revokes sessions and tokens and advances the
auth epoch. Re-enabling does not un-revoke credentials, and the last active
administrator remains protected.
Session-authenticated responses carry `Cache-Control: no-store, max-age=0` so
account and administration HTML containing identity, budget, or CSRF data is
not retained by shared or browser caches. Sign-in responses carry the same
header; `/login` is an unconditionally public path that the middleware returns
from before it applies the header, so the sign-in surface sets it itself.
Privileged browser mutations also
revalidate the exact session, user role, auth epoch, and forced-password state
inside the SQLite transaction that commits the write; creating a user and
assigning its initial profile is one transaction. This closes the interval
between HTTP middleware authentication and database commit.
Failed personal-token lookups use a 256-entry, one-second negative cache keyed
by token digest. Successful token lookups are not cached, so revocation takes
effect on the next request.

Optional short-window fairness uses only the authenticated principal's
database-owned user ID; credentials and request headers cannot choose a bucket.
The process-local table is capacity-bounded, stale entries are reclaimed only
after the full refill window, and a full table fails closed for new principals.
No token, prompt, body, IP, or provider outcome is retained in limiter state.

## Headers, bodies, and redaction

Provider credentials are added only at dispatch-header construction. Incoming
headers forwarded toward provider selection pass through
`filtered_incoming_headers` in `rust/src/server/inference.rs`, which drops
exactly ten entries: `authorization`, `proxy-authorization`, `x-api-key`,
`host`, `content-length`, the route-session header
(`x-eggpool-route-session`, hashed never forwarded), and hop-by-hop framing
(`connection`, `transfer-encoding`, `upgrade`, `keep-alive`); log
redaction defaults to `["authorization", "x-api-key"]` via
`SecurityConfig::redact_headers`. Raw
request/response bodies, prompts, cache keys, and token values are excluded
from persistence and operational snapshots (`dashboard.store_request_content`
is validated `false`). Control sockets and state files use owner-only
permissions (`0o700` runtime/config directories, `0o600` control socket and
secret-bearing files) and fail closed when ownership is ambiguous.

### Persisted terminal detail

A terminal detail is a durable, operator-facing diagnostic column, never a
client-facing field. Before it is bound into a durable statement it passes
through one deterministic sanitizer in `rust/src/coordinator/finalization.rs`:
credential-shaped material is removed first, then control characters other
than newline and tab are dropped and the value is capped at 512 characters.
Redaction runs before the cap, so a secret is never truncated into a partial
credential.

The redaction rule is conservative and deterministic. At every position the
rules are tried in this order, and the first one that matches wins:

1. A labeled credential field loses its value. The label list holds 34
   spellings, including `authorization`, `proxy-authorization`, `x-api-key`,
   `api_key`, `api-secret`, `password`, `passwd`, `secret`, `credential`,
   `token`, `refresh_token`, `id_token`, and `private_key`. The label only
   matches at a word boundary and only when a `:` or `=` separator follows it,
   with an optional closing quote between the label and the separator. The
   plain (`api_key: …`), the quoted (`api_key: "…"`), and the JSON
   (`"api_key":"…"`) spellings are therefore all redacted, while compound
   identifiers such as `input_tokens`, `token_count`, and `api_key_id` are
   not. When the labeled value is itself a scheme word, the credential after
   it is removed and the scheme word stays visible.
2. A URL authority that carries userinfo credentials (`https://user:secret@host`)
   loses the whole userinfo section and keeps the host, so the diagnostic still
   shows which upstream was contacted. This is the same rule the status
   projection applies in `rust/src/operations/status.rs`. The authority ends at
   the first path, query, fragment, or whitespace byte, so a URL without
   userinfo, a `mailto:` fragment, and a bare `word@word` fragment are
   untouched.
3. A known vendor token prefix loses the whole token when the token reaches
   that prefix's minimum total length. The list holds 11 prefixes: `sk-`,
   `sk_`, `ghp_`, `gho_`, `ghs_`, `ghu_`, `github_pat_`, `xoxb-`, `xoxp-`,
   `AKIA`, and `AIza`. The prefix is matched case-insensitively, like the field
   label, so an upper-case spelling of a prefix is redacted as well.
4. A standalone credential scheme word (`Bearer`, `Basic`, `Token`, `Digest`)
   loses the credential that follows it when that credential is at least 16
   characters. The scheme word and the whitespace after it stay visible.
5. A JSON web token — three base64url segments whose first segment starts with
   `eyJ` — loses all three of its segments.
6. A run of 32 or more characters that mixes letters and digits is treated as
   high-entropy credential material and is removed.

Removed material is replaced with `[redacted]`, which contains no part of the
removed value, so neither the secret nor a truncated prefix of it can survive.
The label, the separator, and the scheme word stay visible, so the diagnostic
still shows which credential field was present.

Over-redaction is the accepted failure direction: a long hyphenated word that
merges into a vendor-prefix shape, a 32+ character opaque identifier, and a
labeled field whose value is not really a credential are all redacted. Ordinary
diagnostics — URLs without userinfo, model identifiers, prose, and usage
counters such as `input_tokens` — survive intact, because a label only matches
at a word boundary and only when a `:` or `=` separator follows it, and because
the URL rule only fires on an authority that actually carries an `@`. The rule
is applied on every path that persists a terminal detail, including the request
row, the attempt row, and the streaming terminal path that flows through the
same finalizer. Rows that were persisted before the rule existed are not
retroactively scrubbed.

## Transport ceilings and filesystem

The downstream EggServe HTTP/1 parser applies explicit header, target, and
transport body ceilings before the Axum application (`max_headers` 256,
`max_header_bytes` 128 KiB, `max_request_target_bytes` 16 KiB, 1024
connections / in-flight requests, 5-minute body-read and 15 s header-read
timeouts, a 24-hour handler timeout, and a 5 s graceful-shutdown timeout
inside the 10 s foreground deadline). The fixed 1 GiB transport body ceiling is defense in depth:
EggPool's authenticated inference middleware enforces the lower live
generation limit (`[server].max_request_body_bytes`, default 10 MiB) while
streaming the body — oversize declarations fail with 413, exhausted body
budget backpressures with 429 plus `Retry-After: 1` — and retains
the existing 413 response contract. EggServe's Tower bridge does not move auth,
dashboard exemptions, or integration-profile protection out of EggPool.

Config/state mutation paths (`config_mutation.rs`, `paths.rs`,
`control.rs`, `backup.rs`, `lifecycle.rs`, `update.rs`) write atomically
with symlink refusal and restrictive modes; uninstall only removes
explicitly resolved EggPool targets. Secrets resolve from
`api_key`/`api_key_env` indirection or `$EGGPOOL_ENV`-adjacent `.env`
files, stay `[REDACTED]` in `Debug` impls, and never reach
metrics/status/diagnostics payloads.

## Verification

```bash
cargo test --manifest-path rust/Cargo.toml --test server_transport -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test server_auth -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test terminal_detail_redaction -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test cli_contract -- --test-threads=1
```

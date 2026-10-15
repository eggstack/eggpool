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

`verify_api_key()` accepts `Authorization: Bearer <key>` (either
capitalization) or the `x-api-key` header, pads both sides to fixed
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
session-only; agent credentials cannot access them. Health/readiness and
static assets remain public. Unknown `/api/*` routes require an administrator
session before normal 404 handling.

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

Sessions have a 12-hour idle bound and 7-day absolute expiry. Cookies are
HttpOnly, SameSite=Lax, root-path scoped, and Secure when the accepted
connection is HTTPS. Unsafe browser forms require same-origin Origin/Referer
and a session-bound CSRF token. A same-origin browser form POST presents `Origin`
and `Referer` together, so `server::auth::same_origin` requires every provenance
header that is present to agree with the request `Host` rather than exactly one
of them; duplicated headers and userinfo-bearing authorities still fail closed.
Remote login requires HTTPS or a trusted proxy peer listed as a literal IP in
`security.trusted_proxies` that sends one `X-Forwarded-Proto: https` value.
For browser-login and failed personal-token throttling, one literal client IP
in `X-Forwarded-For` is used when received from that exact trusted peer; the
proxy must replace this header. Missing, duplicated, malformed, or chained
values fall back to the proxy peer. No other forwarded header establishes
trust.
`security.allow_insecure_web_auth` is an explicit development override and
emits a startup warning.
Failed personal-token lookups use a 256-entry, one-second negative cache keyed
by token digest. Successful token lookups are not cached, so revocation takes
effect on the next request.

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
cargo test --manifest-path rust/Cargo.toml --test cli_contract -- --test-threads=1
```

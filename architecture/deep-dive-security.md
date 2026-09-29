# Deep Dive: Security and Redaction

Back to [Architecture](README.md)

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
expected values must both satisfy `valid_key_shape()`. `validate_server_key()`
fails startup closed when a non-loopback bind has no key or any key has a
bad shape; a loopback listener without a key is allowed by design but logs
a loud unauthenticated-loopback warning. `requires_auth()` exempts only
`/v1/healthz`, `/v1/readyz`, and `/static/*`; inference (`/v1/*`),
`/api/integrations/*`, `/api/stats/runtime`, `/api/stats/update`, and
`/api/status` are always authenticated, and dashboard pages plus remaining
`/api/*` routes require a key only once `[dashboard].public` is off. The
integration profile never inherits a dashboard-public exemption.

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
connections / in-flight requests, 5 s body-read and 15 s header-read
timeouts). The fixed 1 GiB transport body ceiling is defense in depth:
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

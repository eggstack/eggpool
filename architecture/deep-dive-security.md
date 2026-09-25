# Deep Dive: Security and Redaction

Back to [Architecture](README.md)

The native runtime applies request limits, API-key authentication
(constant-time `Bearer`/`x-api-key` check in `rust/src/server/middleware.rs`,
valid key shape 8..=512 `[A-Za-z0-9_-]`, loopback exemption for
`localhost`/`127.0.0.1`/`::1`), header filtering, credential redaction, safe
filesystem permissions, and metadata-only
diagnostics. Configuration and deployment operations preserve secret values in
the environment or adjacent `.env`; they do not echo them into logs or
structured events.

Provider credentials are added only at dispatch-header construction. Incoming
headers forwarded toward provider selection pass through
`filtered_incoming_headers` in `rust/src/server/inference.rs` (drops
`authorization`, `proxy-authorization`, `x-api-key`, `host`,
`content-length`, the route-session header, and hop-by-hop framing); log
redaction defaults to `["authorization", "x-api-key"]` via
`SecurityConfig::redact_headers`. Raw
request/response bodies, prompts, cache keys, and token values are excluded
from persistence and operational snapshots (`dashboard.store_request_content`
is validated `false`). Control sockets and state files use owner-only
permissions (`0o700` runtime/config directories, `0o600` control socket and
secret-bearing files) and fail closed when ownership is ambiguous.

The downstream EggServe HTTP/1 parser applies explicit header, target, and
transport body ceilings before the Axum application. The fixed 1 GiB transport
body ceiling is defense in depth: EggPool's authenticated inference middleware
enforces the lower live generation limit while streaming the body and retains
the existing 413 response contract. EggServe's Tower bridge does not move auth,
dashboard exemptions, or integration-profile protection out of EggPool.

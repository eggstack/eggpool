# C004 — Provider-Bound Attempt Construction and Upstream Submission

Status: complete; see [closure record](../../closure/coordinator/004-status.md)

Implementation commit: `97a48464b775514f90d36d021607c091881a36d3`

Source roadmap: `migration-rs/subsystems/coordinator-roadmap.md`

Primary class: capability/invariant

Hard dependency: C003.

## Objective

Compose one durable selected attempt with one caller-selected M6 wire profile and the M4 provider/account HTTP client. Port provider-bound path/model/auth/header construction and the actual upstream send boundary without adding retry policy or client response handoff.

## Python oracle

Use C001 plus `request/coordinator.py`, `provider_bound_request.py`, provider contract/header helpers, provider config/wire profiles, M4 `ProviderClientPool`, and M6 `WireRuntime`.

## Attempt preparation

Build one immutable `PreparedUpstreamAttempt`/equivalent containing only facts needed for one send:

- durable lifecycle identity;
- provider/account/model/upstream-model identity;
- selected wire profile/surface and candidate fingerprint;
- HTTP method and fully resolved provider path;
- encoded M6 request body for that profile;
- static/auth/forwarded headers with clear precedence;
- stream intent;
- timeout inputs owned by M7;
- redacted structural diagnostics.

The original M6 canonical request remains the source of intent for every retry. Never transcode a previously translated provider body into the next attempt.

## Header/auth rules

Match Python `build_auth_headers`, `build_static_headers`, `build_upstream_headers`, incoming-header allow/deny behavior, content headers, provider wire auth shape, and request-ID handling. Auth/header values must never appear in Debug, errors, persisted attempt detail, or traces. Missing required secret/env state fails before network send.

## Submission

Use only the M4 `ProviderHttpClient` selected by provider/account. Do not create a second HTTP client or bypass per-account proxy selection. M4 returns transport/response evidence; C004 records timing/bytes/request-ID evidence needed by later policy but does not retry.

The provider send is cancellation-safe: dropping/cancelling the attempt future must return M4 resources and leave C006-capable terminal identity. No automatic replay at Hyper/M4 or C004 level.

## Tests

Controlled HTTP/HTTPS/provider-proxy fixtures must verify path templates/model substitution, native and cross-wire bodies, auth/header precedence, response request-ID extraction, streaming vs finite request mode, direct vs account proxy client lookup, body single-serialization/no stale retry body, transport error categories, cancellation at connect/write/header wait, and secret redaction.

Assert exactly one upstream request per C004 invocation.

## Dependencies

Reuse M4/M6 and existing `http`/Hyper body types. No Reqwest, no provider SDK, no second TLS stack, no new proxy crate.

## Acceptance criteria

C004 closes when a persisted selected attempt can issue exactly one correctly formed provider request through M4 for every supported wire profile, transport failures remain typed evidence rather than policy, secrets are redacted, and cancellation does not leak transport or lifecycle ownership.

## Closure

Accepted closure: [C004 status record](../../closure/coordinator/004-status.md). C005 is complete in the same implementation sequence.

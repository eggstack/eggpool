# Provider OAuth Milestone 002 — Authenticated Web UI provider login

Status: implemented

Repository baseline: 5b212d2d69137b99ed39f0dbeab10331c839e54e

Source roadmap:

- plans/subsystems/provider-oauth-roadmap.md#milestone-2--authenticated-web-ui-provider-login

Long-term requirements:

- plans/000-long-term-specification.md#1-product-definition
- plans/000-long-term-specification.md#2-end-state-invariants-normative
- plans/001-terminology-and-domain-model.md#identity-and-lifecycle-terms

Applicable ADRs:

- plans/adrs/ADR-0012-provider-oauth-account-custody-and-lifecycle.md (accepted; M002 flow matrix)
- plans/adrs/ADR-0013-provider-oauth-eggfetch-compatibility.md (accepted dependency contract)
- plans/adrs/ADR-0010-personal-upstream-account-custody.md (accepted owner-binding and browser security context)

Primary class: capability

## 1. Objective

Let an authenticated managed user authorize their own xAI account through Web UI device-code, or their own OpenAI account through Web UI authorization-code/PKCE with a fixed callback route on EggPool’s existing web listener. Commit only through M001’s atomic credential store after rechecking the operator gate and the same user/session. Desktop CLI loopback PKCE for both providers remains M003.

## 2. Why this milestone is ready

ADR-0012 accepts the per-user account policy and the scoped flow redesign. The design approval is not evidence that xAI device-code or the OpenAI redirect/client registration is supported by the provider; verify both provider contracts and exact registrations during implementation. M002 can build against local provider stubs and M001’s account identity/atomic-commit interface; integration and closure require that interface. Preserve the existing authenticated user, session, CSRF, transport, and personal-upstream authorization boundaries.

For OpenAI, the current plan has no trusted canonical public callback-base setting. Add a separate optional configuration value, [oauth].callback_base_url, for the canonical public HTTPS origin. Do not derive redirect_uri from Host, Forwarded, X-Forwarded-Host, or [server].host. [integrations].advertise_base_url is profile output only and must not be repurposed. If the setting cannot be added and safely validated, stop for architecture review. An unset setting disables only the OpenAI Web UI flow with a bounded fail-closed error; xAI device-code remains independently available.

## 3. Current implementation evidence

- rust/src/server/mod.rs owns the existing EggPool web listener and dashboard routes. M002 adds a fixed callback route to that listener; it does not create a second listener.
- rust/src/server/middleware.rs and rust/src/web_session.rs own authenticated browser-session admission. Browser mutations already use session/CSRF boundaries.
- rust/src/server/dashboard/admin.rs and rust/src/personal_upstream.rs own the current managed-user account surface and personal-upstream owner checks.
- rust/src/config.rs and rust/src/config_reload_policy.rs own configuration parsing and transition classification. [server].host is the listen socket; [integrations].advertise_base_url is documented profile output, not an OAuth callback authority.
- There is no OAuth callback route or dedicated canonical OAuth callback-base setting at this baseline.
- eggpass-auth 0.1.1 is the protocol candidate for device authorization and OpenAI authorization-code/PKCE transaction primitives. Its presence does not prove a provider grants the requested redirect/client registration.
- docs/providers.md currently excludes OAuth login. No provider support or legal-compliance evidence is created by this plan or the user’s scoped redesign approval.

## 4. Invariants that must not regress

- xAI M002 is authenticated Web UI device-code only. OpenAI M002 is authenticated Web UI authorization-code/PKCE only. Desktop CLI loopback PKCE for both providers is M003.
- All flows bind to the initiating managed user; the owner is derived from the authenticated session, never a username/user_id in request data.
- xAI device-code start, poll, cancel, and commit mutations require the existing browser session and CSRF checks.
- OpenAI callback is a fixed GET route on the existing EggPool listener, not a separate listener. It requires the same live browser session and managed user plus one-time state/nonce validation. The callback GET does not use CSRF; start, cancel, and final commit mutations do.
- OpenAI redirect_uri comes only from the separately configured canonical public HTTPS origin plus the fixed callback path. Never trust arbitrary Host/Forwarded headers, [server].host, or [integrations].advertise_base_url for it.
- Callback code, state, nonce, query, verifier, token responses, and tokens are redacted from logs/diagnostics and never persisted. Pending transaction and exchanged credentials are bounded, short-lived memory state only.
- Recheck operator enablement and the same authenticated session/user before exchange and before M001 commit. Disablement or lost session prevents commit and preserves the prior credential.
- Failed or canceled re-login preserves the prior credential. No shared consumer-subscription pooling, HTTP 401 replay, or cross-account fallback.

## 5. Scope

### In scope

- xAI Web UI device-code start, validated verification URL/user-code presentation, polling, cancel, expiry/denial handling, and M001 commit.
- OpenAI Web UI authorization-code/PKCE using eggpass-auth transaction primitives and fixed GET /oauth/openai/callback on the existing web listener.
- New optional [oauth].callback_base_url configuration for a trusted canonical public HTTPS origin, required only for the OpenAI Web UI callback flow.
- Server-side, bounded, nonpersistent transaction state bound to the exact browser session and managed user; one-time state/nonce and callback consumption.
- A CSRF-protected POST commit step after callback validation/code exchange, with session/user/operator-gate rechecks and M001 atomic commit.
- Tests for both provider flows, origin/redirect validation, callback tamper/replay/session loss, CSRF, operator disablement, failure retention, and redaction.

### Explicitly out of scope

- OpenAI Web UI device-code; xAI Web UI authorization-code/PKCE; any other M002 provider/grant combination.
- Desktop CLI login or CLI-to-server OAuth credential transfer (M003), public/internet-facing callback listeners, or a separate callback socket.
- Deriving callback trust from Host, Forwarded, X-Forwarded-Host, [server].host, or [integrations].advertise_base_url.
- Username/user_id/owner fields as account-binding authority, arbitrary callback paths, arbitrary providers, or raw token import.
- Shared consumer-subscription accounts, credential pooling, 401 replay, cross-account fallback, or a parallel token store.
- Claiming provider support or legal compliance based solely on this design approval.

## 6. Required production changes

1. **Configure a canonical OpenAI callback origin.** Add optional [oauth].callback_base_url, default unset, as a separate config setting. It contains only the canonical public HTTPS origin used by the deployment’s trusted ingress, not a request-derived value. Validate HTTPS scheme, host, port, and origin form; reject userinfo, path, query, fragment, malformed host, and unsupported redirect. Construct the fixed redirect_uri as that validated origin plus /oauth/openai/callback. Match the exact provider registration. If no suitable value is configured, fail the OpenAI flow closed. Do not reinterpret [integrations].advertise_base_url or [server].host. Route config changes through existing validation and config_reload_policy::classify_transition; do not create an independent reload/restart key list.
2. **Keep Web UI ownership authoritative.** Start, poll, cancel, and commit are authenticated user operations protected by the existing session and CSRF checks. Derive the managed user from the authenticated session only; do not accept a target username/user_id. Preserve existing per-user upstream grant and owner checks. Check the operator OAuth gate before start, before provider exchange, and before commit.
3. **Implement xAI device-code in the Web UI.** Use eggpass-auth 0.1.1 device authorization primitives with EggPool’s async host and M001 credential service. Validate the verification URL against the reviewed xAI host before display/open. Present only the ordinary URL and user code, keep device_code transient, honor polling interval/slow_down/expiry/denial/cancel, and never create a callback listener for xAI.
4. **Implement OpenAI authorization-code/PKCE on the existing listener.** Use eggpass-auth’s OpenAI authorization transaction primitives for PKCE S256, state, nonce, and provider parameters. Start through a CSRF-protected Web UI mutation, bind a bounded pending transaction to the exact browser session and user, and redirect to the provider using the configured fixed redirect_uri. Add a fixed GET /oauth/openai/callback route to EggPool’s existing web listener. Do not bind another socket or trust the inbound Host/Forwarded values when reconstructing the redirect.
5. **Validate and consume the OpenAI callback once.** The callback GET requires the same authenticated browser session and user that started the flow. Validate exact one-time state, nonce/transaction, redirect URI, and provider response before exchange; reject missing, duplicate, mismatched, expired, or replayed parameters. The callback GET is exempt from CSRF only because the provider redirect cannot carry the application CSRF token; strict state and same-session binding are mandatory. Do not persist callback code, query, state, nonce, verifier, or raw response.
6. **Separate callback exchange from durable commit.** After a successful OpenAI exchange, retain the complete credential set only in bounded short-lived memory state bound to the same session/user. Return a local completion page with no credentials and require a CSRF-protected POST commit. Recheck session, managed user, owner authorization, and operator gate at commit, then call M001’s atomic store. The xAI device flow also commits only through a CSRF-protected action after the same checks. Clear transient flow state on every terminal path.
7. **Redact callback traffic.** Application logs, traces, diagnostics, error pages, and request inspection must omit the callback query and provider body. Configure/document trusted ingress access logs to omit or redact the fixed callback query. Return only bounded non-secret status/error codes; never echo codes, state, tokens, provider bodies, or query values.

## 7. Ordered work packages

### Work package A — Define and validate the canonical callback base

Intent: Construct a provider redirect URI from an operator-controlled trust root, never an untrusted request header.

Required changes: Add [oauth].callback_base_url as an optional canonical HTTPS origin, validate it strictly, build only the fixed OpenAI callback URI, and use existing config classification. Keep [integrations].advertise_base_url and [server].host semantics unchanged.

Acceptance evidence: Unit tests accept only valid configured HTTPS origins and reject Host/Forwarded spoofing, userinfo, path/query/fragment, malformed URLs, and unset configuration. OpenAI flow refuses safely when unset. Tests prove advertise_base_url and server.host are not read as callback authority.

### Work package B — Implement authenticated xAI device-code

Intent: Preserve the approved user-facing device flow for xAI.

Required changes: Add session/CSRF-protected start, poll, cancel, and commit actions; bind transient state to the session user; validate verification URL; use the M001 atomic store.

Acceptance evidence: xAI stub covers pending, slow_down, success, denial, expiry, cancellation, operator disablement, session loss, prior credential preservation, and redaction. No local callback route or listener is used.

### Work package C — Implement OpenAI Web UI authorization-code/PKCE

Intent: Complete OpenAI browser authorization through a fixed callback on the existing server listener.

Required changes: Start a library PKCE transaction from a CSRF-protected mutation; persist it only in bounded memory bound to session/user; register fixed GET /oauth/openai/callback on the existing listener; validate and consume state/nonce once; stage exchanged credentials in memory; commit only through a CSRF-protected POST after rechecks.

Acceptance evidence: OpenAI stub covers valid callback, provider denial, code/state/nonce tamper, duplicate/replay, wrong session/user, missing session, restart, operator disable before callback and before commit, prior-credential preservation, and M001 atomic commit. No second listener is opened and callback query never reaches logs or persistence.

### Work package D — Qualify session, CSRF, errors, and secret handling

Intent: Prove callback exceptions do not weaken ordinary browser mutation protections.

Required changes: Test CSRF on start/cancel/commit, strict session/state on callback GET, typed bounded errors, transient-state teardown, ingress log redaction guidance, and old-credential retention.

Acceptance evidence: Callback GET accepts no CSRF token only when exact state and same session/user validate; all other state-changing actions reject CSRF failure. Error/status projections contain no query/code/state/token/provider body.

### Work package E — Document deployment and user behavior

Intent: Make the two Web UI flows and the new callback-origin prerequisite explicit.

Required changes: Document xAI device-code versus OpenAI browser callback, the separate [oauth].callback_base_url setting, trusted HTTPS ingress/redirect registration, reverse-proxy query redaction, session loss/restart behavior, and M001 backup/restore.

Acceptance evidence: Documentation makes clear the user decision approves the scoped design only; provider support/registration must still be verified. No route or provider capability is described as already implemented.

## 8. Failure, cancellation, restart, contention semantics

A failed xAI poll or OpenAI callback never changes durable credentials. OpenAI state is consumed once before exchange; tampered, replayed, expired, or wrong-session callbacks fail closed. Provider denial is mapped to a bounded result. Callback GET may exchange the code only after session/state checks and keeps the resulting credentials in memory until the separate CSRF-protected commit. Commit rechecks operator enablement, active session, same managed user, existing owner authorization, and M001 availability. If any check fails, discard transient credentials and retain the previous M001 credential. Restart or session loss destroys pending state; the user begins again. No callback query/state/code is written to disk, SQLite, logs, or diagnostics. Concurrent flows use M001’s per-account version/lock contract; stale completion cannot overwrite a newer credential.

## 9. Compatibility and migration

No inference API change. The fixed OpenAI callback route and [oauth].callback_base_url are new server protocol/config surface. Do not claim they exist at baseline. Existing [integrations].advertise_base_url remains profile output only; [server].host remains the listen socket. Preserve static-key and administrator-shared accounts, existing personal-upstream grants, and M001’s secret-file format. Pending PKCE/device state is memory-only; no callback or provider token is persisted except through M001’s approved credential store. If the new config setting cannot be added without changing unrelated reload semantics, stop for architecture review rather than overloading another setting.

## 10. Required tests

- xAI device-code state machine: pending, slow_down, success, denial, expiry, cancellation, URL validation, and operator disablement.
- OpenAI authorization-code/PKCE transaction using eggpass-auth primitives; callback uses the existing web listener and opens no separate socket.
- Callback tamper matrix: wrong/missing/duplicate state, nonce or code; replay; expired flow; malformed query; provider denial; wrong redirect URI; query mutation; and mismatched provider result.
- Same-browser-session and same-managed-user binding: start as user A, callback/commit as user B or a new session is refused; no username/user_id is accepted as owner authority.
- CSRF failures on start, cancel, and commit are refused; callback GET has no CSRF token but requires exact state and the same authenticated session.
- Operator gate disabled before start, between start/callback, between callback/commit, and at commit prevents credential publication and preserves the previous credential.
- Callback origin validation uses only [oauth].callback_base_url; spoofed Host, Forwarded, X-Forwarded-Host, [server].host, or [integrations].advertise_base_url cannot affect redirect_uri.
- Missing/invalid callback-base setting fails OpenAI closed; configured HTTPS origins with userinfo/path/query/fragment or mismatched provider registration are rejected.
- Restart/session loss after start or callback staging discards transient state and preserves the prior credential; re-login succeeds after restart.
- Callback code/state/query, verifier, tokens, raw provider response, and sensitive errors are absent from app logs, traces, diagnostics, persistent stores, and documented ingress access logs.
- Atomic M001 commit, failed re-login preservation, owner isolation, and model composition with administrator-shared models.
- No fixed sleeps for callback/restart/cancellation synchronization; use observable session, listener-route, provider-stub, and commit boundaries.

## 11. Required verification commands

    cargo fmt --manifest-path rust/Cargo.toml --all -- --check
    cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
    cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
    cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
    cargo test --manifest-path rust/Cargo.toml --test provider_oauth -- --test-threads=1
    cargo test --manifest-path rust/Cargo.toml --test on_prem_admin_ui -- --test-threads=1
    cargo test --manifest-path rust/Cargo.toml --test on_prem_sessions -- --test-threads=1
    cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
    cargo test --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- --test-threads=1
    cargo tree --manifest-path rust/Cargo.toml -e features -i eggfetch-core
    cargo tree --manifest-path rust/Cargo.toml --duplicates
    cargo deny --manifest-path rust/Cargo.toml check
    cargo build --manifest-path rust/Cargo.toml --locked --release
    git diff --check

provider_oauth and the new callback/config tests are implementation targets if they do not yet exist. Run Rust tests serially. Use the single-version Eggfetch 0.4.0 dependency profile approved by ADR-0013; M002 itself must not add further transport changes.

## 12. Documentation updates

- docs/providers.md: after implementation, list xAI Web UI device-code and OpenAI Web UI authorization-code/PKCE, plus both desktop CLI loopback PKCE flows; do not imply unverified provider support.
- Web UI/deployment docs: explain xAI device verification, OpenAI browser redirect, the required [oauth].callback_base_url, exact redirect registration, protected canonical HTTPS origin, and callback query redaction at trusted ingress.
- Security/operations docs: document same-session state binding, CSRF on start/cancel/commit, callback GET exception, one-time state, restart/session-loss re-login, operator gate rechecks, and bounded errors.
- docs/backup-restore.md: continue to follow M001’s credential-file contract; pending callback state is not backed up.

## 13. Acceptance criteria

- xAI M002 is authenticated Web UI device-code; OpenAI M002 is authenticated Web UI authorization-code/PKCE using eggpass-auth transaction primitives and a fixed callback route on the existing listener.
- OpenAI redirect_uri is built only from a separately configured canonical public HTTPS origin and fixed callback path; no Host/Forwarded/server.host/advertise_base_url trust is used.
- The callback requires the same live authenticated browser session/user and one-time state/nonce; start/cancel/commit mutations require CSRF; callback GET requires strict state/session binding.
- Callback code/state/query are redacted and never persisted. M001 commit occurs only after rechecking operator gate, session/user, and owner authorization.
- Tamper, replay, session mismatch, CSRF failure, origin spoof, provider denial, restart/session loss, and disabled gate preserve prior credentials and have bounded errors.
- Desktop CLI loopback PKCE for both xAI and OpenAI remains M003. This plan records approval of the scoped redesign only, not provider support or legal compliance.

## 14. Stop conditions

Stop and report if OpenAI does not support the required authorization-code/PKCE client registration or exact fixed redirect; if a canonical public HTTPS origin cannot be configured and validated; if the route would need to trust Host, Forwarded, X-Forwarded-Host, [server].host, or the profile-only [integrations].advertise_base_url; if a separate listener is required; if the same browser session and one-time state/nonce cannot be enforced; if callback query values cannot be excluded from logs/diagnostics/ingress logs; if CSRF on start/cancel/commit or operator/session/user rechecks cannot be maintained; if M001 atomic commit or existing owner authorization cannot be used; or if provider denial, restart, or session loss can overwrite the prior credential. Do not silently substitute a callback base or flow.

## 15. Closure evidence required

The later closure record must include provider-registration evidence for the exact xAI and OpenAI flows, explicitly separate from the user’s design approval; canonical callback-base validation and redirect URI tests; evidence the callback is on the existing listener with no extra socket; PKCE/state/nonce/session binding and tamper/replay tests; CSRF and operator-gate recheck tests; provider denial, restart/session loss, and prior-credential preservation; M001 atomic commit and owner-binding evidence; log/diagnostic/ingress query redaction; docs and deployment guidance; focused/default/no-default Rust results, strict Clippy, dependency and locked-build results; and a registry unblock audit. No closure record is created by this planning revision.

## 16. Handoff notes

Read accepted ADR-0012 and M001’s current credential-store interface, then inspect server listener/authentication/session/config owners before implementation. OpenAI M002 uses the existing web listener and a configured canonical callback origin; M003 remains desktop loopback for both providers. The callback base setting is separate from [integrations].advertise_base_url and [server].host. eggpass-auth 0.1.1 supplies transaction primitives but does not prove provider registration support. Keep tests serial and do not claim implementation or external provider/legal evidence.


## 17. Append-only scope amendment — ADR-0014

This amendment is authoritative for current M002 work and supersedes conflicting OpenAI Web UI scope, implementation, acceptance, documentation, and verification requirements above without rewriting the historical plan text.

- M002 is authenticated xAI Web UI device-code only.
- OpenAI Web UI authorization, the `/oauth/openai/callback` route, the `/api/user/oauth/openai/*` actions, and `[oauth].callback_base_url` are removed and are not supported scope.
- OpenAI remains in M003's desktop loopback PKCE flow; xAI desktop loopback remains M003 as well.
- M001 custody, user/provider/account ownership, operator gating, CSRF/session checks, secret exclusion, previous-credential preservation, and atomic commit requirements remain unchanged.
- Keep exact provider-registration evidence as an implementation/closure requirement. The approved desktop loopback workflow is not evidence for a separate server-hosted HTTPS redirect.

See `plans/adrs/ADR-0014-provider-oauth-web-ui-and-desktop-flow-boundary.md`. No other accepted ADR-0012 decision is changed.

## 18. Append-only credential-refresh amendment — ADR-0015

ADR-0015 is accepted and supersedes this plan's no-401-replay wording — §4's "No shared consumer-subscription pooling, HTTP 401 replay, or cross-account fallback" and §13's "No consumer-subscription pooling, cross-account fallback, or HTTP 401 replay is introduced". That wording remains historical and is not rewritten.

An upstream HTTP 401 on an owner-bound OAuth account is now handled by M004's owner-scoped credential-refresh retry: the owner's credential for that same account is refreshed and the same request is retried against the same account exactly once. Nothing in this milestone's flow changes. The retry never reselects an account, never crosses a user or owner boundary, never pools credentials, and never relaxes the login, session, CSRF, operator-gate, redaction, previous-credential-preservation, or M001 atomic-commit requirements above. A refresh failure or a second rejection terminalizes the request and disables the account, so the owner re-authenticates through this milestone's flow. This amendment makes no provider-support or provider-registration claim.

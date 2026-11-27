# Provider OAuth Milestone 003 — Desktop loopback browser login

Status: blocked

Repository baseline: 5b212d2d69137b99ed39f0dbeab10331c839e54e

Source roadmap:

- plans/subsystems/provider-oauth-roadmap.md#milestone-3--desktop-loopback-browser-login

Long-term requirements:

- plans/000-long-term-specification.md#1-product-definition
- plans/000-long-term-specification.md#2-end-state-invariants-normative
- plans/001-terminology-and-domain-model.md#identity-and-lifecycle-terms

Applicable ADRs:

- plans/adrs/ADR-0012-provider-oauth-account-custody-and-lifecycle.md (proposed; hard blocker)
- plans/adrs/ADR-0010-personal-upstream-account-custody.md (accepted context only; does not authorize OAuth)

Primary class: capability

## 1. Objective

After M001 and the policy gate close, let an authorized desktop CLI user complete an approved authorization-code OAuth flow through a provider browser page, validate the callback with PKCE/state, and atomically store the resulting credential against the intended account.

## 2. Why this milestone is ready

It is not ready. Hard dependencies: ADR-0012 must authorize the account/owner and provider grant; M001 must close with its durable CredentialStore and account token manager; the provider must permit the exact loopback redirect/callback path and client registration. Do not use a local browser callback to bypass headless policy or expose EggPool's API listener.

## 3. Current implementation evidence

- rust/src/cli.rs and rust/src/runtime.rs own the current EggPool CLI, not server/*.
- rust/src/operations/config_mutation.rs owns file/config mutation and static account connect/logout; it has no OAuth callback listener.
- EggPool's public listener is separately bound through server/runtime ownership; it is not an OAuth redirect listener.
- eggpass-auth 0.1.1 provides begin/validate/complete authorization primitives with PKCE S256, state, nonce, and provider parameters, but it does not create a callback server or launch a browser.
- eggpass 0.1.3 src/auth/flow.rs is the behavior reference: it binds 127.0.0.1:0, constructs the provider-approved callback URI, uses the library transaction, launches the browser best-effort, validates callback state/path/origin, and closes after completion. Callback paste is subject to provider/client-ID rules. Reference: https://github.com/eggstack/eggpass/tree/664207cd898432024f43dff4910ac3fd06c54b6e.

## 4. Invariants that must not regress

- Listener binds only loopback 127.0.0.1 on an OS-assigned ephemeral port; never EggPool's API port or a wildcard/LAN address.
- Listener exists only for the bounded login transaction and closes on success, error, timeout, cancellation, and process shutdown.
- PKCE S256, random state, and nonce come from the approved library transaction; callback validates exact origin/path, unique parameters, and matching state before exchange.
- Provider authorization/token endpoints come only from an approved profile; user-supplied URLs/redirects are never trusted.
- Codes, state, nonce, verifier, access/refresh/ID tokens, and sensitive callback URLs are not logged or persisted outside the approved credential store.
- A browser on another machine cannot reach a local loopback listener. Recommend device-code flow for remote/headless hosts; allow manual callback input only when provider contract supports it and state remains verified.
- No consumer-subscription account becomes pooled/shared by this flow.

## 5. Scope

### In scope

- Desktop login mode in the CLI/account surface selected by ADR-0012, reusing M001 storage/account binding.
- Ephemeral loopback listener, auth URL presentation/opening, callback validation, bounded code exchange, and atomic credential save.
- Safe copy/paste fallback only where approved by provider profile, including full callback metadata when the provider issues a client ID.
- Local callback/OAuth-stub tests for success, denial, mismatch, timeout, cancellation, and listener cleanup.

### Explicitly out of scope

- Headless device-code flow (M002), unapproved provider/account classes, shared subscription pooling, or web-dashboard OAuth.
- Public/internet callback servers, fixed API-port callback, configurable redirect host, arbitrary callback URL, or arbitrary OAuth endpoint.
- Saving partial credentials, accepting tokens in argv, disabling PKCE/state, or falling back to static API keys.
- Recreating eggpass-auth's OAuth protocol, Eggpass's complete CLI, or its synchronous runtime bridge.

## 6. Required production changes

Expose browser login only under the account and owner surface authorized by ADR-0012. Use one explicit account selection and M001's account-scoped credential manager. Keep UI work in the CLI/operations layer; do not add a server/* route.

Bind a standard-library or approved async listener to 127.0.0.1:0 before building the redirect URI. The path must exactly match the approved provider profile. The listener accepts only a minimal callback request, bounds request bytes/time, rejects malformed/duplicate/unexpected parameters, and never logs the query string. Do not hold a SQLite transaction, routing lock, or mutable configuration lock while waiting for a human/browser.

Use eggpass-auth 0.1.1 begin_authorization/complete_authorization so PKCE S256, state, nonce, client-ID policy, and provider parameters are library-owned. The host opens the authorization URL only after validating its HTTPS host against the approved profile; browser-opening failure prints a bounded manual URL. The callback listener verifies loopback origin, exact path, one-time transaction state, and provider response; the library performs final callback validation and code exchange. Use EggPool's existing async runtime without block_on/nested runtimes.

If manual completion is supported, accept only a full callback URL that passes the same origin/path/state checks. A pasted bare code is allowed only when the approved provider/client-ID contract supports it and the library method performs the complete transaction. Never accept a device code, raw token, or callback copied from an untrusted host. On success, save through M001 atomically; failed/canceled reauthorization preserves the existing credential.

## 7. Ordered work packages

### Work package A — Bind a bounded local callback

Intent: Receive the provider redirect without exposing EggPool's service listener.

Required changes: Bind loopback on an ephemeral port, derive only the provider-approved callback path, impose request/deadline bounds, and ensure deterministic listener teardown.

Acceptance evidence: Tests observe loopback-only binding, unique ephemeral port distinct from the service port, bounded request parsing, no external interface, and listener close on every terminal path.

### Work package B — Create and validate the browser authorization transaction

Intent: Use the library's authorization-code/PKCE contract and open only a trusted URL.

Required changes: Begin an authorization transaction, validate generated HTTPS host, print/open URL safely, accept one callback or authorized paste, and validate path/origin/state before token exchange.

Acceptance evidence: Valid callback completes once; wrong state, duplicate state/code, wrong host/path/port, provider denial, malformed URL, replayed callback, userinfo, fragment, or untrusted authorization host is refused without storing credentials.

### Work package C — Persist and report only after success

Intent: Commit a complete credential set for the selected account.

Required changes: Validate issuer/client/account identity, atomically save through M001, retain old credentials on preceding failure, and report only a bounded account/expiry summary.

Acceptance evidence: Failed callback/token exchange/save leaves old credential intact; successful credentials work after restart; no callback material or token appears in output/logs/SQLite.

### Work package D — Desktop/remote guidance and process tests

Intent: Make the environment distinction explicit.

Required changes: Add CLI output/docs for manual URL open, same-machine callback, provider-approved paste fallback, and device-flow choice for a remote/headless host.

Acceptance evidence: CLI process tests verify output/exit behavior; docs do not claim remote browsers can reach the local callback. Device-flow help points to M002 where supported.

## 8. Failure, cancellation, restart, contention semantics

Timeout, malformed callback, browser-open failure followed by cancellation, provider denial, token-exchange error, or failed atomic save does not replace the old credential. Listener teardown is unconditional. A callback arriving after cancellation or successful exchange is rejected. A stale authorization transaction cannot overwrite a newer login; use M001's version/lock contract. Restart destroys the pending PKCE transaction and listener; the user starts a new flow. The service never accepts a late callback on its public port. If the browser is remote and cannot return a callback safely, use device flow instead of exposing a remote callback route.

## 9. Compatibility and migration

No server protocol change. Existing static account credentials and CLI connect/logout stay intact. Reuse M001's credential storage; no parallel file format or SQLite token payload. Redirect URI/client registration are provider-specific and must be documented by the approved profile; no broad provider assumptions.

## 10. Required tests

- Local fake provider and callback tests for PKCE challenge, state/nonce, valid code exchange, token denial, provider error, and one-time callback semantics.
- Callback negative matrix: wrong origin/path/port/state, duplicate parameters, missing code, malformed/oversized request, userinfo, fragment, replay, and untrusted host.
- Listener lifecycle: bind conflict, timeout, Ctrl-C/cancel, malformed request, success, exchange failure, and teardown after each.
- Hostile/invalid authorization URL is rejected before browser open; browser-open failure still permits a safe manual URL.
- Paste fallback only for provider contract that permits it; issued-client-ID provider requires validated full callback metadata.
- Existing credential survives failed/canceled re-login; successful credential is atomic and account-scoped.
- No callback URL, code, state, verifier, or token in logs/stdout/stderr/SQLite/diagnostics.
- No test relies on fixed sleeps; synchronize on callback listener and stub HTTP boundaries.

## 11. Required verification commands

    cargo fmt --manifest-path rust/Cargo.toml --all -- --check
    cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
    cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
    cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
    cargo test --manifest-path rust/Cargo.toml --test provider_oauth -- --test-threads=1
    cargo test --manifest-path rust/Cargo.toml --test cli_contract -- --test-threads=1
    cargo test --manifest-path rust/Cargo.toml --test coordinator_boundaries -- --test-threads=1
    cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
    cargo tree --manifest-path rust/Cargo.toml -e features -i eggfetch-core
    cargo tree --manifest-path rust/Cargo.toml --duplicates
    cargo deny --manifest-path rust/Cargo.toml check
    cargo build --manifest-path rust/Cargo.toml --locked --release
    git diff --check

Compare dependency audit output with baseline; do not waive existing advisories or add features to make the flow work.

## 12. Documentation updates

- CLI docs: browser/opening behavior, manual URL, same-machine callback, and device-code alternative.
- docs/providers.md: document only approved provider/flow after ADR-0012; keep every other OAuth integration explicitly unsupported.
- Security/operations docs: ephemeral listener, callback checks, credential custody, backup/restore, and failure guidance.

## 13. Acceptance criteria

- Desktop login uses a loopback ephemeral listener and approved PKCE/state callback contract, never EggPool's API port.
- Invalid/untrusted/replayed callbacks do not store credentials; failure/cancel preserves old credentials; success is durable and usable only by the approved account/owner.
- Headless/remote users are directed to device flow where approved; no internet-facing callback is added.
- No secret or callback material leaks into output, logs, SQLite, profiles, or diagnostics.
- Static-key and provider transport behavior remains unchanged; all required security and serial tests pass.

## 14. Stop conditions

Stop until ADR-0012 is accepted and M001 is closed. Stop if the provider does not approve the ephemeral callback URI/path, client registration, or PKCE contract; if the flow requires a wildcard/public listener; if callback origin/state validation cannot happen before exchange; if browser and CLI hosts differ without an approved paste/device contract; or if the token store cannot atomically preserve prior credentials. Do not add a callback service or expose the API listener.

## 15. Closure evidence required

Include accepted provider/account scope; callback/PKCE state-machine test map; loopback/interface/port and listener teardown evidence; local OAuth-stub exchange; preserved-old-token failure tests; output/log/DB redaction checks; dependency/full Rust verification results; updated CLI/security docs; and registry audit for any milestone unblocked by M003.

## 16. Handoff notes

Re-read eggpass-auth 0.1.1 and Eggpass CLI 0.1.3 source at commit 664207cd898432024f43dff4910ac3fd06c54b6e, especially the separation between library protocol and host callback listener. Do not copy the sync runtime bridge. Use EggPool's own Tokio runtime and account custody. Keep tests serial and preserve unrelated working-tree changes/artifacts.

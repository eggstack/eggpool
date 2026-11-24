# Provider OAuth Milestone 003 — Desktop CLI loopback PKCE login

Status: implemented

Repository baseline: 5b212d2d69137b99ed39f0dbeab10331c839e54e

Source roadmap:

- plans/subsystems/provider-oauth-roadmap.md#milestone-3--desktop-cli-loopback-pkce-login

Long-term requirements:

- plans/000-long-term-specification.md#1-product-definition
- plans/000-long-term-specification.md#2-end-state-invariants-normative
- plans/001-terminology-and-domain-model.md#identity-and-lifecycle-terms

Applicable ADRs:

- plans/adrs/ADR-0012-provider-oauth-account-custody-and-lifecycle.md (accepted ownership/flow policy)
- plans/adrs/ADR-0013-provider-oauth-eggfetch-compatibility.md (accepted dependency contract)
- plans/adrs/ADR-0010-personal-upstream-account-custody.md (accepted owner-binding context)

Primary class: capability

## 1. Objective

Let the desktop CLI authorize the managed user’s own xAI or OpenAI account through authorization-code/PKCE and a short-lived loopback callback, then commit through M001. The CLI separately authenticates to EggPool with a new, single-purpose `oauth_credentials_write` `epk1_` credential. The managed user obtains that self-only credential through the authenticated Web UI with CSRF protection; the CLI reads it only from a hidden TTY prompt or stdin, never argv or environment. The CLI posts the completed provider credential to `POST /api/user/oauth-credentials`; the route derives ownership only from the verified `AuthenticatedPrincipal.user_id`, preserves M001 owner checks, and delegates to M001’s atomic commit. No username or user ID in CLI arguments or request data is owner authority. M002 is xAI Web UI device-code and OpenAI Web UI authorization-code/PKCE through a fixed callback on EggPool’s existing listener; M003 is desktop loopback PKCE for both providers.

## 2. Why this milestone is ready

ADR-0012 fixes xAI/OpenAI and per-user custody. Callback work can proceed against local OAuth stubs and a test double for M001’s account identity and atomic credential-commit interface. Integration and closure depend on that interface; do not create another store or owner-binding model. The new `oauth_credentials_write` scope and `POST /api/user/oauth-credentials` route are not implemented at baseline and are M003 deliverables. The desktop provider callback is loopback-only and never uses EggPool’s public API listener.

The exact runtime candidate is eggpass-auth 0.1.1. Eggpass 0.1.3 is reference only for host-side callback behavior. M001 implements ADR-0013's single-version Eggfetch 0.4.0 graph and provider-transport requalification; M003 uses that established dependency contract and makes no additional transport changes.

## 3. Current implementation evidence

- rust/src/cli.rs and rust/src/runtime.rs own EggPool CLI operation; rust/src/server/ remains separate from desktop callback ownership.
- rust/src/operations/config_mutation.rs and rust/src/operations/paths.rs own current account mutation and secret-file publication. M001 provides the OAuth-specific commit service.
- The public EggPool listener is not an OAuth redirect listener and must not accept provider callbacks.
- eggpass-auth 0.1.1 provides candidate authorization/PKCE protocol primitives but does not own EggPool’s callback server, browser launch, persistence, or user binding.
- Eggpass 0.1.3 source at 664207cd898432024f43dff4910ac3fd06c54b6e is a behavior reference only.
- `rust/src/identity.rs` resolves verified `epk1_` tokens to `AuthenticatedPrincipal.user_id` and scopes, but the baseline scope map has no `oauth_credentials_write` scope. `rust/src/server/mod.rs` has no `POST /api/user/oauth-credentials` route. M003 must add both; neither is present functionality.
- The current CLI can accept a target username and default the actor to admin (`rust/src/cli.rs`); M003 must not use that target selection as OAuth credential ownership authority.

## 4. Invariants that must not regress

- Bind only 127.0.0.1 on an OS-assigned ephemeral port; never the service API port, wildcard, or LAN address.
- Keep the listener alive only for the bounded transaction and close it on success, error, timeout, cancellation, and shutdown.
- Use PKCE S256, random state, and nonce from the approved provider transaction; validate exact callback origin/path and state before exchange.
- Use only xAI/OpenAI reviewed profiles and the desktop authorization-code/PKCE flow.
- Bind the committed credential to the managed user identified by the verified `AuthenticatedPrincipal.user_id` from the dedicated `oauth_credentials_write` `epk1_` credential. No caller-supplied username/user_id, admin default, cross-user sharing, consumer-subscription pooling, or cross-account fallback.
- Issue the EggPool write credential only to the authenticated managed user through a CSRF-protected Web UI action; accept it in the CLI only through hidden TTY input or stdin, never argv/environment.
- Operator OAuth enablement defaults on; disabled blocks new login, OAuth routing, and refresh while retaining credential files.
- Never log or persist codes, state, nonce, verifier, callback query, tokens, or sensitive URL data outside the approved credential store.
- Do not add a Web UI callback route, automatic HTTP 401 replay, or provider transport policy.

## 5. Scope

### In scope

- Desktop CLI authorization-code/PKCE for the authenticated managed user’s xAI or OpenAI account, using an ephemeral loopback callback.
- A self-only EggPool `epk1_` credential with the single `oauth_credentials_write` scope, provisioned through authenticated Web UI/CSRF and entered by hidden TTY or stdin.
- New `POST /api/user/oauth-credentials`, authorized only by that scope; derive the owner solely from verified `AuthenticatedPrincipal.user_id`, preserve M001 owner checks, and delegate to M001’s atomic commit.
- Validated authorization URL, bounded callback parsing, provider error handling, code exchange, browser-open/manual URL behavior, and safe provider-approved callback handling.
- Tests for credential scope/route isolation, callback success and rejection, state/PKCE, listener teardown, cancellation, owner binding, and secret redaction.

### Explicitly out of scope

- M002’s xAI Web UI device-code flow and OpenAI Web UI authorization-code/PKCE callback on the existing EggPool listener; M003 adds no provider callback to that listener. Also out of scope: CLI device-code, public callback servers, or a separate EggPool callback listener.
- Providers beyond xAI/OpenAI, arbitrary endpoints/redirects, shared consumer-subscription credentials, or account pooling.
- Raw token import, credential arguments, static-key fallback, parallel storage, 401 replay, or cross-account fallback.
- Recreating OAuth protocol logic already supplied by eggpass-auth or copying Eggpass’s full CLI/runtime bridge.

## 6. Required production changes

Expose the desktop provider flow only through the approved CLI account operation. Before starting it, require the managed user’s self-only EggPool credential, provisioned through authenticated Web UI/CSRF with only the new `oauth_credentials_write` scope. Read the secret through hidden TTY input or stdin; never accept it in argv/environment or log/output it. The CLI must not select an owner by username, user ID, provider profile, or admin default. Add `POST /api/user/oauth-credentials`; require the dedicated scope and derive the owner solely from the verified `AuthenticatedPrincipal.user_id`, preserve M001 owner checks, and delegate the provider credential to M001’s atomic commit. The request must not accept an owner field. Check the operator OAuth gate before starting the provider transaction and before commit; if disabled, block login, routing, and refresh but retain stored credentials.

Bind a minimal callback listener to 127.0.0.1:0 before constructing the redirect URI. Use only the provider-approved callback path and endpoint from the xAI/OpenAI profile. Bound request bytes and deadline; reject malformed, duplicate, unexpected, wrong-origin, wrong-path, wrong-port, wrong-state, replayed, userinfo, or fragment cases before token exchange. Never log the query string. Do not hold a SQLite, routing, or mutable configuration lock while waiting for browser interaction.

Use eggpass-auth 0.1.1 for authorization-code/PKCE transaction state, including PKCE S256, state, nonce, and provider parameters. Validate the generated HTTPS authorization host before best-effort browser open; manual URL presentation must remain safe if open fails. Accept only callback input allowed by the reviewed provider contract and validate it with the same transaction. Do not accept a raw token, device code, or arbitrary callback URL.

On successful validated exchange, verify provider/account result and call M001’s atomic credential-commit interface for the intended managed user. Report only a bounded account/expiry status. Failure, cancellation, timeout, invalid callback, disabled gate, or failed save preserves the previous credential. No 401 replay or account switch is added.

## 7. Ordered work packages

### Work package A — Bind the managed principal and local listener

Intent: Commit only for the managed user proven by EggPool authentication while receiving the provider redirect without exposing a network service.

Required changes: Provision a self-only EggPool credential through the authenticated Web UI with CSRF protection and only `oauth_credentials_write`; read it through hidden TTY input or stdin. Add `POST /api/user/oauth-credentials`, require that single-purpose scope, derive ownership only from verified `AuthenticatedPrincipal.user_id`, preserve M001 owner checks, and call M001’s atomic commit. Accept no owner field. Bind the provider callback only to loopback on an ephemeral port, derive the approved callback URI, bound parser/deadline resources, and close deterministically.

Acceptance evidence: Tests prove the credential has only the write scope and cannot access unrelated scoped routes; route ownership follows the verified principal and cannot be changed by request fields or a different username; invalid, revoked, expired, or insufficient-scope credentials are refused. CLI input does not expose the secret in argv, environment, logs, or output. Listener tests observe loopback-only ephemeral binding, bounded parsing, no external interface, and teardown on every terminal path.

### Work package B — Create and validate the PKCE transaction

Intent: Use the library transaction and accept only the matching provider callback.

Required changes: Create authorization state with PKCE S256, state, and nonce; validate HTTPS authorization host before open; parse one callback and validate origin/path/port/state before exchange; apply provider-specific callback rules for xAI and OpenAI.

Acceptance evidence: Correct stub callback completes once. Wrong origin/path/port/state, duplicate parameters, provider denial, malformed/oversized request, userinfo, fragment, untrusted host, or replay is rejected without saving credentials.

### Work package C — Commit only a complete owner-bound credential

Intent: Preserve the previous credential until the new grant is fully accepted.

Required changes: Validate returned provider/account identity, re-check operator enablement, save through M001 atomically, and expose only bounded non-secret status.

Acceptance evidence: Failed callback, exchange, disablement, or save leaves the prior credential intact; success survives restart and is usable only for the intended user; no callback material or token appears in output, logs, SQLite, profiles, or diagnostics.

### Work package D — Exercise process and documentation behavior

Intent: Make local desktop and remote/headless boundaries explicit.

Required changes: Add CLI process tests for browser-open success/failure, manual URL display, timeout/cancel, and safe diagnostics. Document that a remote browser cannot reach local loopback and direct Web UI users to the M002 flow approved for that provider.

Acceptance evidence: CLI tests and provider stubs cover both approved providers; documentation does not claim remote callback access; secrets remain absent from outputs.

## 8. Failure, cancellation, restart, contention semantics

Missing, malformed, revoked, expired, or insufficient-scope EggPool credentials, a denied owner check, timeout, malformed callback, browser-open failure followed by cancellation, provider denial, token-exchange error, operator disablement, or failed atomic save does not replace the prior credential. The route never accepts caller-selected ownership. Listener teardown is unconditional. Late or repeated callbacks are rejected. A stale transaction cannot overwrite a newer login; use M001’s version/lock contract. Restart destroys the pending PKCE transaction and listener, and the user starts again. If the browser and CLI host differ, loopback cannot complete remotely; use the authenticated M002 Web UI flow for that provider rather than exposing a public callback. No 401 replay or alternate-account fallback occurs.

## 9. Compatibility and migration

M003 adds the `oauth_credentials_write` `epk1_` scope and `POST /api/user/oauth-credentials` server API surface; it does not change the public inference API. Existing static-key and administrator-shared accounts remain intact. Reuse M001’s owner-bound secret store and secret-free references; no parallel file format or token column. Callback URI/client registration are provider-specific and must match reviewed xAI/OpenAI profiles. Preserve current account identity and authorization controls.

## 10. Required tests

- Local xAI/OpenAI provider stubs for authorization URL, PKCE S256 challenge, state/nonce, valid code exchange, denial, provider error, and one-time callback.
- The self-only `oauth_credentials_write` token is issued only through authenticated Web UI/CSRF, carries no unrelated scopes, and is consumed only by the new write route; unrelated tokens/scopes cannot use it.
- `POST /api/user/oauth-credentials` derives owner only from verified `AuthenticatedPrincipal.user_id`; no username/user_id field or admin default can select another owner, and M001 owner checks/atomic commit remain authoritative.
- Hidden-TTY/stdin credential entry does not put the EggPool credential in argv, environment, stdout/stderr, logs, or diagnostics.
- Callback negative matrix: wrong origin/path/port/state, duplicate parameters, missing code, malformed/oversized request, userinfo, fragment, replay, and untrusted host.
- Listener lifecycle: bind failure, timeout, cancellation/Ctrl-C, malformed request, success, exchange failure, disablement, and teardown after each path.
- Authorization URL host validation occurs before browser open; browser-open failure permits only safe manual URL presentation.
- Provider-specific callback/paste rules are obeyed; no bare-code acceptance unless the reviewed provider contract permits it and the library validates the full transaction.
- Existing credential survives failed/canceled re-login; success atomically updates only the intended user/account and survives restart.
- Operator disablement blocks login, OAuth routing, and refresh but retains stored credentials.
- No callback URL, code, state, verifier, provider token, EggPool `epk1_` credential, or raw response appears in stdout, stderr, logs, SQLite, profiles, or diagnostics. The EggPool credential is accepted only through hidden TTY input or stdin, not argv/environment.
- No fixed sleeps for listener or cancellation readiness; synchronize on callback and provider-stub boundaries.

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

provider_oauth is an implementation test target. Run Rust tests serially. Compare dependency output to the branch baseline; do not add transport features or import unrelated transport work.

## 12. Documentation updates

- docs/providers.md: document only implemented xAI/OpenAI desktop loopback PKCE support after verification; do not claim other providers or flows.
- docs/cli-reference.md: explain target user/account, browser launch/manual URL, same-machine callback, expiry/cancellation, and bounded output.
- Security/operations guidance: ephemeral loopback listener, PKCE/state checks, credential custody, operator disablement, backup/restore, and failure behavior.
- Point remote/headless users to the M002 Web UI flow approved for that provider; do not claim a remote browser can reach loopback.

## 13. Acceptance criteria

- Desktop CLI completes authorization-code/PKCE for xAI and OpenAI using a loopback-only ephemeral listener, then commits through M001 via `POST /api/user/oauth-credentials` using a self-only `oauth_credentials_write` `epk1_` credential.
- The write route requires that dedicated scope and derives ownership only from verified `AuthenticatedPrincipal.user_id`; no username, user ID request field, or admin default can select the owner.
- Invalid, hostile, replayed, disabled, or canceled flows never replace an existing credential; successful credentials survive restart and belong only to the authenticated managed user.
- EggPool’s public listener receives no provider callback; M002 owns xAI Web UI device-code and OpenAI Web UI authorization-code/PKCE through a fixed callback on the existing listener.
- No secret/callback material leaks to output, logs, SQLite, profiles, or diagnostics. No consumer-subscription pooling, cross-account fallback, or HTTP 401 replay is introduced.
- Focused and full verification passes. These are future criteria; this plan does not claim implementation or test evidence.

## 14. Stop conditions

Stop and report if either approved provider does not support the exact ephemeral loopback redirect and PKCE contract; if loopback-only binding or validation before exchange cannot be guaranteed; if a wildcard/public/API listener is required; if the self-only scoped credential cannot be provisioned through authenticated Web UI/CSRF, safely entered by hidden TTY/stdin, or constrained to `oauth_credentials_write`; if the route cannot derive the owner from verified `AuthenticatedPrincipal.user_id` and preserve M001 checks; if M001’s storage interface is unstable; if operator disablement cannot block login/routing/refresh while retaining credentials; or if safe completion would require a new grant, provider, 401 replay, cross-account fallback, or transport feature widening. Do not improvise an internet-facing callback or dependency upgrade.

## 15. Closure evidence required

The later closure record must include both (a) xAI/OpenAI local-stub callback/PKCE results and (b) separate first-party provider-contract plus exact client-registration evidence that each provider accepts the implemented loopback redirect URI and PKCE parameters; local stubs alone do not establish provider support. Registration evidence must redact client secrets and other credentials, and any unsupported or unverified provider registration blocks M003 closure. Also include exact listener interface, ephemeral port, bounded parsing, and teardown evidence; owner binding and M001 atomic save/restart evidence; disabled-gate and previous-credential preservation; hostile/replayed callback rejection; output/log/database/profile/diagnostic secret-absence checks; CLI and documentation evidence; focused/default/no-default/dependency/locked release results; and a registry dependency audit. No closure record is created in this planning revision.

## 16. Handoff notes

Read ADR-0012, ADR-0013, M001’s account identity/credential-commit interface, and current CLI/operations owners before implementation. M003 is desktop loopback PKCE for both providers plus a self-only `oauth_credentials_write` `epk1_` credential and `POST /api/user/oauth-credentials`; the route trusts only verified `AuthenticatedPrincipal.user_id`, not a caller-selected user. The managed user provisions the credential through authenticated Web UI/CSRF, and the CLI accepts it only via hidden TTY/stdin. M002 separately owns xAI Web UI device-code and OpenAI Web UI PKCE on the existing listener. M003’s provider callback remains loopback-only and never uses EggPool’s listener. eggpass-auth 0.1.1 is the exact runtime candidate; Eggpass 0.1.3 is reference only. Do not copy a synchronous runtime bridge. Keep Rust tests serial and do not claim provider support or implementation evidence from this plan.


## 17. Append-only flow-scope amendment — ADR-0014

M003 remains the desktop loopback authorization-code/PKCE flow for both OpenAI and xAI. The OpenAI browser callback is hosted only by the CLI's ephemeral loopback listener; M002 no longer provides OpenAI Web UI login or any OpenAI callback route/configuration on EggPool's listener. This amendment supersedes conflicting M002 descriptions in this plan but does not change M003's owner-bound upload, loopback-only listener, PKCE/state, or M001 atomic-commit requirements. See `plans/adrs/ADR-0014-provider-oauth-web-ui-and-desktop-flow-boundary.md`.

## 18. Append-only credential-refresh amendment — ADR-0015

ADR-0015 is accepted and supersedes this plan's no-401-replay wording — §4's "Do not add a Web UI callback route, automatic HTTP 401 replay, or provider transport policy", §8's closing sentence, and §13's "No consumer-subscription pooling, cross-account fallback, or HTTP 401 replay is introduced". That wording remains historical and is not rewritten.

An upstream HTTP 401 on an owner-bound OAuth account is now handled by M004's owner-scoped credential-refresh retry: the owner's credential for that same account is refreshed and the same request is retried against the same account exactly once. Nothing in this milestone's flow changes. The CLI still binds only 127.0.0.1 on an ephemeral port, still applies PKCE/state and exact-callback validation, still commits only through `POST /api/user/oauth-credentials` from the verified principal, and still preserves the previous credential on every failure path. The retry never reselects an account, never crosses a user or owner boundary, never pools credentials, and adds no provider transport policy or callback listener. This amendment makes no provider-support or provider-registration claim.

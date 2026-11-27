# Provider OAuth Roadmap

Status: proposed

Long-term references:

- plans/000-long-term-specification.md#1-product-definition
- plans/000-long-term-specification.md#2-end-state-invariants-normative
- plans/000-long-term-specification.md#3-ownership-boundaries-normative
- plans/001-terminology-and-domain-model.md#identity-and-lifecycle-terms
- plans/002-long-term-roadmap.md#phase-5--optional-on-prem-small-team-access-and-budgeting

Related ADRs:

- plans/adrs/ADR-0012-provider-oauth-account-custody-and-lifecycle.md (proposed; blocks every implementation milestone)
- plans/adrs/ADR-0010-personal-upstream-account-custody.md (accepted; static/personal API-key boundary only, not OAuth authorization)

## 1. Purpose and ownership boundary

This proposed subsystem would own OAuth account authentication and token lifecycle for explicitly approved provider/account classes. EggPool owns the CLI surface, account ownership/binding, credential persistence, runtime integration, cancellation, and user-visible errors. eggpass-auth is a candidate for protocol primitives only. EggPool continues to own provider selection, account eligibility, quota/health, request preparation, and finalization.

| Concern | Owner |
|---|---|
| OAuth protocol primitives | Candidate eggpass-auth 0.1.1; caller supplies Eggfetch client and storage |
| OAuth CLI and local callback listener | EggPool CLI/operations layer; no listener in server/* |
| Credential persistence and account custody | EggPool secret-storage boundary selected by accepted ADR-0012; never SQLite for token bytes |
| Provider endpoint/auth metadata | eggpool-provider-profile; secret-free and only explicitly reviewed profiles |
| Token selection/refresh before dispatch | Account-scoped EggPool auth service under runtime/generation ownership |
| Provider HTTP transport | rust/src/providers/; transport-only, no OAuth policy or retry |
| User ownership and eligibility | Existing personal-upstream boundary only if accepted ADR-0012 explicitly applies it |

The reference implementation is the latest [Eggpass CLI crate 0.1.3](https://crates.io/crates/eggpass/0.1.3), sourced from [eggstack/eggpass commit 664207cd898432024f43dff4910ac3fd06c54b6e](https://github.com/eggstack/eggpass/tree/664207cd898432024f43dff4910ac3fd06c54b6e). The candidate crate is [eggpass-auth 0.1.1](https://crates.io/crates/eggpass-auth/0.1.1), the latest release checked 2026-11-22. Eggpack is unrelated producer release tooling and is not used as an OAuth source.

## 2. Work classification

### Invariants

- OAuth credentials remain outside SQLite, provider-profile metadata, logs, and diagnostics.
- A token set is tied to one approved provider account and its accepted owner/custody scope.
- No consumer-subscription credential is shared or pool-eligible without explicit accepted policy.
- Token refresh and credential removal are atomic and account-scoped.
- Requests retain generation leases and the existing owned-attempt boundary; no cross-account reselection after submission.
- Eggfetch remains exact-pinned to the current minimal native-http1 + tls-rustls profile unless a separate ADR authorizes a change.

### Capabilities

- An authorized operator/user can complete an approved OAuth login from a headless CLI using device code where supported.
- An authorized desktop CLI user can complete an approved authorization-code flow using an ephemeral loopback callback, PKCE, and state validation.
- Inference obtains a usable token for exactly the selected, eligible account; expired/revoked credentials fail safely.

### Infrastructure

- Caller-owned atomic OAuth credential storage and an account-scoped token manager.
- Proactive refresh and concurrency coordination, including CLI/server process coordination if both can mutate one token set.
- Async integration using the current EggPool runtime and typed failure ownership.

### Polish

- Bounded login status and expiry summaries that never reveal token material.

## 3. Non-goals

- Treating the candidate library's xAI/OpenAI profiles as automatic EggPool provider approval.
- Pooled consumer-subscription OAuth accounts or shared use across principals without explicit accepted authorization.
- OAuth support for Claude/Gemini consumer web, arbitrary issuers, arbitrary callback hosts, or unreviewed provider flows.
- Storing OAuth tokens in SQLite, provider-profile assets, logs, or inference diagnostics.
- Copying Eggpass's local proxy, synchronous runtime bridge, or credential-file format.
- Moving authentication, retries, or account selection into rust/src/providers/.
- Replaying arbitrary or streaming request bodies or selecting a different account after submission.

## 4. Current state

Repository baseline: 5b212d2d69137b99ed39f0dbeab10331c839e54e. This is planning-only; preserve the existing unrelated working-tree changes and untracked artifacts.

rust/src/config.rs defines static account API-key fields. rust/src/accounts/registry.rs resolves configured credentials into a secret-bearing in-memory CredentialStore while rust/src/db/repositories.rs exposes credential-free account identity and an environment-variable name. rust/src/runtime_lifecycle/generation.rs constructs provider/account runtime state. rust/src/coordinator/attempt.rs prepares an owned upstream attempt from a static account key before submission. rust/src/coordinator/failure.rs maps a generic 401 to authentication failure/backoff; no OAuth refresh handoff exists. rust/src/operations/config_mutation.rs and rust/src/cli.rs implement static-key connect/logout and personal-upstream operations. docs/providers.md explicitly excludes OAuth-only consumer integrations and browser/device-code flows.

The current EggPool package requires Rust 1.89 and uses Eggfetch 0.4.0 with native-http1 and tls-rustls. eggpass-auth 0.1.1 accepts a caller-owned Eggfetch client and CredentialStore; its TokenManager coalesces within one process but leaves cross-process coordination to the host. Eggpass's CLI is the behavior example. None of this is an accepted product/account-policy decision.

## 5. Target architecture

After ADR-0012 is accepted, the account-owned auth service uses eggpass-auth protocol types with an EggPool-owned async Eggfetch client. A host adapter provides one durable credential bundle per approved account, with atomic save/clear and the cross-process coordination specified by the ADR. Persisted references and bindings are non-secret. Refresh-token rotation updates the credential set before the new token is handed to inference.

A generation-owned account manager resolves a fresh token before synchronous attempt preparation. The prepared request owns everything needed before the existing submission await. Provider transport remains neutral HTTP. No token lookup occurs in provider-profile metadata. Login/logout uses an explicit publication path and does not make an in-flight request fall back to another account.

The headless CLI flow presents a validated device verification URL and user code, then polls under provider expiry, cancellation, and interval rules. The desktop flow binds only 127.0.0.1 on an ephemeral port, uses library-generated PKCE/state/nonce, validates the callback, and closes the listener on every terminal path. Device code is the path for remote/headless hosts; loopback is never exposed on EggPool's service listener.

## 6. Dependency graph

- M001 — OAuth credential custody and dispatch integration: hard on acceptance of ADR-0012, including provider/account eligibility, storage/backup, coordination, and 401/replay semantics. ADR-0012 is proposed; M001 is blocked.
- M002 — headless device-code login: hard on M001 closed and an approved provider profile with device-code support.
- M003 — desktop loopback browser login: hard on M001 closed and an approved provider profile with authorization-code/PKCE support. It may proceed in parallel with M002 only after the credential-manager interface is stable.
- No operational exception bypasses the policy gate. If maintainers decline OAuth, retain docs/providers.md and close this roadmap without runtime changes.

## 7. Milestones

### Milestone 1 — OAuth credential custody and dispatch integration

Class: infrastructure

Objective: Persist and refresh the token set for one explicitly approved OAuth account class, and resolve a valid account-scoped token before request preparation.

Dependencies: Hard on accepted ADR-0012. The ADR must name the initial account/owner scope, allowed provider(s), durable secret store and backup contract, refresh coordination, and post-401 behavior.

Deliverable boundary: EggPool-owned CredentialStore adapter, account-scoped TokenManager integration, token resolution before PreparedUpstreamAttempt, atomic rotation/removal, and tests. No login command in this milestone.

User or operator value: Infrastructure only; this is not a completed OAuth login or inference capability.

Exit conditions: An approved token set survives restart; proactive refresh updates access and refresh tokens atomically; concurrent requests do not cause a refresh stampede; failed/missing tokens never fall back to another account; SQLite and diagnostics contain no token values; dispatch obtains the token before the existing submit boundary; default and no-default suites pass.

Deferred work: Headless and desktop authorization entry points (M002/M003).

### Milestone 2 — Headless device-code login

Class: capability

Objective: Let an authorized CLI operator/user complete an approved device-code grant without a local browser or loopback listener.

Dependencies: Hard on M001 closed and a provider flow explicitly allowed by ADR-0012.

Deliverable boundary: CLI command within the approved account-ownership surface, device-code presentation/poll/cancel behavior, atomic save only after approval, logout/re-auth behavior, local OAuth-stub tests, and operator documentation.

User or operator value: A headless server can be authorized while the user completes verification on another device.

Exit conditions: The headless path succeeds without browser availability; only the verification URL and user code are printed; expiry, slow_down, denial, cancellation, and untrusted URLs are handled; failed re-login preserves the previous credential; the token is usable only for the approved account/owner; no shared consumer-subscription use is introduced.

Deferred work: Desktop callback listener (M003) and unapproved profiles.

### Milestone 3 — Desktop loopback browser login

Class: capability

Objective: Let an authorized desktop CLI complete an approved authorization-code grant with PKCE and a local callback.

Dependencies: Hard on M001 closed and a provider flow explicitly allowed by ADR-0012. Interface dependency on M001's stable credential manager.

Deliverable boundary: Ephemeral loopback listener, browser launch/manual URL presentation, callback validation, one-time code exchange, atomic credential save, tests, and operator documentation.

User or operator value: A user can sign in through the provider's browser UI without opening EggPool's API listener to the Internet.

Exit conditions: The listener binds only 127.0.0.1:0, independently of the API port; PKCE/state validation is enforced; hostile or mismatched callbacks do not complete login; timeout/cancel/error closes the listener; callback paste is offered only when the provider contract permits and still validates state; device flow is recommended when the browser is on a different machine.

Deferred work: Browser-session/web-form authorization, arbitrary public callbacks, and providers not approved by ADR-0012.

## 8. Cross-cutting requirements

Storage/migration: Token bytes stay out of SQLite. Add only secret-free references/bindings if the accepted design needs them; append a migration after the current v68 chain and never edit historical migrations. Define secure permissions, atomic rotation, backup/restore, and removal before release.

Protocol/compatibility: Preserve static API-key accounts and existing CLI behavior. Do not imply a new provider in the bundled profile asset solely because the auth crate has a profile.

Security/auth: Policy and owner binding precede eligibility. Redact every credential-bearing type. Validate device URLs and callbacks. Do not expose codes, tokens, or raw callback details in logs.

Concurrency/cancellation/recovery: Account-scoped single-flight refresh; storage-level cross-process coordination if needed; preserve the last valid credential on failed re-login; cancellation and shutdown cannot leave a partial write. Do not use fixed sleeps to assert race state.

Observability: Status exposes only provider/account labels, expiry/freshness, and bounded error codes; no token, code, raw callback, or response body.

Performance: No new transport stack, global blocking runtime, second SQLite writer, or synchronous OAuth network call on the Tokio current-thread request path.

Docs/ops: Keep docs/providers.md exclusion unchanged until policy and end-to-end capability are implemented and closed. Update operator/provider/security documentation only in the implementing milestone.

## 9. Verification strategy

Add a local OAuth stub suite; no live provider account or subscription is required for correctness tests. Verify grant success/denial/expiry/cancellation, PKCE/state and trusted-host checks, atomic storage/rotation, restart, concurrent refresh, account isolation, generation publication, and secret absence. Run focused OAuth and coordinator/account tests, strict lint, full serial workspace tests, no-default check/clippy, cargo deny, feature/duplicate review, and a locked release build. Compare cargo deny with the known baseline; introduce no new exception or advisory.

## 10. Risks and decision points

The largest blocker is product policy: current docs explicitly exclude OAuth consumer integrations, and no approval for pooled consumer subscriptions exists. This roadmap is proposed; it does not change that exclusion. ADR-0010's static personal API-key contract cannot be silently stretched to rotating OAuth credentials.

Other risks: provider client registration may restrict redirect ports/device grants; an OAuth library profile may change with a release; a process-local manager lock may not coordinate CLI and server writes; refresh failure can otherwise become account-wide backoff; and a 401 retry can replay a request beyond EggPool's current attempt contract.

## 11. Completion definition

The accepted policy is explicit and the current exclusion is updated only as authorized. M001 is closed with secret-free persistence and dispatch evidence. M002 and M003 are closed for every flow required by the accepted policy, with local-stub end-to-end tests and documented headless/desktop behavior. Ownership/isolation, restart, concurrency, cancellation, and dependency boundaries pass. If policy is declined, supersede/close this roadmap with no runtime changes and retain the OAuth exclusion.

## 12. Milestone status

| Milestone | Status | Implementation plan | Closure record | Blockers |
|---|---|---|---|---|
| M001 | blocked | [M001](../implementation/provider-oauth/001-oauth-credential-lifecycle-and-dispatch.md) | — | Accepted ADR-0012 must authorize account class, provider scope, storage, refresh coordination, and 401 semantics |
| M002 | blocked | [M002](../implementation/provider-oauth/002-headless-device-code-login.md) | — | M001 closed; approved device-code provider/profile |
| M003 | blocked | [M003](../implementation/provider-oauth/003-desktop-loopback-browser-login.md) | — | M001 closed; approved authorization-code/PKCE provider/profile |

# ADR-0012: Provider OAuth account custody and token lifecycle

Status: proposed

Date: 2026-11-22

Decision owners: project maintainers

Related specification sections:

- plans/000-long-term-specification.md#1-product-definition
- plans/000-long-term-specification.md#2-end-state-invariants-normative
- plans/001-terminology-and-domain-model.md#identity-and-lifecycle-terms

Affected subsystem roadmaps:

- plans/subsystems/provider-oauth-roadmap.md

## Context

EggPool's provider catalog currently excludes OAuth-only consumer integrations and browser/device-code flows. Account configuration is static-key-oriented; provider profiles are secret-free, the account row carries an environment-variable name, and resolved credentials stay outside routing identity. Accepted ADR-0010 covers a granted personal upstream API-key path, but it does not define OAuth refresh-token custody, rotating-token persistence, consumer-subscription eligibility, or refresh coordination.

The candidate protocol library is eggpass-auth 0.1.1, the latest release checked on 2026-11-22. It provides asynchronous OAuth primitives, xAI/OpenAI profiles, device authorization, authorization-code/PKCE, refresh, and a caller-owned CredentialStore/TokenManager. It does not create a Tokio runtime, launch a browser, own a loopback callback listener, or provide EggPool persistence. Its lock coalesces calls only within one process. Eggpass 0.1.3 is the latest CLI crate release checked on 2026-11-22 and is the behavior example, not a dependency: its host code demonstrates headless device-code presentation, ephemeral loopback callbacks, and owner-only atomic local storage. Reference source is pinned to [eggstack/eggpass commit 664207cd898432024f43dff4910ac3fd06c54b6e](https://github.com/eggstack/eggpass/tree/664207cd898432024f43dff4910ac3fd06c54b6e). Relevant files: [CLI login](https://github.com/eggstack/eggpass/blob/664207cd898432024f43dff4910ac3fd06c54b6e/src/cli/commands/login.rs), [flow host](https://github.com/eggstack/eggpass/blob/664207cd898432024f43dff4910ac3fd06c54b6e/src/auth/flow.rs), and [library token manager](https://github.com/eggstack/eggpass/blob/664207cd898432024f43dff4910ac3fd06c54b6e/crates/eggpass-auth/src/manager.rs). Versions: [eggpass 0.1.3](https://crates.io/crates/eggpass/0.1.3) and [eggpass-auth 0.1.1](https://crates.io/crates/eggpass-auth/0.1.1).

EggPool's provider transport remains Eggfetch 0.4.0 with only native-http1 and tls-rustls. The candidate library uses the same Eggfetch generation/profile; this is compatibility evidence, not permission to widen transport features or move auth policy into transport.

## Decision drivers

- Preserve the current exclusion until maintainers explicitly authorize an OAuth account class and provider scope.
- Do not assume consumer-subscription accounts may enter the shared pool.
- Keep credentials out of SQLite, provider-profile metadata, logs, and diagnostics.
- Refresh before expiry where possible and persist refresh-token rotation atomically.
- Keep the token bound to the exact approved provider account and principal/custody class.
- Preserve generation leases, owned attempt preparation, and no cross-account fallback after submission.
- Reuse EggPool's Tokio/Eggfetch runtime instead of copying Eggpass's synchronous bridge.

## Considered options

### Option A — Keep OAuth excluded

Retain current provider documentation and static-key account behavior. This has the smallest security and compatibility surface, but provides no OAuth login capability.

### Option B — Permit explicitly granted, owner-bound personal OAuth accounts

OAuth may be used only for an account whose owner and custody are defined by an accepted policy. It is not shared with other principals. A caller-owned atomic secret store retains the token set; SQLite remains secret-free. This reuses the personal-upstream ownership concept but requires a separate decision for token rotation, revocation, and eligibility.

### Option C — Add consumer OAuth accounts as shared pooled accounts

An operator signs in once and all eligible principals route through the resulting subscription. This changes the security and product meaning of an account. No approval for this option has been given.

## Decision

This ADR is a proposal, not an accepted authorization. Until maintainers accept the scope below, the shipped exclusion remains in force and no implementation plan is ready for handoff.

Proposed direction: if OAuth is approved, start with the narrow account-ownership model selected explicitly by maintainers; do not make consumer-subscription OAuth credentials shared or pool-eligible by default. Only provider profiles and grants expressly approved in the accepted decision may be implemented. If no provider/account class is approved, retain Option A and close this proposed work without runtime changes.

The proposed dependency is eggpass-auth = 0.1.1, exact-pinned after confirming latest release, license/advisory state, API, MSRV, and resolved graph. Eggpass 0.1.3 at the source revision above is a behavior example only. EggPool supplies the Eggfetch client and owns the runtime, CLI, callback listener, credential store, account binding, refresh lifecycle, and inference integration.

An accepted decision must settle before implementation:

1. Whether OAuth is allowed and whether it is operator-owned, owner-bound personal, or another explicitly defined class. Shared consumer-subscription pooling stays denied unless separately and explicitly approved.
2. Exact provider profiles and grant types. A bundled library profile is not itself provider approval.
3. Account-scoped durable credential store, atomic rotation/removal, backup/restore treatment, and any secret-free account reference.
4. Whether refresh coordination must cross processes and how CLI login, the running server, reload, cancellation, and concurrent requests converge on one credential version.
5. Whether HTTP 401 may trigger a bounded same-account refresh/replay. Default proposal: proactive refresh before dispatch; no cross-account retry and no post-submission replay until a body/replay contract is accepted.
6. How the existing ADR-0010 account grant/binding applies, if at all. ADR-0010 does not itself authorize OAuth or alter the current provider exclusion.

For an approved desktop authorization-code flow, the host binds only 127.0.0.1 on an OS-assigned ephemeral port, independently of EggPool's API listener; validates callback origin/path and state; and closes the listener on every terminal path. PKCE/state/nonce and provider parameters use the library. For headless environments, a supported device-code flow presents only the verification URL and user code, honors polling/expiry/cancellation, and needs no browser on the server. Verification URLs are checked against the approved provider host set before display or open.

## Consequences

### Positive

- The current exclusion and no-pooling boundary remain explicit while policy is unresolved.
- EggPool can reuse protocol primitives without adopting Eggpass's proxy, sync bridge, CLI, or file store.
- Device and browser flows have distinct, testable host responsibilities.

### Negative

- OAuth cannot be handed off until maintainers accept custody and eligibility policy.
- Durable refresh-token storage and coordination require more than resolving one static environment-variable key.
- The library's process-local lock is insufficient if CLI and server can update the same account concurrently.

### Neutral or deferred

- Whether xAI or OpenAI, both candidates in the library, are approved for EggPool.
- Provider-specific client registration, terms, quota semantics, and subscription usage.
- Any shared consumer-subscription OAuth pool.

## Compatibility and migration

No config, database, profile, CLI, or runtime change is authorized by this proposal. If accepted, preserve static api_key/api_key_env accounts and append only additive, secret-free schema changes after the current migration chain. Never store access, refresh, or ID tokens in SQLite or edit historical migrations. Any new credential format needs explicit backup, restore, permission, rotation, and removal behavior before the first write.

## Security and reliability implications

Authorization codes, device codes, state, nonces, PKCE verifiers, access tokens, refresh tokens, and ID tokens must not enter logs, diagnostics, audit payloads, SQLite, or provider profiles. Debug output must redact credential-bearing types. Callback parsing must reject mismatched state, origin, path, duplicate security parameters, userinfo, fragments, and untrusted hosts. Device verification URLs must be validated before use. Refresh-token rotation must commit atomically; failure must not silently use another account or leave a stale generation using a revoked credential.

## Verification

Before acceptance, maintainers must record the provider/account policy and custody choice. An implementation must use local OAuth stubs to prove success, denial, expiry, cancellation, untrusted URLs, state mismatch, refresh-token rotation, atomic persistence, restart, and concurrent refresh. It must prove credentials are absent from SQLite, logs, and diagnostics; the wrong principal cannot select a personal account; no token enters provider metadata; and the Eggfetch feature tree remains minimal. Any 401 retry needs explicit same-account/body-replay evidence. Default and no-default Rust suites, strict Clippy, dependency review, and a locked release build are required.

## Supersession

None.

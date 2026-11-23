# ADR-0012: Provider OAuth account custody and lifecycle

Status: accepted

Date: 2026-11-22

Decision owners: project maintainers

Related specification sections:

- plans/000-long-term-specification.md#1-product-definition
- plans/000-long-term-specification.md#2-end-state-invariants-normative
- plans/001-terminology-and-domain-model.md#identity-and-lifecycle-terms

Affected subsystem roadmaps:

- plans/subsystems/provider-oauth-roadmap.md

## Context

EggPool has static-key provider accounts and an accepted personal-upstream API-key path (ADR-0010), but no OAuth credential lifecycle. The accepted on-prem direction is per-managed-user custody: a user-owned OAuth account can contribute models to that user alongside administrator-shared models. This is a private/on-prem product-policy decision. This ADR does not make or claim an independent legal or regulatory-compliance determination.

The candidate protocol crate is eggpass-auth 0.1.1, the latest release checked on 2026-11-22. Eggpass 0.1.3 is a reference implementation only, not a runtime dependency. Eggpass demonstrates host-side device-code presentation, loopback callback handling, and owner-only atomic file techniques; EggPool retains ownership of its runtime, account binding, storage decision, and inference dispatch. References: eggpass-auth 0.1.1 (https://crates.io/crates/eggpass-auth/0.1.1), Eggpass 0.1.3 (https://crates.io/crates/eggpass/0.1.3), source at https://github.com/eggstack/eggpass/tree/664207cd898432024f43dff4910ac3fd06c54b6e.

The OAuth-only branch baseline is 5b212d2d69137b99ed39f0dbeab10331c839e54e and pins eggfetch-core 0.2.2. A separate main checkout has unrelated, uncommitted 0.4.0 provider-transport work; it is not part of this decision or branch. Implementation must resolve a compatible dependency graph without importing that work, while preserving the minimal native-http1 plus tls-rustls feature pair.

## Decision drivers

- Bind each OAuth credential to exactly one managed EggPool user; never share or pool consumer-subscription OAuth credentials.
- Permit only reviewed xAI and OpenAI OAuth profiles and the two approved login surfaces.
- Keep token material out of SQLite, profiles, logs, diagnostics, and audit projections.
- Refresh proactively and coordinate refresh across processes before a token is used.
- Preserve generation ownership and the synchronous, owned attempt-preparation boundary.
- Fail closed on disabled OAuth, account failure, or ambiguous ownership; never use another account as fallback.
- Keep provider transport neutral and do not replay a submitted request after HTTP 401.

## Considered options

### Option A — Keep OAuth excluded

Retain the static-key-only account surface. This has the smallest change surface but does not provide the approved per-user OAuth capability.

### Option B — Add individual user-owned OAuth accounts

Store one credential set for one managed user, use it only for that user, and compose that user’s OAuth-backed models with the user’s existing administrator-shared model set. Use provider-specific authenticated Web UI flows and desktop loopback PKCE for both providers. This adds token-storage and lifecycle work while preserving principal isolation.

### Option C — Add shared consumer-subscription OAuth accounts

Authenticate once and pool the subscription across users. This violates the approved ownership boundary and is rejected. Neither a provider profile nor a successful login implies permission to share a credential.

## Decision

Accept Option B with the following binding policy and lifecycle contract:

1. **Account and model ownership.** Each OAuth credential belongs to exactly one managed user and one provider account. Only that user may route through it. That user’s OAuth-backed models may be composed alongside administrator-shared models; this model composition does not share credentials or pool consumer subscriptions. No credential is reusable by another user or shared account.
2. **Provider and flow scope.** The flow matrix is: xAI Web UI uses authenticated device-code; OpenAI Web UI uses authenticated authorization-code/PKCE through fixed `GET /oauth/openai/callback` on EggPool’s existing listener; desktop CLI uses authorization-code/PKCE with a loopback callback for both providers. The OpenAI Web UI redirect URI uses a separately configured canonical HTTPS callback origin and the fixed path; it never trusts request Host/Forwarded headers or the server listen/profile-advertisement settings. OpenAI Web UI device-code and xAI Web UI PKCE are not approved. Provider registration/support for each exact flow remains an implementation-time verification requirement, not evidence supplied by this decision. No arbitrary issuer, provider, endpoint, or callback is approved by this ADR.
3. **Operator control.** OAuth enablement defaults to enabled. Disabling it blocks new OAuth login, OAuth account routing, and OAuth refresh, while retaining stored credentials for possible re-enablement. Re-enabling does not bypass normal credential validation or owner binding.
4. **Refresh and dispatch.** Refresh is proactive before expiry and coordinated across processes for each account/token version. Refresh-token rotation is durably committed atomically before the new access token is used. HTTP 401 never triggers automatic refresh-and-replay. A selected OAuth account failure never falls back to a different provider account.
5. **Credential custody.** Tokens remain outside SQLite, provider profiles, logs, diagnostics, and audit records. Implementation may use EggPool’s existing private per-user secret-file approach or Eggpass’s owner-only atomic-file techniques; it must choose one concrete host-owned design before the first write. Supported backup/restore must account for these secret files, preserve owner binding and owner-only permissions, and restore atomically. Missing or invalid restored material requires explicit re-authentication; it is never replaced from another user’s account.
6. **Runtime ownership.** EggPool owns the async host, account binding, operator gate, persistence, cross-process coordination, token resolution, and errors. eggpass-auth 0.1.1 is the exact runtime protocol candidate; Eggpass 0.1.3 is reference only. The dependency and feature graph must be checked against the branch baseline. Do not import or prescribe the unrelated Eggfetch 0.4.0 transport work.

## Consequences

### Positive

- The user-owned account boundary is explicit and compatible with the existing personal-upstream model.
- The authenticated Web UI device-code flow, fixed existing-listener OpenAI callback, and desktop loopback callbacks have distinct host surfaces and bounded responsibilities.
- The operator can disable OAuth without deleting credentials or weakening static-key behavior.
- Refresh-token rotation and cross-process races are treated as durable lifecycle concerns rather than inference retries.

### Negative

- The host must own protected per-user files, backup/restore, and cross-process coordination in addition to using protocol primitives.
- A provider/account with a failed or revoked credential remains unavailable until that same account is repaired; no other account masks the failure.
- The 0.2.2 baseline may constrain compatibility with eggpass-auth 0.1.1; implementation must resolve this without unrelated transport changes.

### Neutral or deferred

- Provider-specific registration, quota, usage terms, and service availability remain governed by reviewed xAI/OpenAI profiles. No legal compliance conclusion is made here.
- OAuth providers or grants beyond xAI and OpenAI, and flows beyond the two named surfaces, require a new decision.

## Compatibility and migration

Existing static api_key/api_key_env accounts and administrator-shared accounts remain unchanged. OAuth tokens never enter SQLite or a historical migration. Add only secret-free owner/account references if required, using a migration after the current v68 chain; do not edit historical migrations. Backup and restore must cover the chosen owner-only secret-file format and preserve its account binding. A missing or rejected file produces an explicit re-authentication state rather than fallback.

## Security and reliability implications

Device codes, authorization codes, PKCE verifiers, state, nonce, access tokens, refresh tokens, ID tokens, and sensitive callback data are secret material. They must not appear in logs, diagnostics, audit rows, provider-profile data, SQLite, or Debug output. Web UI mutations use the authenticated user and existing CSRF/transport checks; the OpenAI callback GET requires the same live session and one-time state/nonce on the existing listener, with its redirect origin derived only from the separate trusted HTTPS setting. The desktop listener binds only to 127.0.0.1 on an ephemeral port, validates the exact callback transaction, and closes on every terminal path. Refresh coordination must prevent two processes from using the same rotating refresh token or overwriting a newer token set. Disabling OAuth gates new login, routing, and refresh without deleting files. No 401 replay or cross-account fallback is permitted.

## Verification

Implementation evidence must cover the approved provider/flow matrix: xAI Web UI device-code, OpenAI Web UI authorization-code/PKCE on the existing listener, and xAI/OpenAI desktop loopback PKCE. Include provider-registration evidence for each exact flow, authenticated-user/owner isolation, enable/disable and retained-credential behavior, device expiry/denial/cancellation and URL validation, PKCE/state/callback rejection and listener cleanup, proactive refresh, cross-process contention, token rotation, restart, atomic backup/restore, and secret absence from SQLite, profiles, logs, and diagnostics. Run default and no-default Rust checks/tests, strict Clippy, dependency audit, feature-tree and duplicate review, and a locked release build. These are future acceptance requirements, not evidence that implementation or provider support has occurred.

## Supersession

None.

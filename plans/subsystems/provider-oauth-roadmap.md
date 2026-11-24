# Provider OAuth Roadmap

Status: active

Long-term references:

- plans/000-long-term-specification.md#1-product-definition
- plans/000-long-term-specification.md#2-end-state-invariants-normative
- plans/000-long-term-specification.md#3-ownership-boundaries-normative
- plans/001-terminology-and-domain-model.md#identity-and-lifecycle-terms
- plans/002-long-term-roadmap.md#phase-5--optional-on-prem-small-team-access-and-budgeting

Related ADRs:

- plans/adrs/ADR-0012-provider-oauth-account-custody-and-lifecycle.md (accepted ownership/flow policy)
- plans/adrs/ADR-0013-provider-oauth-eggfetch-compatibility.md (accepted dependency decision)
- plans/adrs/ADR-0010-personal-upstream-account-custody.md (accepted context for owner-bound personal accounts)

## 1. Purpose and ownership boundary

This subsystem adds per-user OAuth account authentication and token lifecycle for the approved private/on-prem use. Each managed user owns a distinct credential. That user’s OAuth-backed models may be composed alongside administrator-shared models, but credentials and consumer subscriptions are never pooled or shared. EggPool owns account binding, storage, operator enablement, refresh coordination, routing eligibility, dispatch, and user-visible errors. eggpass-auth supplies protocol primitives only.

| Concern | Owner |
|---|---|
| OAuth protocol primitives | eggpass-auth 0.1.1 runtime candidate; caller supplies Eggfetch and persistence |
| OAuth login hosts | Authenticated Web UI: xAI device-code and OpenAI authorization-code/PKCE on the existing listener; desktop CLI: loopback authorization-code/PKCE for both providers |
| Credential persistence and owner binding | EggPool private per-user secret-file boundary, using either the existing private-file approach or owner-only atomic-file techniques |
| Provider endpoint/auth metadata | eggpool-provider-profile; secret-free reviewed profiles only |
| Token resolution and refresh | EggPool account/runtime service, coordinated across processes |
| Provider selection, quota, health, attempt preparation | Existing EggPool routing/coordinator owners; provider transport remains transport-only |
| User ownership | Existing managed-user and personal-upstream boundaries, extended only as needed for OAuth |

The reference is Eggpass 0.1.3 at source revision 664207cd898432024f43dff4910ac3fd06c54b6e; it is not a dependency. eggpass-auth 0.1.1 is the exact runtime candidate. The branch baseline 5b212d2d69137b99ed39f0dbeab10331c839e54e pins eggfetch-core 0.2.2; ADR-0013, approved on 2026-11-23, narrowly moves the single resolved Eggfetch version to 0.4.0 to share the library's client type. Keep the native-http1 plus tls-rustls feature pair. Do not import source changes from the separate checkout or alter transport policy; requalify existing provider behavior.

## 2. Work classification

### Invariants

- Every OAuth credential is bound to one managed user and provider account; no sharing or consumer-subscription pooling.
- A user may compose their own OAuth-backed models with administrator-shared models, without credential reuse or fallback across accounts.
- Tokens stay outside SQLite, provider profiles, logs, diagnostics, and audit projections.
- Operator OAuth enablement defaults on. Disabling blocks new login, OAuth routing, and refresh but retains credential files.
- Refresh is proactive, account-scoped, atomic, and coordinated across processes.
- HTTP 401 never causes automatic refresh-and-replay; an account failure never selects another account as fallback.
- Request preparation remains synchronous and owned before provider submission; provider transport gains no OAuth policy.

### Capabilities

- A managed user can authorize their own xAI account in the authenticated Web UI using device-code, or OpenAI using authorization-code/PKCE through a fixed callback on EggPool’s existing listener.
- A desktop CLI user can authorize the intended managed user’s xAI or OpenAI account using loopback authorization-code/PKCE.
- Inference can use a fresh credential for only the owning user’s eligible account.

### Infrastructure

- Per-user credential custody, atomic rotation and removal, backup/restore, and cross-process coordination.
- A shared account token-resolution interface for the web and desktop login hosts and inference dispatch.
- An operator enablement gate and secret-free status projection.

### Polish

- Bounded expiry and re-authentication status that reveals no token material.

## 3. Non-goals

- Shared or pooled consumer-subscription OAuth accounts, credential sharing between users, or cross-account fallback.
- OAuth accounts for providers beyond xAI and OpenAI, arbitrary issuers, arbitrary endpoints, or unreviewed grants.
- CLI device-code login; public or separate Web UI callback listeners; xAI Web UI authorization-code/PKCE; or OpenAI Web UI device-code.
- Storing OAuth tokens in SQLite, provider-profile assets, logs, diagnostics, or audit rows.
- Copying Eggpass’s CLI, proxy, synchronous runtime bridge, or credential-file format as an unreviewed whole.
- Moving authentication, token refresh, retries, or account selection into rust/src/providers/.
- Automatic HTTP 401 retry/replay or buffering arbitrary request bodies.
- Editing docs/providers.md in this planning revision; user-facing provider claims change only with implemented and verified flows.

## 4. Current state

Repository baseline: 5b212d2d69137b99ed39f0dbeab10331c839e54e. No OAuth implementation exists. rust/src/config.rs and rust/src/accounts/registry.rs model static-key accounts; rust/src/db/repositories.rs keeps account identity credential-free. The accepted personal-upstream path binds a secret-free account reference to a managed user and stores secret material in a private config-adjacent file, not SQLite. rust/src/routing/eligibility.rs owns owner eligibility. rust/src/runtime_lifecycle/generation.rs publishes runtime account state, and rust/src/coordinator/attempt.rs prepares an owned attempt before provider I/O. Existing provider documentation excludes OAuth-only consumer integrations and login flows.

At the branch baseline, Cargo.lock resolved eggfetch-core 0.2.2; ADR-0013 approves a single-version move to 0.4.0 with the same minimal feature pair. The uncommitted source changes in the other checkout remain unrelated and must not be imported. eggpass-auth 0.1.1 is the runtime dependency and Eggpass 0.1.3 remains reference only. API/MSRV/license and graph checks have preliminary evidence; provider-transport requalification and serial tests remain open and this roadmap does not claim them complete.

## 5. Target architecture

A single account-scoped EggPool auth service owns one token set per managed user/provider account. It uses the approved private-file custody mechanism, a cross-process lock/version contract, proactive refresh, and atomic replacement of the full rotated credential set. The operator gate is enabled by default; when disabled, no new OAuth login, refresh, or OAuth routing occurs, while credential files remain intact for re-enablement. Any required database fields are non-secret owner/account references only.

The service resolves an owned access token before synchronous attempt preparation. The selected account remains the account for that attempt; auth or provider failure cannot replay on another account. HTTP 401 is handled by the existing failure path without refresh-and-replay. The provider transport and profile crate remain secret-free and policy-neutral. A user’s effective model surface may combine their OAuth-backed models with administrator-shared models.

The authenticated Web UI uses xAI device-code and presents only a validated verification URL/user code; for OpenAI it uses authorization-code/PKCE with fixed `GET /oauth/openai/callback` on EggPool’s existing listener. The OpenAI redirect URI is constructed only from a separately configured canonical public HTTPS origin and the fixed callback path, never request Host/Forwarded headers, `[server].host`, or `[integrations].advertise_base_url`; pending state is bound to the initiating session/user and consumed once. The desktop CLI uses authorization-code/PKCE with a loopback-only ephemeral callback for both providers. Before provider login, it receives a self-only `epk1_` EggPool credential carrying only `oauth_credentials_write`, provisioned through authenticated Web UI/CSRF and entered via hidden TTY or stdin. Its `POST /api/user/oauth-credentials` commit route derives ownership only from verified `AuthenticatedPrincipal.user_id`, preserves M001 owner checks, and delegates to M001’s atomic commit. Both surfaces reuse the same credential service; provider support/registration remains unverified until implementation-time evidence.

## 6. Dependency graph

- M001 — credential custody, enablement, proactive refresh, and dispatch: hard on accepted ADR-0012 and ADR-0013 (both accepted). It owns the storage, account-token interface, approved Eggfetch 0.4.0 graph, and required transport requalification used by the login plans.
- M002 — authenticated Web UI provider login (xAI device-code; OpenAI authorization-code/PKCE callback on the existing listener): hard on accepted ADR-0012 and ADR-0013 (satisfied); interface dependency on M001’s stable account identity and atomic credential-commit contract. UI/protocol work may proceed against a test double; integration and closure require M001’s interface to be available.
- M003 — desktop CLI loopback PKCE for xAI and OpenAI: hard on accepted ADR-0012 and ADR-0013 (satisfied); interface dependency on M001’s account identity and credential-commit contract. The `oauth_credentials_write` scope and `POST /api/user/oauth-credentials` principal-bound route are M003 deliverables; callback work may proceed against test doubles, while integration and closure require M001’s commit interface.
- M002 and M003 may proceed in parallel against that shared interface. Neither may define a second store or ownership model.

## 7. Milestones

### Milestone 1 — OAuth credential lifecycle and dispatch

Class: infrastructure

Objective: Store and proactively refresh a user-owned OAuth credential, and resolve it for that same user’s selected provider account before dispatch.

Dependencies: Accepted ADR-0012 and ADR-0013; no remaining dependency-choice blocker. Implement the approved single-version Eggfetch 0.4.0 move and requalify transport without importing unrelated source changes.

Deliverable boundary: Owner-bound secret-file adapter, safe backup/restore, operator gate, cross-process refresh coordination, account-token service, and pre-dispatch integration. No login interface in this milestone.

User or operator value: Infrastructure only; not a completed OAuth login or end-to-end OAuth capability.

Exit conditions: Rotated credentials are atomically durable across restart; refresh coordination prevents stale-token races across processes; disabled OAuth blocks login, routing, and refresh without deleting credentials; dispatch uses only the owner’s selected account; no 401 replay or cross-account fallback; secret-free persistence and diagnostics; default/no-default verification passes.

Deferred work: Web UI provider login (M002) and desktop loopback PKCE login (M003).

### Milestone 2 — Authenticated Web UI provider login

Class: capability

Objective: Let an authenticated managed user authorize their own xAI account using Web UI device-code, or OpenAI using Web UI authorization-code/PKCE with a fixed callback on EggPool’s existing listener.

Dependencies: Accepted ADR-0012; interface dependency on M001’s stable account identity and atomic credential-commit contract. Integration and closure require M001’s service interface.

Deliverable boundary: Existing authenticated user surface and CSRF/transport gate; xAI device-code request/poll/cancel and safe verification URL/user-code presentation; OpenAI authorization-code/PKCE with fixed callback route on the existing listener, separate canonical HTTPS callback-origin configuration, one-time same-session state/nonce, and CSRF-protected commit through M001. No CLI device-code mode or separate callback listener.

User or operator value: A user can complete provider verification through the authenticated Web UI without a server-side browser or user-local callback; OpenAI returns through the fixed callback on EggPool’s existing listener.

Exit conditions: xAI device-code and OpenAI authorization-code/PKCE stubs succeed only for the initiating user; xAI verification URLs are validated; device expiry, polling interval, slow_down, denial, cancellation, and disablement fail safely; OpenAI origin/callback validation, same-session state/nonce, replay/tamper rejection, and CSRF mutations are verified. Callback/query/device secrets and tokens are not logged or persisted; unsuccessful re-login preserves the prior credential. Verify exact provider support/registration at implementation time; this plan does not establish it.

Deferred work: Desktop CLI loopback authorization-code/PKCE (M003), providers beyond xAI/OpenAI, and other Web UI grant/provider combinations.

### Milestone 3 — Desktop CLI loopback PKCE login

Class: capability

Objective: Let the desktop CLI authorize the authenticated managed user’s xAI or OpenAI account using authorization-code/PKCE and a bounded loopback callback, then commit through a dedicated self-only EggPool credential.

Dependencies: Accepted ADR-0012; interface dependency on M001’s stable account identity and atomic credential-commit contract. Integration and closure require M001’s service interface.

Deliverable boundary: Desktop CLI operation, self-only `oauth_credentials_write` `epk1_` credential provisioned through authenticated Web UI/CSRF and consumed via hidden TTY/stdin, loopback listener, browser URL, callback validation, code exchange, and `POST /api/user/oauth-credentials` commit through M001. The route requires that single-purpose scope and derives ownership only from verified `AuthenticatedPrincipal.user_id`; it accepts no caller-supplied owner. No provider callback on EggPool’s API listener and no device-code CLI mode.

User or operator value: A desktop user can complete provider browser sign-in without exposing EggPool’s API listener.

Exit conditions: Listener binds only 127.0.0.1:0 and closes on every terminal path; PKCE/state and exact callback validation are enforced; invalid callbacks never save credentials; xAI/OpenAI desktop stubs pass; the write scope is isolated and the route derives ownership only from verified principal identity; credential entry does not expose the token in argv/environment/output/logs; failed/canceled re-login preserves the prior credential. Provider loopback registration/support is an implementation-time check, not claimed here.

Deferred work: Public callbacks, arbitrary redirect hosts, and providers beyond xAI/OpenAI.

## 8. Cross-cutting requirements

Storage/migration: Tokens remain outside SQLite. Choose EggPool’s existing private per-user secret-file approach or owner-only atomic-file techniques and document the chosen format before the first write. Supported backup/restore must include secret handling, preserve user/account binding and owner-only permissions, and restore atomically; missing or invalid material requires re-authentication. Add only secret-free metadata migrations after v68; never edit history.

Protocol/compatibility: Preserve static API-key and administrator-shared accounts. Keep provider profiles secret-free and do not infer support from library profiles alone. Use the single Eggfetch 0.4.0 graph approved by ADR-0013 with the native-http1 plus tls-rustls feature pair; do not transplant unrelated source changes or alter transport policy.

Security/auth: Use the authenticated owner identity and existing user-session, CSRF, and transport gates. M003’s write token is self-only and carries only `oauth_credentials_write`; the CLI reads it via hidden TTY/stdin, and the commit route trusts only verified `AuthenticatedPrincipal.user_id`. Never expose credentials, codes, raw callbacks, or provider response bodies. Operator disablement blocks new login, refresh, and routing but preserves files. Do not assert provider support or legal compliance based on this project-policy decision.

Concurrency/cancellation/recovery: Coordinate refresh across processes, prevent stale rotated-token writes, preserve the previous credential on failed login or refresh, and make cancellation/restart boundaries explicit. Do not use fixed sleeps to prove contention state.

Observability: Only bounded provider/account labels, expiry state, and typed non-secret errors may be projected.

Performance: No second SQLite writer, new transport stack, nested runtime, or synchronous OAuth network call on the current-thread inference path.

Docs/ops: M001 documents storage, gate, and backup/restore. M002/M003 document only their implemented flows and update docs/providers.md when the user-facing capability is actually implemented. No documentation change in this planning task claims runtime support.

## 9. Verification strategy

Use local OAuth stubs for the approved matrix—xAI Web UI device-code, OpenAI Web UI authorization-code/PKCE on the existing listener, and xAI/OpenAI desktop loopback PKCE; no live account is required for correctness tests. Verify exact provider registration/support separately at implementation time, owner isolation, the M003 scope/route, model composition without credential reuse, default-enabled and disabled-gate behavior, retained credentials, atomic backup/restore, proactive refresh, cross-process contention, restart, cancellation, device polling, PKCE/callback rejection, and secret absence. Run focused OAuth, owner/UI, CLI, account/routing, and coordinator tests; default and no-default check/clippy; serial workspace tests; cargo deny; dependency feature/duplicate review; and a locked release build. Tests must synchronize on observable fixture boundaries, not fixed sleeps.

## 10. Risks and decision points

The approved ownership and flow scope is settled by ADR-0012; no milestone may reopen shared OAuth pooling or add providers. ADR-0013 settles the Eggfetch 0.4.0 dependency choice; implementation must preserve the minimal feature pair and complete transport requalification without importing other-checkout source. Cross-process refresh and protected backup/restore need concrete mechanisms. Exact provider registration/support for every approved flow remains unverified; if a provider cannot meet its contract, stop rather than substitute a flow. This roadmap makes no legal-compliance claim.

## 11. Completion definition

M001 closes with secure per-user storage, backup/restore, gate semantics, proactive cross-process refresh, and dispatch evidence. M002 closes with authenticated, owner-bound xAI Web UI device-code and OpenAI Web UI authorization-code/PKCE evidence; M003 closes with authenticated, owner-bound xAI/OpenAI desktop loopback PKCE plus scoped credential-route evidence. Secret exclusion, static-key compatibility, failure/cancellation/restart behavior, dependency boundaries, docs, provider registration evidence, and default/no-default verification all pass. No consumer-subscription OAuth credential is shared or pooled, and no implementation, provider-support, or legal-compliance evidence is claimed by this roadmap itself.

## 12. Milestone status

| Milestone | Status | Implementation plan | Closure record | Blockers |
|---|---|---|---|---|
| M001 | active | ../implementation/provider-oauth/001-oauth-credential-lifecycle-and-dispatch.md | — | None; dependency compatibility is the first implementation check in M001 |
| M002 | active | ../implementation/provider-oauth/002-authenticated-web-ui-provider-login.md | — | Interface dependency on M001 account identity/commit contract; verify exact xAI/OpenAI provider registrations before closure |
| M003 | active | ../implementation/provider-oauth/003-desktop-loopback-browser-login.md | — | M001 account identity/commit interface plus scoped `oauth_credentials_write` route; verify exact provider loopback registrations before closure |
| M004 | active | ../implementation/provider-oauth/004-oauth-401-credential-refresh-retry.md | — | ADR-0015 supersedes the no-replay clause; verify exact provider registration separately |


## 13. Append-only flow-scope amendment — ADR-0014

This amendment controls the current milestone boundaries where earlier roadmap text conflicts; the earlier text remains historical and is not rewritten.

- M002 is the authenticated xAI Web UI device-code flow only. OpenAI Web UI authorization and any OpenAI callback/configuration surface on EggPool's listener are out of scope and removed.
- M003 remains desktop loopback authorization-code/PKCE for both xAI and OpenAI. Its ephemeral callback is not an EggPool server callback.
- The M001 custody, owner-binding, operator-gate, no-pooling, no-cross-user-fallback, no-401-replay, and secret-exclusion invariants remain in force.
- Exact provider support/registration remains a separate implementation-time and closure check; local stubs do not establish live support.

See `plans/adrs/ADR-0014-provider-oauth-web-ui-and-desktop-flow-boundary.md` and the M002/M003 append-only amendments. M001 and ADR-0013 are unaffected.

## 14. Append-only credential-refresh amendment — ADR-0015

This amendment controls the current milestone boundaries and supersedes the earlier no-replay wording where it conflicts. The earlier text remains historical and is not rewritten.

- M004 is the owner-scoped credential-refresh retry. An upstream HTTP 401 on an owner-bound OAuth account refreshes that owner's credential for that account and retries the same request against the same account exactly once, using the compare-and-refresh primitive the shared Eggpass library already provides.
- The retry holds the account constant. It never reselects, never substitutes another account, provider, or user, and never crosses an owner boundary. No pooling and no cross-account fallback are introduced.
- One refresh and one retry per request, inside the ordinary attempt budget. A refresh failure, or a second rejection of the refreshed credential, terminalizes the request and records the authentication failure exactly as before, so the account is disabled and the owner must log in again.
- The M001 custody, owner-binding, operator-gate, secret-exclusion, and cross-process refresh-rotation invariants remain in force. The operator gate blocks the refresh exactly as it blocks login, routing, and proactive refresh.
- The failure classifier's recorded output is unchanged. The recovery is coordinator-owned and adds its own scope and action labels for the healed attempt, so the frozen compatibility observation fixture for the 401 classification is unaffected.
- M001's earlier "no 401 replay" wording, and the corresponding sentences in ADR-0012, ADR-0013, and ADR-0014, remain historical records. ADR-0015 supersedes that clause only.
- Exact provider support/registration remains a separate implementation-time and closure check; local stubs do not establish live support.

See `plans/adrs/ADR-0015-provider-oauth-owner-scoped-credential-refresh-retry.md` and `plans/implementation/provider-oauth/004-oauth-401-credential-refresh-retry.md`.

### 14.1 Earlier text superseded by ADR-0014 and ADR-0015

The sections below describe scope that the accepted amendments have since changed. They are retained as written and are not authoritative for current scope.

- §1, §3, §6, §7, §9, and §11 still describe an OpenAI Web UI authorization-code/PKCE callback on EggPool's listener. ADR-0014 removed that surface: M002 is xAI Web UI device-code only, and M003 owns both desktop loopback flows.
- §8, §11, and the M001, M002, and M003 plan text still state a no-replay rule for HTTP 401. ADR-0015 supersedes that clause with the owner-scoped credential-refresh retry described above. The prohibitions on pooling, cross-user fallback, and cross-account fallback are unchanged.

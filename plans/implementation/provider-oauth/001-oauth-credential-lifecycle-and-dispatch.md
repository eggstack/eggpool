# Provider OAuth Milestone 001 — Credential lifecycle and dispatch integration

Status: implemented

Repository baseline: 5b212d2d69137b99ed39f0dbeab10331c839e54e

Source roadmap:

- plans/subsystems/provider-oauth-roadmap.md#milestone-1--oauth-credential-lifecycle-and-dispatch

Long-term requirements:

- plans/000-long-term-specification.md#1-product-definition
- plans/000-long-term-specification.md#2-end-state-invariants-normative
- plans/001-terminology-and-domain-model.md#identity-and-lifecycle-terms

Applicable ADRs:

- plans/adrs/ADR-0012-provider-oauth-account-custody-and-lifecycle.md (accepted ownership/flow policy)
- plans/adrs/ADR-0013-provider-oauth-eggfetch-compatibility.md (accepted targeted dependency move)
- plans/adrs/ADR-0010-personal-upstream-account-custody.md (accepted context; its owner-binding and secret-free database boundary remain relevant)

Primary class: infrastructure

## 1. Objective

Implement EggPool-owned per-user OAuth credential custody, proactive cross-process refresh, operator enablement, and token resolution for the owning user’s selected account before the existing upstream attempt is prepared. This is infrastructure: it adds no login surface and is not a completed OAuth capability.

## 2. Why this milestone is ready

ADR-0012 is accepted and fixes the provider, owner, flow, refresh, replay, and storage boundaries. M001 is the first implementation slice. M002 and M003 are active against an interface dependency on this milestone’s stable per-user identity and atomic credential-commit contract; their end-to-end integration and closure depend on this service being available. Do not block initial interface work on unrelated transport changes.

The branch was cut at EggPool Rust 1.89 baseline 5b212d2d69137b99ed39f0dbeab10331c839e54e, which pinned eggfetch-core 0.2.2 with native-http1 plus tls-rustls. ADR-0013 now approves moving the single resolved Eggfetch version to 0.4.0 so exact eggpass-auth 0.1.1 can share the selected account client. Eggpass 0.1.3 at source revision 664207cd898432024f43dff4910ac3fd06c54b6e remains reference only. Do not import source changes from the separate dirty checkout; preserve the minimal feature pair and make only OAuth-required adapter changes. Requalify provider transport before closure; stop if additional features, a second Eggfetch version, policy changes, or unrelated transport work are required.

## 3. Current implementation evidence

- rust/src/config.rs and rust/src/accounts/registry.rs define static-key account configuration and resolve secrets separately from routing identity.
- rust/src/db/repositories.rs exposes credential-free account identity. Existing personal-upstream bindings are secret-free and owner-scoped.
- rust/src/personal_upstream.rs, rust/src/operations/config_mutation.rs, and rust/src/operations/paths.rs own the existing personal secret/config-adjacent file and publication path; rust/src/routing/eligibility.rs enforces owner eligibility.
- rust/src/runtime_lifecycle/generation.rs constructs account runtime state. rust/src/coordinator/attempt.rs prepares an owned upstream attempt synchronously before provider I/O.
- rust/src/coordinator/failure.rs classifies authentication failure; no OAuth refresh handoff exists. Existing behavior must not gain automatic 401 replay.
- rust/src/catalog/refresh.rs owns provider discovery. Any authenticated discovery must use the same user/account token service; public discovery is not credential proof.
- docs/providers.md currently excludes OAuth-only integrations. This infrastructure milestone does not itself change that user-facing claim.
- The original baseline pinned eggfetch-core 0.2.2; ADR-0013 approves the exact 0.4.0 move for eggpass-auth 0.1.1. The selected API/MSRV/license and provisional single-version graph have preliminary evidence, but implementation tests and provider-transport requalification remain required.

## 4. Invariants that must not regress

- No access, refresh, or ID token in SQLite, provider profiles, logs, diagnostics, audit records, public projections, or credential Debug output.
- Every OAuth credential is bound to exactly one managed user and provider account. No shared subscription, credential pooling, or cross-user use.
- A user’s OAuth-backed models may coexist with their administrator-shared models; this never authorizes credential reuse or cross-account fallback.
- Operator OAuth enablement defaults on. Disabled means no new login, OAuth routing, or refresh; stored files remain intact for re-enablement.
- Refresh is proactive and coordinated across processes. The full rotated set is atomically durable before a refreshed token is returned.
- Preserve static API-key and administrator-shared account behavior.
- Resolve tokens before synchronous borrowed attempt preparation; PreparedUpstreamAttempt remains fully owned before submission.
- HTTP 401 never triggers automatic refresh-and-replay. Failed refresh or dispatch never falls back to another account.
- Provider profiles remain secret-free and the Eggfetch feature pair remains native-http1 plus tls-rustls.

## 5. Scope

### In scope

- One EggPool-owned credential-store adapter for one OAuth credential set per managed user/provider account, using either the existing private per-user secret-file approach or owner-only atomic-file techniques.
- Secret-free owner/account references only, if needed; no token columns or token payload migration.
- An operator OAuth enablement control that defaults to enabled and gates login, OAuth account eligibility/routing, and refresh without deleting credentials when disabled.
- Proactive expiry-aware refresh with cross-process coordination and atomic refresh-token rotation.
- A shared per-account token service and pre-dispatch resolution for the owning user’s account, including authenticated catalog calls only if the reviewed provider requires them.
- Supported backup/restore behavior for the chosen secret files and owner binding.
- Local OAuth stubs and focused storage, refresh, ownership, routing, and dispatch tests.

### Explicitly out of scope

- Web UI device-code UX (M002) and desktop CLI loopback PKCE UX (M003).
- Shared consumer-subscription OAuth accounts, credential sharing, other providers, or other grant types.
- Automatic HTTP 401 retry/replay, cross-account fallback, provider-specific retry policy in rust/src/providers/, or public callback routes.
- Importing source changes from the separate main checkout, widening the minimal Eggfetch feature pair, or changing proxy/timeout/retry/TLS policy or updating unrelated transport dependencies. ADR-0013 permits only the single-version eggfetch-core 0.4.0 move and OAuth-required adapter changes.
- Token storage in SQLite, provider-profile metadata, logs, or diagnostics.

## 6. Required production changes

1. **Apply the accepted dependency decision.** Pin eggpass-auth =0.1.1 and the single root eggfetch-core version to =0.4.0 under ADR-0013. Keep only native-http1 plus tls-rustls, with no second Eggfetch version. Make only the OAuth-required client adapter change; do not import source changes or alter proxy, timeout, retry, TLS, or other transport policy from the separate checkout. Review cargo tree features, duplicates, advisories, and the focused provider-transport suite; stop if further transport behavior changes or feature widening is needed.
2. **Choose and implement secret custody.** Select either EggPool’s existing private per-user secret-file approach or owner-only atomic-file techniques informed by Eggpass 0.1.3. Document the actual path/format, owner-only permissions, atomic replacement/removal, corruption handling, and process-lock/version contract before the first credential write. Secret material never enters SQLite.
3. **Define safe backup and restore.** Include the OAuth secret files in the supported secret-file backup/restore contract. Restore atomically with owner-only permissions and the same managed-user/account binding. Missing, invalid, or mismatched material produces explicit re-authentication; it never substitutes another user’s credential. Update docs/backup-restore.md and relevant operator guidance.
4. **Implement operator enablement and owner metadata.** Default OAuth enablement to on. When disabled, reject new OAuth login, exclude OAuth accounts from routing, and do not start refresh; retain secret files. If secret-free metadata is needed, append a migration after v68 and preserve immutable history. Use the established owner-binding path and do not add a parallel identity model.
5. **Implement proactive cross-process refresh.** Use one account/issuer-scoped manager. Coordinate refresh across processes with the chosen file/lock/version mechanism so only the current token version can refresh and publish. Persist the complete rotated credential set atomically before returning the new access token. Cancellation or failed refresh leaves the last committed file intact.
6. **Resolve before dispatch.** Obtain an owned token before synchronous request preparation and carry only owned values into PreparedUpstreamAttempt. Keep auth awaits out of borrowed preparation and transport. Enforce the owner binding and operator gate before routing/use. Do not switch to a shared or other user account when OAuth is unavailable.
7. **Keep failure semantics explicit.** Use proactive refresh only. A later HTTP 401 follows the existing failure path; it never triggers refresh-and-replay, even for the same account. No post-failure account fallback or reselection is added.
8. **Preserve model composition.** The owning user may see/use their OAuth-backed models together with administrator-shared models. Keep each account’s health/quota/credential identity separate; model composition is not pooling. If catalog auth is required, it uses this same owner/account service.

## 7. Ordered work packages

### Work package A — Resolve dependency and storage contract

Intent: Establish an implementable credential boundary using the dependency decision approved by ADR-0013.

Required changes: Pin one eggfetch-core 0.4.0 version with the exact native-http1 plus tls-rustls feature pair alongside eggpass-auth 0.1.1; limit adapter changes to OAuth needs; select the allowed private-file mechanism; define per-user/account identity, cross-process lock/version behavior, and backup/restore semantics.

Acceptance evidence: Cargo tree shows one Eggfetch version and the approved feature pair; no new duplicate or advisory exception; focused provider-transport qualification covers the version move; storage contract names permission, atomic commit, ownership, backup, and restore outcomes. Report unresolved baseline advisories separately without suppressing them.

### Work package B — Implement per-user custody and operator gate

Intent: Store a complete credential set outside persistence and make enablement fail closed.

Required changes: Implement load/save/clear, atomic replacement, owner binding, secret-free metadata only if required, default-enabled operator control, disabled-state behavior, and restart/backup/restore.

Acceptance evidence: Tests prove owner-only file behavior, old credential survives failed replacement, rotated credentials commit together, restart and restore preserve owner binding, corrupted/missing restore requires re-authentication, disabled OAuth blocks login/routing/refresh but retains bytes, and database/audit projections contain no token.

### Work package C — Add proactive, coordinated token refresh

Intent: Return a valid token for the owning account before it expires, without refresh-token races.

Required changes: Add expiry-aware refresh, per-account single-flight, cross-process coordination/version checks, typed failures, cancellation behavior, and atomic publication of rotated tokens.

Acceptance evidence: Concurrent same-account processes cause one effective refresh per token version; independent users refresh independently; stale writers cannot replace newer tokens; restart observes one complete token set; refresh failure creates no provider attempt and no account switch.

### Work package D — Integrate token resolution and verify dispatch

Intent: Feed an owned access token into the existing account dispatch boundary.

Required changes: Resolve before synchronous attempt preparation and before authenticated catalog calls only where required; enforce owner and operator gates; preserve separate account health/quota identity and existing static-key behavior.

Acceptance evidence: A local provider stub sees only the selected owner’s token; other users cannot route through it; disabled or failed credentials make no OAuth provider attempt; HTTP 401 has no replay and no fallback; static-key fixtures remain unchanged; request preparation remains owned before submission.

## 8. Failure, cancellation, restart, contention semantics

A disabled operator gate blocks new login, refresh, and OAuth routing but does not delete or invalidate stored bytes. Re-enablement reuses only a validated credential for its bound owner. A refresh failure before dispatch is an account-scoped failure and produces no provider attempt or alternate-account selection. A successful refresh is not returned until the complete rotated set is durably replaced. Before commit, cancellation leaves the prior set authoritative; after commit, the new set is authoritative. Cross-process coordination prevents replay of a rotating refresh token and stale overwrite. Restart loads only a complete, validated owner/account-matched set. HTTP 401 follows existing failure handling without refresh, replay, or cross-account fallback.

## 9. Compatibility and migration

Existing api_key/api_key_env configuration and static-key behavior remain unchanged. OAuth token bytes never enter migrations. Any required owner/account reference is secret-free and appended after v68; no historical migration changes. Backup/restore explicitly handles the protected per-user secret files, permissions, and owner binding. If a backup lacks valid OAuth material, status reports re-authentication; no account is substituted. Provider documentation is not changed until the user-facing login milestones are implemented and verified.

## 10. Required tests

- Credential-store load/save/clear, owner-only permissions, atomic replacement, corruption, restart, failed-save rollback, and backup/restore.
- Secret redaction in Debug, typed errors, audit/status projection, database dump, logs, and diagnostics.
- User/provider/account mismatch rejection and cross-user routing denial; per-user OAuth models compose with admin-shared models without credential sharing.
- Operator default-enabled behavior; disable blocks login, routing, and refresh but retains files; re-enable uses only the bound credential.
- Local stub success, expiry, denial, transport failure, cancellation, refresh-token rotation/omission, and proactive refresh before dispatch.
- Observable-boundary same-account cross-process contention, independent-account refresh, stale-writer rejection, and restart recovery.
- Generation publication/retirement and async token resolution before attempt preparation; no auth await crosses a borrowed boundary.
- Generic HTTP 401 produces no retry/replay and no alternate-account selection; existing static-key path remains unchanged.
- Authenticated catalog use only if approved and required; public discovery never proves credential validity.
- No-default build/test coverage and dependency feature/duplicate/audit checks.

## 11. Required verification commands

    cargo fmt --manifest-path rust/Cargo.toml --all -- --check
    cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
    cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
    cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
    cargo test --manifest-path rust/Cargo.toml --test provider_oauth -- --test-threads=1
    cargo test --manifest-path rust/Cargo.toml --test on_prem_identity -- --test-threads=1
    cargo test --manifest-path rust/Cargo.toml --test routing_domain -- --test-threads=1
    cargo test --manifest-path rust/Cargo.toml --test coordinator_boundaries -- --test-threads=1
    cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
    cargo test --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- --test-threads=1
    cargo tree --manifest-path rust/Cargo.toml -e features -i eggfetch-core
    cargo tree --manifest-path rust/Cargo.toml --duplicates
    cargo deny --manifest-path rust/Cargo.toml check
    cargo build --manifest-path rust/Cargo.toml --locked --release
    git diff --check

The test target provider_oauth is a required implementation target and may be added during M001. Run serial Rust tests. Compare dependency output to ADR-0013's single-version 0.4.0 contract; do not widen the feature pair, add an advisory exception, or modify unrelated transport dependencies to resolve separate findings.

## 12. Documentation updates

- docs/backup-restore.md: supported OAuth secret-file backup/restore, owner binding, and re-authentication when material is absent or invalid.
- Operator/configuration guidance: default-enabled gate and disabled-state retention/routing/refresh behavior.
- architecture/deep-dive-providers.md and the relevant account/runtime owner docs: token-service ownership, cross-process refresh, and unchanged transport/attempt boundaries.
- Do not update docs/providers.md to claim login support in this infrastructure milestone.

## 13. Acceptance criteria

- One complete OAuth credential set is stored outside SQLite for exactly one managed user and provider account.
- Proactive refresh is coordinated across processes; a rotated set is atomic and survives restart and supported restore.
- The default-enabled operator gate, disabled retention, and re-enable semantics are enforced for login, routing, and refresh.
- The owning user’s OAuth models may coexist with admin-shared models, but no credential sharing, pooling, cross-account fallback, HTTP 401 replay, or account reselection occurs.
- Tokens resolve before synchronous owned attempt preparation; static API-key behavior remains unchanged; the minimal Eggfetch feature pair is preserved.
- Focused, default, no-default, dependency, and secret-exclusion evidence passes. This milestone remains infrastructure, not a completed login capability.

## 14. Stop conditions

Stop and report if the ADR-0013 single-version Eggfetch 0.4.0 graph cannot be built without a second Eggfetch version or feature widening; if OAuth adaptation requires unrelated proxy/timeout/retry/TLS policy changes; if the owner-only file/backup/restore and cross-process refresh contract cannot be made atomic; if OAuth disablement cannot block routing and refresh while retaining credentials; if any token must enter SQLite, profile data, logs, or diagnostics; if ownership is ambiguous; or if the change requires 401 replay or cross-account fallback. Do not copy source changes from the separate checkout or silently alter transport behavior.

## 15. Closure evidence required

The later closure record must include: exact accepted scope and dependency graph; chosen file format, permissions, atomicity, backup/restore and owner-binding evidence; local stub results for refresh/rotation, cross-process contention, cancellation, restart, and dispatch; proof disabled OAuth retains bytes but blocks use; proof of no HTTP 401 replay or cross-account fallback; database/log/profile/diagnostic secret-absence assertions; focused/default/no-default test and strict Clippy results; cargo deny, feature/duplicate tree, locked release results; documentation updates; and registry audit confirming M002/M003 integrate only through the closed service interface. No closure record is created by this planning revision.

## 16. Handoff notes

Read accepted ADR-0012, ADR-0013, and ADR-0010 plus current config, personal-upstream, account, generation, eligibility, and attempt owners. eggpass-auth 0.1.1 is the exact runtime candidate; Eggpass 0.1.3 is reference only. The user approved the single Eggfetch 0.4.0 dependency move under ADR-0013; do not copy unrelated source changes from the separate checkout. Preserve native-http1 plus tls-rustls and requalify provider transport. Rust tests run serial. This plan records no completed milestone evidence.

## 17. Append-only credential-refresh amendment — ADR-0015

ADR-0015 is accepted and supersedes this plan's no-401-replay clause only. The earlier wording remains historical and is not rewritten: §4's "HTTP 401 never triggers automatic refresh-and-replay" invariant, §6.7's failure-semantics rule, §8's closing sentence, §13's acceptance bullet, and §15's "proof of no HTTP 401 replay" evidence requirement.

The current rule is M004's owner-scoped credential-refresh retry: an upstream HTTP 401 on an owner-bound OAuth account refreshes that owner's credential for that same account and retries the same request against the same account exactly once. The owner, provider, and account stay constant; the retry never reselects and never falls back to another account or user. A refresh failure, a denied grant, or a second rejection of the refreshed credential terminalizes the request and disables the account under the existing authentication-failure semantics, and the operator gate blocks the refresh exactly as it blocks login and proactive refresh.

Everything else above is unchanged: proactive refresh, atomic rotation, cross-process coordination, owner binding, secret exclusion, no pooling, and no cross-account fallback. M001's deliverable boundary is unchanged too — this milestone adds no login surface and makes no provider-support or provider-registration claim. Its closure record evaluates §15's replay requirement in the superseded sense: no cross-account, cross-user, or cross-owner replay or fallback exists. See `plans/adrs/ADR-0015-provider-oauth-owner-scoped-credential-refresh-retry.md` and `plans/implementation/provider-oauth/004-oauth-401-credential-refresh-retry.md`.

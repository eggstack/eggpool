# Provider OAuth Milestone 001 — Credential lifecycle and dispatch integration

Status: blocked

Repository baseline: 5b212d2d69137b99ed39f0dbeab10331c839e54e

Source roadmap:

- plans/subsystems/provider-oauth-roadmap.md#milestone-1--oauth-credential-custody-and-dispatch-integration

Long-term requirements:

- plans/000-long-term-specification.md#1-product-definition
- plans/000-long-term-specification.md#2-end-state-invariants-normative
- plans/001-terminology-and-domain-model.md#identity-and-lifecycle-terms

Applicable ADRs:

- plans/adrs/ADR-0012-provider-oauth-account-custody-and-lifecycle.md (proposed; hard blocker)
- plans/adrs/ADR-0010-personal-upstream-account-custody.md (accepted context; does not authorize OAuth or shared consumer-account use)

Primary class: infrastructure

## 1. Objective

After the policy gate is accepted, add an EggPool-owned, account-scoped OAuth credential store and token resolver that can provide a usable access token before the existing upstream attempt is prepared. This milestone is infrastructure only: it adds no authorization UI/CLI flow and must not be represented as a completed OAuth capability.

## 2. Why this milestone is ready

It is not ready for handoff. Hard dependency: maintainers must accept ADR-0012 and decide the permitted account/owner class, initial provider profile, durable secret store and backup/restore behavior, CLI/server refresh coordination, and post-401 replay behavior. The current provider exclusion remains in force until then. Do not implement against the proposed ADR as though it were accepted.

At the planning baseline, EggPool is Rust 1.89 and Eggfetch 0.4.0 with native-http1 + tls-rustls. eggpass-auth 0.1.1 is the latest checked candidate and uses the Eggfetch 0.4 API; revalidate exact version, MSRV, license/advisory state, and resolved features before adding it. Eggpass 0.1.3 at commit 664207cd898432024f43dff4910ac3fd06c54b6e is a source reference only.

## 3. Current implementation evidence

- rust/src/config.rs defines AccountConfig with static api_key/api_key_env fields.
- rust/src/accounts/registry.rs resolves static keys into an in-memory CredentialStore separate from routing identity.
- rust/src/db/repositories.rs exposes credential-free AccountIdentity and account records use api_key_env, not token values.
- rust/src/runtime_lifecycle/generation.rs constructs provider/account runtime state for a published generation.
- rust/src/coordinator/attempt.rs prepares an owned PreparedUpstreamAttempt synchronously from account_api_key before submission; do not await OAuth inside the preparation borrow or transport layer.
- rust/src/coordinator/failure.rs classifies a generic HTTP 401 as authentication failure/backoff; this does not currently refresh OAuth tokens.
- rust/src/catalog/refresh.rs obtains a static account key for provider catalog work; any OAuth use there must follow the same account token contract, not a separate fallback.
- rust/src/operations/config_mutation.rs and rust/src/cli.rs own the existing static connect/logout and personal-upstream command paths.
- docs/providers.md explicitly excludes OAuth-only consumer integrations and browser/device-code flows.
- eggpass-auth 0.1.1 supplies async OAuth protocol types and a caller-owned CredentialStore/TokenManager. Its manager lock is process-local; the host implements durable storage and cross-process coordination. Eggpass's CLI store and synchronous runtime bridge are not part of the library contract.

## 4. Invariants that must not regress

- No access, refresh, or ID token in SQLite, provider metadata, logs, diagnostics, audit rows, or Debug output.
- No OAuth token is used for an account or principal outside the accepted ADR-0012 policy and binding.
- Existing static API-key configuration and accounts remain compatible.
- Provider profiles remain secret-free; provider transport remains transport-only and keeps the Eggfetch native-http1 + tls-rustls feature pair.
- Request preparation remains synchronous and fully owned before provider I/O; no auth-network await crosses a borrowed preparation boundary.
- No cross-account fallback/reselection after submission and no arbitrary stream/request-body buffering.
- Refresh failure is typed and account-scoped; stale or malformed credentials do not become a usable key.
- Configuration reload continues to use classify_transition as the only reload/restart authority.

## 5. Scope

### In scope

- One storage adapter implementing eggpass-auth's caller-owned credential-store contract using the exact secret custody authorized by accepted ADR-0012.
- An account/issuer-scoped TokenManager with proactive refresh, atomic save/clear, refresh-token rotation, and cancellation behavior.
- The minimum runtime-generation/service seam to resolve an owned access token before preparing an upstream request and before catalog calls that require auth.
- Account status projected without secrets, with explicit unavailable/expired/re-auth-needed states.
- Local OAuth-stub tests for storage, refresh, account isolation, and generation use.

### Explicitly out of scope

- Any account type/provider/profile not named by accepted ADR-0012.
- Headless device-code or desktop browser authorization UX (M002/M003).
- Shared consumer-subscription OAuth pooling unless separately and explicitly approved.
- A new network transport, provider-specific retry logic in rust/src/providers/, or server-side callback route.
- Automatic request replay after HTTP 401 unless accepted ADR-0012 explicitly defines the same-account replay contract and the request body is proven replayable.
- Secret columns or token payloads in SQLite.

## 6. Required production changes

1. Add eggpass-auth only at the exact version accepted by ADR-0012 (currently proposed as 0.1.1). Inspect cargo tree features and duplicates; preserve Eggfetch 0.4.0 with only native-http1 and tls-rustls. Do not add Eggfetch's high-level http1 alias or a second HTTP client.
2. Implement caller-owned async CredentialStore operations with atomic replacement/removal, strict file permissions, interruption safety, backup/restore integration, and account-specific identity. Storage path/format is unresolved until ADR-0012 is accepted; stop if the design would put a token in SQLite or silently reuse a static-key field.
3. Use one manager per approved account/issuer. Confirm issuer and client identity before accepting stored credentials. On refresh, persist the complete rotated credential set atomically before returning a new token; preserve an old refresh token only under the library/provider contract when rotation is omitted.
4. Define CLI/server concurrency. The library manager mutex coalesces only in-process calls. If CLI and server can mutate one account, add the accepted storage-level locking/version-check/ownership mechanism; atomic rename alone does not prevent refresh-token replay races.
5. Resolve the access token asynchronously before synchronous attempt preparation. Pass an owned token or already-built secret header into that boundary; credentials remain absent from Debug and traces. Keep provider transport neutral.
6. Refresh proactively using expiry metadata and the accepted skew. If refresh is unavailable/denied, return a typed account credential failure before submission; do not select a different account as an auth fallback.
7. Implement 401 handling only as accepted by ADR-0012. Default scope is no transparent post-submission replay; if same-account forced refresh/replay is authorized, it is at most once, must compare the failed token to the current token, and must not reselect or replay a non-replayable stream.
8. Apply the same account token contract to catalog/discovery only if the approved provider requires authenticated catalog calls. Public discovery is never credential proof.

## 7. Ordered work packages

### Work package A — Freeze the accepted auth and custody interface

Intent: Do not invent account semantics in code.

Required changes: Reconcile implementation against accepted ADR-0012, identify the exact approved provider profile/grant, persistence adapter, backup path, and replay rule. If the accepted ADR differs from this plan, stop and register a corrective/superseding plan before editing runtime.

Acceptance evidence: A checked decision matrix with all unknowns closed; no dependency/API or account class inferred from Eggpass alone.

### Work package B — Implement atomic account-scoped credential storage

Intent: Persist and rotate tokens without weakening the secret-free database boundary.

Required changes: Implement the approved CredentialStore adapter and restart-safe load/save/clear. Guard account/issuer identity. Add no token payload columns. Ensure replacement and cleanup are crash-safe and concurrency behavior matches the accepted cross-process contract.

Acceptance evidence: Local tests prove old credentials survive failed replacement, new access/refresh tokens become visible together, cancellation leaves no partial file, wrong issuer/account is rejected, and the database remains token-free.

### Work package C — Integrate token resolution before dispatch

Intent: Supply valid account-scoped bearer credentials to the current prepared-attempt boundary.

Required changes: Build the auth manager with generation-owned account state; obtain/refresh tokens asynchronously before borrowed synchronous preparation; transfer only owned values into PreparedUpstreamAttempt; preserve catalog auth behavior if in scope.

Acceptance evidence: A local provider stub observes only the expected account token. Expired credentials refresh before dispatch. Missing/denied credentials consume no upstream attempt. Existing static-key fixtures remain behavior-compatible. No transport or route-reselection policy changes.

### Work package D — Qualify concurrency, reload, and failure semantics

Intent: Prove all-or-nothing token updates under concurrent use and lifecycle changes.

Required changes: Test same-account concurrent refresh, multiple accounts refreshing independently, CLI/server storage contention as specified, refresh-token rotation, cancellation, reload/restart, and 401 behavior as explicitly accepted.

Acceptance evidence: One effective refresh per account/version; no stale token overwrites a newer refresh; no leaked or cross-account token; an in-flight generation follows the accepted lease/retirement contract.

## 8. Failure, cancellation, restart, contention semantics

A refresh error before dispatch is an account-scoped authentication failure and creates no provider attempt. No account switch masks failed OAuth. A successful refresh is not reported to inference until the entire credential set is durably replaced. If cancellation occurs before atomic commit, the previous set remains authoritative; after commit, the new set is authoritative. Restart loads only a validated issuer/account-matched set. Same-process callers use the manager lock; cross-process coordination must be proven by the chosen adapter. 401 behavior is exactly the accepted ADR rule; absent a rule, preserve current failure handling and do not replay.

## 9. Compatibility and migration

Keep existing api_key/api_key_env config and API behavior unchanged. OAuth token bytes never enter a migration. If the accepted model needs a credential reference or account-auth-mode field, add only an additive migration after v68 and make it secret-free. Backup/restore must include or explicitly exclude the token store with a documented safe recovery path. No historical migration is edited. Do not change provider templates before the provider is approved and verified.

## 10. Required tests

Add a focused provider_oauth test target or equivalent module tests:

- CredentialStore load/save/clear, atomic replacement, permissions, corruption, restart, and failure rollback.
- Credential Debug/error/status redaction and database/audit/log absence assertions.
- Provider/issuer/account mismatch rejection and personal-owner isolation.
- Local OAuth stub for successful refresh, denial, transport failure, expiry, refresh-token rotation/omission, and cancellation.
- Contended same-account refresh and independent-account refresh with observable fixture synchronization, not sleeps.
- Generation publication/retirement and async token resolution before request preparation.
- Catalog auth only if authorized; generic 401 behavior and any explicit single same-account retry contract.
- Existing static credential, provider transport, coordinator boundary, and no-default coverage.

## 11. Required verification commands

    cargo fmt --manifest-path rust/Cargo.toml --all -- --check
    cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
    cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
    cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
    cargo test --manifest-path rust/Cargo.toml --test provider_oauth -- --test-threads=1
    cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
    cargo tree --manifest-path rust/Cargo.toml -e features -i eggfetch-core
    cargo tree --manifest-path rust/Cargo.toml --duplicates
    cargo deny --manifest-path rust/Cargo.toml check
    cargo build --manifest-path rust/Cargo.toml --locked --release
    git diff --check

Compare audit output with baseline; existing russh advisories are not permission to add an exception or new advisory.

## 12. Documentation updates

- Update docs/providers.md only after policy acceptance and only for the approved provider/account class.
- Update operator CLI, credential backup/restore, and failure guidance for the selected token-store contract.
- Update architecture/deep-dive-providers.md and the relevant account/runtime owner doc; keep transport ownership unchanged.

## 13. Acceptance criteria

- A valid token is resolved for only the approved account/owner, with no secret in SQLite, logs, diagnostics, profiles, or public projections.
- Expired credentials refresh atomically before request preparation; rotated refresh tokens survive restart.
- Missing, revoked, wrong-issuer, or corrupt credentials fail closed and do not fall back to another account.
- Static API-key behavior is unchanged; provider transport features remain minimal.
- Concurrency, cancellation, restart, and any accepted 401 behavior have deterministic local evidence.
- This infrastructure milestone is not described as OAuth login capability; M002/M003 must close for that claim.

## 14. Stop conditions

Stop before implementation until ADR-0012 is accepted. Also stop if the accepted provider/account policy is unspecified; no safe atomic persistent store or backup/restore rule exists; the library requires feature expansion or incompatible MSRV; refresh coordination crosses a process boundary without a stable contract; secrets would enter SQLite; account eligibility is ambiguous; or post-submission retry violates attempt/body-replay invariants. Preserve all unrelated worktree changes and ignored artifacts.

## 15. Closure evidence required

Include accepted ADR and exact supported provider/account scope; requirement-to-test map; local stub evidence for refresh/rotation, account isolation, cancellation, restart, and any retry semantics; proof SQLite/logs/diagnostics contain no credential bytes; Cargo feature/duplicate/audit and locked release outputs; focused and serial full-suite commands/results; docs and backup/restore updates; and a registry unblock audit promoting M002/M003 only after the token interface is closed.

## 16. Handoff notes

Read accepted ADR-0012, ADR-0010, and current config/account/generation/attempt owners before editing. eggpass-auth 0.1.1 is the latest checked candidate, not an unconditional dependency decision; eggpass 0.1.3 is the CLI reference only. The library does not supply EggPool storage, browser opening, a loopback server, or cross-process refresh locking. Rust tests run serial. Do not commit or push as part of this plan.

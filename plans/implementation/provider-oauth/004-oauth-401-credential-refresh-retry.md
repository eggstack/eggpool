# Provider OAuth Milestone 004 — Owner-scoped credential refresh and retry

Status: implemented

Repository baseline: `7874d0e27d2942c310b414b4a4633629de59afb4`

Source roadmap:

- `plans/subsystems/provider-oauth-roadmap.md` (`14, ADR-0015 credential-refresh amendment)

Long-term requirements:

- `plans/000-long-term-specification.md#1-product-definition`
- `plans/000-long-term-specification.md#2-end-state-invariants-normative`
- `plans/001-terminology-and-domain-model.md#identity-and-lifecycle-terms`
- `plans/002-long-term-roadmap.md#phase-5--optional-on-prem-small-team-access-and-budgeting`

Applicable ADRs:

- `plans/adrs/ADR-0015-provider-oauth-owner-scoped-credential-refresh-retry.md` (accepted decision implemented by M004)
- `plans/adrs/ADR-0012-provider-oauth-account-custody-and-lifecycle.md` (historical custody, owner, gate, and secret-handling authority; retained except where ADR-0015 supersedes its no-replay clause)
- `plans/adrs/ADR-0013-provider-oauth-eggfetch-compatibility.md` (historical dependency decision; otherwise unchanged)
- `plans/adrs/ADR-0014-provider-oauth-web-ui-and-desktop-flow-boundary.md` (historical login-flow boundary; otherwise unchanged)

Primary class: capability

## 1. Objective

Recover one upstream HTTP 401 for an owner-bound OAuth account by refreshing that same owner's credential for that same account and retrying the same request once. A healed stale credential must not disable the account. Refresh failure or a second rejection remains terminal and applies the existing authentication-failure consequence.

This is one bounded inference capability. It does not add login flows or make a claim about live provider support.

## 2. Why this milestone is ready

ADR-0015 is accepted and specifies the compare-and-refresh operation, owner/account boundary, operator gate, health behavior, attempt limit, and compatibility contract. M001's owner-bound credential manager and atomic credential-file contract are available to the request coordinator. M001–M003 remain separately scoped roadmap milestones; this work neither changes their UI/CLI flows nor introduces another identity or storage contract.

The current worktree already contains the M004 runtime implementation and focused tests described below. The remaining plan boundary is to preserve that implementation, complete the exact qualification and documentation evidence, and close M004; do not re-implement or broaden it.

## 3. Current implementation evidence

- `rust/src/oauth.rs:265-305` adds `refresh_rejected_credential_for_user` and its private identity seam. It checks the operator gate, resolves the owner-bound configured account, takes the existing account OS lock, and runs Eggpass `TokenManager::force_refresh`; the detached transaction retains the lock through the atomic credential save. Compare-and-refresh returns the newer stored access token if it is no longer the rejected one. `rust/src/oauth.rs:540-552` maps a denied refresh grant to `NeedsReauthentication`. The test-only provider profile override is at `rust/src/oauth.rs:204`.
- `rust/src/coordinator/mod.rs:79-97` limits the special case to a configured OAuth account and defines the healed retry effects. `rust/src/coordinator/mod.rs:128` begins the shared refresh helper. The published `RetryScope` and `NextAction` variant sets are unchanged, so the library's public API does not move; the recovery is described by its own scope and action labels, `same_account_refreshed_credential` and `refresh_credential_same_account`, at `rust/src/coordinator/mod.rs:110-121`. `terminalize_oauth_401` remains the terminal path when recovery fails or the refreshed credential is rejected again (`rust/src/coordinator/failure.rs:230-241`). The classifier remains the compatibility authority at `rust/src/coordinator/failure.rs:372`.
- Finite response-status and embedding-status handling each gate on the per-request refresh flag and ordinary attempt budget, refresh before retry, skip applying the first 401 health effect when healed, re-admit and pin the rejected wire surface for that account, and resubmit through the normal credential-resolution path (`rust/src/coordinator/finite.rs:1830-1860`, `:2159-2189`). The request-local pin is declared at `rust/src/coordinator/finite.rs:782` and honored by the candidate selection at `rust/src/coordinator/finite.rs:1008-1035`. `prepare_next` pins the same account for the healed retry (`rust/src/coordinator/finite.rs:2470-2487`).
- Streaming performs the equivalent recovery only in the pre-handoff coordinator; it keeps the account and re-offers the rejected wire surface, and remains within the shared attempt budget (`rust/src/coordinator/streaming/coordinator.rs:799-830`, `:1524-1541`, with the request-local pin declared at `:291`). Retry does not cross the `StreamingExecution` handoff.
- Borrowed `AttemptPreparation` ends at synchronous preparation and `PreparedUpstreamAttempt` is owned before `submit_once` (`rust/src/coordinator/attempt.rs:39-67`, `:160-165`, `:287-290`).
- Five focused refresh unit tests are present at `rust/src/oauth.rs:1760`, `:1798`, `:1839`, `:1875`, and `:1896`. The shared detached-refresh cancellation test is `rust/src/oauth.rs:1445`; cross-process OS-lock exclusivity is covered at `rust/src/oauth.rs:1686`.
- Coordinator integration tests cover same-account stream recovery, second-401 terminalization, and finite recovery at `rust/src/coordinator/oauth_integration_tests.rs:748`, `:820`, and `:874`. The compatibility fixture is included by `rust/tests/coordinator_c011.rs:70-71` and `rust/tests/coordinator_c013.rs:29-30`; C013 compares classifier results to the frozen cases at `rust/tests/coordinator_c013.rs:137-147`. The OAuth/static-account predicate is also tested at `rust/src/coordinator/mod.rs:197-198`.
- The retry deep dive is updated for M004: the frozen `RetryScope`/`NextAction` variant sets, the healed attempt's own scope and action labels, the pinned wire surface, and the unchanged classifier are described in `architecture/deep-dive-retry.md`.

No credentials, request bodies, prompts, or cache keys belong in plan text or qualification artifacts.

## 4. Invariants that must not regress

- The refreshed credential belongs to the authenticated owner and the exact selected provider account. Never cross users, providers, or accounts; never pool credentials or use cross-account fallback.
- The operator OAuth gate applies to this refresh exactly as it applies to login, routing, and proactive refresh. A disabled gate fails closed.
- Access/refresh/ID tokens remain outside SQLite and all logs, diagnostics, audits, and public projections. Do not log a rejected credential or include one in an error.
- Keep the existing credential file format, owner binding, and private permissions. Do not add a configuration key, schema migration, SQLite connection, or lock other than use of the existing per-account OS lock.
- Compare before refreshing. A newer stored credential wins without a second token request; rotated credentials are committed atomically before the account lock is released.
- At most one credential refresh and one same-account replay per request, bounded by the existing upstream-attempt budget. No replay after streaming handoff and no arbitrary body buffering.
- A healed 401 carries no account-health penalty. Refresh failure or a second 401 retains the existing authentication-failure/account-disable behavior.
- Static-key 401 behavior and all existing `classify` outputs/recorded labels remain unchanged. Only the healed attempt receives the new scope/action labels.
- Attempt preparation remains synchronous and borrowed only before submission; provider awaits receive a fully owned `PreparedUpstreamAttempt`.

## 5. Scope

### In scope

- The existing rejected-credential refresh service and its use for owner-bound OAuth 401s.
- Finite response and embedding provider-error handling plus streaming pre-handoff handling.
- Same-account, same-wire, once-per-request retry under the ordinary attempt budget.
- Focused owner/gate/compare-and-refresh, cancellation, compatibility, terminalization, and secret-hygiene evidence.
- A narrow retry-deep-dive correction and complete M004 verification/closure evidence.

### Explicitly out of scope

- Cross-account fallback, cross-user fallback, OAuth pooling/sharing, or retrying against another provider.
- Unbounded refresh/replay, a second refresh after the refreshed credential is rejected, or any post-handoff streaming retry.
- Changes to failure classifier outputs, static-key 401 policy, storage format/permissions, config keys, database schema, or credential custody.
- New provider/login support, a live-provider qualification claim, legal-compliance claims, or changes to provider transport.
- Roadmap/registry edits in this plan; those surfaces are owned by the parallel planning update.

## 6. Required production changes

The production delta is already present in this worktree; this section records the exact M004 contract rather than authorizing a second implementation:

1. Keep the owner-scoped `force_refresh` service using the existing account identity, operator gate, OS lock, file store, and atomic commit. A rejected credential is compared in memory only. Denied grants request reauthentication; other refresh errors fail closed.
2. Keep the classifier's frozen output unchanged. The coordinator adds a separate healed-retry scope/action, and terminalizes the OAuth 401 if refresh is unavailable/fails or the refreshed credential is rejected.
3. In finite response, finite embedding, and streaming pre-handoff provider-error handling, refresh only the current OAuth account's credential. On success, avoid the first 401's health penalty, clear only that account's attempted wire surfaces, retain that account, and resolve the committed credential through the ordinary next-attempt path.
4. Preserve one per-request refresh flag and the shared attempt budget. Do not move auth/retry policy into provider transport or extend the retry window beyond the existing pre-handoff boundary.

## 7. Ordered work packages

### Work package A — Owner-scoped credential refresh transaction

Intent: Heal only the rejected owner's credential without spending a rotated refresh token twice.

Required changes: Retain the existing `refresh_rejected_credential_for_user` path, its account/owner and operator checks, compare-and-refresh call, existing per-account OS lock, atomic persistence, cancellation detachment, and typed reauthentication mapping. Do not add another persistence or lock mechanism.

Acceptance evidence: Refresh rotates and persists the owner's complete set; when another process has already rotated the access credential, the newer credential is reused with no second token request; denied grant maps to reauthentication; disabled OAuth and non-owner calls do not refresh. Cancellation leaves the OS lock owned until atomic save completes. Cross-process contenders serialize on the existing account lock.

### Work package B — Coordinator recovery and compatibility

Intent: Add a separate recovery action without changing the classifier's recorded contract.

Required changes: Retain the OAuth-account 401 predicate, healed effects, new scope/action labels, and terminalization after failed recovery or a second rejection. Keep static-key decisions and all frozen classifier cases byte-for-byte equivalent in meaning.

Acceptance evidence: `coordinator_c011` and `coordinator_c013` still match the compatibility fixture, including both 401 cases; helper coverage shows OAuth 401 qualifies and static-key 401 does not; the healed retry records only its designated new scope/action labels.

### Work package C — Finite and pre-handoff streaming consumers

Intent: Retry the original request against the same OAuth account and wire surface once, without penalizing a healed account.

Required changes: Retain the per-request flag and attempt-budget check in finite response/embedding and streaming pre-handoff paths; keep the account pinned, clear only its attempted wire surfaces, and let ordinary owner-scoped credential resolution load the committed value. Leave post-handoff execution and attempt preparation unchanged.

Acceptance evidence: Local stub integration proves a first OAuth 401 refreshes and retries the same account once with the new credential; no health penalty is applied after a successful refresh; a second 401 does not refresh again and disables the account; a refresh failure also terminalizes with the existing account failure effect; static-key 401 behavior is unchanged.

### Work package D — Qualification, documentation, and closure

Intent: Establish bounded capability evidence and reconcile the retry documentation with the accepted decision.

Required changes: Run the focused and broad verification commands in `11; verify cancellation/lock and cross-process contention at observable fixture boundaries; update only the retry deep dive needed to describe M004. Keep roadmap and registry changes with their parallel owner.

Acceptance evidence: All required tests/checks pass serially; no secret appears in persistence/logs/diagnostics/projections; architecture text accurately distinguishes unchanged classification from coordinator-owned retry; no live-provider capability is claimed.

## 8. Failure, cancellation, restart, contention semantics

- **Refresh error or gate denial:** the coordinator does not retry with a missing/invalid credential. It terminalizes the OAuth 401 and applies the existing authentication-failure/account-disable effect; it never chooses another account.
- **Second 401:** the per-request refresh flag prevents another refresh. The second rejection terminalizes and disables the account as before; later requests cannot route through that disabled account until repaired by the existing owner login path.
- **Cancellation:** canceling the request stops that request from continuing, but does not cancel the detached refresh transaction. The transaction keeps the existing OS lock until the full atomic credential save finishes, so another process cannot read/spend a partially rotated set.
- **Restart:** the credential file remains in its existing format. After an atomic save, a restarted manager reads the complete rotated set; before commit, the old set remains authoritative. No database migration or restart/config boundary changes.
- **Contention:** the existing per-account OS lock serializes refresh work across processes. A waiter then observes the current file; compare-and-refresh returns a newer credential if the rejected token is stale, avoiding a second refresh-token spend.

## 9. Compatibility and migration

No credential-format, permission, owner-binding, database, config, route, or wire migration is introduced. Existing static API-key accounts retain their prior 401 classifier/retry/fallback behavior. The classifier's recorded outputs remain frozen; the recovery's own scope and action labels apply only to the healed coordinator retry, the durable attempt rows keep their existing shape, and the published `RetryScope`/`NextAction` variant sets do not move. M004 does not establish live xAI/OpenAI behavior or change provider support claims.

## 10. Required tests

- `rust/src/oauth.rs`: rejected credential rotates and persists; already-rotated credential is reused without refreshing; denied grant requires reauthentication; operator gate blocks refresh; non-owner is rejected.
- `rust/src/oauth.rs`: detached cancellation holds the same OS lock through atomic save; cross-process lock exclusivity and serialized refresh contention are proven at an observable boundary, not by fixed sleeps. Reopening the manager/store after commit observes a complete rotated credential set.
- `rust/src/coordinator/oauth_integration_tests.rs`: same-account streaming recovery exactly once; second 401 terminalizes/disables without a second refresh; finite response recovery exactly once. Add a finite embedding case or demonstrate its shared tested response-status path if the coordinator interface makes a separate fixture redundant.
- Verify refresh failure (including gate-denied refresh) terminalizes and keeps the prior failure effect; healed first 401 is not applied as account health; no cross-account/user fallback occurs.
- Verify the failed credential is never written into SQLite, logs, diagnostics, audit, or public projections. Do not print fixture secrets in test output.
- `coordinator_c011` and `coordinator_c013`: frozen failure-classifier oracle including 401 outputs; verify static-key 401 behavior remains unchanged.
- Verify the same wire surface is reused and the ordinary attempt budget still bounds the retry. Confirm no retry after streaming handoff and no borrowed attempt data crosses `submit_once`.

## 11. Required verification commands

Run Rust tests serially. From the repository root:

~~~bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo test --manifest-path rust/Cargo.toml --lib oauth -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_c008 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_c009 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_c011 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_c013 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_boundaries -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_finalization -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_publication -- --test-threads=1
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- --test-threads=1
git diff --check
~~~

Do not claim these commands passed in a closure record unless they were run and their results captured. No dependency change is in scope, so dependency audit/release qualification is not added unless implementation evidence changes that boundary.

## 12. Documentation updates

- Update `architecture/deep-dive-retry.md` narrowly: document the healed attempt's own scope and action labels, the frozen `RetryScope`/`NextAction` variant sets, same-account and re-offered-wire behavior, one refresh inside the shared budget, terminal failure/second rejection, and the pre-handoff-only boundary. Preserve the generic classifier description and static-key behavior.
- Do not add a live-provider support claim to `docs/providers.md`; local stubs prove coordinator behavior only.
- The M004 roadmap milestone table and `plans/registry.md` are being changed by their assigned owner. Do not edit or duplicate those control-surface changes here.

## 13. Acceptance criteria

- An upstream 401 for an owner-bound OAuth account causes one compare-and-refresh for that owner/account and at most one retry of the same request on the same account, re-offering the rejected wire surface. The retry holds the account constant; the surface is a resolver-level preference, so a surface the shared resolver has since learned is rejecting is not offered again and the account's next candidate is used instead.
- A healed credential does not incur account-health penalty. Refresh failure or a second 401 is terminal and disables the account under existing auth-failure semantics.
- Static-key 401 behavior and every existing failure-classifier observation remain unchanged; only the healed retry has the new coordinator scope/action labels.
- The operator gate, owner boundary, atomic credential rotation, per-account cross-process lock, cancellation guarantee, and secret-free storage/diagnostic boundary remain intact.
- One retry consumes the ordinary attempt budget; streaming retries remain pre-handoff; attempt preparation remains synchronously borrowed and fully owned before submission.
- Focused, coordinator, workspace, and no-default checks pass; the retry deep dive is accurate; no migration, config key, second SQLite connection, new lock, or live-provider claim is introduced.

## 14. Stop conditions

Stop and report if owner identity/account binding or the operator gate is ambiguous; compare-and-refresh cannot prove the rejected-token comparison; atomic save or the existing cross-process lock cannot be preserved; a second refresh, alternate account, or other user would be required; the compatibility fixture changes; static-key handling changes; retry must cross streaming handoff or attempt-preparation ownership; or the change requires a new storage format/permission, config key, database connection, lock, or live-provider assumption. Do not resolve a documentation mismatch by changing canonical policy or the parallel-owned roadmap/registry.

## 15. Closure evidence required

The M004 closure record must include:

- The accepted M004 scope and implementation commit(s), with owner/account and operator-gate review.
- Focused tests for successful refresh, compare-and-refresh, denial/gate/non-owner, exact same-account retry, failed refresh, second 401, static-key compatibility, and secret exclusion.
- Observable cancellation/atomic-commit and cross-process-lock/contention evidence; restart/readback evidence for a complete rotated set.
- The C011/C013 frozen classifier oracle result and default/no-default check, Clippy, and serial workspace test results, with exact command outcomes.
- Review that request-body replay is bounded by one existing attempt, same wire is retried, and streaming remains pre-handoff with owned attempt submission.
- The retry-deep-dive update, no live provider claim, and confirmation that the parallel roadmap/registry update was completed by its owner.
- Any remaining limitation stated explicitly; do not mark closed based on compilation alone or infer live support from local stubs.

## 16. Handoff notes

The runtime implementation is already present in the assigned worktree; this plan is an active qualification/closure contract, not a request to duplicate it. Preserve unrelated working-tree changes. The baseline SHA above is the specified plan baseline; current worktree edits include the M004 runtime plus parallel documentation/registry/roadmap work. Do not touch those parallel-owned files. Use only local fixtures for OAuth qualification, keep Rust tests serial, and record that no live-provider support is established.

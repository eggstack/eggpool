# Provider OAuth Milestone M004 — Closure Status

Status: closed

Source implementation plan:

- `plans/implementation/provider-oauth/004-oauth-401-credential-refresh-retry.md`

Source subsystem roadmap:

- `plans/subsystems/provider-oauth-roadmap.md#14-append-only-credential-refresh-amendment--adr-0015`

Repository baseline reviewed: `7874d0e27d2942c310b414b4a4633629de59afb4`

Implementation commits or pull requests:

- `a597691c` — accepted ADR-0015 decision, the M004 implementation plan, the roadmap §14/§14.1 append-only amendment, and the registry rows.
- `fe8be969` — the owner-scoped credential-refresh runtime, the coordinator recovery path, the first regression tests, the retry deep-dive update, and the provider documentation update.
- `49cb609b` — the review follow-up: the published `RetryScope`/`NextAction` variant sets are restored so the library's public API does not move, the retry pins the rejected wire surface instead of clearing the account's whole attempted-surface set, and three more regression tests cover cross-account fallback, token hygiene, and the finite embedding branch.

## 1. Executive finding

The owner-scoped credential-refresh retry is implemented and verified. An upstream HTTP 401 on an account whose auth mode is OAuth now refreshes that owner's credential for that same account and retries the same request against the same account exactly once.

The recovery is compare-and-refresh: it rotates the credential only when the stored access token is still the token the provider rejected, so a rotated refresh token is never spent twice. It runs under the existing per-account OS lock, commits atomically before the lock is released, and is detached from request cancellation. A healed credential does not incur an account-health penalty. A refresh failure, a denied refresh grant, or a second rejection of the refreshed credential terminalizes the request and disables the account under the existing authentication-failure semantics.

The retry holds the account constant and re-offers the wire surface that was rejected, because the provider rejected a stale credential rather than the surface. The surface is a resolver-level preference, not a bypass: if the shared resolver has since learned that the surface is rejecting, it is not offered again and the account's next candidate is used instead.

The failure classifier's recorded output is unchanged, so the frozen compatibility observation fixture remains valid, and static-key 401 behavior is unchanged. The published `RetryScope` and `NextAction` variant sets are unchanged, so the library's public API does not move; the recovery is described by its own scope and action labels, which extend the `FailureEffects` string vocabulary without adding a durable attempt column.

An independent review of the change produced four findings, and all four were addressed before closure: the public API drift, the wire-surface re-selection, the durability wording for the new labels, and three missing test boundaries.

M004 closes on deterministic local-fixture evidence plus the full local and CI verification matrix. No live xAI or OpenAI provider support is claimed or established by this milestone: local stubs prove coordinator and credential-manager behavior only, and exact provider registration remains an open M001–M003 item.

## 2. Requirement-to-evidence matrix

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| Owner-scoped compare-and-refresh; a rotated refresh token is never spent twice | `rust/src/oauth.rs:265` `refresh_rejected_credential_for_user`; `rust/src/oauth.rs:281` identity seam; `rust/src/oauth.rs:1760` `rejected_credential_refresh_rotates_the_owner_credential`; `rust/src/oauth.rs:1798` `rejected_credential_refresh_reuses_a_rotated_credential_without_refreshing` | pass | The reuse test points the token endpoint at a closed socket, so a second token request would fail the test. |
| Denied refresh grant, operator gate, and non-owner rejection | `rust/src/oauth.rs:1839` `rejected_credential_refresh_denial_requires_reauthentication`; `rust/src/oauth.rs:1875` `rejected_credential_refresh_honours_the_operator_gate`; `rust/src/oauth.rs:1896` `rejected_credential_refresh_rejects_a_non_owner`; `rust/src/oauth.rs:540` `map_rejected_credential_error` | pass | The provider's error body is redacted from the mapped error and the stored credential set is byte-identical after a denial. |
| Exactly one same-account retry after a 401 | `rust/src/coordinator/oauth_integration_tests.rs:886` `oauth_401_refreshes_the_owner_credential_and_retries_the_same_account_once`; `:1012` `finite_oauth_401_refreshes_the_owner_credential_and_retries_the_same_account_once` | pass | Two chat requests and one token request; the stale bearer is replaced by the rotated bearer on the second request. |
| The retry re-offers the rejected wire surface | The request-local pin at `rust/src/coordinator/finite.rs:782` and `rust/src/coordinator/streaming/coordinator.rs:291`, honored by the candidate selection in both coordinators | pass | The pin is a resolver-level preference; the shared resolver may still withdraw a surface it has learned is rejecting. |
| The retry never falls back to another account | `rust/src/coordinator/oauth_integration_tests.rs:1068` `oauth_401_retry_never_falls_back_to_a_static_account` | pass | A second configured static-key account is present, receives no inference request, and has no usage or attempt rows; the classifier's ordinary static-key fallback would have selected it. |
| A second rejection terminalizes and disables the account with no second refresh | `rust/src/coordinator/oauth_integration_tests.rs:958` `oauth_401_terminalizes_when_the_refreshed_credential_is_rejected_again` | pass | Exactly one refresh and one retry, then the request ends as `NoEligibleRoute` with the account disabled. |
| A healed credential incurs no account-health penalty | The healed-retry integration test; `rust/src/coordinator/mod.rs:110` `healed_credential_refresh_effects` | pass | The account remains eligible after the healed attempt. |
| The failure classifier's recorded output is unchanged | `rust/tests/coordinator_c011.rs` and `rust/tests/coordinator_c013.rs` against `tests/fixtures/coordinator/compatibility-observations.json`; `rust/src/coordinator/failure.rs:372` `classify` | pass | The frozen oracle compares byte-for-byte; the new labels appear only on the healed coordinator retry. |
| Static-key 401 behavior is unchanged | `rust/src/coordinator/mod.rs:79` `oauth_401_for_account` predicate; the C011/C013 frozen cases | pass | A static-key account is never eligible for the refresh path. |
| The published library API does not change | `rust/src/coordinator/failure.rs` `RetryScope`/`NextAction`; the `rust/src/lib.rs` re-exports | pass | The review follow-up removed the two added variants; the recovery is expressed through the label vocabulary instead. |
| Operator gate, owner boundary, atomic rotation, cross-process lock, cancellation | `rust/src/oauth.rs:1277` `expired_credentials_rotate_atomically_and_restart_with_new_token`; `rust/src/oauth.rs:1445` `canceled_refresh_finishes_atomic_commit_before_releasing_lock`; `rust/src/oauth.rs:1686` `identity_lock_is_exclusive_across_processes` | pass | Synchronized on observable fixture boundaries, not on fixed sleeps. |
| Secrets stay out of SQLite, logs, diagnostics, and public projections | `rust/src/coordinator/oauth_integration_tests.rs:1137` `oauth_401_retry_does_not_persist_or_diagnose_either_access_token`, with the `database_table_contains` and `database_account_scoped_rows` helpers; the redaction assertions in `rust/src/oauth.rs:1839` and `rust/src/oauth.rs:1760` | pass | Neither the rejected nor the rotated access token reaches any SQLite table, any attempt row, or any coordinator-produced diagnostic. |
| The finite embedding branch performs the same recovery | `rust/src/coordinator/oauth_integration_tests.rs:1216` `finite_embedding_oauth_401_refreshes_the_owner_credential_and_retries_once` | pass | The embedding-status provider-error branch is a separate code path from the response-status branch. |
| One refresh and one retry inside the ordinary attempt budget; streaming stays pre-handoff | `rust/src/coordinator/finite.rs`; `rust/src/coordinator/streaming/coordinator.rs` | pass | The heal branches sit in the provider-error paths ahead of the streaming handoff; the retry consumes the ordinary attempt budget. |
| Default and no-default parity, strict Clippy, and the serial workspace suite | Section 4 | pass | The no-default feature set retains the direct/non-SSH proxy paths and compiles and tests cleanly. |
| Architecture and operator documentation | `architecture/deep-dive-retry.md`; `docs/providers.md` | pass | No live-provider support claim is added. |
| Live provider registration and live 401 behavior | Not run | not run | Local stubs cannot establish live xAI/OpenAI behavior. This is an M001–M003 closure condition, not an M004 defect. |

## 3. Production implementation evidence

`rust/src/oauth.rs` owns the refresh primitive. `refresh_rejected_credential_for_user` applies the operator gate, resolves the owner-bound configured account, and delegates to a private identity seam that takes the existing per-account OS lock and runs Eggpass `TokenManager::force_refresh` inside a detached task. The detached task owns the lock until Eggpass's atomic credential save completes, so a canceled request cannot leave a partially rotated credential set or release the lock early. A denied grant maps to `NeedsReauthentication`; other provider failures map to a refresh failure, and both fail closed.

`rust/src/coordinator/mod.rs` owns the recovery decision. `oauth_401_for_account` recognizes the case from the observation plus the configured account, `refresh_rejected_oauth_credential` performs the owner-scoped refresh and logs a failure without secrets, and `healed_credential_refresh_effects` derives the healed attempt's scope and action labels.

`rust/src/coordinator/failure.rs` keeps its published `RetryScope` and `NextAction` variant sets and its `classify` output unchanged, so the frozen compatibility oracle stays valid. `terminalize_oauth_401` remains the terminal path when recovery fails or the refreshed credential is rejected again.

`rust/src/coordinator/finite.rs` applies the recovery in both the response-status and the embedding-status provider-error branches, and `rust/src/coordinator/streaming/coordinator.rs` applies it in the pre-handoff provider-error branch. Each site gates on a per-request refresh flag and the ordinary attempt budget, re-admits the rejected `WireSurface` for that account and pins it for the next attempt, holds the account constant through `prepare_next`, and leaves the account-health effect unapplied when the refresh succeeded.

## 4. Verification executed

### Commands run

```bash
cargo fmt --manifest-path rust/Cargo.toml --all
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
git diff --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --lib oauth -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --lib coordinator::oauth_integration_tests -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_c008 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_c009 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_c011 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_c013 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_boundaries -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_finalization -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_publication -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- --test-threads=1
cargo deny --manifest-path rust/Cargo.toml check advisories
uv sync --locked
uv run ruff format --check scripts/ tests/tooling/
uv run ruff check scripts/ tests/tooling/
uv run pyright scripts/
uv run pytest tests/tooling/ -q --tb=short --maxfail=1
uv run python scripts/validate_release_docs.py
```

### Results

All Rust commands above were run locally on this branch at `49cb609b`. Every Rust test command was serial (`--test-threads=1`).

| Command | Result |
|---|---|
| `cargo fmt --all` then `--check` | pass, no diff |
| `git diff --check` | pass, no whitespace error |
| strict Clippy, default features | pass, exit 0, no warnings |
| `--lib oauth` | pass, 62 passed, 0 failed |
| `--lib coordinator::oauth_integration_tests` | pass, 7 passed, 0 failed |
| `--test coordinator_c008` | pass, 34 passed, 0 failed |
| `--test coordinator_c009` | pass, 13 passed, 0 failed |
| `--test coordinator_c011` (frozen oracle) | pass, 18 passed, 0 failed |
| `--test coordinator_c013` (frozen oracle) | pass, 5 passed, 0 failed |
| `--test coordinator_boundaries` | pass, 6 passed, 0 failed |
| `--test coordinator_finalization` | pass, 12 passed, 0 failed |
| `--test coordinator_publication` | pass, 11 passed, 0 failed |
| workspace suite, default features, serial | pass, exit 0, 1347 passed, 0 failed, 2 ignored |
| `cargo check --no-default-features` | pass, exit 0 |
| strict Clippy, no-default features | pass, exit 0 |
| workspace suite, no-default features, serial | pass, exit 0, 1348 passed, 0 failed, 2 ignored |
| `cargo deny check advisories` | fail, RUSTSEC-2026-0492 and RUSTSEC-2026-0495 reach `russh 0.62.7` through Eggress `1.0.12`; pre-existing and unrelated to M004, which changed no dependency; tracked as Security hardening M008 |
| `uv sync --locked` | pass, 9 packages checked |
| `ruff format --check` | pass, 74 files already formatted |
| `ruff check` | pass, all checks passed |
| `pyright scripts/` | pass, 0 errors, 0 warnings, 0 informations |
| `pytest tests/tooling/` | pass, 267 passed, 1 skipped |
| `scripts/validate_release_docs.py` | pass, 7 documents checked |
| CI workflow, commit `49cb609b` | pass, run `45314240224`, job `check` concluded success on the exact head |

## 5. Invariant review

- **Owner boundary.** `refresh_rejected_credential_for_user` resolves the identity for the authenticated principal, and a non-owner call returns `InvalidOwner` without touching the credential store. The retry never reselects an account.
- **No pooling, no cross-user fallback, no cross-account fallback.** The retry holds the account constant and uses only that owner's credential. A second configured account is present in the regression test and never receives a request.
- **Operator gate.** `[oauth].enabled = false` blocks the refresh exactly as it blocks login, routing, and proactive refresh; the gate-denied path returns `Disabled`.
- **No double-spend of a rotated refresh token.** The refresh is compare-and-refresh, so a credential another process already rotated is reused without a second token request.
- **Atomicity and cross-process safety.** The refresh commits atomically before the per-account OS lock is released; cross-process contenders serialize on that lock.
- **Secret-free boundary.** No credential, request body, prompt, or cache key enters SQLite, logs, diagnostics, or a public projection. The token-hygiene test scans every SQLite table and account-scoped row.
- **Classifier stability.** `classify` is byte-identical for every existing observation, including both 401 cases, so the frozen compatibility fixture remains valid.
- **Published API stability.** `RetryScope` and `NextAction` keep their published variant sets, so the published `eggpool` library's public API does not change.
- **Static-key parity.** A static-key account never enters the refresh path and keeps its previous 401 classifier, retry, and fallback behavior.

## 6. Failure and recovery review

- **Refresh failure or gate denial.** The coordinator does not retry with a missing or invalid credential. The request terminalizes with the existing authentication-failure consequence and the account is disabled. It never chooses another account.
- **Second rejection.** The per-request refresh flag prevents another refresh. The second 401 terminalizes and disables the account exactly as before, and later requests cannot route through that account until the owner logs in again.
- **Cancellation.** Canceling the request does not cancel the detached refresh transaction. The transaction keeps the OS lock until the complete atomic save finishes, so another process cannot read or spend a partially rotated set.
- **Restart.** The credential file format is unchanged. After an atomic save a restarted manager reads the complete rotated set; before commit the old set remains authoritative. No database migration and no restart or config boundary change was introduced.
- **Contention.** The existing per-account OS lock serializes refresh work across processes. A waiter observes the current file, and compare-and-refresh returns the newer credential when the rejected token is stale, avoiding a second refresh-token spend.
- **Duplicate delivery.** The retry consumes one ordinary attempt from the existing budget, so request replay stays bounded by the attempt budget rather than by a new loop.
- **Concurrent wire-surface withdrawal.** The retry's surface pin is a resolver-level preference. If the shared resolver has learned that the surface is rejecting, the retry uses the account's next candidate instead of forcing a surface the resolver has retired.
- **Malformed or unauthorized input.** The refresh path accepts only a credential identity that the owner already resolved; an unrecognized account or an unauthorized principal is rejected before any token request.

## 7. Migration and compatibility review

No credential-format, permission, owner-binding, database, config, route, or wire migration was introduced. The database schema and the migration set are unchanged. Existing static API-key accounts keep their previous 401 behavior. The classifier's recorded outputs stay frozen; the recovery's own scope and action labels apply only to the healed coordinator retry, the durable attempt rows keep their existing shape, and the published `RetryScope`/`NextAction` variant sets do not move. The default and no-default feature boundaries both compile and pass their serial suites, so the direct/non-SSH proxy paths and the SSH proxy rejection path are unchanged.

## 8. Security review

- **Authentication.** The refresh is reachable only for the authenticated owner of a configured OAuth account. A non-owner, an unconfigured account, and a disabled OAuth service are all refused before any token request.
- **Secret handling.** Credentials live only in the private `.eggpool-oauth/` directory outside SQLite. The refresh compares the rejected token in memory only and never writes it to storage, logs, diagnostics, or a projection. Error mapping reduces a provider's error body to a typed reason, and the redaction and hygiene assertions cover the provider's error text and both access tokens.
- **Privilege bounds.** The recovery cannot escalate: it may only refresh the owner's own credential and retry the owner's own request on the owner's own account.
- **DoS bounds.** One refresh and one retry per request, inside the ordinary attempt budget. No unbounded refresh loop and no new timer or background task was introduced.
- **Audit behavior.** The failure path logs provider id, account name, and the typed error without any secret, so an operator can see why a recovery did not happen.

## 9. Documentation and operations

`architecture/deep-dive-retry.md` documents the frozen `RetryScope`/`NextAction` variant sets, the healed attempt's own scope and action labels and the fact that they are not new durable columns, the same-account and re-offered-wire behavior, the one-refresh budget, the terminal failure and second-rejection paths, the pre-handoff-only streaming boundary, and the fact that the classifier itself is unchanged. `docs/providers.md` now describes the recovery instead of stating that OAuth accounts never retry after a provider 401. No operator-facing command, diagnostic, or configuration key was added, and no live-provider support claim appears in either document.

## 10. Unresolved findings

| Severity | Finding | Impact | Required action |
|---|---|---|---|
| medium | Exact xAI and OpenAI provider registration and live 401 behavior are unverified; the refresh path is proven only against a local stub | A live provider may answer a refresh denial or a second rejection differently from the stub | Verify against the real providers during M001–M003 closure. No M004 code change is implied. |
| medium | `RUSTSEC-2026-0492` and `RUSTSEC-2026-0495` reach `russh 0.62.7` through Eggress `1.0.12`, so the repository dependency-audit job is red | The repository-wide audit gate fails independently of M004 | Tracked as Security hardening M008, which requires an Eggress release permitting `russh >=0.65`, an accepted ADR for a patched `0.62.x`, or removal of the default `ssh` feature. M004 changed no dependency. |
| low | The finite embedding branch and the finite response-status branch carry separate copies of the recovery logic, and the streaming branch carries a third | A future change to the recovery path must be applied in three sites, and a divergence would not be caught by the other branch's fixture | Keep the finite response, finite embedding, and streaming recovery branches in sync when the recovery path changes. Each branch now has its own regression test. |
| low | The retry's wire-surface reuse is a resolver-level preference rather than a guarantee | A concurrent rejection of the same surface can move the retry to the account's next candidate | Accepted deliberately: forcing a surface the shared resolver has retired would defeat the resolver's purpose. The account and the owner boundary are unaffected. |

## 11. Roadmap disposition

Milestone closed; the next dependency may proceed. The M004 roadmap milestone row moves to `closed` with a link to this record. M001, M002, and M003 remain `active` and are not promoted or reopened: each is gated on exact provider registration and live provider evidence that cannot be produced in this environment, which this milestone does not supply. No other registered plan depends on M004, and the blocked-work table is unchanged. A new corrective plan is not required.

## 12. Registry updates

Applied in the same commit as this record:

- `plans/registry.md`: the M004 row leaves the active implementation-plans table and is added to the recently-closed table with this closure record as evidence; the Provider OAuth roadmap row records M004 as closed; a dated closure-audit paragraph records the closure, the blocked-work audit, and the still-open M001–M003 live-evidence condition.
- `plans/implementation/provider-oauth/004-oauth-401-credential-refresh-retry.md`: the plan header moves from `active` to `implemented`.
- `plans/subsystems/provider-oauth-roadmap.md`: the M004 row in the milestone-status table moves from `active` to `closed` and links this closure record; a dated append-only §15 closure note records the closure and the remaining M001–M003 evidence condition.

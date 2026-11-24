# Provider OAuth Milestone M001 — Closure Status

Status: closed

Source implementation plan:

- `plans/implementation/provider-oauth/001-oauth-credential-lifecycle-and-dispatch.md`

Source subsystem roadmap:

- `plans/subsystems/provider-oauth-roadmap.md#milestone-1--oauth-credential-lifecycle-and-dispatch`

Repository baseline reviewed: `ac839859` (branch `feat/provider-oauth-user-accounts`; the plan's own baseline is `5b212d2d69137b99ed39f0dbeab10331c839e54e`)

Implementation commits or pull requests:

- `a78d40be` — owner-bound provider OAuth: the credential service, proactive refresh, the operator gate, pre-dispatch token resolution, backup/restore membership, configuration validation, and the operator documentation.
- `aa2caf20` — bound the OAuth refresh cancellation test wait on an observable commit boundary instead of a fixed sleep.
- `ef619067` — isolate OAuth tests from shared temporary locks so serial and parallel-safe runs do not contend on one directory.
- `ac839859` — the M004 closure that accepted ADR-0015 and superseded this plan's no-401-replay clause.

## 1. Executive finding

M001 is implemented and verified as the infrastructure milestone it was scoped to be. One complete OAuth credential set per managed user, provider, and configured account is stored outside SQLite in the config-adjacent `.eggpool-oauth/` directory, with owner-only permissions, a hashed filename that must agree with the owner tuple inside the file, a per-identity OS lock that spans load, refresh, and rotation across processes, and atomic replacement of the whole rotated set before a new access token is returned. Backup and restore carry those files, validate the same owner binding, and clear stale files when an older archive has none.

The operator gate `[oauth].enabled` defaults to on. Disabling it rejects refresh and OAuth routing and retains the stored bytes; re-enabling uses only the bound credential. Token resolution happens before synchronous attempt preparation, and an OAuth account that fails resolution produces no provider attempt and no alternate-account selection.

The plan's original "HTTP 401 never triggers automatic refresh-and-replay" invariant is superseded by accepted ADR-0015 and implemented by M004, which this milestone's §17 amendment records. M001's closure therefore evaluates §15's replay requirement in the superseded sense: there is no cross-account, cross-user, or cross-owner replay or fallback anywhere in the path. The one same-account, same-owner refresh-and-retry is M004's and is out of this milestone's scope.

M001 adds no login surface and makes no provider-support or provider-registration claim. That condition is carried by M002 and M003, which close conditionally on it.

## 2. Requirement-to-evidence matrix

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| One complete credential set stored outside SQLite for exactly one managed user and provider account | `rust/src/oauth.rs:641` `save_sync`; `:584` `load_sync`; `:114` `credential_filename`; `rust/src/oauth.rs:1625` `credentials_are_private_atomic_and_owner_scoped` | pass | The stored document is the complete credential set, not a token column. No migration mentions OAuth and the schema is unchanged. |
| The file identity is bound to user, provider, and account | `rust/src/oauth.rs:1659` `file_identity_is_bound_to_provider_and_account_as_well_as_user`; the owner tuple check in `load_sync` | pass | A file whose internal owner tuple disagrees with its hashed name requires re-authentication instead of being adopted. |
| Owner-only permissions on directory and file | `rust/src/oauth.rs:829` (directory `0o700`), `:848` (file `0o600`); `:1625` | pass | Permissions are applied at creation and re-asserted on replacement. |
| Atomic replacement, with rollback when publication fails | `rust/src/oauth.rs:1117` `failed_directory_sync_rolls_back_the_published_credential`; `:641` | pass | A failed directory sync restores the previous file set rather than leaving a partial credential. |
| Interrupted restore recovers or fails closed | `rust/src/oauth.rs:1071` `interrupted_restore_recovers_the_previous_oauth_directory`; `:1094` `ambiguous_interrupted_restores_fail_closed_without_deleting_either_copy`; `:735` `recover_interrupted_oauth_restore` | pass | An ambiguous recovery keeps both copies and refuses to guess. |
| Proactive expiry-aware refresh publishes the complete rotated set before use | `rust/src/oauth.rs:1277` `expired_credentials_rotate_atomically_and_restart_with_new_token` | pass | The restarted reader observes the rotated set, not a partial write. |
| A refresh that returns no new refresh token preserves the current one | `rust/src/oauth.rs:1379` `refresh_without_new_refresh_token_preserves_the_current_refresh_token` | pass | Rotation omission is not treated as revocation. |
| A denied grant or a transport failure leaves the previous credentials intact and redacts the provider body | `rust/src/oauth.rs:1345` `refresh_denial_leaves_previous_credentials_and_redacts_provider_body`; `:1413` `refresh_transport_failure_keeps_previous_credentials`; `:540` `map_rejected_credential_error` | pass | The provider's error text is reduced to a typed reason. |
| Cross-process coordination yields one effective refresh per token version and rejects stale writers | `rust/src/oauth.rs:795` `lock_oauth_credential_root`; `:1686` `identity_lock_is_exclusive_across_processes`; `:1669` `cross_process_lock_helper`; `:1798` `rejected_credential_refresh_reuses_a_rotated_credential_without_refreshing` | pass | The reuse test points the token endpoint at a closed socket, so a second token request would fail the test rather than pass silently. Contention is synchronized on the helper process boundary, not on a sleep. |
| Cancellation does not abandon the lock mid-rotation or publish a partial set | `rust/src/oauth.rs:1445` `canceled_refresh_finishes_atomic_commit_before_releasing_lock` | pass | The commit task is detached from request cancellation and owns the lock until the atomic save completes. |
| Missing, invalid, or mismatched material requires re-authentication and never substitutes another user's credential | `rust/src/oauth.rs:1589` `corrupted_credentials_require_reauthentication`; `:576` `invalid_load_requires_reauthentication` | pass | No fallback path exists to select a different file or user. |
| The operator gate defaults on and disabling blocks refresh and routing while retaining bytes | `rust/src/config.rs:983` `OAuthConfig`; `:987` default `enabled: true`; `rust/src/oauth.rs:221`, `:273`, `:320`, `:354`; `rust/src/oauth.rs:1569` `disabled_oauth_rejects_refresh_without_changing_stored_credentials` | pass | The disabled test asserts the stored credential is byte-identical afterwards. `[oauth].enabled` is classified restart-required at `rust/src/config_reload_policy.rs:818`, using the existing classifier rather than a second key list. |
| Backup and restore include the OAuth files, validate owner binding, and never keep stale credentials | `rust/src/operations/backup.rs:1762` `oauth_credentials_are_backed_up_and_owner_binding_is_validated`; `:1809` `restoring_legacy_or_replaced_oauth_files_never_keeps_stale_credentials`; `:2002` `backup_recovers_an_interrupted_oauth_restore_before_collecting_files`; `:2022` `oauth_backup_read_waits_until_restore_rollback_finishes` | pass | `docs/backup-restore.md:42`, `:49`, `:86`, `:138`, `:165` document the members, the restore rule, and the manual procedure. |
| Token resolution happens before synchronous attempt preparation, and a resolution failure produces no provider attempt or fallback | `rust/src/coordinator/oauth_integration_tests.rs` `catalog_and_streaming_dispatch_use_owner_oauth_token_without_persisting_it`; the finite and streaming resolution seams described in `architecture/deep-dive-providers.md:150` | pass | Resolution yields an owned value before `PreparedUpstreamAttempt` construction, so no auth await crosses a borrowed boundary. |
| Tokens stay out of SQLite, logs, diagnostics, profiles, and public projections | The persistence assertions in `rust/src/coordinator/oauth_integration_tests.rs`; `rust/tests/provider_oauth.rs` `secret_string_debug_is_redacted`; no `oauth` match in any file under `rust/assets/db/migrations/` | pass | Redaction is asserted on the wrapped secret's `Debug` output, not only on log strings. |
| Static API-key and administrator-shared behavior is unchanged | `rust/tests/provider_oauth.rs` `oauth_is_enabled_by_default_without_changing_static_key_accounts`; `rust/src/config.rs:2504` `validate_oauth_accounts`; `rust/src/config.rs:2009` | pass | OAuth accounts are restricted to the reviewed OpenAI and xAI providers with a canonical base URL and a single Bearer header; static accounts are untouched. |
| The selected owner's account is the only account used; a non-owner and a non-owner credential are refused | `rust/src/oauth.rs:1896` `rejected_credential_refresh_rejects_a_non_owner`; `rust/src/coordinator/oauth_integration_tests.rs` `oauth_401_retry_never_falls_back_to_a_static_account` | pass | A configured static account is present in the fallback test and receives no request. |
| No cross-account, cross-user, or cross-owner replay or fallback (superseded §15 wording, per §17) | The fallback and non-owner tests above; `plans/adrs/ADR-0015-provider-oauth-owner-scoped-credential-refresh-retry.md`; §17 of the source plan | pass | The same-account, same-owner, one-shot recovery is M004's and is authorized by ADR-0015. |
| One eggfetch-core 0.4.0 version with the approved feature pair, and eggpass-auth 0.1.1 | `rust/Cargo.toml:75` (exact pin, `default-features = false`, `native-http1` + `tls-rustls`), `:79` (`eggpass-auth = "=0.1.1"`); the tree results in §4 | pass | No second Eggfetch version, no added feature, no advisory exception. |
| Focused, default, and no-default verification, strict Clippy, dependency tree, and locked release build | §4 | pass | Serial Rust tests throughout. |
| Operator and architecture documentation | `docs/backup-restore.md`; `docs/providers.md:541`; `architecture/deep-dive-providers.md:150` | pass | `docs/providers.md` states the flows are implemented and explicitly disclaims live provider validation and provider registration, which this milestone does not establish. |
| M002 and M003 integrate only through this milestone's identity and atomic-commit interface | `rust/src/oauth.rs:312` `commit_credentials_for_user`; `rust/src/server/oauth_credentials.rs:23`; `rust/src/server/oauth_login.rs:1100`; the M002 and M003 closure records | pass | Neither login surface defines a second store or owner model. |

## 3. Production implementation evidence

`rust/src/oauth.rs` is the whole credential boundary. It owns the credential-directory root derived from the config path, the hashed filename, the owner tuple inside the document, permission enforcement, the per-identity OS lock, the interrupted-restore recovery, and the three public operations: `commit_credentials_for_user`, `clear_credentials_for_user`, and `refresh_rejected_credential_for_user`. Storage sits behind a small trait with synchronous `load_sync`/`save_sync`/`clear_sync` implementations and async wrappers, so the atomic file work is testable without a runtime.

`rust/src/config.rs` owns `OAuthConfig` and the account validation that keeps an OAuth account on a reviewed provider, on its canonical origin, without a static key or key environment variable, and with a single Bearer authorization header. `rust/src/config_reload_policy.rs` classifies `oauth.enabled` as restart-required through the existing classifier.

`rust/src/coordinator/` resolves the owner's token before publication and synchronous attempt preparation, and `rust/src/coordinator/oauth_integration_tests.rs` proves that the catalog and streaming dispatch paths use the owner token without persisting it. The refresh-and-retry itself is M004's; M001 supplies the refresh primitive and the lock/version contract it uses.

`rust/src/operations/backup.rs` carries the credential files as first-class backup members, validates owner binding on restore, and clears stale files when a legacy archive has no OAuth members. `rust/src/operations/oauth_login.rs`, `rust/src/server/oauth_login.rs`, and `rust/src/server/oauth_credentials.rs` are the login surfaces and are closed separately as M002 and M003.

No database migration was added. The credential set never enters SQLite, the provider-profile assets, or an audit row.

## 4. Verification executed

### Commands run

```bash
TMPDIR=/private/tmp cargo fmt --manifest-path rust/Cargo.toml --all -- --check
TMPDIR=/private/tmp cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
TMPDIR=/private/tmp cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
TMPDIR=/private/tmp cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
TMPDIR=/private/tmp cargo test --manifest-path rust/Cargo.toml --test provider_oauth -- --test-threads=1
TMPDIR=/private/tmp cargo test --manifest-path rust/Cargo.toml --test on_prem_identity -- --test-threads=1
TMPDIR=/private/tmp cargo test --manifest-path rust/Cargo.toml --test routing_domain -- --test-threads=1
TMPDIR=/private/tmp cargo test --manifest-path rust/Cargo.toml --test coordinator_boundaries -- --test-threads=1
TMPDIR=/private/tmp cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
TMPDIR=/private/tmp cargo test --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- --test-threads=1
TMPDIR=/private/tmp cargo tree --manifest-path rust/Cargo.toml -e features -i eggfetch-core
TMPDIR=/private/tmp cargo tree --manifest-path rust/Cargo.toml --duplicates
TMPDIR=/private/tmp cargo deny --manifest-path rust/Cargo.toml check advisories
TMPDIR=/private/tmp cargo build --manifest-path rust/Cargo.toml --locked --release
git diff --check
```

### Results

All commands ran locally on this branch from the closure worktree, with `TMPDIR=/private/tmp` and serial Rust tests (`--test-threads=1`).

| Command | Result |
|---|---|
| `cargo fmt --all -- --check` | pass, no diff |
| strict Clippy, default features | pass, exit 0, no warnings |
| `cargo check --workspace --all-targets --no-default-features` | pass, exit 0 |
| strict Clippy, no-default features | pass, exit 0, no warnings |
| `--test provider_oauth` | pass, 2 passed, 0 failed |
| `--test on_prem_identity` | pass, 6 passed, 0 failed |
| `--test routing_domain` | pass, 15 passed, 0 failed |
| `--test coordinator_boundaries` | pass, 6 passed, 0 failed |
| workspace suite, default features, serial | pass, 1347 passed, 0 failed, 2 ignored |
| workspace suite, no-default features, serial | pass, 1348 passed, 0 failed, 2 ignored |
| `cargo tree -e features -i eggfetch-core` | pass: exactly one `eggfetch-core v0.4.0`; `native-http1` resolves through `advanced-routing` and is consumed by `eggpass-auth v0.1.1` |
| `cargo tree --duplicates` | 37 duplicate crate-name groups, the same 37 as the plan baseline lock; the only differences are `rand` 0.10.2→0.10.4 and `sha2` 0.11.0→0.11.1, so no new duplicated dependency was introduced |
| `cargo deny check advisories` | fail: RUSTSEC-2026-0492 and RUSTSEC-2026-0495 reach `russh 0.62.7` through Eggress `1.0.12`; pre-existing, unrelated to this milestone, which changed no dependency, and tracked as Security hardening M008 |
| `cargo build --locked --release` | pass |
| `git diff --check` | pass |
| `uv run pytest tests/tooling/` | pass, 267 passed, 1 skipped |

The duplicate-crate comparison is against `git show 5b212d2d69137b99ed39f0dbeab10331c839e54e:rust/Cargo.lock`, which is the plan's own baseline. No advisory exception was added, and no dependency was pinned or waived to make this result.

## 5. Invariant review

- **Secrets outside persistence.** The credential document lives only under `.eggpool-oauth/`. No migration, provider profile, audit row, attempt row, or diagnostic projection carries token material, and the redaction test covers `SecretString`'s `Debug` output.
- **Owner binding.** Every file is keyed by `(user_id, provider_id, account_name)`, and the internal tuple must agree with the hashed filename. A mismatch requires re-authentication rather than adoption.
- **No pooling and no fallback.** The store is per-identity, the lock is per-identity, and neither a resolution failure nor a refresh failure may select another account. The non-owner and static-account fallback tests assert this.
- **Operator gate.** `[oauth].enabled` defaults on and is checked on every refresh and commit path. Disabling retains bytes and blocks use; the disabled test asserts the file is unchanged.
- **Atomicity and cross-process safety.** The lock spans load, refresh, and the atomic replacement, and the commit is detached from request cancellation. A rotated credential another process already published is reused rather than spent again.
- **Owned resolution before submission.** The token is resolved into an owned value before synchronous attempt preparation, so no auth await crosses a borrowed boundary and no partial attempt is submitted.
- **Superseded replay clause.** §17 of the source plan records that ADR-0015 supersedes only the no-401-replay sentence. No cross-account, cross-user, or cross-owner replay exists. Static-key 401 behavior is unchanged.
- **Provider neutrality.** OAuth accounts are restricted to reviewed OpenAI and xAI configuration with the canonical origin, and the provider profiles remain secret-free and transport-policy-free.

## 6. Failure and recovery review

- **Cancellation.** A canceled refresh still completes the atomic commit before releasing the lock (`rust/src/oauth.rs:1445`). The prior set is authoritative until that commit, the new set afterwards.
- **Restart.** A restart loads only a complete, validated, owner-matched set (`rust/src/oauth.rs:1277`, `:1589`). A pending login transaction is memory-only and is destroyed, which requires the user to start again.
- **Partial persistence.** A failed directory sync rolls the published credential back (`rust/src/oauth.rs:1117`), and an interrupted restore either recovers the previous directory or fails closed while keeping both copies (`:1071`, `:1094`).
- **Contention.** Cross-process contenders serialize on the per-identity OS lock, and a stale writer cannot replace a newer token version (`:1686`, `:1798`). The test bounds are observable process boundaries, not fixed sleeps.
- **Malformed or unauthorized input.** Corrupted files require re-authentication (`:1589`), a non-owner is refused before any file is touched (`:1896`), and an unrecognized account or unauthorized principal never reaches a token request.
- **Duplicate delivery.** A refresh that another process already performed is detected by comparing the rejected token, so a rotating refresh token is not spent twice (`:1798`).

## 7. Migration and compatibility review

No schema migration was added; the migration set still ends at v68 and no migration mentions OAuth. `[oauth].enabled` is a new restart-required configuration key with a serde default of `true`, so an existing configuration that omits the section keeps OAuth enabled without a rewrite. Existing static-key and administrator-shared accounts are unchanged, and OAuth accounts are validated into the reviewed OpenAI and xAI shape rather than a new account kind.

The backup archive gains `.eggpool-oauth/` members. An older archive that has none clears any current OAuth files on restore instead of leaving a credential that the archive does not describe (`rust/src/operations/backup.rs:1809`; `docs/backup-restore.md:138`). Rollback limits are unchanged: restoring an older archive removes OAuth credentials and requires re-authentication.

## 8. Security review

- **Authentication and authorization.** Every operation is reached through an authenticated managed user or the runtime's own account selection. A non-owner is refused before the credential directory is touched, and the owner is never taken from request data.
- **Secret handling.** Tokens exist in the credential document, in memory, and in the outbound Authorization header only. Errors map to typed reasons, and provider error bodies are redacted.
- **Privilege bounds.** The store cannot be repointed at another user's directory or account; the owner tuple and the hashed filename must agree.
- **DoS bounds.** Refresh is bounded and single-flight per identity; there is no unbounded loop, timer, or background task introduced by this milestone.
- **Audit behavior.** The failure paths log provider id, account name, and a typed reason without token material or raw provider bodies.
- **Dependency surface.** The exact `eggfetch-core =0.4.0` pin keeps the minimal `native-http1` + `tls-rustls` pair and adds no advisory exception.

## 9. Documentation and operations

`docs/backup-restore.md` documents the credential directory, its backup membership, the restore rule, the stale-member clearing, and the manual restore procedure. `docs/providers.md:541` documents the per-user OAuth account model, the operator gate, the retention behavior, and the explicit disclaimer that implemented flows are not a live provider-validation or registration claim. `architecture/deep-dive-providers.md:150` documents the credential service, the lock and atomic-rotation contract, the pre-dispatch resolution boundary, and the ADR-0015 recovery as of this closure; the previously stale "401 is terminal" sentence is corrected in the same commit as this record. No operator command or diagnostic was added.

## 10. Unresolved findings

| Severity | Finding | Impact | Required action |
|---|---|---|---|
| medium | Exact xAI and OpenAI provider registration and live provider behavior are unverified; the credential and refresh paths are proven against local stubs and fixture providers only | A live provider may answer a refresh, a denial, or a re-authentication differently from the fixture | Verify against the real providers during M002 and M003 closure. This milestone makes no provider-support claim and needs no further code change. |
| medium | `RUSTSEC-2026-0492` and `RUSTSEC-2026-0495` reach `russh 0.62.7` through Eggress `1.0.12`, so the repository dependency-audit job is red | The repository-wide audit gate fails independently of this milestone | Tracked as Security hardening M008, which requires an Eggress release permitting `russh >=0.65`, an accepted ADR for a patched `0.62.x`, or removal of the default `ssh` feature. No advisory exception was added here. |
| low | `rust/tests/provider_oauth.rs` is a 14-line contract target, while the substantive coverage lives in the unit-test modules of `rust/src/oauth.rs`, `rust/src/server/`, `rust/src/operations/`, and `rust/src/coordinator/` | A reader may expect the OAuth suite in the integration target and under-count the coverage | Cite the in-module tests directly in later closure records, as this record does. Not a defect in the milestone. |

## 11. Roadmap disposition

Milestone closed; M002 and M003 may proceed to their own closure. This milestone supplies the identity and atomic-commit interface both login surfaces use, and neither defines a second store or owner model. No other registered plan depends on M001, so nothing is promoted, and the blocked-work table gains no Provider OAuth entry. M002 and M003 remain gated on exact provider registration and close conditionally on it; that condition is recorded in their own closure records, not deferred here.

## 12. Registry updates

Applied in the same commit as this record:

- `plans/registry.md`: the M001 row leaves the active implementation-plans table and is added to the recently-closed table with this record as evidence; the Provider OAuth roadmap row records M001 as closed; a dated closure-audit paragraph records the closure and the blocked-work audit.
- `plans/implementation/provider-oauth/001-oauth-credential-lifecycle-and-dispatch.md`: the plan header moves from `active` to `implemented`, and §17 records the ADR-0015 amendment.
- `plans/subsystems/provider-oauth-roadmap.md`: the M001 row in the milestone-status table moves from `active` to `closed` and links this record; an append-only closure note records the closure without rewriting the historical milestone text.

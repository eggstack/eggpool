# Provider OAuth Milestone M002 — Closure Status

Status: conditionally closed

Source implementation plan:

- `plans/implementation/provider-oauth/002-authenticated-web-ui-provider-login.md`

Source subsystem roadmap:

- `plans/subsystems/provider-oauth-roadmap.md#milestone-2--authenticated-web-ui-provider-login`

Repository baseline reviewed: `ac839859` (branch `feat/provider-oauth-user-accounts`; the plan's own baseline is `5b212d2d69137b99ed39f0dbeab10331c839e54e`)

Implementation commits or pull requests:

- `a78d40be` — the authenticated xAI Web UI device-code flow, its routes and flow registry, the operator gate, the owner-bound commit through M001, and the account-page surface.
- `ef619067` — OAuth test isolation from shared temporary locks.
- `ac839859` — the M004 closure that accepted ADR-0015 and superseded this plan's no-401-replay wording.

## 1. Executive finding

The authenticated xAI Web UI device-code flow is implemented and verified on local fixtures. A signed-in managed user starts a device authorization from their own account page, sees only the validated standard verification URI and user code, polls through pending, `slow_down`, success, denial, expiry, and cancellation, and commits the resulting credential through M001's owner-bound atomic store. The start, poll, cancel, and commit mutations require the existing browser session and CSRF checks; the flow is bound to the exact session and managed user that started it, and revoking the xAI grant discards credentials staged for commit.

ADR-0014 narrowed this milestone to xAI device-code only. The OpenAI Web UI authorization-code/PKCE callback, the `/oauth/openai/callback` route, the `/api/user/oauth/openai/*` actions, and the `[oauth].callback_base_url` setting are absent, and a guard test asserts their absence while asserting the xAI device routes remain registered. Desktop OpenAI and xAI loopback PKCE remain M003.

The milestone closes **conditionally**. Local stubs cannot establish that xAI grants this client the device-code authorization, that the verification hosts are the live ones, or that a live device-code exchange and refresh behave as the fixtures do. The plan's §15 requires provider-registration evidence separate from the design approval, and that evidence is not obtainable in this environment. The condition, its risk, and the exact future evidence are recorded in §11.

## 2. Requirement-to-evidence matrix

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| xAI device-code start, poll, cancel, commit, and status are registered on the existing listener | `rust/src/server/mod.rs:1332` start, `:1338` poll, `:1344` cancel, `:1350` commit, `:1355` status; handlers at `rust/src/server/oauth_login.rs:950`, `:1000`, `:1081`, `:1100`, `:1044` | pass | No second socket or listener is bound; the routes are mounted on the existing Axum router. |
| The OpenAI Web UI surface is absent and the xAI surface remains | `rust/tests/on_prem_admin_ui.rs:1119` `openai_web_oauth_routes_are_absent_and_xai_device_routes_remain` | pass | ADR-0014 scope is asserted by a guard test, not only by documentation. |
| No OpenAI callback-origin configuration was introduced | `rust/src/config.rs:983` `OAuthConfig` carries only `enabled` | pass | The removed `callback_base_url` has no residual key, so no configuration can imply the withdrawn flow. |
| The flow binds to the initiating managed user, never to request data | `rust/src/server/oauth_login.rs:857` `xai_flow_authorized`; `:848` `account_available`; `:1574` `flow_is_bound_to_exact_session_and_managed_user` | pass | Start takes an account selection that is re-authorized against the live personal-upstream grant and owner snapshot. |
| Start, poll, cancel, and commit require the session and CSRF token | `rust/src/server/oauth_login.rs:1844` `csrf_requires_the_session_token_and_matching_cookie`; the existing browser-session middleware | pass | The callback GET exception does not exist in this milestone because ADR-0014 removed the callback. |
| The verification URL is validated before presentation | `rust/src/server/oauth_login.rs:751` `trusted_xai_verification_uri` (HTTPS plus `accounts.x.ai` or `auth.x.ai`); enforced at `:123` and `:356`; bounded at `:130` | pass | An untrusted URL maps to `UntrustedVerificationUrl` instead of being displayed. |
| Presentation exposes only the standard verification URI and the user code | `rust/src/server/oauth_login.rs:1467` `presentation_contains_only_the_standard_verification_uri_and_user_code` | pass | The device code itself is never part of the rendered presentation. |
| Polling honors pending, `slow_down`, success, denial, expiry, and cancellation as bounded statuses | `rust/src/server/oauth_login.rs:1493` `poll_exposes_pending_slow_down_and_cancel_without_leaking_flow_identity`; `:1523` `poll_completion_denial_and_expiry_are_bounded_statuses` | pass | Status output is a bounded enum shape, not a provider body. |
| Successful exchange stages credentials until an owner-bound commit claim | `rust/src/server/oauth_login.rs:1545` `success_stages_credentials_until_the_owner_bound_commit_claim`; `:1100` `commit` | pass | The staged set is released only to the same session and user that started the flow. |
| Revoking the xAI grant discards credentials staged for commit | `rust/src/server/oauth_login.rs:1612` `revoking_xai_grant_discards_credentials_staged_for_commit`; `:867` `watch_xai_flow_authorization` | pass | A grant revoked between start and completion cannot publish a credential. |
| The operator gate is checked before start, before exchange, and at commit | `rust/src/server/oauth_login.rs:811` `oauth_disabled`; `:1198`; `:1809` `operator_gate_and_owner_binding_are_required_for_xai_account_selection` | pass | Disabled OAuth is a bounded `403 oauth_disabled`, and the account page states the disabled state (`:1309`). |
| Pending device state is memory-only and destroyed by restart or session loss | `rust/src/server/oauth_login.rs:273` `FlowRegistry`; `rust/src/server/mod.rs:492` constructs it on `AppState` | pass | There is no persistence path for flow state, so a restart cannot resurrect a pending transaction. |
| No device code, verification code, token, or provider body reaches logs, diagnostics, or the database | The redaction assertions in `:1493`; ``OAuthAccountError`` mapping; no OAuth migration | pass | Status and error surfaces carry typed reasons only. |
| A failed or canceled re-login preserves the prior credential | `rust/src/server/oauth_login.rs:1100` commit path; M001's `rust/src/oauth.rs:312` `commit_credentials_for_user` | pass | Nothing is published unless the full staged set reaches M001's atomic commit. |
| The flow uses the reviewed xAI profile and the existing transport | `rust/src/server/oauth_login.rs` Start/Poll through eggpass-auth with the account's existing provider client | pass | No proxy, timeout, retry, or TLS policy is added by this milestone. |
| Documentation states the implemented flow and disclaims provider support | `docs/providers.md:541` | pass | The section separates Web UI xAI device-code from desktop loopback and disclaims live validation and registration. |
| Exact xAI device-code provider registration and live behavior | Not run | not run | Local fixtures cannot establish that xAI grants the device flow, that the verification hosts are live, or that a live exchange and refresh match the stub. This is the closure condition in §11. |
| Focused, default, and no-default verification, strict Clippy, dependency tree, and locked release build | §4 | pass | Serial Rust tests throughout. |

## 3. Production implementation evidence

`rust/src/server/oauth_login.rs` owns the flow. `FlowRegistry` (`:273`) is bounded in-memory state stored on `AppState`, keyed by session and managed user. The handlers start a device authorization through the library, validate the returned verification URI against the reviewed xAI hosts, record the polling interval, and expose only a bounded view. The commit handler rechecks the operator gate, the live session, the same managed user, and the personal-upstream grant before calling `commit_credentials_for_user`.

`rust/src/server/mod.rs` mounts the four mutations and the status read on the existing router (`:1332`–`:1355`) under the ordinary body limit, so the flow adds routes but no listener. `rust/src/server/oauth_login.rs:1209` `account_ui` renders the account surface, including the disabled state.

The milestone adds no configuration key and no migration. The device flow's protocol primitives come from `eggpass-auth =0.1.1`; EggPool owns the session binding, the gate, the presentation validation, and the commit.

## 4. Verification executed

### Commands run

```bash
TMPDIR=/private/tmp cargo fmt --manifest-path rust/Cargo.toml --all -- --check
TMPDIR=/private/tmp cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
TMPDIR=/private/tmp cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
TMPDIR=/private/tmp cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
TMPDIR=/private/tmp cargo test --manifest-path rust/Cargo.toml --test provider_oauth -- --test-threads=1
TMPDIR=/private/tmp cargo test --manifest-path rust/Cargo.toml --test on_prem_admin_ui -- --test-threads=1
TMPDIR=/private/tmp cargo test --manifest-path rust/Cargo.toml --test on_prem_sessions -- --test-threads=1
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
| `--test on_prem_admin_ui` | pass, 16 passed, 0 failed |
| `--test on_prem_sessions` | pass, 13 passed, 0 failed |
| workspace suite, default features, serial | pass, 1347 passed, 0 failed, 2 ignored |
| workspace suite, no-default features, serial | pass, 1348 passed, 0 failed, 2 ignored |
| `cargo tree -e features -i eggfetch-core` | pass: exactly one `eggfetch-core v0.4.0`, consumed by `eggpass-auth v0.1.1` |
| `cargo tree --duplicates` | 37 duplicate crate-name groups, the same 37 as the plan baseline lock; only `rand` and `sha2` patch versions moved, so no new duplicated dependency |
| `cargo deny check advisories` | fail: RUSTSEC-2026-0492 and RUSTSEC-2026-0495 reach `russh 0.62.7` through Eggress `1.0.12`; pre-existing and unrelated, tracked as Security hardening M008 |
| `cargo build --locked --release` | pass |
| `git diff --check` | pass |

The device-flow tests are local fixtures. They prove the state machine, the session binding, the CSRF gate, the operator gate, and the owner-bound commit; they do not prove that xAI grants this client the device-code authorization.

## 5. Invariant review

- **Owner binding.** The started account is re-authorized against the live session's managed user and personal-upstream grant, and the commit derives the owner from the authenticated principal. No username or user id is accepted as authority.
- **Session and CSRF.** All four mutations require the existing browser session and CSRF token. ADR-0014 removed the callback GET exception, so no unauthenticated entry point exists.
- **Gate.** `[oauth].enabled = false` refuses start, exchange, and commit with a bounded error and preserves stored credentials.
- **Transient state.** Pending flows are memory-only and bounded; restart or session loss destroys them and the user starts again.
- **Previous-credential preservation.** A failed, denied, expired, canceled, or revoked flow never reaches M001's commit, so the prior credential stays authoritative.
- **No pooling and no fallback.** The flow can only commit the initiating user's own account.
- **Verification URL trust.** Only HTTPS URIs on `accounts.x.ai` or `auth.x.ai` are presented, and the length is bounded.
- **Superseded replay clause.** §18 of the source plan records that ADR-0015 supersedes only the no-401-replay wording; the login flow itself is unchanged.
- **Provider neutrality.** The flow is xAI-only and adds no other provider, grant, callback, or transport policy.

## 6. Failure and recovery review

- **Cancellation.** A canceled poll or explicit cancel clears the pending entry and discards staged credentials; nothing is published.
- **Restart.** Because `FlowRegistry` is `AppState` memory, a restart destroys the transaction. No partial state is loaded.
- **Session loss.** `session_is_current` (`rust/src/server/oauth_login.rs:889`) fails the commit after a session change, and the staged set is dropped.
- **Grant revocation.** The watcher (`:867`) discards staged credentials when the underlying xAI grant is revoked before commit.
- **Provider denial and expiry.** Both are bounded statuses; neither mutates the stored credential.
- **Malformed input.** Body size is capped by the router's per-route limit, the verification URI is validated, and only the flow's own status shape is returned.

## 7. Migration and compatibility review

No database migration and no configuration key were added by this milestone. Four additive authenticated routes and one read route appear on the existing listener; the public inference API is unchanged. ADR-0014 removed the OpenAI Web UI scope before implementation, so the plan's historical OpenAI sections describe work that is not present and is not claimed; §17 of the plan records that narrowing. Static-key and administrator-shared accounts are unchanged, and OAuth account validation remains M001's.

## 8. Security review

- **Authentication and authorization.** Every mutation requires the live session plus CSRF; the flow is bound to the session and managed user, and the account is re-authorized against the live grant.
- **Secret handling.** The device code stays server-side, the presentation carries only the standard URI and user code, and status/error surfaces carry typed reasons. No provider body or token is logged or persisted.
- **Privilege bounds.** The flow can commit only the initiating user's own account through M001's owner-checked store.
- **DoS bounds.** Pending flows are bounded in-memory entries, the request body is capped per route, and the verification URI is length-bounded.
- **Audit behavior.** The gate-denied and failure paths emit bounded status codes without secret material.

## 9. Documentation and operations

`docs/providers.md:541` documents the xAI Web UI device-code flow, the desktop loopback flows, the operator gate, the retention rule, and the explicit disclaimer that local tests do not establish provider registration, live validation, or provider terms. The account page states the operator-disabled condition. No new operator command or configuration key exists.

## 10. Unresolved findings

| Severity | Finding | Impact | Required action |
|---|---|---|---|
| medium | Exact xAI device-code client registration and live device-flow behavior are unverified | A live xAI tenant may not grant the device flow to this client, or may return a verification host or polling behavior the stub does not model | Capture redacted first-party registration evidence and one live device-code run before promoting this milestone from `conditionally closed` to `closed`. See §11. |
| medium | `RUSTSEC-2026-0492` and `RUSTSEC-2026-0495` reach `russh 0.62.7` through Eggress `1.0.12`, so the repository dependency-audit job is red | The repository-wide audit gate fails independently of this milestone | Tracked as Security hardening M008. No advisory exception was added here. |
| low | The plan body still describes the withdrawn OpenAI Web UI callback and `[oauth].callback_base_url` | A reader who skips §17 could believe the callback is in scope | The §17 ADR-0014 amendment is authoritative; the historical text is retained deliberately and is not rewritten. |

## 11. Roadmap disposition

Conditionally closed. The implemented capability is the authenticated xAI Web UI device-code flow committing through M001's owner-bound store.

- **Condition.** Exact xAI device-code client registration plus live device-flow evidence.
- **Risk while open.** A live tenant may refuse the device grant, present a different verification host, or behave differently on `slow_down`, expiry, or denial. The worst case is a login that cannot complete; no credential is misdirected or leaked, because every failure path discards the staged set and preserves the prior credential.
- **Exact future evidence.** A redacted client-registration record showing the device-code grant enabled for this client, plus a recorded live start/poll/commit against xAI that ends in a usable owner-bound credential and one successful inference. No client secret, device code, or token may appear in that evidence.

M003 is independent of this condition. No other registered plan depends on M002, so nothing is promoted, and the blocked-work table gains no Provider OAuth entry.

## 12. Registry updates

Applied in the same commit as this record:

- `plans/registry.md`: the M002 row leaves the active implementation-plans table and is added to the recently-closed table with this record as evidence; the Provider OAuth roadmap row records M002 as conditionally closed; a dated closure-audit paragraph records the disposition and the blocked-work audit.
- `plans/implementation/provider-oauth/002-authenticated-web-ui-provider-login.md`: the plan header moves from `active` to `implemented`, and §18 records the ADR-0015 amendment.
- `plans/subsystems/provider-oauth-roadmap.md`: the M002 row in the milestone-status table moves from `active` to `conditionally closed` with this record linked; an append-only closure note records the condition without rewriting the historical milestone text.

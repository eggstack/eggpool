# Provider OAuth Milestone M003 — Closure Status

Status: conditionally closed

Source implementation plan:

- `plans/implementation/provider-oauth/003-desktop-loopback-browser-login.md`

Source subsystem roadmap:

- `plans/subsystems/provider-oauth-roadmap.md#milestone-3--desktop-cli-loopback-pkce-login`

Repository baseline reviewed: `ac839859` (branch `feat/provider-oauth-user-accounts`; the plan's own baseline is `5b212d2d69137b99ed39f0dbeab10331c839e54e`)

Implementation commits or pull requests:

- `a78d40be` — the desktop loopback PKCE flow, the `oauth_credentials_write` scope, `POST /api/user/oauth-credentials` and its hint route, the Web UI issuance route, and the CLI command.
- `ef619067` — OAuth test isolation from shared temporary locks.
- `ac839859` — the M004 closure that accepted ADR-0015 and superseded this plan's no-401-replay wording.

## 1. Executive finding

The desktop CLI login is implemented and verified on local fixtures. `eggpool accounts oauth-login --provider {openai,xai} --account ACCOUNT [--token-stdin]` binds an ephemeral callback listener to `127.0.0.1:0`, builds a PKCE S256 authorization transaction with state and nonce, opens the reviewed provider authorization host, accepts exactly one matching loopback callback, exchanges the code, and posts the resulting credential set to `POST /api/user/oauth-credentials`.

Ownership is not caller-selected. The CLI authenticates with a self-only `epk1_` credential that carries only `oauth_credentials_write`, issued to the signed-in managed user through the authenticated Web UI and read from a hidden TTY prompt or stdin. The upload route requires that single scope, derives the owner only from the verified `AuthenticatedPrincipal.user_id`, accepts no owner field, and delegates to M001's atomic commit. The provider callback never reaches EggPool's public listener.

The milestone closes **conditionally**. Local stubs cannot establish that xAI and OpenAI register this client for the exact ephemeral loopback redirect with PKCE, and the plan states that unverified registration blocks closure. That external evidence is not obtainable in this environment; the condition, risk, and exact future evidence are recorded in §11.

## 2. Requirement-to-evidence matrix

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| A desktop CLI operation performs xAI and OpenAI authorization-code/PKCE | `rust/src/cli.rs:507` `oauth-login`; `rust/src/operations/oauth_login.rs:121` `login_provider` | pass | Provider selection is limited to the two reviewed providers. |
| The listener binds only `127.0.0.1` on an OS-assigned port | `rust/src/operations/oauth_login.rs:187` `TcpListener::bind(("127.0.0.1", 0))`; `:1530` `loopback_api_url_requires_local_server_bind_configuration` | pass | There is no wildcard or LAN bind path. |
| The listener closes on success, error, timeout, cancellation, and shutdown | `rust/src/operations/oauth_login.rs:76`, `:86`, `:88` typed terminal errors; `:1254` `ctrl_c_after_callback_cancels_exchange_before_upload`; `:1278` `ctrl_c_during_upload_drops_pending_commit_work` | pass | Cancellation is synchronized on the callback and upload boundaries, not on fixed sleeps. |
| PKCE S256, state, and nonce come from the approved transaction and the callback is validated exactly once | `:1425` `provider_stub_completes_pkce_exchange_with_matching_state`; `:1051` `callback_parser_accepts_only_exact_loopback_host_path_and_unique_query` | pass | The stub fails the exchange unless the verifier matches the challenge and the state matches. |
| The callback negative matrix is rejected before exchange | `:1132` documented success and denial parameters; `:1153` unknown and percent-encoded unknown parameters; `:1174` browser CRLF request; `:1215` malformed encoding, duplicates, and unknown keys | pass | Wrong host, path, port, userinfo, fragment, duplicate, and replayed callbacks never reach a token exchange. |
| The authorization host is validated before the browser is opened | `:983` `authorization_url_is_limited_to_the_openai_https_endpoint`; `:1003` `xai_authorization_url_is_limited_to_the_xai_https_host`; `:1234` presentation fallback order | pass | A failed browser open falls back to a safe manual presentation of the validated URL. |
| The OpenAI client identity is stable, private, and not silently replaced | `:1304` `openai_authorization_url_contains_the_stable_host_identifier`; `:1325` stable, canonical, private | pass | A malformed identity is refused (`:1372`) and symlinked or insecure files are rejected (`:1397`), so concurrent initializations converge (`:1356`). |
| The EggPool credential is self-only and carries only `oauth_credentials_write` | `rust/src/identity.rs:389` `OAuthCredentialsWrite`; `:392` `OAUTH_CREDENTIALS_WRITE_SCOPE`; `:410` route mapping; `rust/src/server/oauth_credentials.rs:26` requires exactly one scope; `:236` `upload_requires_the_single_self_only_agent_scope` | pass | A token with an added `inference` scope is refused, as is an unrelated token. |
| The write credential is issued only through the authenticated Web UI mutation | `rust/src/server/mod.rs:1382` `/account/tokens/oauth` → `rust/src/server/dashboard/admin.rs:1232` `create_oauth_credentials_token` | pass | The route sits in the authenticated browser-mutation group under the existing session and CSRF gate. |
| The CLI reads the credential only from a hidden TTY prompt or stdin | `rust/src/operations/oauth_login.rs:120`, `:126` `interactive_tty`, `:212` presentation, `:399`; `docs/cli-reference.md:31` | pass | The command line offers a `--token-stdin` mode selector; the credential value itself is never taken from an argument or environment variable. |
| The upload route derives ownership only from the verified principal and accepts no owner | `rust/src/server/oauth_credentials.rs:23` `principal_user_id`; `:37` `upload`; `:203` `upload_payload_has_no_owner_authority`; `:216` hint request; `:1485` `successful_cli_upload_posts_credentials_to_owner_bound_route` | pass | No username, user id, provider-owner, or admin-default field can select another owner. |
| The upload delegates to M001's atomic commit and keeps the selected provider | `rust/src/server/oauth_credentials.rs:37`; `rust/src/oauth.rs:312` `commit_credentials_for_user`; `:1028` `credential_upload_keeps_the_selected_provider_id` | pass | There is no second store or file format. |
| Returning-account hints expose no credential material | `rust/src/operations/oauth_login.rs:973` `returning_account_hint_contains_no_credentials_or_id_token`; `rust/src/server/oauth_credentials.rs:224` hint response carries only the client id | pass | The hint is an account-selection aid, not a credential. |
| A failed, canceled, or disabled login preserves the prior credential | `rust/src/operations/oauth_login.rs:1254`, `:1278`; M001's gate and atomic commit | pass | Nothing is published unless the full exchange and upload succeed. |
| EggPool's listener receives no provider callback | `rust/tests/on_prem_admin_ui.rs:1119` asserts the OpenAI web route is absent; the callback is owned by the CLI loopback listener | pass | ADR-0014 narrowed M002 so no server callback exists for either provider. |
| Operator disablement blocks login and commit because they route through the gated store | `rust/src/oauth.rs:221`, `:273`; `plans/closure/provider-oauth/001-status.md` | pass | The gate is authoritative in M001, which both the login and the upload call. |
| Documentation covers the command, the same-machine limit, and the credential handling | `docs/cli-reference.md:31`, `:40`; `docs/providers.md:541` | pass | The remote-browser limitation and the absence of live provider validation are stated. |
| Exact xAI and OpenAI loopback redirect and PKCE client registration, and live behavior | Not run | not run | Local stubs cannot establish that either provider registers the ephemeral loopback redirect. This is the closure condition in §11. |
| Focused, default, and no-default verification, strict Clippy, dependency tree, and locked release build | §4 | pass | Serial Rust tests throughout. |

## 3. Production implementation evidence

`rust/src/operations/oauth_login.rs` owns the desktop transaction: listener creation, the PKCE transaction, callback parsing and validation, browser presentation with a safe manual fallback, code exchange, and the upload. `rust/src/cli.rs:507` exposes it as `accounts oauth-login`.

`rust/src/identity.rs` owns the new scope. `OAUTH_CREDENTIALS_WRITE_SCOPE` is the single scope for the two credential routes, and `required_agent_scope` maps exactly `/api/user/oauth-credentials` and its hint route to it, so an inference-scoped token cannot reach them.

`rust/src/server/oauth_credentials.rs` owns the route: it accepts only a principal whose scope set is exactly that one scope, takes the owner from the principal, and forwards the provider credential set to `commit_credentials_for_user`. `rust/src/server/dashboard/admin.rs:1232` issues the self-only token through the authenticated Web UI.

No migration and no configuration key were added. The milestone adds one scope and two authenticated routes plus the CLI command.

## 4. Verification executed

### Commands run

```bash
TMPDIR=/private/tmp cargo fmt --manifest-path rust/Cargo.toml --all -- --check
TMPDIR=/private/tmp cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
TMPDIR=/private/tmp cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
TMPDIR=/private/tmp cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
TMPDIR=/private/tmp cargo test --manifest-path rust/Cargo.toml --test provider_oauth -- --test-threads=1
TMPDIR=/private/tmp cargo test --manifest-path rust/Cargo.toml --test cli_contract -- --test-threads=1
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
| `--test cli_contract` | pass, 7 passed, 0 failed |
| `--test coordinator_boundaries` | pass, 6 passed, 0 failed |
| workspace suite, default features, serial | pass, 1347 passed, 0 failed, 2 ignored |
| workspace suite, no-default features, serial | pass, 1348 passed, 0 failed, 2 ignored |
| `cargo tree -e features -i eggfetch-core` | pass: exactly one `eggfetch-core v0.4.0`, consumed by `eggpass-auth v0.1.1` |
| `cargo tree --duplicates` | 37 duplicate crate-name groups, the same 37 as the plan baseline lock; only `rand` and `sha2` patch versions moved, so no new duplicated dependency |
| `cargo deny check advisories` | fail: RUSTSEC-2026-0492 and RUSTSEC-2026-0495 reach `russh 0.62.7` through Eggress `1.0.12`; pre-existing and unrelated, tracked as Security hardening M008 |
| `cargo build --locked --release` | pass |
| `git diff --check` | pass |

The callback, PKCE, and upload tests use local provider stubs and a loopback socket. They prove the transaction, the callback validation, the scope isolation, and the owner-bound upload; they do not prove that xAI or OpenAI registers this client for the ephemeral loopback redirect.

## 5. Invariant review

- **Loopback only.** The listener binds `127.0.0.1:0`; no wildcard, LAN, service-port, or public callback exists. The API URL additionally requires a local server bind configuration (`:1530`).
- **Bounded lifetime.** The listener exists only for the transaction and is dropped on every terminal path, including Ctrl-C after the callback and during upload.
- **PKCE and state.** The transaction uses PKCE S256, random state, and nonce from the approved primitives, and the callback must match exactly once.
- **Owner binding.** The upload route requires the single self-only scope and takes the owner only from the verified principal. No caller-selected owner exists in the payload or the route.
- **Secret entry.** The EggPool credential is read from a hidden TTY prompt or stdin; the CLI argument selects the mode, not the value.
- **Atomic commit.** M001's store is the only writer, so a failed or canceled login leaves the prior credential intact.
- **No server callback.** EggPool's listener carries no provider callback for either provider.
- **Superseded replay clause.** §18 of the source plan records that ADR-0015 supersedes only the no-401-replay wording; the CLI flow is unchanged.
- **Provider neutrality.** Only the reviewed xAI and OpenAI authorization hosts and callback rules are accepted.

## 6. Failure and recovery review

- **Cancellation.** Ctrl-C before upload cancels the exchange; Ctrl-C during upload drops the pending commit. Neither publishes a credential.
- **Timeout and malformed callback.** The callback read is deadline- and size-bounded, and a malformed or unexpected request is rejected before exchange.
- **Restart.** The transaction and listener are process-local; a restart destroys them and the user starts again.
- **Session loss.** The upload is authenticated by the self-only EggPool credential; a missing, revoked, expired, or broader-scoped credential is refused.
- **Stale transaction.** M001's version and lock contract prevents a stale completion from overwriting a newer login.
- **Browser-open failure.** The presentation falls back to a safe manual URL and never to an insecure one.

## 7. Migration and compatibility review

No database migration and no configuration key were added. The milestone adds one `epk1_` scope and two additive authenticated routes, plus the CLI command. The public inference API is unchanged, and a token carrying `inference` cannot use the credential routes. Static-key and administrator-shared accounts are unchanged, and the credential file format remains M001's.

## 8. Security review

- **Scope isolation.** The route requires the scope set to be exactly `oauth_credentials_write`; an extra scope is refused (`rust/src/server/oauth_credentials.rs:26`, `:236`).
- **Authentication.** The write credential comes from the authenticated Web UI and is presented to the API listener as a normal bearer token; the provider callback is loopback-only.
- **Secret handling.** The credential is entered through a hidden prompt or stdin, the callback query is never logged, and the hint and status surfaces carry bounded non-secret values.
- **Privilege bounds.** The route cannot select an owner, cannot import a raw token, and cannot bypass M001's gate or owner checks.
- **DoS bounds.** The callback read is bounded in bytes and time, and the API routes carry their own body limit.
- **Audit behavior.** Failures map to typed status codes without callback or token material.

## 9. Documentation and operations

`docs/cli-reference.md:31` lists the command and its credential modes, and `:40` records the same-machine loopback requirement, the Web UI issuance path, the no-argv/no-environment rule, the previous-credential preservation, and the explicit statement that local tests do not verify provider registration, live behavior, or provider terms. `docs/providers.md:541` describes the desktop loopback flow and the self-only credential. No operator command or diagnostic beyond the CLI operation was added.

## 10. Unresolved findings

| Severity | Finding | Impact | Required action |
|---|---|---|---|
| medium | Exact xAI and OpenAI loopback redirect and PKCE client registration and live behavior are unverified | A live provider may refuse the ephemeral loopback redirect, or behave differently on denial, expiry, or refresh | Capture redacted first-party registration evidence and one live loopback login per provider before promoting this milestone from `conditionally closed` to `closed`. See §11. |
| medium | `RUSTSEC-2026-0492` and `RUSTSEC-2026-0495` reach `russh 0.62.7` through Eggress `1.0.12`, so the repository dependency-audit job is red | The repository-wide audit gate fails independently of this milestone | Tracked as Security hardening M008. No advisory exception was added here. |
| low | The plan body still names the withdrawn OpenAI Web UI callback in its historical sections | A reader who skips §17 or §18 could believe a server callback is in scope | The amendments are authoritative; the historical text is retained deliberately and is not rewritten. |

## 11. Roadmap disposition

Conditionally closed. The implemented capability is the desktop loopback PKCE login with the self-only `oauth_credentials_write` credential and the owner-bound upload route.

- **Condition.** Exact xAI and OpenAI client registration for the ephemeral loopback redirect with PKCE S256, plus live loopback login evidence.
- **Risk while open.** A live provider may reject the redirect pattern or the client. The worst case is a login that cannot complete; ownership, scope isolation, and previous-credential preservation do not depend on provider behavior, and no credential can be misdirected.
- **Exact future evidence.** A redacted registration record for each provider showing the loopback redirect and PKCE authorization-code grant, plus a recorded live `accounts oauth-login` completion per provider that produces a usable owner-bound credential and one successful inference. No client secret, authorization code, verifier, or token may appear in that evidence.

M002 is independent of this condition. No other registered plan depends on M003, so nothing is promoted, and the blocked-work table gains no Provider OAuth entry.

## 12. Registry updates

Applied in the same commit as this record:

- `plans/registry.md`: the M003 row leaves the active implementation-plans table and is added to the recently-closed table with this record as evidence; the Provider OAuth roadmap row records M003 as conditionally closed; a dated closure-audit paragraph records the disposition and the blocked-work audit.
- `plans/implementation/provider-oauth/003-desktop-loopback-browser-login.md`: the plan header moves from `active` to `implemented`, and §18 records the ADR-0015 amendment.
- `plans/subsystems/provider-oauth-roadmap.md`: the M003 row in the milestone-status table moves from `active` to `conditionally closed` with this record linked; an append-only closure note records the condition without rewriting the historical milestone text.

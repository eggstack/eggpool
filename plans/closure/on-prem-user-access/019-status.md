# On-Prem User Access M019 — Closure Status

Status: closed

Source implementation plan:

- `plans/implementation/on-prem-user-access/019-login-transport-solo-lan-corrective.md`

Source subsystem roadmap:

- `plans/subsystems/on-prem-user-access-roadmap.md#m019--login-transport-and-solo-lan-operability`

Repository baseline reviewed: post-merge `88f58bbf`; implementation candidate
`b18a561` after M018 closure and M019 activation.

## 1. Executive finding

The login page now uses the same transport policy as login submission. Remote
plaintext HTTP receives a static 403 explanation with no username/password
controls and no database access. Loopback HTTP and HTTPS connections arriving
from a specifically configured proxy can show the form. Forwarded scheme
headers from other peers, duplicate/chained values, and malformed values do
not grant transport trust. Solo operators have an SSH loopback-tunnel path;
team browser administration uses an operator-managed HTTPS proxy or VPN.

## 2. Requirement-to-evidence matrix

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| Unsafe remote GET never solicits credentials | `login_get_never_solicits_credentials_on_untrusted_remote_http`; `login_get_fails_closed_for_missing_peer_and_ambiguous_proxy_scheme` | pass | Asserts forbidden response and absence of password/username fields. |
| Trust derives from connection and configured proxy, not client headers | `forwarded_https_is_accepted_only_from_the_configured_proxy_socket`; login GET transport tests | pass | Spoofed headers at an untrusted peer, duplicate scheme, malformed scheme, or absent peer are denied. |
| Loopback and trusted HTTPS proxy can reach the login form | `login_get_shows_credentials_only_on_loopback_or_trusted_https_proxy`; `managed_browser_login_account_csrf_and_logout_work_over_loopback` | pass | Existing session test exercises successful login and cookie behavior. |
| Existing browser session and CSRF behavior remains intact | `on_prem_sessions`; `on_prem_admin_ui`; dashboard parity tooling | pass | Session cookies, CSRF, account/admin flows, and role projections pass. |
| Solo and team operator paths are documented | `README.md`, `docs/raspberry-pi.md`, `docs/deployment.md`, `docs/firewall.md` | pass | Pi SSH forwarding, proxy trust/firewall constraints, and LAN inference separation are explicit. |

## 3. Implementation evidence

- `rust/src/server/auth.rs` gates `GET /login` with
  `web_auth_transport_allowed`, returning a static help page when transport
  is unsafe. The rejected branch only reads request/connection facts and
  server configuration; it does not touch the database or set cookies.
- Login POST continues to use the same transport predicate and same-origin
  validation. Existing secure-cookie and CSRF behavior is unchanged.
- The trusted proxy check binds forwarded HTTPS to the socket peer IP and
  requires exactly one `X-Forwarded-Proto: https` value.
- Docs explain the explicit local SSH tunnel and operator-managed team TLS
  proxy paths. No TLS listener, CA management, migration, or inference-path
  change was introduced.

## 4. Verification executed

- `cargo test --manifest-path rust/Cargo.toml --lib server::auth::tests -- --test-threads=1` — 10 passed.
- `cargo test --manifest-path rust/Cargo.toml --test on_prem_sessions -- --test-threads=1` — 10 passed.
- `cargo test --manifest-path rust/Cargo.toml --test on_prem_admin_ui -- --test-threads=1` — 3 passed.
- `uv run pytest tests/tooling/test_dashboard_parity_on_prem.py -q` — 10 passed.
- Default serial workspace: 1,030 passed, 1 ignored across 79 suites.
- No-default serial workspace: 1,031 passed, 1 ignored across 79 suites.
- Formatting, strict default/no-default Clippy, no-default workspace check, release-doc validation, and `git diff --check` passed.
- These are local results. Hosted CI and a separately deployed reverse-proxy/browser session were not run in this closure pass.

## 5. Invariant and recovery review

An absent or unrecognized peer, a remote direct HTTP peer, and untrusted or
ambiguous forwarded metadata all fail closed. The loopback exception relies
on the actual socket peer address; `Host`, `Forwarded`, and
`X-Forwarded-Proto` cannot turn a remote direct connection into loopback or
trusted proxy transport. The existing explicit insecure development setting
retains its warning and is not the default. No schema or session state changes
were made.

## 6. Unresolved findings

| Severity | Finding | Impact | Required action |
|---|---|---|---|
| none | No unresolved M019 implementation finding | — | — |

## 7. Roadmap and unblock disposition

M019 is closed. The dependency audit searched the on-prem implementation
plans, roadmap graph, and ready/active/blocked registry tables. M020's hard
dependencies, M017 and M019, are both closed; M020 is promoted from blocked
to ready. M021 remains blocked on M020. M023 remains blocked on M022 and
ordinary-server operational evidence. M022 remains ready. No other plan is
promoted and no user-count, concurrency, or capacity claim is introduced.

The registry and roadmap record M019 closed and M020 ready. Historical
M001–M018 closure records were not modified.

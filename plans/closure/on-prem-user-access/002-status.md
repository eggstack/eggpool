# On-Prem User Access Milestone 002 — Closure Status

Status: closed

Source implementation plan:

- `plans/implementation/on-prem-user-access/002-browser-sessions-and-route-auth.md`

Source subsystem roadmap:

- `plans/subsystems/on-prem-user-access-roadmap.md#m002--browser-sessions-and-route-authorization`

Repository baseline reviewed: `dcf184f` (M002 activation; M001 accepted)

Implementation commits:

- `087953a3` — implement browser sessions and route authorization.
- `f4a1861` — move M002 to closure after implementation and verification.

## 1. Executive finding

M002's browser authentication, session lifecycle, CSRF checks, managed-mode
authorization gates, and operator-managed HTTPS posture are implemented.
The session principal is resolved from a database-backed opaque cookie on each
request, so password reset, logout, expiry, and user disablement take effect
immediately for subsequent requests. Agent bearer credentials remain limited
to inference/discovery and cannot establish a browser session or reach admin
surfaces. `dashboard.public` does not grant public access after identity
initialization.

M002 is closed. The physical SBC Argon2 measurement recorded as M001's
condition remains an explicit M007 qualification item; no M002 or SBC sizing
claim depends on a desktop estimate.

## 2. Requirement-to-evidence matrix

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| Opaque session and CSRF values, expiry, revocation | Migration 0056 and `rust/tests/on_prem_sessions.rs` digest-only/revoke, expiry, disabled-user and password-reset cases | pass | Session and CSRF secrets are stored as SHA-256 digests; session secret has 256 random bits. Absolute expiry is 7 days; idle expiry is 12 hours; activity timestamp updates at most every 15 minutes. |
| Login/logout/password change and forced first change | Managed loopback HTTP integration fixture | pass | Covers login, account page, forced change, re-login, logout, missing CSRF denial, and stale-session rejection with ordinary HTML forms and no JavaScript. |
| Generic login failure and bounded abuse controls | `server/auth.rs`, dummy Argon2 verification path, bounded username/IP throttle | pass | Same failure text is used for bad credentials; throttling state is capped at 2,048 hashed keys, with 10 failures per username and 30 per peer per 15-minute window. Argon2 settings and maximum two hash workers are inherited from M001. |
| Admin/member/agent/anonymous route separation | Managed HTTP fixture plus middleware classification | pass | Anonymous access is limited to health/readiness, static assets, and login. Browser admins access protected dashboard/admin routes; non-admin sessions are limited to account routes. Personal tokens are restricted to agent routes and the scoped integration profile. |
| Public-dashboard, shared-key and unknown API bypass prevention | Managed HTTP fixture (`dashboard.public=true` still denied) and middleware lifecycle rules | pass | The global key is accepted only in `legacy_unmigrated`; unknown `/api/*` paths do not inherit the public dashboard setting. Agent access to `/account` and `/api/status` is denied. |
| CSRF, same-origin and proxy transport rules | Auth module unit tests and HTTP unsafe-method fixture | pass | Unsafe browser methods require Origin/Referer same-origin validation and a session-bound CSRF token. Forwarded HTTPS is accepted only from an exact trusted peer IP. Remote cleartext login is rejected unless the explicit development override is enabled. |
| Migration, backup and config compatibility | Database compatibility, migration checksum, runtime asset manifest tests | pass | Migration 0056 is append-only; previous migration checksums remain unchanged. Trusted proxy entries must parse as literal IP addresses. The insecure override defaults off and is restart-required. |
| Documentation and release asset integrity | Release-doc validator and runtime manifest test | pass | Deployment/firewall docs describe operator-owned HTTPS/VPN; both config examples are hash-locked. |
| Physical SBC login latency/RSS | M001 condition and M007 target plan | carried | Not measured on this Darwin x86_64 host; M007 must record physical target results before any SBC performance claim. |

## 3. Production implementation evidence

Migration `0056_web_sessions.sql` adds the session selector, token digest,
CSRF digest, timestamps, user association, and lookup indexes. Its SHA-256 is
`b35541860dbc9c258de617f6f5a9c0111de6428be3128cd79d962bdda9634167`.
`rust/src/web_session.rs` owns session issuance, authentication, CSRF proof,
revocation, cookie attributes, and bounded login throttling. Password reset
invalidates both personal API credentials and browser sessions.

`rust/src/server/auth.rs` provides `/login`, `/account`, password change,
logout, same-origin parsing, and trusted-proxy HTTPS validation. The server
middleware enforces role and credential-surface rules before handlers run.
`users enable-managed` performs the checked transition only after the shared
key is cleared and the public dashboard setting is off. The optional
`security.allow_insecure_web_auth` setting defaults to false and emits a
startup warning when enabled.

## 4. Verification executed

### Commands run

```bash
rtk cargo fmt --manifest-path rust/Cargo.toml --all -- --check
rtk cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
rtk cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
rtk cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
rtk cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
rtk cargo test --manifest-path rust/Cargo.toml --test on_prem_sessions -- --test-threads=1
rtk cargo test --manifest-path rust/Cargo.toml --test server_transport -- --test-threads=1
rtk cargo test --manifest-path rust/Cargo.toml --test dashboard_filters -- --test-threads=1
rtk cargo test --manifest-path rust/Cargo.toml --test runtime_lifecycle_r005 -- --test-threads=1
rtk cargo build --manifest-path rust/Cargo.toml --locked --release
rtk cargo deny --manifest-path rust/Cargo.toml check
rtk cargo tree --manifest-path rust/Cargo.toml -e features
rtk uv run python scripts/validate_release_docs.py
rtk git diff --check
```

### Results

- Full serial workspace suite: **947 passed, 1 ignored across 74 suites**.
- Focused suites: `on_prem_sessions` 5 passed; `server_transport` 14 passed;
  `dashboard_filters` 2 passed; `runtime_lifecycle_r005` 10 passed.
- Formatting, strict default Clippy, no-default-features check, and strict
  no-default-features Clippy passed.
- Locked release build, `cargo tree -e features`, `cargo deny`, release-doc
  validation, and `git diff --check` passed. Cargo deny reported existing
  duplicate-version warnings; advisories, bans, licenses, and sources passed.
- Release binary: 32,205,208 bytes; SHA-256
  `942ca66e289f82a779b3184965b8391ab281d19fbef58f8544807bd2284bc25a`.
- One early suite attempt exposed runtime-manifest drift for the edited config
  examples and a missing reload-schema disposition. Hashes and the canonical
  reload-policy fixture were corrected; the focused contract test and final
  full suite then passed.
- These are local results. No hosted CI or physical SBC result is claimed.

## 5. Invariant review

- Browser sessions and agent tokens remain separate credentials. Browser
  cookies are not copied into inference requests.
- One SQLite worker/gate remains the session authority; no positive session
  cache delays revocation. Database failure rejects protected routes.
- Argon2 runs under M001's bounded worker limit, outside SQLite transactions.
- TOML/environment continue to own providers and upstream credentials. No
  web-based provider-secret editor or external identity service was added.
- The legacy shared key remains confined to `legacy_unmigrated`; managed
  access has no public-dashboard or loopback privilege shortcut.
- The added security setting uses the existing `classify_transition`
  authority and is restart-required; no separate reload list was introduced.

## 6. Failure and recovery review

Invalid, expired, revoked, reset, or disabled-user sessions fail closed.
Logout and password reset revoke database rows before returning success.
Database errors reject protected requests and do not fall back to anonymous
or shared-key access. Session issuance requires an active user inside the
database transaction. Malformed cookies and CSRF values are rejected.
Requests already executing may finish; revocation applies to subsequent
requests. Session state survives restart only while it remains unexpired and
unrevoked.

## 7. Migration and compatibility review

Migration 0056 is additive and forward-only. Fresh databases retain the
existing bootstrap flow; legacy databases retain their shared-key behavior
until identity initialization. Managed browser authorization supersedes the
legacy `dashboard.public` behavior without changing legacy-readonly dashboard
compatibility. Provider APIs, streaming behavior, and request wire formats
were not changed. Downgrade to binaries predating schema 56 requires restoring
a protected pre-upgrade database backup.

## 8. Security review

The browser session has a random selector and 256-bit secret; only its digest
and the CSRF digest are persisted. Cookies are HttpOnly, scoped to `/`,
SameSite Lax/Strict, and Secure when the connection is directly HTTPS or
HTTPS from a configured trusted proxy. Same-origin checks reject missing,
ambiguous, or cross-origin mutation metadata. The opt-in insecure setting is
explicitly labeled for development and warns at startup. Login throttling is
bounded in memory and keyed by digests, not raw usernames or addresses.

## 9. Documentation and operations

Updated the security, dashboard, and database architecture deep dives, backup
and restore, deployment, firewall, and CLI reference docs. Both example
configs document exact trusted proxy IPs and the insecure-auth override.
Operators must provide TLS termination or use a VPN for remote browser access;
EggPool does not provide a native TLS server or remote-access service.

## 10. Unresolved findings

| Severity | Finding | Impact | Required action |
|---|---|---|---|
| low | Physical Le Potato/Pi Argon2 login latency, RSS, and concurrency remain unmeasured. | No target-class login sizing claim is supported. | M007 carries M001's exact physical qualification; record model, OS, storage, repetitions, wall-time percentiles, peak RSS, and one/two-worker results. |

## 11. Roadmap disposition

M002 is closed. Its browser session and authorization interfaces are stable
for M003 principal propagation. M003 is promoted to ready. M004–M007 remain
blocked on their listed hard predecessors; M001's physical hash condition
remains with M007.

## 12. Registry updates

The closure commit marks this plan implemented and M002 closed, moves it from
the active table to recently closed, promotes M003 to the dependency-ready
table, removes M003 from blocked work, and updates the subsystem roadmap in
the same commit.

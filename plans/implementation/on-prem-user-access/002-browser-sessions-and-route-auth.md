# On-Prem User Access Milestone 002 — Browser sessions and route authorization

Status: ready

Repository baseline: `main` at `9a0a903c9944b69a6c1ae9fa969bb8603d5c65d9` (architecture/roadmap authority committed on `plans/on-prem-small-team-user-access` at `81beec4e215e8991880d407ed2752d01ff88d58a`)

Source roadmap:
- `plans/subsystems/on-prem-user-access-roadmap.md#m002--browser-sessions-and-route-authorization`

Long-term requirements:
- `plans/000-long-term-specification.md#2-end-state-invariants-normative`
- `plans/001-terminology-and-domain-model.md#identity-and-lifecycle-terms`
- `plans/002-long-term-roadmap.md#phase-5--optional-on-prem-small-team-access-and-budgeting`

Applicable ADRs:
- `plans/adrs/ADR-0002-on-prem-user-identity-and-policy-boundaries.md` (accepted)

Primary class: capability

## 1. Objective and boundary

Enable password-authenticated browser sessions and explicit admin/member route
authorization, without mixing agent bearer keys with human sessions.
**Hard blocker:** M001 must have an accepted closure; do not mark ready
before the schema/credential/bootstrap contracts are verified.

## 2. Baseline and contracts

`rust/src/server/mod.rs::build_router` installs global
`authenticate` / `admit_inference_body` layers; `requires_auth` has
special public dashboard cases, including `/api/*`.
`DashboardConfig.public` defaults true. Dashboard rendering lives in
`rust/src/server/dashboard/`; there is currently no session/CSRF web
authority. M001 supplies `users`, credential handling and auth-state
transition. Do not replace EggServe or add OAuth/OIDC.

### Always-preserved invariants
- A single on-prem instance; TOML/environment retains upstream provider topology/credentials, model routing, and operator-owned settings. SQLite owns only downstream users, sessions, grants, profiles, budget reservations, consumption and audit.
- No SaaS organizations, hosted signup, embedded reverse proxy/VPN, enterprise identity dependency, separate service/database, provider credential web editor, or assertion of legal authority to share personal subscription seats.
- All legacy inference wire paths and return shapes (Chat Completions, Messages, Responses, Responses compact), streaming/terminal semantics, redaction, configuration lifecycle, and operator CLI remain compatible unless an explicit migration contract states otherwise.
- Respect `GenerationLease`, coordinator publication/finalization compensation, one SQLite gate/worker with WAL/NORMAL, `config_reload_policy.rs` as reload authority, and `--no-default-features` compilation.
- In-flight resource ownership remains deterministic. No database locks span provider I/O or expensive password hashing. No raw prompt, token, password, session, provider key, or cache key reaches diagnostics or ordinary request storage.

### Shared repository verification
Run on implementation and report actual results in the closure record, not the plan:
```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
```
For dependency changes also run locked release build, `cargo deny` and
`cargo tree -e features`; for user-facing release-doc edits run
`uv run python scripts/validate_release_docs.py`. Ensure `git diff --check`.

## 3. Required production changes

### A — Browser authentication surface
Add dedicated `GET /login`, `POST /login`, `POST /logout`,
`GET /account`, `POST /account/password` (or equivalent versioned
auth API), with typed request validation, generic failure messages,
login throttling and no username enumeration. Support first-login
password change. Session IDs are independent high-entropy opaque tokens;
store only digests plus user association, issue/expiry/revocation, inactivity
limits and rotating tokens as appropriate in `web_sessions`. Bind
cookies `HttpOnly`, `SameSite` appropriately, `Secure` on HTTPS,
narrow `Path`, and server-side expiry. Ensure logouts/password resets
revoke active sessions with a bounded invalidation cache.

### B — Explicit route/access matrix
Replace catch-all public/private boolean behavior with tested classifications:
anonymous: only health/readiness, needed static assets and login;
member: own account/settings/usage when that surface exists;
admin: full dashboard diagnostics and protected config-adjacent views;
agent token: inference, `/v1/models`, scoped integration profile only.
`/api/stats/runtime`, `/api/status`, admin mutations and
`/api/integrations/*` receive explicit rules. Unknown `/api/*`
paths default private/404, never inherit `dashboard.public`. No agent
credential may create web sessions/admin privilege. Preserve legacy-readonly
dashboard compatibility only in legacy mode with explicit migration guard.

### C — CSRF and transport posture
For cookie-authenticated unsafe methods, require CSRF proof (same-origin
Origin/Referer validation plus per-session token or synchronizer mechanism);
reject cross-site GET-to-mutation, CORS credential surprises, and spoofed
forwarding headers. Ensure state-changing methods never accept GET.
Local loopback HTTP can be explicitly supported; nonloopback password login
requires operator-configured HTTPS termination with *trusted-proxy-only*
forwarded-proto validation, or a consciously configured documented insecure
development override with warnings and limited blast radius. A LAN address
is not a trust boundary; no hidden integrated TLS server or proxy.

### D — Managed transition
Add a checked transition from `admin_initialized` to managed. Require
explicit retirement of shared global legacy key and disabling of
unauthenticated loopback and public private-data exemptions **before**
granting second-user access. Fresh initial single-admin experience remains
a basic login and ordinary dashboard, not a compulsory setup wizard.
Ensure change-password, session logout and re-authentication flows are
usable without frontend dependencies.

## 4. Ordered work packages

1. Schema/session repository with expiry/revocation and Argon2 bounds; tests.
2. Strict route classification and role/scope principal extraction;
   negative tests across every existing endpoint.
3. Login/logout/change-password pages, cookies, CSRF and transport checks;
   browser/HTTP fixture coverage.
4. Managed-mode transition and legacy key retirement + documentation.

## 5. Failure, cancellation, restart and contention

Server-side session revocation must be observed for **new** requests
immediately; in-progress HTML render may finish. Expired/disabled accounts
cannot renew sessions. DB failure rejects protected endpoints. Throttle
login attempts and cap Argon2 concurrent workers on SBC hardware without
locking the SQLite gate while hashing. Sessions survive a normal restart
only if deliberately unexpired; password reset/global revocation invalidates
them. Cookies must never be forwarded upstream.

## 6. Compatibility and migration

Preserve API bearer semantics for agents, guest health/readiness and
legacy dashboard in explicitly unmigrated deployments. Do not silently
turn public dashboards into write/admin access. `dashboard.public`
in managed mode must not expose user/admin data; define a safe precedence
and a startup diagnostic. CLI admin recovery bypasses HTTP only via
OS-owned local access.

## 7. Tests and verification

New `rust/tests/on_prem_sessions.rs` and extended
`server_transport.rs`/`dashboard_filters.rs`/`cli_contract.rs`.
Fixtures: all endpoints by anonymous/member/admin/agent, stale cookie,
stolen/revoked session, password reset, CSRF origin and token missing,
remote HTTP vs trusted TLS proxy vs spoofed headers, limited login
concurrency, first-login forced change, browser-disabled/Javascript-off
login and initial single-admin use. Focused:
```bash
cargo test --manifest-path rust/Cargo.toml --test on_prem_sessions -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test server_transport -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test dashboard_filters -- --test-threads=1
```
Run shared matrix and browser parity baseline where dashboard shell changes.

## 8. Docs, acceptance and stop conditions

Update `architecture/deep-dive-security.md`,
`architecture/deep-dive-dashboard.md`, `docs/deployment.md` and
`docs/firewall.md`; document operator-managed HTTPS/VPN rather than
promising a built-in remote feature. Accept only when anonymous/agent/member
cannot reach privileged dashboard/admin data, no CSRF bypass exists,
and local admin login/logout/change works. Stop on unresolved trust-proxy
or unknown-route ambiguity. Closure
`plans/closure/on-prem-user-access/002-status.md`: exact route matrix,
browser negative tests, protocol compatibility and login cost, migration
evidence, CI results and unblock audit.


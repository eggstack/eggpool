# On-Prem User Access M019 — Login transport and solo LAN operability corrective

Status: implemented

Repository baseline: `88f58bbf2e9095cf46437945edf83e580270bdb6` (`main`, post-PR #7).

Source roadmap: `plans/subsystems/on-prem-user-access-roadmap.md#m019--login-transport-and-solo-lan-operability`

Long-term requirements: `plans/000-long-term-specification.md#1-product-definition`, `#2-end-state-invariants-normative`; `plans/002-long-term-roadmap.md#phase-5--optional-on-prem-small-team-access-and-budgeting`.

Applicable ADR: `plans/adrs/ADR-0002-on-prem-user-identity-and-policy-boundaries.md` (accepted).

Primary class: invariant

## 1. Objective

Prevent a remote plaintext HTTP GET from soliciting credentials that the POST handler later rejects, and document a secure, low-friction solo SBC browser path while retaining managed-team HTTPS requirements.

## 2. Readiness and dependencies

Hard: none. Interface: existing `web_auth_transport_allowed`, trusted proxy peer/scheme detection, same-origin/CSRF, and dashboard routes. No native TLS stack or certificate manager is authorized by this plan.

## 3. Current evidence

`rust/src/server/mod.rs::build_router` mounts `GET /login` at `rust/src/server/auth.rs::login_page`. The GET handler renders a form unconditionally; `server/middleware.rs::is_unconditionally_public` bypasses authentication. POST `login_submit` rejects remote HTTP before verifying passwords, but the browser has already sent the password unencrypted. Managed sessions also make the legacy `dashboard.public` default insufficient for ordinary LAN dashboard access; README and Pi instructions are currently misleading.

## 4. Invariants

Admin/password/session browser routes do not become public on LAN or loopback by accident; only explicit loopback or authenticated HTTPS-proxy transport may carry password forms. Never trust arbitrary Host, `Forwarded` or `X-Forwarded-Proto`. Keep HttpOnly/SameSite/Secure-cookie behavior, CSRF, separate agent credentials and no additional required daemon for solo use.

## 5. Scope

In: gate `GET /login`, safe transport explanatory page and accurate operator docs/onboarding hints. Out: implementing native TLS, enabling insecure HTTP by default, automatically generating or trusting CA roots, putting TLS or browser auth in the inference path.

## 6. Production changes

Reuse one canonical transport predicate for GET/POST using actual peer scheme and configured proxy trust. For rejected GET serve a static no-password no-username HTML explanation or redirect only to an operator-configured trusted HTTPS URL; never infer redirect destination from untrusted request headers. Render an explicit solo remote-headless option using SSH port forwarding to local loopback; ordinary teams use operator-managed TLS reverse proxy/VPN.

## 7. Ordered work packages

### A — Prevent credential solicitation on unsafe GET

Change `login_page` signature to obtain connection/header/server context; gate forms using the same policy as POST. Ensure a rejected GET contains no password input and cannot set a browser session.

Acceptance: remote direct HTTP GET and POST reject safely, loopback GET/POST work.

### B — Test proxy/scheme and browser contracts

Build negative tests for spoofed, duplicated, malformed forwarding headers, direct bypass around proxy, wrong Host, same-origin mismatch; positive tests for configured HTTPS peer, loopback, cookie security, CSRF, M011 dashboard shell/parity.

Acceptance: non-spoofable transport checks with browser tests on the rendered HTML.

### C — Give solo and server operators supported paths

Update first-run help, README/Pi guide, firewall/deployment examples. Clearly distinguish LAN HTTP inference tokens from browser password safety, local loopback browser, `ssh -L` on a Pi, and TLS termination for on-prem teams. Preserve the development-only insecure override with a warning.

Acceptance: a headless personal Pi can reach dashboard without installing any extra Eggpool component; secure ordinary-server team path explained.

## 8. Failure, cancellation, restart and contention

An unknown connection or untrusted proxy must not display a credential form. Avoid database access on rejected GET. Security decision must not depend on DNS reachability or cached forwarded headers. Browser sessions remain invalid after disable/revocation/restart as currently documented.

## 9. Compatibility and migration

No schema, server protocol or API key changes. Legacy public read-only dashboard rules before initialization remain as implemented; initialized pages remain private regardless of `dashboard.public`. An operator opting into the explicit insecure developer override retains warning/flag semantics.

## 10. Tests

Extend `rust/tests/on_prem_sessions.rs`, `rust/tests/server_transport.rs`, `rust/tests/on_prem_admin_ui.rs` and `tests/tooling/test_dashboard_parity_on_prem.py`. Assert remote HTTP GET produces zero password fields; POST rejects; loopback and trusted proxy positive flows work; cookie flags and CSRF unchanged.

## 11. Verification commands

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
uv run pytest tests/tooling/test_dashboard_parity_on_prem.py -q
uv run python scripts/validate_release_docs.py
git diff --check
```

## 12. Documentation

`README.md`, `docs/raspberry-pi.md`, `docs/deployment.md`, `docs/firewall.md`, `architecture/deep-dive-security.md`; avoid promising plain HTTP browser login on a non-loopback LAN address.

## 13. Acceptance criteria

Insecure remote GET never presents a password form; insecure POST rejects without authenticating; loopback and correctly proxied HTTPS browser login work; solo SBC has clear documented path without sacrificing secure defaults.

## 14. Stop conditions

Stop for an ADR if transport privilege or cookie security meaning changes, or if proposed solution enables browser authentication on arbitrary RFC1918 peers as an undocumented bypass.

## 15. Closure evidence

Add `plans/closure/on-prem-user-access/019-status.md` with HTML and transport negative matrix, browser evidence, exact SHA, serial/no-default and hosted test outcomes, docs/security review and blocked-work audit.

## 16. Handoff

P1 security + solo ergonomics correction. POST rejection alone does not protect a user who has already submitted an HTTP login form.

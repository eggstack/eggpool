# On-Prem User Access Milestone 006 — Web administration and personal client integration

Status: blocked

Repository baseline: `main` at `9a0a903c9944b69a6c1ae9fa969bb8603d5c65d9` (accepted new architecture on `plans/on-prem-small-team-user-access`).

Source roadmap: `plans/subsystems/on-prem-user-access-roadmap.md`.

Long-term requirements:
- `plans/000-long-term-specification.md#2-end-state-invariants-normative`
- `plans/001-terminology-and-domain-model.md#identity-and-lifecycle-terms`
- `plans/002-long-term-roadmap.md#phase-5--optional-on-prem-small-team-access-and-budgeting`

Applicable ADR: `plans/adrs/ADR-0002-on-prem-user-identity-and-policy-boundaries.md` (accepted).

Primary class: capability

## 1. Objective

Expose secure small-team administration and member self-service through
the existing embedded Rust dashboard. Admins can create/disable users,
assign profiles, apply overrides and budget rules, inspect aggregate and
per-user usage, revoke tokens, reset passwords, and audit changes. Members
can log in, change passwords, manage their own scoped agent tokens and
view their accessible models/usage. A personal client profile works with
OpenCode, Codex and current Eggpool remote desktop helper. **Hard
dependency:** accepted M005 closure (all lower layers/quotas functional).

## 2. Why blocked

All admin mutation endpoints must rely on already-proven M002 sessions/CSRF,
M003 principal, M004 authorization and M005 budget persistence. A static
dashboard mock is not accepted capability evidence.

## 3. Baseline

`rust/src/server/dashboard/routes.rs` serves read-only pages,
`dashboard/render/layout.rs` renders navigation and layout,
`dashboard/api.rs` reads aggregate stats, and bundled
`rust/assets/dashboard/static/dashboard.css` and `dashboard.js`
own frontend assets. `rust/src/operations/integrations.rs` generates
Codex/OpenCode snippets and remote profiles using one resolved server
key, while `rust/crates/eggpool-client-config/` and
`rust/crates/eggpool-connect/` own portable projection and desktop
configuration. Current `docs/agent-configuration.md` documents
`EGGPOOL_API_KEY` global key handling.

## 4. Non-regressing invariants

- This is one LAN/on-prem instance, not hosted SaaS. The first-admin SBC flow remains canonical; no mandatory external service.
- TOML/environment retain provider topology, provider credentials, outbound routing, model overrides and self-hosted LLM connection details. SQLite holds downstream users, policies, budgets, sessions and usage. Never make the web dashboard a general provider configuration editor.
- Existing Chat Completions/Responses/Messages/compact wire, streaming termination, auth-header compatibility, metadata redaction, recovery, provider scoring and health remain intact.
- Single SQLite gate/worker (WAL/NORMAL), `GenerationLease` and immutable provider topology remain authoritative. No secret/body logging, second retry/finalization owner or unbounded in-memory cache.
- A denied user cannot transform a permitted virtual model or provider qualifier into an unauthorized underlying dispatch.

## 5. Required production changes

### A — Authorized administrator endpoints

Add minimal versioned web/API handlers for user create, disable/reactivate,
admin-initiated password reset, profile CRUD, profile assignment, user-level
model/budget overrides, token revocation and audit log browsing. Return
generic redacted failures; enforce actual DB-based admin role and CSRF on
every mutation, validate bounded form bodies and constrained input. Prevent
deleting/disabling the last usable admin, self-demotion bypass and stale
write overwrites (profile edit version or compare-and-swap). All policy
writes atomically bump effective-policy revision/invalidation. No
reconfiguration of provider TOML or upstream secrets via browser.

### B — Embedded web UI and member scope

Extend existing Rust-rendered pages with `Users`, `Profiles`,
`Usage` and `My Account` tabs, retaining Halloy themes, current
layout, keyboard focus/assistive labels, ordinary form semantics,
graceful no-JS operation where practical and the historical overview/
routing/latency/account pages. Admins see user-status, token counts,
budget remaining and safe aggregate operational diagnostics. Members
see only their own identity, usage, remaining quotas, accessible
models and a password/token change interface; no exposure of other
users' usernames, emails, provider-account lists or diagnostic
summaries. Token plaintext appears once on creation and is omitted
from HTML on refresh/history; warn and permit safe regeneration if lost.

### C — Personal desktop-client configuration

`/v1/models` and `/api/integrations/v1/profile` are filtered by
the authenticated token owner. Adapt `configsetup`, `configremote`,
`eggpool-connect` flows to prompt for or consume the user's personal
token from `EGGPOOL_API_KEY`, an interactive terminal or
`--api-key-stdin`; never embed a global admin key in a shared
bootstrap URL, URL query, checked-in file or output by default. Keep
the existing `epc1` profile/revision contract or introduce explicit
backward-compatible version negotiation only if necessary, with
conservative model metadata and scopes. A profile/policy update
invalidates ETag/client catalogue; revoked credentials cannot refresh.
Do not silently allow client helpers to change server policy.

### D — Onboarding and operator ergonomics

A fresh administrator logs in with the bootstrap credentials and can
continue one-user work without seeing forced team setup; a clear
`Add user` step activates managed mode only after legacy key retirement.
Create users with one-time temporary passwords or local password-reset
mechanism requiring first login change, never email-delivered secrets or
`admin/admin`. Provide a `users list` local CLI summary but keep
primary team management in the web admin. Document no native remote
hosting, terms-sensitive pooling, VPN/reverse-proxy instructions and
self-hosted model examples controlled in TOML.

## 6. Out of scope

No provider TOML web editor, central SSO, registration/sign-up page,
email invitation or automated password email, tenant hierarchy,
billing/invoices, native TLS certificate manager or external frontend.

## 7. Ordered work packages

1. Define admin/member HTTP/CSRF, authorization and DTO contract with
   an exhaustive route-method matrix; implement thin DB-backed handlers.
2. Add profile and user forms using current dashboard render/asset
   system; ensure accessibility and legacy dashboard parity.
3. Integrate filtered per-user agent profiles and token delivery into
   `configremote`/`eggpool-connect`, preserve atomic backup/rollback.
4. Add testable two-user onboarding journey, error handling, docs
   and migration guidance with perf/size spot checks.

## 8. Failure, cancellation, restart and contention

All admin mutation operations must be atomic/consistent; password reset
invalidates existing sessions and follows a documented token policy.
Concurrent profile edits have one winner or structured conflict; users
never briefly gain more access than the committed policy. A user's
revoked API credential cannot reuse cached discovery or existing sessions
to start new inference. A rotating browser session retains correct CSRF
binding. A historical request remains visible to an admin even when
the owner is disabled; member queries cannot infer other users' data.
No blocking DB transaction spans page render/network calls.

## 9. Compatibility and migration

`/v1/models` keeps OpenAI schema, only filters visibility.
`GET /api/integrations/v1/profile` retains revision/ETag and
conservative limits, but scopes model selection to token owner.
All existing `epc1`, Codex TOML and OpenCode JSONC paths must
remain supported, including rollback and remove of managed entries;
replacing legacy server key may require user rerunning credential
bootstrap, not full client reinstall. Legacy single-owner dashboard
pages and PNG/JS/theme parity must not regress.

## 10. Tests and qualification

Add `rust/tests/on_prem_admin_ui.rs` and
`rust/tests/on_prem_client_profiles.rs`; extend
`rust/tests/dashboard_filters.rs`, `server_transport.rs`,
`operations_o005.rs`, `cli_contract.rs`,
`rust/tests/runtime_lifecycle_r005.rs` and
`rust/crates/eggpool-connect/tests/connect_transaction.rs`.
Test login-to-create-user flow, direct endpoint privilege escalation,
CSRF, password first-change, profile grant/deny, stale ETag,
two concurrent profile editors, last-admin protection, member's
view of a forbidden user, token reveal-once/revoke/refresh,
Codex/OpenCode live model subset, compact Responses client use,
connection helper rollback, no-credential logs and no-JS fallback.
Repeat dashboard browser parity snapshots/interaction harness where
shell/nav/layout differs.
```bash
cargo test --manifest-path rust/Cargo.toml --test on_prem_admin_ui -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test on_prem_client_profiles -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test dashboard_filters -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test operations_o005 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml -p eggpool-connect -- --test-threads=1
cargo test --manifest-path rust/crates/eggpool-client-config/Cargo.toml -- --test-threads=1
```
## 11. Required verification commands

Run the following after focused tests; preserve exact results for closure:
```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
git diff --check
```
If new dependencies/feature flags: `cargo deny --manifest-path rust/Cargo.toml check`,
`cargo tree --manifest-path rust/Cargo.toml -e features`,
`cargo build --manifest-path rust/Cargo.toml --locked --release`.
No test may be declared passed unless actually run.

## 12. Documentation

Update `architecture/deep-dive-dashboard.md`,
`architecture/deep-dive-integrations.md`,
`architecture/deep-dive-security.md`,
`docs/agent-configuration.md`, `docs/cli-reference.md`,
`docs/deployment.md`, `docs/api-reference.md` and dashboard
qualification manifests; keep operational docs accurate about
plaintext LAN password hazards.

## 13. Acceptance criteria

A one-user admin can log in and continue use without creating another
user. Admin can create two members with distinct roles/profiles/budgets,
and each agent sees only allowed models, receives correctly scoped 401/403
on revocation/denial, and uses Codex/OpenCode without server-key sharing.
Non-admin cannot mutate any user or inspect others' usage. Existing
dashboard visual/functional parity and theme switching remain intact.

## 14. Stop conditions

Stop on incomplete lower-layer auth, CSRF, policy revocation, budget or
private-data query fencing. Never ship web forms as a facade over
unsafe admin operations. Do not convert this into an SPA/framework rewrite.

## 15. Closure evidence required

`plans/closure/on-prem-user-access/006-status.md`: exact screen/route
authorization matrix, two-user full journey, client integration and ETag
fixtures, browser parity/CSRF tests, security review, footprint and
commands actually run, known gaps and unblock audit.

## 16. Handoff

Keep the Rust dashboard read/render system and portable
`eggpool-client-config` ownership; server remains the source of truth
for identity and policy, provider TOML remains operator-owned.

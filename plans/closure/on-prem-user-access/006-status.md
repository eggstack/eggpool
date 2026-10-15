# On-Prem User Access Milestone 006 — Closure Status

Status: conditionally closed

Source implementation plan:

- `plans/implementation/on-prem-user-access/006-web-admin-and-personal-integrations.md`

Source subsystem roadmap:

- `plans/subsystems/on-prem-user-access-roadmap.md`

Repository baseline reviewed: `5bf0cfe4` (M005 conditional closure)

Implementation commits or pull requests:

- `a3f2d00a` — add on-prem user administration and qualification updates
- `1aeb892c` — begin M006 closure evidence collection

## 1. Executive finding

The embedded browser admin/member capability is implemented and exercised by a
real two-user loopback journey. Administrators can manage users, profile rules,
model overrides, budgets, credentials, and audit history; members can change
their initial password, inspect their own models and usage, and issue/revoke
personal credentials. Credential delivery is one-time and no-store. M006 is
conditionally closed because matched dashboard parity captures for the new
admin/member pages were not available in this environment. The underlying M005
estimate limits remain accurately labeled in the UI and are carried to M007.

## 2. Requirement-to-evidence matrix

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| Admin/member route authorization and CSRF | `rust/src/server/middleware.rs`; `rust/tests/on_prem_admin_ui.rs` | pass | Agent token receives 401 for `/users`; member browser receives 403 for `/users` and an admin create post; missing CSRF is rejected. |
| User lifecycle and last-admin safety | `identity::create_member`, `set_user_enabled`, `reset_user_password`; `on_prem_admin_ui` | pass | Temporary password forces first-login change; disabling/recovery revokes credentials and sessions; last active admin cannot be disabled. |
| Profile CRUD, rules, and stale writes | `model_policy.rs`; profile CAS/audit test | pass | Version compare-and-swap accepts one edit and rejects stale edit; rule removal and audit facts verified. |
| Member-scoped account, models, and usage | `/my-account`, `/my-account/models`, `/usage`; two-user browser test | pass | Member pages do not expose the admin username or admin summaries. |
| One-time personal credentials and revocation | `on_prem_admin_ui`; `/account/tokens*` | pass | Token appears once, has `no-store`, authenticates discovery, cannot enter admin pages, and stops authenticating after revocation. Refresh omits plaintext. |
| Budget and audit administration | `/users`, `/usage`, `/admin/audit`; API and UI renderers | pass | Existing M005 estimate dimensions are labeled as estimates. Audit rows contain actor/action/target/time only. |
| Migrations and manifest | migration 60, checksum/runtime manifests, migration inventory and compatibility tests | pass | Append-only migration adds secret-free administrative audit rows; older schema fixtures upgrade through v60. |
| Personal client profile and desktop paths | `on_prem_client_profiles`, `operations_o005`, `eggpool-connect`, client-config tests in workspace suite | pass | Existing Codex/OpenCode profile flow consumes per-user authorization facts and the user's `EGGPOOL_API_KEY`; no server-side provider editor was added. |
| Dashboard browser parity captures for new pages | Frozen oracle/browser interaction harness | not run | The pinned oracle worktree and its required `EGGPOOL_DASHBOARD_ORACLE_*` paths were not available. New pages use ordinary server-rendered forms. |
| Physical SBC sizing | M007 | deferred | This closure makes no hardware capacity or named-user performance claim. |

## 3. Production implementation evidence

Migration 60 adds indexed `user_admin_audit` storage and is checksum-pinned in
the migration and runtime manifests. Identity and policy services now provide
transactional user creation, enabled-state changes, forced password reset,
bounded credential and profile listing, profile compare-and-swap, profile and
rule management, and secret-free audit projection. Audit rows are committed
with the corresponding mutation.

The existing embedded dashboard now has admin pages for users, profiles,
usage, and audit, plus member pages for their own account, model access, and
usage. Unsafe form posts require same-origin evidence and the session-bound
CSRF token; admin operations check the active browser principal's current role.
Agent tokens never provide dashboard access. The one-time token response is
`no-store`, `no-referrer`, and `nosniff`. The local provider configuration
remains operator-owned.

The offline live-provider qualification fixture now bootstraps a synthetic
administrator in its isolated temporary database, uses the resulting personal
token for discovery/inference, and redacts it from candidate diagnostics. This
keeps the fixture aligned with fresh-database authentication after legacy-key
retirement.

## 4. Verification executed

All results below are local runs, not hosted CI results.

### Commands run

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test on_prem_admin_ui -- --test-threads=1
uv sync --frozen
uv run pytest tests/tooling/ -q --tb=short --maxfail=1
uv run ruff format --check scripts/ tests/tooling/
uv run ruff check scripts/ tests/tooling/
uv run pyright scripts/
uv run python scripts/validate_release_docs.py
uv run python scripts/validate_runtime_package_boundary.py
git diff --check
```

### Results

- Rust workspace: 964 passed, 1 ignored across 78 suites (serial).
- Focused admin journey: 2 passed after the final route and token-revocation assertions.
- Default and no-default strict Clippy: pass; no-default workspace check: pass; formatting check: pass.
- Tooling suite: 166 passed, 1 skipped. The skipped test requires a separately built or provisioned qualification candidate; the focused offline-provider test passed after rebuilding `cargo build --manifest-path rust/Cargo.toml --locked`.
- Ruff: 59 files already formatted; all checks passed. Pyright: 0 errors, 0 warnings, 0 informations.
- Release documentation validator: pass, 7 docs checked. Runtime package-boundary validator: pass.
- `git diff --check`: pass.
- No dependency or feature graph changed; no dependency audit or release build was required.

## 5. Invariant review

- Provider topology/configuration and upstream credentials remain outside the web admin.
- User/profile mutations use SQLite transactions and policy revision invalidation; profile edits reject stale versions.
- Dashboard mutations use session-bound CSRF and database-backed role checks; personal tokens are not browser sessions.
- Token/password values, prompts, request bodies, and raw credentials are absent from audit and admin projections.
- Member account, model, and usage views are scoped to the logged-in user; query/list projections are bounded.
- Existing Codex/OpenCode portable configuration and helper ownership remains unchanged; `EGGPOOL_API_KEY` receives a per-user token.

## 6. Failure and recovery review

User creation commits the forced password-change state, managed-mode transition,
and audit record atomically. Profile edits use compare-and-swap; stale updates
return a conflict. Password reset, disable, and token revocation invalidate
credentials/sessions before subsequent requests. A user created without an
assigned profile remains under the member default-deny policy while assignment
completes. Migration 60 is append-only and audit writes share mutation
transactions. Cross-version binary rollback still requires restoring a
compatible pre-upgrade database backup; this is qualified in M007.

## 7. Migration and compatibility review

Migration 60 is additive and checksum-locked. Migration inventory, runtime
asset manifest, fresh-database counts, and historical schema upgrade tests now
expect versions 1–60. `/v1/models` and the versioned integration profile retain
their existing response contracts while applying user-specific visibility.
Codex/OpenCode configurations continue to consume the same versioned profile
and environment-key contract.

## 8. Security review

The two-user HTTP journey confirms admin/member separation, forced initial
password change, same-origin/CSRF enforcement, one-time token display,
non-admin denial, and immediate token revocation. Admin browser routes require
a browser session even when the legacy dashboard is public; agent tokens cannot
authenticate them. Token-bearing response headers prevent caching/referrer
leaks. The new audit table stores only actor/action/target identifiers/time.

## 9. Documentation and operations

Updated the dashboard, database, integrations, CLI, deployment, agent
configuration, and API reference documents. They describe per-user token
setup, member/admin route scope, the UI's estimated budget dimensions, and the
operator-managed TLS proxy requirement for remote browser login. No capacity or
SBC performance statement was added.

## 10. Unresolved findings

| Severity | Finding | Impact | Required action |
|---|---|---|---|
| Medium | Matched dashboard parity screenshots/interaction captures for the new admin/member screens were not run because the pinned dashboard oracle worktree was not available. | Visual and interaction parity evidence is incomplete; runtime route/security tests pass. | Run the dashboard parity and interaction harness against the pinned oracle and current build, or record exact intentional differences in M007 closure. |
| Medium | M005's estimated output-token and unpriced-USD limits, instance-wide ceiling, crash/restore qualification, and overhead measurements remain open. | UI does not claim exact enforcement; some budget dimensions remain estimate-based and are not a global instance cap. | Preserve labels and carry the existing M005 closure conditions into M007; create a separate corrective plan if exact caps are required. |
| Low | Physical SBC Argon2/RSS and small-team capacity evidence remain unavailable on this macOS x86_64 host. | No SBC sizing or 49-named-user operating claim is supported. | Collect paired physical SBC/ordinary-host evidence under M007 when provisioned hardware is available. |

## 11. Roadmap disposition

M006 is conditionally closed. The capability is implemented and local Rust/tooling
verification is green. Browser parity evidence is explicitly outstanding, and
M005 budget limitations remain visible and carried forward. M007 is promoted to
ready: its hard M006 dependency is satisfied; its physical hardware requirement
is an operational evidence gate for qualification/claims, not a reason to block
preparatory implementation work.

## 12. Registry updates

`plans/registry.md` and the source roadmap record M006 as conditionally closed,
link this closure, and promote M007 from blocked to ready. The roadmap remains
active while M007 qualification proceeds. The M006 unblock audit found no
other registered dependent plan in the subsystem or dependency tables.

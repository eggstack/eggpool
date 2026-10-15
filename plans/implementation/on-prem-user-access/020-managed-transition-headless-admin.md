# On-Prem User Access M020 — Managed transition consistency and headless administration

Status: ready

Repository baseline: `88f58bbf2e9095cf46437945edf83e580270bdb6` (`main`, post-PR #7).

Source roadmap: `plans/subsystems/on-prem-user-access-roadmap.md#m020--managed-transition-and-headless-administration`

Long-term requirements: `plans/000-long-term-specification.md#1-product-definition`, `#2-end-state-invariants-normative`; `plans/002-long-term-roadmap.md#phase-5--optional-on-prem-small-team-access-and-budgeting`.

Applicable ADR: `plans/adrs/ADR-0002-on-prem-user-identity-and-policy-boundaries.md` (accepted). Escalate any changed security semantics for ADR review.

Primary class: capability

## 1. Objective

Make first-member transition and small-team management consistent across CLI/browser while keeping one-admin solo installation canonical. Enable headless operators to create, disable/reactivate, inspect and provision members without enabling dashboard browser administration.

## 2. Dependencies and readiness

Hard: M017 credential semantics and M019 browser-security closure, so the transition cannot strand users with invalid tokens or unsafe management guidance. Interface: `identity.rs` DB transactions, `model_policy.rs`, CLI user operations, and session fences. No hardware dependency.

## 3. Current evidence

`rust/src/runtime.rs::users` refuses `users enable-managed` when a shared key is configured or `dashboard.public = true`. `rust/src/identity.rs::create_member_with_fence` transitions the DB to `managed` directly when a member is created without those prerequisites. All initialized modes already reject the legacy shared key through middleware. `rust/src/cli.rs::UsersCommand` includes bootstrap/list/token/revoke/reset-password/policy/budget but no `add`, `disable`, `enable` or `show` member commands. Browser admin can create/disable members.

## 4. Invariants

Managed users never authenticate using legacy shared key, even if stale configuration remains. Bootstrap remains local with non-default password and one initial admin. Browser writes preserve CSRF/session fencing; CLI writes require OS-owned data-directory authority and do not accept passwords in argv. Never create public registration, provider web secrets, hosted groups or a second DB worker.

## 5. Scope

In: reconcile managed state machine, CLI member lifecycle commands and consistent first-member provisioning semantics. Out: adding arbitrary remote administrators, public sign-up, changing role hierarchy, native TLS or new global policy DSL.

## 6. Production changes

Define one state-transition service used by CLI and browser. Distinguish a safe functional auth cutover from *configuration hygiene* (removing a now-dead shared key). Decide whether `admin_initialized` and `managed` are semantically distinct; retain compatible stored states and ensure any automatic transition is atomic with member creation/profile assignment/audit. Add local CLI `users add`, `users disable`, `users enable` and `users show` using existing identity services and actor validation; support TTY/password-stdin and non-TTY secret handling. Do not recreate identities on retries.

## 7. Ordered work packages

### A — State-table review and reconciliation

Freeze actual startup/middleware transitions for legacy, fresh, initialized and managed modes. Unify `enable-managed` and first-member creation preconditions and status messages. Require a plan or ADR if changing a public auth promise.

Acceptance: tests demonstrate same security gates irrespective of CLI/browser trigger, with stale config requiring explicit cleanup but never allowing old key.

### B — Headless member lifecycle

Add CLI member create/enable/disable/show with bounded username/role, optional profile, and protected temporary password via TTY/FD/stdin. Reuse transactional privilege/fencing and mutation audit; preserve last-admin protections. Avoid a CLI path that bypasses initial profile or password-change requirements.

Acceptance: headless workflow creates member, logs in, issues personal token, restricts/disabled account, and records audit.

### C — Transition/compat regression

Test concurrent member-creation attempts, duplicate username, racing disable and token authentication, restart, backup/restore, stale config key, public-dashboard setting and no-dashboard mode. Prove no partial user without assigned profile after a transactional failure.

Acceptance: consistent mode and audit state with no orphan identities or active tokens on failed transition.

## 8. Failure, cancellation, restart and contention

Transaction must commit user, initial profile, access-state and audit together; errors cannot half-enable managed mode. Disability revokes credentials/sessions and fences in-flight admin mutations. No external provider calls or blocking password hashing inside SQLite gate.

## 9. Compatibility/migrations

Prefer no migration and preserve stored access-state labels. If a state merge is necessary, use append-only versioned migration/checksum, tested v54→current restore and backup rollback; never silently change legacy daemon authentication. CLI command names are additions, not behavior changes for old users.

## 10. Tests

Extend `rust/tests/on_prem_identity.rs`, `rust/tests/on_prem_admin_ui.rs`, `rust/tests/on_prem_sessions.rs`, `rust/tests/cli_contract.rs`, `rust/tests/database_compatibility.rs`. Include dashboard-disabled/headless E2E, stale shared key and dashboard.public transition contradictions, first-member atomicity, cross-member isolation and concurrent admin races.

## 11. Verification commands

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
uv run pytest tests/tooling/ -q --tb=short --maxfail=1
git diff --check
```

## 12. Documentation

Update `docs/cli-reference.md`, `docs/deployment.md`, `architecture/deep-dive-security.md`, and administrative onboarding help with exact transition and member CLI examples.

## 13. Acceptance criteria

Fresh solo owner stays operational without additional mode toggles; first member can be provisioned fully headlessly; CLI/browser use identical cutover rules; stale server key cannot authenticate; no session/credential lost on failed transition.

## 14. Stop conditions

Do not proceed until M017/M019 closed. Stop if change reintroduces legacy shared key access, unauthenticated admin setup, breaking migration or inconsistent transactional security.

## 15. Closure evidence

`plans/closure/on-prem-user-access/020-status.md`: implementation SHAs, state matrix, headless CLI and two-user/negative E2E, serial/no-default CI, migration and security review and unblock audit.

## 16. Handoff

P2 capability. Prefer minimizing persistent mode state rather than adding new legacy compatibility flags; accepted ADR still controls bootstrap authority.

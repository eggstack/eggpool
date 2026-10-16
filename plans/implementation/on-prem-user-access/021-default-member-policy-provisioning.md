# On-Prem User Access M021 — Default member policy and provisioning UX

Status: implemented

Repository baseline: `88f58bbf2e9095cf46437945edf83e580270bdb6` (`main`, post-PR #7).

Source roadmap: `plans/subsystems/on-prem-user-access-roadmap.md#m021--default-member-policy-and-provisioning-ux`

Long-term requirements: `plans/000-long-term-specification.md#1-product-definition`, `plans/002-long-term-roadmap.md#phase-5--optional-on-prem-small-team-access-and-budgeting`.

Applicable ADR: `plans/adrs/ADR-0002-on-prem-user-identity-and-policy-boundaries.md` (accepted).

Primary class: capability

## 1. Objective

Make creation of a member useful immediately without weakening default-deny policy enforcement. Offer explicit reusable default-profile and default-budget selection for small teams, while keeping the single-admin personal case zero-configuration.

## 2. Dependencies/readiness

Hard: M020 headless/member lifecycle closure. Interface: `model_policy.rs` snapshots and revision triggers, `identity.rs` atomic create with optional profile, `user_budget.rs` rules and browser admin renderer. No physical SBC performance dependency.

## 3. Current evidence

`rust/src/model_policy.rs::ModelPolicySnapshot::allows` defaults to deny for a member without assigned profile and allow for an unprofiled administrator. `rust/src/server/dashboard/render/admin.rs` offers `Initial profile: No profile` when creating members, which succeeds but can leave an active member unable to use any model. Profiles already carry named exact model allow/deny rules and a preferred model; daily/monthly budgets are separately stored in SQLite.

## 4. Invariants

Default deny remains the internal safe fallback; instance-deny always overrides profile/user allow. Never copy provider secrets into user data or infer a model allow from client-supplied identity. No cloud catalog dependency for provisioning. Preserve revision invalidation, model alias and provider-qualified eligibility and user/global budget intersection.

## 5. Scope

In: optional operator-configured default profile, explicit create-time profile choice, policy visibility/reason codes, admin+CLI consistent defaults. This pass does not add a default budget template: user and instance rules remain independently configured, and member creation copies no budget state. Out: arbitrary role hierarchy, orgs, broad wildcard rules, billing/invoices, prompt/content retention, model policy language redesign.

## 6. Production changes

Provide a bounded instance-local default member provisioning choice, stored in SQLite under admin authorization (never modifying provider TOML). Choose whether one existing named profile can be marked default; do not add duplicate policy semantics. Member creation without explicit profile must either use the configured default transactionally or clearly surface the resulting no-access state before/after creation. Optional default budget template should copy limits at creation with clear provenance/semantics, not silently enforce approximate estimates as hard ceilings.

## 7. Ordered work packages

### A — Default-profile authority

Select and persist one administrator-managed default member profile using existing policy revisions and audit. Retain safe no-profile/deny fallback if no default is configured. Reassignment or removal must have explicit effect and no accidental retroactive grant.

Acceptance: deterministic effective grants for admin, no-profile member and default-profile member.

### B — Provisioning integration

Use default policy during CLI and browser member creation with atomic user/profile/audit write. Show resolved profile and any budget rules before final confirmation. Provide explicit allow-all, restricted/model-specific and local-model-only examples as editable regular profiles; do not embed provider IDs or automatically grant models without consent.

Acceptance: newly created member sees the intended models on `/v1/models`, cannot request a denied provider-qualified target or alias.

### C — Member/operator visibility

Make empty catalog and denied model/budget errors distinguishable to users without exposing upstream private account details. Update admin/member pages and negative policy tests.

Acceptance: clear explanation of why an account has no models; no user can see another user's budgets or profile internals.

## 8. Failure/restart/contention

Creating a user and assigning default profile/budget must not partially commit; concurrent admin change of default must use a consistent transactional snapshot. Policy edits invalidate discovery on next request; in-flight policy snapshot semantics remain unchanged. Do not hold the DB writer across network calls.

## 9. Compatibility/migration

Append a minimal numbered/checksum SQLite migration only if new default metadata is needed; preserve restored v54 and schema62 data. No implicit migration of existing users to a new default or hard budgets; new defaults apply only to future member creation unless an explicit admin action requests otherwise.

## 10. Tests

Extend `rust/tests/on_prem_model_policy.rs`, `rust/tests/on_prem_admin_ui.rs`, `rust/tests/on_prem_identity.rs`, `rust/tests/on_prem_budgets.rs`, `rust/tests/database_compatibility.rs` and `rust/tests/cli_contract.rs`; include no-default fallback, create-time admin race, alias/provider bypass, schema restore and no-dashboard CLI path.

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

Update `docs/deployment.md`, `docs/model-routing.md`, `docs/model-limits.md`, `docs/cli-reference.md`, `architecture/deep-dive-security.md`, and embedded admin help. Preserve configuration clarity for a single admin with no profiles.

## 13. Acceptance criteria

One-admin solo mode remains unrestricted by default, and small-team admins can provide deterministic member defaults via CLI/browser. New member's catalog and admission agree, no surprise model grants occur, and instance/global budget authority is unchanged.

## 14. Stop conditions

Do not start before M020 closure. Stop for architectural review if default policy changes existing users, requires wildcard-based bypass or cannot be created atomically with the member.

## 15. Closure evidence

Create `plans/closure/on-prem-user-access/021-status.md` with policy and budget admission matrix, browser/CLI user journey, negative cross-user tests, migration evidence if any, serial/no-default checks, docs, severity-tagged residuals and unblock audit.

## 16. Handoff

P2 product ergonomics; ordinary small teams should not need a new policy DSL to establish a few reusable access classes.

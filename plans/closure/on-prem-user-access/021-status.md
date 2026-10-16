# On-Prem User Access M021 — Closure Status

Status: closed

Source implementation plan:

- `plans/implementation/on-prem-user-access/021-default-member-policy-provisioning.md`

Source subsystem roadmap:

- `plans/subsystems/on-prem-user-access-roadmap.md#m021--default-member-policy-and-provisioning-ux`

Repository baseline reviewed: post-merge `88f58bbf`; implementation commits
`b76d3590`, `ee563be`, and `3f1ca54` after M020 closure. Status transitions:
`2e99cdd` activation, `4db0ead` active → closing.

## 1. Executive finding

One audited instance-local default profile now applies transactionally to
future member creation through both the local CLI and browser admin. Explicit
profile selection takes precedence, and an explicit no-profile choice bypasses
the default. Setting or clearing the default never edits existing assignments.
When no profile is selected, members remain deny-by-default; a single
administrator without a profile retains the existing unrestricted behavior.

This pass does not copy budget rules or introduce a default budget template.
Per-user and instance budgets remain separate and unchanged.

## 2. Requirement-to-evidence matrix

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| Select or clear one default profile with audit and policy revision invalidation | `ModelPolicyService::set_default_member_profile`; `policy_state_default_member_profile_revision_update`; `on_prem_model_policy::default_profile_provisions_future_members_only_and_is_audited` | pass | Selection and clearing require an active admin; browser mutations also revalidate the session inside the write transaction. Repeating the current choice is idempotent. |
| Apply defaults atomically to future CLI and browser members | `identity::create_member_with_fence`; `on_prem_headless_admin::headless_member_lifecycle_is_atomic_audited_and_works_with_stale_config`; `on_prem_admin_ui::two_user_browser_journey_fences_admin_data_and_personal_credentials` | pass | The default read, member insert, assignment, access transition, and audit writes share one SQLite transaction. CLI and browser paths exercise default selection. |
| Explicit profile and no-profile choices override the configured default | `users add --profile-id` / `--no-profile`; browser `profile_id` handling; `on_prem_headless_admin`; `on_prem_admin_ui`; `cli_contract` | pass | Both CLI and browser integration journeys prove the explicit no-profile path does not inherit the default. |
| Existing users are not retroactively changed; concurrent changes are consistent | `on_prem_model_policy::default_profile_provisions_future_members_only_and_is_audited`; `on_prem_model_policy::member_creation_racing_default_change_observes_one_complete_policy` | pass | Existing assignments remain empty when a default is added; concurrent creation sees exactly one complete old-or-new profile assignment. |
| Profile policy and instance denials still govern visible and requested models | `on_prem_model_policy` qualified allow/deny precedence; `on_prem_admin_ui`; existing coordinator policy admission tests | pass | Provider-qualified denial remains in force. Generic model-forbidden response behavior is unchanged. |
| Empty member model lists explain setup or policy state without private upstream details | `server/dashboard/admin.rs::my_models_page` | pass | Distinguishes no instance models, no member profile, and a profile with no available grants; renders no provider/account data. |
| Existing admin-only personal mode and budgets are unchanged | `on_prem_model_policy`; `on_prem_budgets`; `on_prem_global_budgets`; model-limits docs | pass | No default budget template is added or copied. |
| Migration is additive, checksummed, and default-null | migration `0063_default_member_profile.sql`; checksums/runtime manifest; `database_compatibility::fresh_database_migrates_idempotently_and_preserves_pragmas`; upgrade compatibility target | pass | Existing rows receive no profile value or reassignment. Historical migrations remain immutable. |

## 3. Production implementation evidence

Migration 0063 adds nullable `policy_state.default_member_profile_id` with a
restricting profile foreign key and a revision trigger. Policy administration
lists the selected profile, audits set/clear, increments the shared policy
revision, and prevents deleting a selected default.

`identity.rs` resolves the default in the same transaction as member creation
and its audit rows. The CLI adds `users defaults show|set|clear` and
`users add --no-profile`; the admin UI can use the default, choose a named
profile, or explicitly leave the member unassigned. The member model page
reports safe empty-catalog explanations.

## 4. Verification executed

### Commands run

```bash
rtk cargo fmt --manifest-path rust/Cargo.toml --all -- --check
rtk cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
rtk cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
rtk cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
rtk cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
rtk cargo test --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- --test-threads=1
rtk cargo test --manifest-path rust/Cargo.toml --test on_prem_model_policy -- --test-threads=1
rtk cargo test --manifest-path rust/Cargo.toml --test on_prem_headless_admin -- --test-threads=1
rtk cargo test --manifest-path rust/Cargo.toml --test on_prem_admin_ui -- --test-threads=1
rtk cargo test --manifest-path rust/Cargo.toml --test database_compatibility -- --test-threads=1
rtk cargo test --manifest-path rust/Cargo.toml --test cli_contract -- --test-threads=1
rtk uv run ruff format --check scripts/ tests/tooling/
rtk uv run ruff check scripts/ tests/tooling/
rtk uv run pyright scripts/
rtk uv run pytest tests/tooling/ -q --tb=short --maxfail=1
rtk uv run python scripts/validate_release_docs.py
rtk uv run python scripts/validate_runtime_package_boundary.py
rtk git diff --check
```

### Results

- Default workspace: 1,037 passed, 1 ignored across 80 suites.
- No-default workspace: 1,038 passed, 1 ignored across 80 suites.
- Focused final targets: policy 6, headless admin 2, browser admin 3,
  database compatibility 11, and CLI contract 7 passed.
- Strict Clippy passed with and without default features; no-default workspace
  check passed; formatting passed.
- Tooling: Ruff format/check passed, Pyright reported 0 errors, and pytest
  reported 192 passed and 1 skipped.
- Release-doc and runtime-package-boundary validators passed; `git diff --check`
  passed.

## 5. Invariant review

- No profile remains the safe deny-by-default member fallback; the existing
  administrator default is unchanged.
- Instance denials override profile or user allows; model identifiers remain
  exact and provider-qualified checks remain enforced.
- Defaults apply only to future members. Revision changes refresh new policy
  snapshots without mutating admitted snapshots or existing assignments.
- No provider secrets, prompts, request bodies, or hidden upstream facts enter
  the new policy state, audit records, or member page.
- No request-time network or DB hold across provider I/O was added.

## 6. Failure and recovery review

Default lookup, member creation, optional assignment, access transition, and
audit are one SQLite transaction. A failed assignment rolls back the member
and transition as covered by the headless admin lifecycle test. Concurrent
default mutation and creation serialize at the existing SQLite write gate and
the race test accepts only one complete old-or-new assignment. Clearing is
idempotent; a selected profile cannot be removed before clearing it.

## 7. Migration and compatibility review

Migration 0063 is append-only and nullable, with a checksum entry and runtime
asset hash update. It does not backfill existing user assignments. Fresh
databases have no default selected. Compatibility tests cover historical and
production-schema forward upgrades through version 63, and the no-default
build retains the same identity/policy behavior.

## 8. Security review

CLI writes require an active administrator actor. Browser writes require
same-origin CSRF validation, administrator role, and an in-transaction session
fence. Invalid profile IDs fail without partial member creation. Empty-model
messages do not expose provider topology, accounts, or credentials. Per-user
and instance budget boundaries remain unchanged.

## 9. Documentation and operations

Updated `docs/deployment.md`, `docs/cli-reference.md`,
`docs/model-routing.md`, `docs/model-limits.md`, `docs/backup-restore.md`,
`architecture/deep-dive-security.md`, and
`architecture/deep-dive-database.md`. CLI and browser help explain future-only
default behavior and separate budget configuration.

## 10. Unresolved findings

| Severity | Finding | Impact | Required action |
|---|---|---|---|
| none | None | No known correctness, security, compatibility, or operational issue remains within M021. | None |

## 11. Roadmap disposition

M021 is closed. M022 was already ready because its hard M017 dependency had
closed; this closure does not add a new dependency or change its readiness.
M023 remains blocked on M022 closure and ordinary-server operational evidence
from the independent on-prem concurrency M002 workstream. M002 is registered
ready but no qualifying ordinary Linux/SSD-or-NVMe evidence was added here.

## 12. Registry updates

In the closure commit, M021 moves `closing` → `implemented` and is added to
recent closures in `plans/registry.md`; its roadmap row moves to `closed`.
M022 remains ready and is the next plan in the user-requested sequence. M023
remains blocked; no newly-ready plan was found by the closure audit.

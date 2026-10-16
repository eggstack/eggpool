# On-Prem User Access M020 — Closure Status

Status: closed

Source implementation plan:

- `plans/implementation/on-prem-user-access/020-managed-transition-headless-admin.md`

Source subsystem roadmap:

- `plans/subsystems/on-prem-user-access-roadmap.md#m020--managed-transition-and-headless-administration`

Repository baseline reviewed: post-merge `88f58bbf`; implementation
`8a875a1` after M017/M019 closure and M020 activation.

## 1. Executive finding

The CLI and browser member-creation path now use one database transaction for
the access-state transition, member, optional initial profile, and audit
entries. The first successful member add transitions `admin_initialized` to
`managed`; invalid profile assignment leaves the original state, users, audit,
and existing admin credential unchanged. The explicit `users enable-managed`
command uses the same transition helper and requires an active admin actor.
Stale shared-key and public-dashboard settings are reported as cleanup work;
they neither block the cutover nor restore legacy authentication.

Local operators can add, inspect, disable, and reactivate members. Passwords
come from a hidden TTY prompt or stdin, never argv. Disable revokes current
sessions and personal tokens; reactivation does not restore them. The last
active administrator remains protected.

## 2. Requirement-to-evidence matrix

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| CLI/browser use the same managed-state transition rules | `identity.rs::transition_to_managed`; CLI `enable_managed`; `create_member_with_fence`; `on_prem_identity` | pass | The transition remains atomic with member creation on the first-member path. |
| Failed initial profile assignment leaves no partial user or state | `on_prem_headless_admin::headless_member_lifecycle_is_atomic_audited_and_works_with_stale_config` | pass | State stays `admin_initialized`; no member/transition/profile audit is committed; admin token remains valid. |
| Headless add/show/disable/enable lifecycle is usable | `on_prem_headless_admin`; `cli_contract::headless_member_lifecycle_commands_parse_with_secret_safe_inputs` | pass | Temporary password delivered by stdin; profile assignment, show output, disable, re-enable, and token reissue guidance exercised. |
| Shared key is rejected after cutover even if stale in config | `on_prem_sessions::token_guess_throttle_runs_before_access_state_and_database_lookup`; headless stale-config fixture | pass | A request bearing the configured legacy key receives 401 after managed transition. |
| Access transition requires an active admin and records audit once | `on_prem_identity::managed_transition_requires_an_active_admin_and_audits_once`; stale-settings CLI fixture | pass | Repeated enable is idempotent and does not duplicate transition audit. |
| Concurrent first-member attempts preserve uniqueness and transition integrity | `on_prem_identity::concurrent_first_member_creation_commits_one_transition_and_one_user` | pass | Same normalized username raced; one creation and one transition commit, other attempt conflicts. |
| Disable/re-enable preserves revocation and last-admin protection | Headless CLI fixture; `on_prem_sessions`; `on_prem_admin_ui` | pass | Old token remains invalid after reactivation; last active admin disable is rejected. |
| Existing browser role/session and migration behavior stays intact | `on_prem_admin_ui`, `on_prem_sessions`, `database_compatibility` within workspace suite | pass | No schema migration or role change was added. |

## 3. Implementation evidence

- `rust/src/identity.rs` provides bounded username lookup, shared managed
  transition logic, local member creation with optional profile assignment,
  actor checks, last-admin fencing, credential/session revocation, and audit.
- `rust/src/cli.rs` and `rust/src/runtime.rs` add `users add`, `show`,
  `disable`, and `enable`; explicit `enable-managed` accepts `--actor` and
  works with stale legacy configuration. Temporary passwords are hidden in a
  TTY or read from stdin.
- Member creation, the first managed transition, optional profile assignment,
  and audit rows commit or roll back together. No new DB worker or table was
  added.
- `tests/fixtures/cli/contract-matrix.json`, CLI/deployment docs, and the
  security deep dive now describe the supported operator interface and
  configuration-hygiene boundary.

## 4. Verification executed

- `cargo test --manifest-path rust/Cargo.toml --test cli_contract -- --test-threads=1` — 7 passed.
- `cargo test --manifest-path rust/Cargo.toml --test on_prem_identity -- --test-threads=1` — 5 passed.
- `cargo test --manifest-path rust/Cargo.toml --test on_prem_sessions -- --test-threads=1` — 10 passed.
- `cargo test --manifest-path rust/Cargo.toml --test on_prem_admin_ui -- --test-threads=1` — 3 passed.
- `cargo test --manifest-path rust/Cargo.toml --test on_prem_headless_admin -- --test-threads=1` — 2 passed.
- Default serial workspace: 1,035 passed, 1 ignored across 80 suites.
- No-default serial workspace: 1,036 passed, 1 ignored across 80 suites.
- Strict default/no-default Clippy, no-default workspace check, formatting, Ruff format/check, Pyright, and full tooling pytest passed (192 passed, 1 skipped).
- These are local results. Hosted CI was not run in this closure pass.

## 5. Invariant and recovery review

No migration was introduced; access-state labels and the existing schema stay
compatible. The configured shared key remains rejected after identity
initialization, even if its stale value is still loaded from config. The
public-dashboard flag cannot bypass managed role/session checks. A failed
profile foreign-key insert rolls back the transition, user, and audit rows.
Password hashing occurs before entering the serialized SQLite transaction.
Reactivation advances auth epoch and does not restore revoked credentials.

## 6. Unresolved findings

| Severity | Finding | Impact | Required action |
|---|---|---|---|
| none | No unresolved M020 implementation finding | — | — |

## 7. Roadmap and unblock disposition

M020 is closed. The dependency audit searched the on-prem implementation
plans, roadmap graph, and ready/active/blocked registry tables. M021's sole
hard dependency, M020, is closed, so M021 moves `blocked` → `ready`. M023
remains blocked on M022 and ordinary-server operational evidence. M022 remains
ready. No other registered plan is promoted and no capacity claim is added.

The registry and roadmap record M020 closed and M021 ready. Historical
M001–M019 closure records were not modified.

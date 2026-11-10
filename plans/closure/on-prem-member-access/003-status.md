# On-Prem Member Access Milestone 003 — Closure Status

Status: closed

Source implementation plan:

- `plans/implementation/on-prem-member-access/003-member-navigation-and-admin-roster.md`

Source subsystem roadmap:

- `plans/subsystems/on-prem-member-access-roadmap.md#milestone-3--member-navigation-and-admin-roster`

Repository baseline reviewed: `9da5e2d383c73035850c37f8125c145b8b7ff9ff`

Implementation commits or pull requests:

- `0767576c4b23b53b3125f0dbe75adb731048d9ab` — Implement on-prem member access M001–M005

## 1. Executive finding

Milestone 003 is closed. A member page links only the member destinations, and the administrator users page shows the current assignment, overrides, budget rules in USD, and credential labels without secrets.

`MEMBER_NAVIGATION` is My account, My models, Usage, and Password. `the_standalone_shells_carry_the_navigation_chrome` renders a member usage page with those member links and without `/users`, `/accounts`, `/profiles`, `/runtime`, or the operator overview. The same test still renders those operator links on the administrator shells. The browser journey reaches My account and asserts "My accessible models", and the member's create-user and instance-budget posts are still `403`.

The users roster fixture shows the assigned profile, the selected option, the `model-a deny` override, `1500000 (1.5 USD)`, the credential label, and the creation time. It shows the unassigned member as denied until a profile is assigned. The HTML does not contain the credential id, the selector, the digest, or a secret field. The budget form still posts `max_microdollars`.

The member role gate gained only `/my-account/upstreams` and `/my-account/upstreams/remove`, which milestone 005 uses. `/users`, `/profiles`, and every `/admin/` path stay off that set, so a non-administrator is still refused with the existing HTML refusal.

## 2. Requirement-to-evidence matrix

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| A member session's My account, My models, and Usage pages link to member destinations only | `the_standalone_shells_carry_the_navigation_chrome`; `MEMBER_NAVIGATION` in `rust/src/server/dashboard/render/layout.rs` | pass | The member shell forbids the operator routes named above. |
| An administrator session still reaches Users, Profiles, and Accounts | same test, administrator shell loop | pass | The admin shell contains `/users` and `/accounts` and the overview link. |
| The users page shows the current profile, overrides, budget rules with USD text, and credential labels | `the_users_roster_shows_the_current_assignment_without_secrets` | pass | Assigned and unassigned members are both rendered. |
| The roster HTML contains no password, token, or digest | same roster test | pass | The credential id, the word `digest`, and `name="secret"` are absent. The label and timestamp are present. |
| Existing admin form actions still succeed for an administrator and fail for a member | `rust/tests/on_prem_admin_ui.rs` — `two_user_browser_journey_fences_admin_data_and_personal_credentials` | pass | The member create and instance-budget posts are `403`. The journey still completes the administrator's earlier profile and budget actions before that assertion. |
| Middleware role gates were not widened onto administrative routes | `rust/src/server/middleware.rs` account-surface set | pass | The only added member paths are the two personal-upstream paths from milestone 005. |

## 3. Production implementation evidence

- `rust/src/server/dashboard/render/layout.rs` — `MEMBER_NAVIGATION`, `member_navigation_markup`, and `member_standalone_page`.
- `rust/src/server/dashboard/admin.rs` — member pages use `member_standalone_page`.
- `rust/src/server/dashboard/render/admin.rs` — roster facts for profile, overrides, USD, credentials, and the upstream grant label. The secret field is not on the roster.
- `architecture/deep-dive-dashboard.md` and `architecture/deep-dive-on-prem-user-access.md` describe the member navigation.

## 4. Verification executed

Local, on the developer machine. These commands are not a hosted CI result. The pull request `check` job is the CI truth for the closure commit.

### Commands run

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
cargo build --manifest-path rust/Cargo.toml --locked --release
uv run ruff format --check scripts/ tests/tooling/
uv run ruff check scripts/ tests/tooling/
uv run pyright scripts/
uv run pytest tests/tooling/ -q --tb=short
uv run python scripts/validate_release_docs.py
uv run python scripts/validate_release_workflow.py \
  .github/workflows/release.yml \
  .github/workflows/qualification-release-artifacts.yml \
  .github/workflows/qualification-public-release.yml
```

### Results

| Command | Result |
|---|---|
| `cargo fmt --all -- --check` | clean |
| default `cargo clippy --workspace --all-targets -- -D warnings` | clean |
| `cargo check --workspace --all-targets --no-default-features` | passes |
| no-default `cargo clippy --workspace --all-targets -- -D warnings` | clean |
| `cargo test --workspace --all-targets -- --test-threads=1` | **1257 passed, 0 failed, 2 ignored**, 95 targets, exit 0 |
| `cargo build --manifest-path rust/Cargo.toml --locked --release` | `Finished release profile` in 2m 21s |
| `ruff format --check` | 72 files already formatted |
| `ruff check` | all checks passed |
| `pyright scripts/` | 0 errors, 0 warnings, 0 informations |
| `pytest tests/tooling/` | **253 passed, 2 skipped** |
| `validate_release_docs.py` | pass, 7 documents, version 0.8.3 |
| `validate_release_workflow.py` | pass for all three workflows |

The two ignored tests are pre-existing manual qualifications and are outside this milestone: `qualify_dashboard_ttft_query_plans_and_serialized_write_delay` and `qualified_ollama_model_returns_live_embedding_vectors_through_eggpool`.

The passing suite is the rerun after the schema-54 count correction and the member-sentence correction recorded under milestones 004 and 005.

## 5. Invariant review

- Operator navigation is intact on administrator shells.
- Member shells do not link `/users`, `/accounts`, `/profiles`, `/runtime`, or the operator overview.
- The integer posted to the budget action is still `max_microdollars`. USD is display text beside that integer.
- A non-administrator outside the account surface still receives the member refusal.

## 6. Failure and recovery review

The roster is a render of already-stored facts. It adds no write. A member who posts an administrative form is refused before the handler. The browser journey still refuses those posts with `403`.

## 7. Migration and compatibility review

No schema, configuration, or published inference API change belongs to this milestone. Administrator form actions and their paths are unchanged.

## 8. Security review

The roster fixture contains no password, token, credential id, or digest. Credential display is the label and the timestamps. The member navigation change does not add an administrative path to the member allow list.

## 9. Documentation and operations

`architecture/deep-dive-dashboard.md` and `architecture/deep-dive-on-prem-user-access.md` describe the separate member navigation. No CLI change belongs to this milestone.

## 10. Unresolved findings

| Severity | Finding | Impact | Required action |
|---|---|---|---|
| — | None | — | — |

## 11. Roadmap disposition

Milestone 003 is closed. Milestones 001, 002, 004, and 005 are closed in the same commit by their own records. The on-prem member-access roadmap is closed. No successor is registered.

## 12. Registry updates

`plans/registry.md` moves M001–M005 out of the active table and records the roadmap closed. The roadmap status table links this record.

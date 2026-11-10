# On-Prem Member Access Milestone 002 — Closure Status

Status: closed

Source implementation plan:

- `plans/implementation/on-prem-member-access/002-usage-windows-and-member-limits.md`

Source subsystem roadmap:

- `plans/subsystems/on-prem-member-access-roadmap.md#milestone-2--usage-windows-and-member-limits`

Repository baseline reviewed: `9da5e2d383c73035850c37f8125c145b8b7ff9ff`

Implementation commits or pull requests:

- `0767576c4b23b53b3125f0dbe75adb731048d9ab` — Implement on-prem member access M001–M005

## 1. Executive finding

Milestone 002 is closed. A member's current day and current month are separate totals, and remaining for a window is that window's ceiling minus its settled usage minus its active holds.

The budget fixture writes 40 settled requests into the member's current day and 40 into the current month, plus an old day bucket and another user's 99. The current day total is 40 and the current month total is 40. They are not added. With a day ceiling of 100 and 10 active holds, remaining is 50. The other user's allowance is empty. The member usage page names the day window and says other people's consumption is not shown. The administrator page renders the two windows under separate headings and does not render a summed 14.

The member `/api/user-budget` payload adds `remaining` and `instance_rules`. It still omits `instance_usage`. An instance usage row of 80 stays readable through `InstanceBudgetService` and is still present on the administrator payload. Settlement functions in `rust/src/user_budget.rs` were not edited. This milestone adds no migration. The v66 checksum in the same implementation commit belongs to milestone 004.

## 2. Requirement-to-evidence matrix

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| One settled request appears once in the current day and once in the current month, and those totals are not added | `rust/tests/on_prem_budgets.rs` — `usage_windows_are_not_added_and_remaining_subtracts_holds` | pass | Both window totals are 40. The test asserts they are not equal to their sum. |
| A member page names day and month on each usage row | `rust/src/server/dashboard/tests.rs` — `member_usage_names_the_window_and_admin_windows_stay_separate` | pass | The member page contains `>day<`. The admin page contains "Current UTC day" and "Current UTC month" and does not contain a summed `>14<`. |
| Remaining matches ceiling minus settled minus active holds | same budget test | pass | Ceiling 100, settled 40, held 10, remaining 50. An absent token ceiling stays `None`. |
| A member response excludes instance usage and other users | `rust/tests/on_prem_global_budgets.rs` — `member_allowance_excludes_other_users_and_instance_usage` | pass | Member one settled 4, remaining 16. Member two settled 9, remaining 11. The encoded allowance contains neither `instance_usage` nor the other username. |
| Instance ceiling configuration is visible and instance usage stays admin-only | `rust/src/server/health.rs` — `user_budget_api`; the same global-budget test | pass | Members receive `instance_rules`. `instance_usage` is added only when the principal role is `admin`. The instance service still reads the row of 80. |
| An administrator can still read instance usage | `user_budget_api` administrator branch; `InstanceBudgetService::usage` | pass | The administrator JSON still includes `instance_usage`. |
| Settlement code and schema checksums did not change for this milestone | `git diff` of `rust/src/user_budget.rs` adds `current_allowance`, `admin_usage_by_window`, and read helpers. No settlement function is in the diff. No migration file belongs to this milestone. | pass | v66 is milestone 004. |

## 3. Production implementation evidence

- `rust/src/user_budget.rs` — `current_allowance` and `admin_usage_by_window` read one window at a time. Holds are subtracted inside that window.
- `rust/src/server/health.rs` — member JSON gains `remaining` and `instance_rules` and still withholds `instance_usage`.
- `rust/src/server/dashboard/render/admin.rs` — separate day and month tables, USD text via `format_microdollar_usd`.
- `docs/model-limits.md`, `docs/api-reference.md`, and `architecture/deep-dive-on-prem-user-access.md` describe the projection.

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

The first two full-suite invocations failed before this passing run. The schema-54 upgrade count and the denied-member sentence were corrected in the closure commit. Those corrections belong to milestones 004 and 005. The counts above are the rerun.

An earlier locked release invocation failed because the release `libsqlite3-sys` build directory was missing `bindgen.rs`. `cargo clean -p libsqlite3-sys --release` and the same locked release build then finished. That was a stale build directory, not a product failure.

## 5. Invariant review

- Day and month remain two enforcement buckets. The projection does not delete or merge the month bucket.
- Remaining is computed inside one window. Holds in that window reduce remaining. Another user's rows do not.
- Instance usage stays off the member payload. Instance ceiling configuration is the rule list, not a remaining shared headroom figure.
- The single SQLite gate is unchanged. The new reads use the existing `Database::call` path.

## 6. Failure and recovery review

A failed allowance or instance-rule read returns the existing degraded user-budget response. No new write, reservation, or settlement step was added, so there is no new partial-persistence path. An old day bucket is excluded from the current-day total by the window start.

## 7. Migration and compatibility review

No table, column, or checksum change belongs to this milestone. `rules` and `usage` remain on `/api/user-budget`. The added fields are `remaining` and, for members, `instance_rules`. Administrator responses still include `instance_rules` and `instance_usage`.

## 8. Security review

A non-administrator still cannot query another user's id: the existing owner-or-admin check in `user_budget_api` is unchanged. The member payload used in the fixture does not contain the other username or `instance_usage`. USD text is a rendering of the stored integer microdollars. The budget form still posts `max_microdollars` as that integer.

## 9. Documentation and operations

`docs/model-limits.md`, `docs/api-reference.md`, and `architecture/deep-dive-on-prem-user-access.md` describe the separate windows and the member payload. `validate_release_docs.py` passed.

## 10. Unresolved findings

| Severity | Finding | Impact | Required action |
|---|---|---|---|
| — | None | — | — |

## 11. Roadmap disposition

Milestone 002 is closed. Milestones 001 and 003–005 are closed in the same commit by their own records. The on-prem member-access roadmap is closed. No successor is registered.

## 12. Registry updates

`plans/registry.md` moves M001–M005 out of the active table and records the roadmap closed. The roadmap status table links this record.

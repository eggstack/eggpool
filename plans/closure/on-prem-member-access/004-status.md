# On-Prem Member Access Milestone 004 — Closure Status

Status: closed

Source implementation plan:

- `plans/implementation/on-prem-member-access/004-personal-upstream-custody-and-routing.md`

Source subsystem roadmap:

- `plans/subsystems/on-prem-member-access-roadmap.md#milestone-4--personal-upstream-custody-and-routing`

Repository baseline reviewed: `9da5e2d383c73035850c37f8125c145b8b7ff9ff`

Implementation commits or pull requests:

- `0767576c4b23b53b3125f0dbe75adb731048d9ab` — Implement on-prem member access M001–M005
- The closure commit corrects the schema-54 upgrade count from 11 migrations to 12 so the chain through v66 is what the test asserts.

Governing decision:

- `plans/adrs/ADR-0010-personal-upstream-account-custody.md` (accepted 2026-11-10, in the implementation commit above)

## 1. Executive finding

Milestone 004 is closed. An administrator can attach one bundled-provider account to one user. The secret is written only to the config-adjacent `.env` with mode `0600`. The TOML stanza names `EGGPOOL_UPSTREAM_` plus the first 8 bytes of SHA-256 over `provider`, a NUL, and the account name. The database row stores ids and state and the audit row stores the account name. Neither stored text contains the secret.

Routing excludes that account for every principal except the bound user. A withdrawn binding is excluded for the owner as well. An account with no binding is not excluded. A failed publication restores the original config, removes a newly created `.env`, and deletes the binding, so the account does not become shared. The instance grant inserted by the migration is `deny`. User disable withdraws active bindings, and re-enable does not restore them.

Amazon Bedrock is not a bundled provider and is not added. v1–v65 migration files are unchanged. v66 is `0066_personal_upstream.sql`, checksum `5f37ccad85c006e3e535322155d52a3bb75cc2fad649af54021c9905db742e4c`.

This is the custody and CLI consumer. The member-facing grant is milestone 005.

## 2. Requirement-to-evidence matrix

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| ADR-0010 is accepted | `plans/adrs/ADR-0010-personal-upstream-account-custody.md` status `accepted`, date 2026-11-10 | pass | Accepted in `0767576c` with the named canonical amendments. |
| Before any binding, an account is not excluded | `PersonalUpstreamSnapshot::excludes` returns false when the key is absent | pass | The eligibility loop consults that predicate before scoring. |
| An administrator can add an `opencode-go` account for one user | `rust/tests/on_prem_identity.rs` — `personal_upstream_grant_defaults_to_deny_and_a_failed_publish_is_not_shared` | pass | The secret is in `.env`, absent from the TOML, and the TOML names `EGGPOOL_UPSTREAM_`. |
| The database row and the audit row have no secret | same test, concatenated grants, bindings, and `user_admin_audit` | pass | The stored text does not contain `sk-personal-sentinel-value`. |
| That user can be routed to the account. Another user and an unattributed caller cannot | `rust/tests/routing_domain.rs` — `a_personal_binding_is_selectable_only_by_its_owner`; snapshot asserts in the identity test | pass | The owner receives `account-a`. The other user and a missing principal get no claim. |
| A withdrawn account is routed to nobody | same routing test after `state = 'withdrawn'` | pass | The owner is also excluded. |
| A failed rehash does not leave the account shared | identity test with `PublishMode::Fail` | pass | The error is `PublicationRestored`. The config bytes match the original, `.env` is absent, and the binding list is empty. |
| Self-service default is deny | migration insert and `instance_grant` / `effective_grant` in the identity test | pass | Both reads are `GrantEffect::Deny`. |
| The CLI can set allow, deny, and inherit | `UsersUpstreamCommand::Grant` value parser `allow`, `deny`, `inherit`; `runtime.rs` dispatches to `set_user_grant` | pass | See the low finding in §10. The browser allow and deny path is milestone 005. |
| User disable withdraws the binding and re-enable does not restore it | identity test after `set_user_enabled` | pass | The snapshot excludes the owner after disable and still excludes the owner after enable. |
| A second add of the same name is a collision | identity test | pass | `PersonalUpstreamError::NameCollision`. |
| Amazon Bedrock is not added | `bundled_provider` rejects an id that is not in the bundled asset; `member_upstream_form_hides_the_key_until_self_service_is_allowed` asserts the select does not contain `amazon` | pass | No Bedrock profile was added. |
| Migration v66 and its checksum | `0066_personal_upstream.sql`; `checksums.json`; `MigrationRunner::migrations().len() == 66` | pass | The schema-54 upgrade now asserts 12 applied versions, 55 through 66 inclusive. |
| No-default clippy | see §4 | pass | `--no-default-features` check and clippy are clean. |
| Account add/remove diagnostics do not record the secret | `account_env_name` in `config_reload_policy.rs` | pass | The change record stores the env-var name. The account transition stays non-reloadable. No new reload-key list was added. |

## 3. Production implementation evidence

- `rust/assets/db/migrations/0066_personal_upstream.sql` — `personal_upstream_grants` and `personal_upstream_bindings`. No secret column. The instance row is `deny`.
- `rust/src/personal_upstream.rs` — grants, bindings, snapshot, env-var name, and the add/remove service.
- `rust/src/operations/config_mutation.rs` — `.env` is written mode `0600` before credential validation. A validation or config-write failure restores the previous `.env`.
- `rust/src/accounts/registry.rs` — `CredentialStore::from_config_near` reads the config-adjacent env file.
- `rust/src/routing/eligibility.rs` — `build_eligible_candidates` excludes on `personal_bindings.excludes` before the account is scored.
- `rust/src/cli.rs` and `rust/src/runtime.rs` — `eggpool users upstream` default, grant, add, list, and remove. Add reads the secret from stdin.
- `docs/cli-reference.md`, `docs/providers.md`, `docs/backup-restore.md`, `architecture/deep-dive-routing.md`, and `architecture/deep-dive-on-prem-user-access.md`.

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

The two ignored tests are pre-existing manual qualifications: `qualify_dashboard_ttft_query_plans_and_serialized_write_delay` and `qualified_ollama_model_returns_live_embedding_vectors_through_eggpool`.

The first full-suite invocation failed in `schema54_image_upgrades_with_legacy_state_preserved`: `applied_this_run.len()` was 12 and the assertion still expected 11. The closure commit expects 12 and says the range is migrations 55 through 66 inclusive. The passing count above is the rerun after that correction and the milestone 005 sentence correction.

An earlier locked release invocation failed because the release `libsqlite3-sys` build directory was missing `bindgen.rs`. `cargo clean -p libsqlite3-sys --release` and the same locked release build then finished. That was a stale build directory, not a product failure.

The tooling census required `personal_upstream.rs` in the `AGENTS.md` layout section and `v1–v66` in the persistence roadmap's current-chain sentence. Both are updated in the closure commit. The persistence roadmap still authorizes no schema change of its own. v66 was added by this milestone.

## 5. Invariant review

- One SQLite writer. Grants and bindings use `Database::call` and `with_transaction`.
- No second routing lock. Exclusion is a membership test on the candidate loop that already existed.
- Selector decisions are unchanged. The filter runs inside account eligibility and cannot pin an account that failed the predicate.
- Attempt preparation is unchanged. The snapshot is generation state, read during candidate build.
- Reload classification stays in `classify_transition`. Account add and remove remain non-reloadable. The diagnostic value is the env-var name, not the secret.
- Credentials stay out of SQLite, audit rows, and the reload diagnostic.

## 6. Failure and recovery review

`PublishMode::Fail` restores the config bytes, does not leave `.env`, and leaves no binding. Credential validation runs after the `.env` write and restores that file on failure. Remove withdraws the binding before the config and env omission. A withdrawn row stays withdrawn across user re-enable. A name collision does not insert a second row. In-flight requests keep the generation they started on; the snapshot is replaced with the generation.

## 7. Migration and compatibility review

v66 is append-only. v1–v65 files and checksums are unchanged. Fresh databases apply 66 migrations. A schema-54 image applies 55 through 66 inclusive and then passes `quick_check`. Existing accounts with no binding stay eligible for every principal. `Cargo.lock` gained no dependency. The locked release build passed.

Rollback of a failed add restores the files and deletes the binding. A failed remove after the binding is withdrawn reports repair rather than republishing the account as shared.

## 8. Security review

The secret is not a CLI argument. Add requires `--secret-stdin`. The env file mode is `0600` on Unix. SQLite has no secret column. The identity test dumps grants, bindings, and audit text and asserts the sentinel is absent. The env-var name is a hash prefix, not the secret. Self-service remains deny until a grant changes it. Grant denial does not, by itself, withdraw a binding; that withdraw path is disable, or an explicit remove.

## 9. Documentation and operations

`docs/cli-reference.md`, `docs/providers.md`, `docs/backup-restore.md`, `architecture/deep-dive-routing.md`, and `architecture/deep-dive-on-prem-user-access.md` describe the binding, the env file, and the exclusion filter. `AGENTS.md` names `personal_upstream.rs`. `architecture/overview.md` lists the module and the new routes. The persistence roadmap's current-chain sentence now says v1–v66. `validate_release_docs.py` passed.

## 10. Unresolved findings

| Severity | Finding | Impact | Required action |
|---|---|---|---|
| low | `eggpool users upstream grant` accepts `allow`, `deny`, and `inherit` in clap and dispatches to `set_user_grant`. No test spawns the CLI process for those three effects. | The service and the browser allow/deny path are tested. A parser regression that dropped `inherit` would not be caught by a process test. | None for closure. A later CLI contract test can cover the three effects if that surface grows. |

## 11. Roadmap disposition

Milestone 004 is closed. Milestone 005's interface matches the shipped binding: grant denial blocks member add and remove and leaves an existing binding selectable. Milestones 001–003 and 005 are closed in the same commit by their own records. The roadmap is closed. No successor is registered.

## 12. Registry updates

`plans/registry.md` moves M001–M005 out of the active table and records the roadmap closed. The roadmap status table links this record.

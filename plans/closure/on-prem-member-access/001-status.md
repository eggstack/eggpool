# On-Prem Member Access Milestone 001 — Closure Status

Status: closed

Source implementation plan:

- `plans/implementation/on-prem-member-access/001-solo-sign-in-and-onboarding-credential.md`

Source subsystem roadmap:

- `plans/subsystems/on-prem-member-access-roadmap.md#milestone-1--solo-sign-in-and-onboarding-credential`

Repository baseline reviewed: `9da5e2d383c73035850c37f8125c145b8b7ff9ff`

Implementation commits or pull requests:

- `0767576c4b23b53b3125f0dbe75adb731048d9ab` — Implement on-prem member access M001–M005
- The closure commit that adds this record corrects two suite defects found while gathering this evidence. Neither changes the onboarding credential decision.

## 1. Executive finding

Milestone 001 is closed. A fresh onboard no longer writes a server key that the same run retires, and an unauthenticated browser on a managed instance is sent to the existing sign-in page. Machine routes stay JSON 401.

`apply_onboarding_server_key` writes `[server].api_key` only when the database is `legacy_unmigrated` and the key is absent. A new configuration, a missing access state, `fresh_uninitialized`, `admin_initialized`, and `managed` skip the write. An existing key is left in place. The printed guidance names `/login`. A line that mentions `EGGPOOL_API_KEY` does not call that value the server key.

An unauthenticated `GET` of an HTML navigation page on `admin_initialized` or `managed` is a `303` to `/login` with `Cache-Control: no-store` and no password form. `GET /v1/models` and `GET /api/user-budget` without a credential stay JSON 401. `POST /account` stays 401. A personal API token does not authorize `/account` or `/api/status`. A `legacy_unmigrated` public dashboard is still `200` and is not redirected.

No migration, dependency, or authentication-mode change belongs to this milestone.

## 2. Requirement-to-evidence matrix

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| A new configuration passed through the fresh bootstrap path writes no `[server].api_key` | `rust/src/runtime.rs` — `fresh_onboarding_does_not_write_a_server_key_before_bootstrap`, `initialized_onboarding_does_not_write_a_server_key`, `onboarding_writes_a_server_key_only_for_a_legacy_database` | pass | The fresh path compares the file bytes before and after and reads the key back as absent. |
| Guidance names `/login` and does not export the server key | `bootstrap_leaves_an_existing_server_key_unchanged` | pass | An existing key is left byte-identical. Guidance that contains `EGGPOOL_API_KEY` must not contain "server". |
| A browser `GET` of a managed dashboard without a cookie can follow one redirect to `/login` | `rust/tests/on_prem_sessions.rs` — `managed_browser_login_account_csrf_and_logout_work_over_loopback` | pass | `GET /` and `GET /usage` are `303` to `/login`, `no-store`, and contain no password field. |
| `curl` of `/v1/models` without a key still receives JSON 401 | same HTTP test | pass | `/v1/models` and `/api/user-budget` are `401` and contain `"detail"`. `POST /account` is `401` and is not a `303`. |
| A pre-existing legacy database still authenticates with its server key | `legacy_onboarding_still_writes_a_missing_server_key`; `legacy_public_dashboard_stays_available_without_a_sign_in_redirect`; `legacy_server_key_is_valid_only_before_identity_cutover` | pass | The key is generated only for `legacy_unmigrated`. The legacy public dashboard stays `200`. The server key is accepted only in that access state. |
| The handoff does not apply to `/v1`, `/api`, or an unsafe method | `initialized_html_navigation_offers_sign_in_and_machine_routes_stay_json` | pass | `/v1/models`, `/api/user-budget`, and `/api/status` are excluded. `POST /` is excluded. `fresh_uninitialized` and `legacy_unmigrated` are excluded. |
| No migration, dependency, or auth-mode change | This milestone adds no file under `rust/assets/db/migrations/` and no dependency. `Cargo.lock` is unchanged by the implementation commit. | pass | v66 belongs to milestone 004. |

## 3. Production implementation evidence

- `rust/src/runtime.rs` — `should_generate_legacy_server_key`, `apply_onboarding_server_key`, and `onboarding_credential_guidance`.
- `rust/src/server/auth.rs` — `sign_in_handoff_response` is a `no-store` redirect to `/login` and embeds no password form.
- `rust/src/server/middleware.rs` — `sign_in_handoff_applies` is limited to `GET` of an HTML navigation page when the access state is `admin_initialized` or `managed`.
- `rust/config.example.toml`, `README.md`, and `docs/deployment.md` describe the fresh-onboard credential.
- `architecture/deep-dive-on-prem-user-access.md` records the handoff.

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

The first full-suite invocation stopped in `database_compatibility` because the schema-54 upgrade still expected 11 migrations after v66. The assertion now expects 12, migrations 55 through 66 inclusive. That correction belongs to milestone 004. The second invocation stopped in `on_prem_admin_ui` because the denied-member sentence contained the substring `admin`, which is the fixture username. The sentence now says "The instance operator". That correction belongs to milestone 005. The counts above are the rerun after both corrections.

An earlier locked release invocation failed because the release `libsqlite3-sys` build directory was missing `bindgen.rs`. `cargo clean -p libsqlite3-sys --release` and the same locked release build then finished. That was a stale build directory, not a product failure.

The first tooling run failed two census guards: `AGENTS.md` did not name `personal_upstream.rs`, and the persistence roadmap still said the current chain was v1–v65. Both documents now state the current facts. The pytest count above is the rerun.

## 5. Invariant review

- The server key remains the legacy shared credential. It is still accepted only while the access state is `legacy_unmigrated`.
- Fresh, initialized, and managed installs do not gain a newly written server key.
- `/v1` and `/api` do not receive the HTML redirect. A missing credential there is still a JSON 401.
- The sign-in form is still the `/login` document. The redirect does not embed a password form, so the transport gate in `rust/src/server/auth.rs` is unchanged.
- Personal API tokens remain limited to the agent surfaces already enforced by `personal_credential_allowed`.

## 6. Failure and recovery review

Onboard writes the key only for the legacy missing-key case and leaves an existing file byte-identical otherwise. A failed key decision does not create a second disclosure path. The redirect is a single `303` to `/login`; `POST` is not redirected. No new persistence step was added.

## 7. Migration and compatibility review

No schema, checksum, or dependency change belongs to this milestone. A legacy database that still has no server key still receives one. A legacy public dashboard is not redirected. Existing managed sign-in, CSRF, and logout behavior in the same HTTP test still completes.

## 8. Security review

The handoff does not authenticate the caller and does not accept a credential. It does not open an unauthenticated inference path. The redirect body contains no password field and is `no-store`. The server key is not printed as the token to export.

## 9. Documentation and operations

`README.md`, `docs/deployment.md`, `rust/config.example.toml`, and `architecture/deep-dive-on-prem-user-access.md` describe the fresh-onboard credential and the sign-in handoff. `validate_release_docs.py` passed.

## 10. Unresolved findings

| Severity | Finding | Impact | Required action |
|---|---|---|---|
| — | None | — | — |

## 11. Roadmap disposition

Milestone 001 is closed. Milestones 002–005 are closed in the same commit by their own records. The on-prem member-access roadmap is closed. No successor is registered. On-prem user access M001–M024 stay closed.

## 12. Registry updates

`plans/registry.md` moves M001–M005 out of the active table and records the roadmap closed. The roadmap status table links this record.

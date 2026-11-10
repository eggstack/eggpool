# On-Prem Member Access Milestone 005 — Closure Status

Status: closed

Source implementation plan:

- `plans/implementation/on-prem-member-access/005-personal-upstream-browser-grant.md`

Source subsystem roadmap:

- `plans/subsystems/on-prem-member-access-roadmap.md#milestone-5--personal-upstream-browser-grant`

Repository baseline reviewed: `9da5e2d383c73035850c37f8125c145b8b7ff9ff`

Implementation commits or pull requests:

- `0767576c4b23b53b3125f0dbe75adb731048d9ab` — Implement on-prem member access M001–M005
- The closure commit rewords the denied-member sentence so it does not contain the fixture username `admin`.

## 1. Executive finding

Milestone 005 is closed. A member who has not been granted self-service sees no secret field and receives `403` on add. An administrator can allow that member from `/users`. The member can then submit one bundled-provider key from My account. The secret is in `.env`, absent from the config, absent from the HTML responses, and absent from the stored grants, bindings, and audit text. A personal API token cannot call the mutation. Setting the grant back to deny hides the field and leaves the existing binding and the env entry in place.

The provider select offers `opencode-go` and does not offer Amazon Bedrock. The handlers call the milestone 004 service. Grant denial does not withdraw the binding.

The denied sentence now says "The instance operator can add an account from the command line." The previous wording, "An administrator...", made the member-page fence test fail because the fixture username is `admin`.

## 2. Requirement-to-evidence matrix

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| Default deny: My account has no upstream secret field | `member_upstream_form_hides_the_key_until_self_service_is_allowed`; `personal_upstream_browser_grant_never_echoes_the_key` | pass | The denied page contains "not granted" and does not contain `name="secret"`. The denied POST is `403`. `.env` is not created. |
| An administrator can allow one user | same browser test, `POST /admin/users/upstream/grant` with `effect=allow` | pass | The response is `303`. The users page contains the grant form and does not contain `name="secret"`. |
| That user can add an OpenCode Go account in the browser | same test, `POST /my-account/upstreams` | pass | The response is `303`. `.env` contains the sentinel. The config does not. The following My account page names `personal-browser` and does not contain the sentinel. |
| The secret is absent from the response and the database | same test | pass | The add response, the account page, and the concatenated grant, binding, and audit text do not contain the sentinel. |
| Deny again hides the field and keeps the binding | same test, second grant POST with `effect=deny` | pass | The binding list still has one row. `.env` still contains the sentinel. The page has no secret field. |
| A personal API token cannot add or remove an upstream account | same test, `POST /my-account/upstreams` with the admin token and no session cookie | pass | The response is `401` and does not contain the sentinel. |
| CSRF | the new posts include the session CSRF field; `requires_browser_session` includes the two member paths and every `/admin/` path | pass | The existing missing-cookie mutation test still refuses an account mutation. The new posts are browser-session posts, not token posts. |
| Amazon Bedrock is not offered | `member_upstream_form_hides_the_key_until_self_service_is_allowed` | pass | The allowed form contains `value="opencode-go"` and does not contain `amazon`. |
| Grant denial does not withdraw existing bindings | browser test after the deny POST; milestone 004 `effective_grant` | pass | The 004 service was not changed to treat denial as withdraw. |
| Milestones 001–004 are closed in the same commit | their closure records | pass | This record does not close the roadmap alone. |

## 3. Production implementation evidence

- `rust/src/server/dashboard/admin.rs` — `set_upstream_default`, `set_upstream_grant`, `remove_upstream_account`, `add_member_upstream`, `remove_member_upstream`. Each mutation calls `personal_upstream`.
- `rust/src/server/dashboard/render/admin.rs` — `render_member_upstream` and the users-page grant controls.
- `rust/src/server/mod.rs` registers the five routes.
- `rust/src/server/middleware.rs` — the two member paths are on the member account surface and require a browser session. They are not personal-API-token routes.
- `docs/deployment.md` and `architecture/deep-dive-on-prem-user-access.md` describe the grant.
- `architecture/overview.md` lists `/my-account/upstreams`, `/my-account/upstreams/remove`, `/admin/users/upstream/default`, `/admin/users/upstream/grant`, and `/admin/users/upstream/remove`.

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

The second full-suite invocation failed `two_user_browser_journey_fences_admin_data_and_personal_credentials` because the denied-member copy contained `administrator` and the fixture username is `admin`. The closure commit uses "The instance operator". `personal_upstream_browser_grant_never_echoes_the_key` had already passed in that invocation. The passing count above is the rerun.

## 5. Invariant review

- Custody and eligibility stay in milestone 004. These handlers do not write `.env` or TOML themselves.
- A grant of deny does not withdraw a binding.
- Member navigation stays on the member destinations. The secret form is a section of My account, not a new operator link.
- Personal API tokens remain excluded from browser mutations. The token POST is `401`.
- The provider list is the bundled profile ids. The rendered select used in the test does not contain `amazon`.

## 6. Failure and recovery review

A denied member POST does not create `.env` or a binding. A failed add uses the 004 rollback. The browser test's successful add is followed by a deny that preserves the binding. CSRF failures follow the existing browser-session check before the handler. The token attempt is refused before a write.

## 7. Migration and compatibility review

No migration belongs to this milestone. The routes are HTML form actions. `/api/user-budget` is unchanged by this milestone. Existing administrator user actions in the browser journey still complete.

## 8. Security review

The secret is a password input on the granted member form only. It is not rendered back. The users page has grant controls and no secret input. The stored grant, binding, and audit text in the browser test do not contain the sentinel. The member paths require a browser session. A personal token is not a session.

## 9. Documentation and operations

`docs/deployment.md` and `architecture/deep-dive-on-prem-user-access.md` describe the browser grant. The route topology in `architecture/overview.md` lists the five new paths. `validate_release_docs.py` passed.

## 10. Unresolved findings

| Severity | Finding | Impact | Required action |
|---|---|---|---|
| — | None | — | — |

## 11. Roadmap disposition

Milestone 005 is closed. Milestones 001–004 are closed in the same commit by their own records, which is the condition this milestone set for closing the roadmap. The on-prem member-access roadmap is closed. No successor is registered. Nothing else was blocked on this roadmap.

## 12. Registry updates

`plans/registry.md` moves M001–M005 out of the active table and records the roadmap closed. The roadmap status table links this record. The unblock audit records that no other plan is promoted.

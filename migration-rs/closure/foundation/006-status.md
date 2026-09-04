# F006 Closure — Side-by-Side Safety and Serve-Contract Closure

Status: closed

Recommendation: closed; M4 Provider HTTP + Eggress roadmap and implementation
handoff may now be drafted and registered when independently reviewed.

Implementation commit: [`df902b5`](https://github.com/eggstack/eggpool/commit/df902b5)

Plan: [F006 — side-by-side safety and serve-contract closure](../../implementation/foundation/006-side-by-side-safety-and-serve-contract-closure.md)

## Outcome

F006 closes the corrective defects found after F005. Rust now reserves the
configured listener before opening or mutating SQLite, and all tested
post-admission startup failures release resources before returning a non-zero
result. The executable Rust serve contract is explicit: only
`serve --verbose` starts the migration-stage foreground candidate; daemon and
root-gated options fail before config/database/listener side effects.

Python remains the production implementation. No provider transport, Eggress,
daemon lifecycle, runtime-generation, routing, or inference scope was pulled
forward.

## Requirement matrix

| Requirement | Evidence | Result |
|---|---|---|
| Listener ownership precedes durable startup | `rust/src/server.rs::run` binds before `Database::open`, migration, or `sync_accounts`; `test_bind_rejection_does_not_create_nonexistent_database` | Pass |
| Bind rejection has zero durable side effects | `test_bind_rejection_does_not_create_nonexistent_database` preserves absence; `test_bind_rejection_preserves_existing_migration_and_account_state` preserves 54 migration rows and account facts, with the occupier still running | Pass |
| Post-bind failures clean up | `test_post_bind_database_failure_releases_listener` uses a database directory to force open failure and successfully rebinds the port; process exits boundedly | Pass |
| Explicit serve contract | `runtime::validate_serve_args`; parameterized black-box coverage for plain `serve`, `--log-file`, `--quiet`, and `--as-root`; F005 invocation updated to `serve --verbose` | Pass |
| Foreground development path remains usable | Existing F005 health/SSR/shutdown tests and F006 server launches use `serve --verbose` | Pass |
| `server.threads` staging is visible | `server::run` emits a bounded warning for non-default values; `test_non_default_threads_are_accepted_but_report_staged_runtime` observes acceptance plus the single-threaded diagnostic | Pass |
| Checksum parsing is structural | `rust/build_support.rs` uses semantic JSON deserialization with duplicate/type/shape/checksum validation; `rust/tests/build_manifest.rs` covers compact/reformatted valid JSON and malformed content | Pass |
| Dual-run writable-state safety | `IsolatedEnvironment.implementation_root` and `database_path` provide per-implementation writable roots; migration guidance and Rust README prohibit shared concurrent SQLite writers | Pass |
| Scope remains bounded | Dependency delta is build-time `serde`/`serde_json` only; no provider, Eggress, runtime-generation, or lifecycle implementation was added | Pass |

## Serve behavior matrix

| Invocation | Rust F006 behavior | Python comparison |
|---|---|---|
| `serve --verbose` | Supported foreground candidate; starts the current-thread server | Python foreground mode |
| `serve` | Exit 1 with deferred-daemon diagnostic; no listener or DB side effect | Python daemon mode |
| `serve --log-file ...` | Exit 1; daemon log routing deferred | Python daemon output option |
| `serve --quiet` | Exit 1; daemon output suppression deferred | Python daemon output option |
| `serve --as-root` | Exit 1; root-gated lifecycle behavior deferred | Python root policy option |
| `serve --verbose` with `server.threads != 1` | Starts, but emits that the field is compatibility-only and Rust remains single-threaded | Python maps the field to Granian runtime threads |

Rust's supported differences are intentional migration-stage behavior and are
now explicit rather than silently reinterpreting Python daemon options.

## Verification evidence

Completed successfully:

- `rtk cargo fmt --manifest-path rust/Cargo.toml -- --check`
- `rtk cargo clippy --manifest-path rust/Cargo.toml --all-targets -- -D warnings`
- `rtk cargo test --manifest-path rust/Cargo.toml` — 19 tests passed
- `rtk uv run pytest tests/migration_rs -q --tb=short --maxfail=1` — 31 tests passed
- `rtk uv run pytest tests/unit/test_config.py tests/unit/test_db.py tests/smoke/ -q --tb=short --maxfail=1` — 149 tests passed
- `rtk uv run ruff format --check tests/migration_rs/test_f006_safety.py tests/migration_rs/test_f005_server.py tests/migration_rs/harness.py tests/migration_rs/__init__.py`
- `rtk uv run ruff check tests/migration_rs/test_f006_safety.py tests/migration_rs/test_f005_server.py tests/migration_rs/harness.py tests/migration_rs/__init__.py`
- `rtk git diff --check`

The migration suite includes the prior F003/F004/F005 differential and read
plane coverage; it passes with the explicit foreground invocation. The new
black-box assertions inspect SQLite rows and filesystem existence, not just
logs or exit status. The checksum test accepts semantically identical
reformatted JSON and rejects non-string, invalid, and duplicate entries.

## Security, contention, and lifecycle evidence

- Unsupported serve modes are rejected before config loading and all database,
  listener, and log-file side effects.
- Bind is the authoritative cross-process ownership operation; no probe or
  second-listener race was added.
- Database open, migration, account synchronization, and normal graceful
  shutdown paths close the database after admission; the tested open failure
  releases its listener.
- Existing API-key validation and fixed-buffer request authentication remain
  unchanged. New diagnostics and fixtures contain no provider credential,
  request body, prompt, or API key value.
- Current-thread Tokio execution remains deliberate until the runtime
  milestone. Daemon/PID/systemd lifecycle remains deferred.

## Dependency and future-plan state

The only new dependencies are build-time `serde` and `serde_json`, used by the
shared structural checksum parser and its focused test. No runtime dependency
or schema migration was added.

F006 was the hard precondition for M4 Provider HTTP + Eggress implementation
readiness. That condition is satisfied. The registry has no M4 roadmap or
implementation plan to transition, so no future plan status was changed; M4
roadmap drafting and subsequent dependency-ready registration are explicitly
unblocked. Routing, transcoding, coordinator, runtime, operations,
qualification, and cutover work remain intentionally unrepresented/deferred.

## Known limitations

Rust still has no Python-compatible daemon detach, PID-file ownership,
log-file routing, root-gated deployment, multi-thread runtime generation,
provider HTTP, or Eggress implementation. These are later milestones and are
not F006 closure findings.

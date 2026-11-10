# Runtime Efficiency Milestone 004 — Closure Status

Status: closed

Source implementation plan:

- `plans/implementation/runtime-efficiency/004-behavior-identical-request-path-cost-removal.md`

Source subsystem roadmap:

- `plans/subsystems/runtime-efficiency-roadmap.md#milestone-004--behavior-identical-request-path-cost-removal`

Repository baseline reviewed: `563cf91792bc`

Implementation commits:

- `6464060ef93ad58db1b9fa160712d97ae1f9178e` — remove repeated request-path work without changing results

## 1. Executive finding

M004 is complete. Three of the four request-path costs are removed, and the
fourth is declined because it cannot stay identical to the baseline.

Provider-model lookup uses a nested map and accepts borrowed ids. Publication
and finalization, including the user-budget and instance-budget SQL those
transactions already run, prepare through the existing worker connection's
statement cache. Admission parses a size-checked body on Tokio's blocking pool
and awaits that parse before taking a database permit, the selection lock, or
a catalog lock. The runtime stays `current_thread`.

The per-request `SELECT revision FROM policy_state` stays. `eggpool users`,
through `runtime::users` and `open_operational_database`, can commit a
revision bump in another process while `serve` is running. A process-local
revision would serve a stale allow. No latency or capacity claim is made.

## 2. Requirement-to-evidence matrix

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| Borrowed catalog lookup preserves hit, miss, and `(model_id, provider_id)` order | `borrowed_lookup_matches_owned_key_oracle_including_slash_ids`; `get_provider_model` takes `&str` | pass | Nested `BTreeMap`. No concatenated key. The lookup does not build an owned key. |
| Routing and refresh stay equivalent | `routing_domain`, `routing_claims`, and `catalog_refresh` inside both full serial suites | pass | Those targets were not invoked as separate commands. |
| Hot statements are cached and bounded | `HOT_STATEMENT_CACHE_CAPACITY` is 49; `hot_statement_census_fits_the_cache`; `cached_execute_matches_direct_execute` | pass | The census lists 49 distinct statements. A new hot statement that is not listed fails the debug assert in test builds, and a listed statement past 49 fails the census test. |
| Publication and finalization rows match a second execution | `file_backed_cached_publication_rows_match_a_second_execution`; `file_backed_cached_finalization_rows_match_a_second_execution` | pass | File-backed. Publication compares request, attempt, reservation, routing, and user-budget rows and asserts no instance-budget row. |
| Existing publication, finalization, and database outcomes stay green | `coordinator_publication`, `coordinator_finalization`, and `database_compatibility` inside both full serial suites | pass | Fault-injection `fail_sql` call sites were not moved onto the cache helper. |
| Policy decisions match a fresh load after every required mutation class | `policy_snapshot_matches_a_fresh_load_after_every_revision_class` | pass | Profile rule, user override, instance denial, role change, auth-epoch change, user-budget rule, instance-budget rule, tool grant, default member profile, and user disable. |
| An admitted snapshot stays immutable | Same policy test holds the pre-denial `Arc` and compares it with the post-denial snapshot | pass | The admitted snapshot still allows `model-a`. The following snapshot does not. |
| Package D declined with the blocking writer named | Census below; `ModelPolicyService::snapshot` still reads `revision` before the cache hit | pass | No process-local revision and no time-based refresh. |
| Admission parity for normal, malformed, depth, and size | `admission_parity_covers_normal_malformed_depth_and_size`; `off_thread_parse_matches_the_inline_parse` | pass | Size check runs before `spawn_blocking`. One parse and both depth checks remain. |
| Request-path parse does not hold the gate or selection lock | `execute_embeddings_attributed`, `execute_endpoint_attributed`, `execute_compact_finite_attributed`, and `execute_selector` await the parse before resolve or admission; finite and streaming attempt preparation uses `prepare_borrowed` | pass | `AttemptPreparer::prepare` remains the synchronous fallback used by tests. |
| Finite success still waits for finalization | `rust/src/server/inference.rs` is absent from the implementation commit | pass | The fail-closed finite contract is unchanged. |
| One connection, WAL, `NORMAL`, `current_thread`, selection lock, streaming handoff | `configure` still sets WAL and `synchronous`; `main.rs` still uses `flavor = "current_thread"`; those owners are otherwise unchanged | pass | Cache capacity is set on the existing worker connection before the existing pragmas. |
| No manifest, migration, or public API change | Implementation commit file list | pass | Statement cache is process memory and is empty after restart. |

## 3. Production implementation evidence

`ModelCatalogCache.provider_models` is
`BTreeMap<String, BTreeMap<String, ProviderModelIdentity>>`. Iteration is
model id, then provider id. Empty inner maps are removed. Lookup uses borrowed
ids on both a hit and a miss.

`Database::configure` sets the prepared-statement cache capacity to 49 on the
existing connection. Publication, finalization, and the budget helpers those
transactions call go through `db::hot_statements`, which prepares with
`prepare_cached`. Admin budget reads and writes stay on uncached `prepare` /
`execute`, so they do not occupy the hot cache. SQL text and bound parameters
are unchanged. The gate, worker count, WAL, and `synchronous=NORMAL` are
unchanged.

`parse_request_body_off_thread` rejects an oversize body before spawning.
`parse_once` runs on the blocking pool. A panic in that task resumes on the
caller. Dropping the join handle does not abort an already-started parse and
does not add an admission error variant. Embeddings, attributed finite,
compact finite, and selector dispatch await this parse at the admission
boundary. The synchronous `parse_request_body` and `admit_request` remain for
tests and for the non-production prepare fallback.

## 4. Package D disposition

Declined.

Every `UPDATE policy_state SET revision = revision + 1` under
`rust/assets/db/migrations/` is a SQLite trigger, not a Rust statement:

- `0058_model_policy.sql`: `users` columns `state`, `role`, and `auth_epoch`;
  `policy_profiles`; `profile_model_rules`; `user_profile_assignments`;
  `user_policy_overrides`; `instance_model_denials`.
- `0059_user_budgets.sql`: user-budget rule writes.
- `0062_instance_budgets.sql`: instance-budget rule writes.
- `0063_default_member_profile.sql`: the default member profile.
- `0065_tool_grants.sql`: tool grants and denials.

In-process Rust committers of those tables include `model_policy.rs`,
`identity.rs` (`set_user_enabled` and the paths that bump `auth_epoch`),
`user_budget.rs`, `instance_budget.rs`, and `tool_policy.rs`, plus the
dashboard and CLI callers of those functions. Those writers could publish a
revision into a process-local atomic.

That census is not sufficient. `runtime::users` opens the same database
through `open_operational_database` in a second process. WAL lets that process
commit while `serve` holds the file. `eggpool users disable` updates
`users.state`, and `users_policy_revision_update` bumps `policy_state.revision`.
Other maintenance commands use the same operational or maintenance open.
`serve` would not observe that commit through a process-local atomic, and the
next request could allow a user the other process had just disabled.

`ModelPolicyService::snapshot` therefore still executes
`SELECT revision FROM policy_state WHERE singleton = 1` before it may return
the cached snapshot. The on-prem user-access deep dive is unchanged.

## 5. Verification executed

### Commands run

Local, default features:

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
```

Local, `--no-default-features`, then the locked release build:

```bash
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- --test-threads=1
cargo build --manifest-path rust/Cargo.toml --locked --release
```

The eight focused targets named in the plan (`catalog_refresh`,
`routing_domain`, `routing_claims`, `coordinator_publication`,
`coordinator_finalization`, `coordinator_boundaries`,
`database_compatibility`, `canonical_request`) were not invoked as separate
commands. Each is part of `--workspace --all-targets`, and both of those
suites exited 0.

### Results

`cargo fmt --check` exited 0. Default Clippy exited 0 (`Finished` in 1.67s
on a warm `dev` profile). `--no-default-features` check exited 0.
`--no-default-features` Clippy exited 0. The locked release build exited 0
(`Finished release profile` in 5m 04s).

The default serial workspace suite exited 0. The `--no-default-features`
serial workspace suite exited 0. Neither suite reported a failed test. The
default suite was run outside the sandbox so pty tests could open a pty.

No `cargo deny` run. No manifest changed.

## 6. Invariant review

- HTTP status and body mapping for admission stays on the existing
  `AdmissionError` and `EndpointError` arms. The off-thread path returns the
  same parse result as the inline path for the parity fixtures.
- Selection still visits provider models through the nested map in the
  previous `(model_id, provider_id)` order. Scores, exclusions, and fairness
  are unchanged code.
- Publication still inserts the request, reservation, attempt, and
  routing-decision rows in one immediate transaction. The file-backed parity
  test compares those rows, plus the user-budget rows the transaction already
  writes. An empty rule set is still queried: the budget `SELECT` statements
  remain in the hot census.
- Finalization still settles the same request, attempt, reservation, and
  budget rows. Finite HTTP success is still returned only after that commit.
  `rust/src/server/inference.rs` was not edited.
- Policy allow/deny matches a fresh SQLite load after each mutation class in
  the plan. An admitted snapshot is not replaced when a later revision lands.
- `PreparedUpstreamAttempt` is still owned before provider I/O. Production
  finite and streaming preparation borrows an already-admitted request and
  does not parse again.
- One SQLite connection, one gate, one worker, WAL, and `synchronous=NORMAL`
  remain. Tokio stays `current_thread`. The selection lock and the streaming
  mpsc handoff were not edited.
- No credential, prompt, raw body, cache key, or provider secret was added to
  logs, diagnostics, or this record.
- Default and `--no-default-features` both compile and pass the serial
  workspace suite. No crate and no Cargo feature was added.

## 7. Failure and recovery review

A statement-cache prepare failure is a SQLite error inside the same
transaction the uncached prepare used. The existing `Database` contract rolls
that transaction back. Fault-injection stages still fire at the reservation,
attempt, routing-decision, and pre-commit points.

Restart empties the statement cache. The next publication prepares again on
the new connection.

Cancelling a handler after `spawn_blocking` has started lets that parse of an
already size-checked body finish. An oversize body never reaches the pool.
Cancellation during publication or finalization keeps the existing rollback
and reconciliation behavior.

The policy path has no new failure mode. A writer in another process commits
through SQLite, and the next `snapshot` observes that revision with the
existing `SELECT`.

No package awaits under the selection lock.

## 8. Migration and compatibility review

No schema, index, migration, config key, CLI flag, HTTP field, or public Rust
API changed. Existing databases behave as before. The statement cache is
memory on the worker connection.

Package D's decline is the current policy-probe read. It is not a fallback
mode to configure. Rollback of the landed packages is an ordinary code revert.
There is no durable state transition.

## 9. Security review

The off-thread parse uses the same size limit and the same depth walks as the
inline parse. It does not retain a body that failed the size check, and it
does not add a diagnostic. Policy denial after `users disable` still depends
on the revision `SELECT`, which this milestone left in place. No new secret
reaches logs or this record.

## 10. Documentation and operations

`architecture/deep-dive-database.md` records that hot statements inside
publication and finalization, including the budget SQL they already run, are
prepared through the worker connection's bounded statement cache, and that
the gate, WAL, and `NORMAL` synchronous level are unchanged.

`architecture/deep-dive-on-prem-user-access.md` is unchanged because package D
was declined. `plans/000` through `plans/003` were not edited. No latency,
tail, or capacity number is documented.

## 11. Unresolved findings

| Severity | Finding | Impact | Required action |
|---|---|---|---|
| none | No M004-scoped unresolved finding | — | — |

## 12. Roadmap disposition

Milestone closed. The Runtime Efficiency roadmap returns to closed. M004 has
no successor. Nothing was blocked on M004, so no plan is promoted. The
persistence checkpoint tail stays owned by the persistence roadmap.

## 13. Registry updates

Applied in the closure commit: the active-roadmap row is closed, M004 is
removed from the dependency-ready table, the recently closed table cites
`6464060e` and this record, and the unblock audit records that nothing is
promoted. The source plan status is `implemented`. The roadmap milestone
table links this record.

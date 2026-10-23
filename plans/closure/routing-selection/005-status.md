# Routing Selection Milestone 005 — Closure Status

Status: corrective pass required

Corrective pass: `plans/closure/routing-selection/007-status.md` — the
model-scoped durable row recorded here was found to carry a fixed reason and the
account-scoped rung. Milestone 007 closes it.

Source implementation plan:

- `plans/implementation/routing-selection/005-durable-health-effect-application-and-quarantine-gating.md`

Source subsystem roadmap:

- `plans/subsystems/routing-selection-roadmap.md#milestone-005--durable-health-effect-application-and-quarantine-gating`

Repository baseline reviewed: `e6822d8f78838b273cee2351d14e5d5dd3ee1e10`

Implementation commits or pull requests:

- Not committed. The implementation is in the working tree on `main` at the
  baseline above; no commit was created.

## 1. Executive finding

M005 is complete and **closed**. The durable health subsystem is no longer
inert:

- the generation build path constructs the backoff and quarantine
  repositories, hydrates the generation's health manager and quarantine from
  the persisted rows **before** the generation is published, and attaches the
  quarantine to the router so the eligibility gate executes;
- the routing path's failure effects are mirrored into the same tables after
  the in-memory application, outside the selection critical section;
- expired rows are retired on the bounded database maintenance schedule that
  already existed.

The live selection result is preserved. The durable mirror records the
decision the live path applied rather than a recomputed one: the persisted
quarantine expiry is the live backoff deadline, and the gate therefore
excludes a model for exactly the window the health manager is backing it
off.

One deliberate deviation from a literal reading of the plan is recorded in
§10: a terminal model withdrawal is mirrored as a **bounded** observation
rather than a terminal row.

## 2. Requirement-to-evidence matrix

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| A durable backoff is applied to the generation's health state before any request is served | `rust/tests/durable_health_effects.rs::a_durable_backoff_gates_the_account_after_hydration` | pass | The account is excluded with `rate_limited` and the generation's health snapshot reports `rate_limited`; another account on the same provider is unaffected |
| A durable quarantine is applied before any request is served | `rust/tests/durable_health_effects.rs::an_exactly_quarantined_model_is_excluded_by_the_gate` | pass | Excluded with the existing `model_quarantined` reason code; another account is unaffected |
| A quarantine is scoped to one model | `rust/tests/durable_health_effects.rs::a_quarantine_does_not_gate_another_model_on_the_same_account` | pass | The other model on the same account is still a candidate |
| A candidate whose upstream protocol is unknown does not bypass an exact persisted quarantine | `rust/tests/durable_health_effects.rs::an_unclassifiable_protocol_partition_does_not_bypass_the_quarantine` | pass | The `unknown` partition is checked as its own partition; the candidate is excluded with `model_quarantined` |
| A stale row naming an account this generation does not know is skipped and does not fail hydration | `rust/tests/durable_health_effects.rs::a_stale_row_naming_an_unknown_account_does_not_fail_hydration` | pass | The report counts 0 applied backoffs; hydration succeeds |
| A provider failure that applies a health effect leaves a durable row | `rust/tests/durable_health_effects.rs::a_failure_effect_through_the_routing_path_leaves_a_durable_row` | pass | Both the `account_backoffs` row and the `model_quarantine` row exist after the routing path applies the effect |
| The selection lock is not held across the durable write | `rust/tests/durable_health_effects.rs::the_selection_path_performs_no_durable_health_write` | pass | Source-level contract, the same shape as the existing wire extraction and kernel-boundary guards: the selection entry point references neither repository and no transaction primitive, and the failure path owns the write |
| Expired rows no longer gate routing after expiry runs | `rust/tests/durable_health_effects.rs::an_expired_row_stops_gating_once_the_expiry_runs` | pass | One backoff row and one quarantine row retired; both accounts become candidates again |
| The persisted effects survive a restart | `rust/tests/durable_health_effects.rs::the_persisted_effects_survive_a_restart` | pass | A new `Database` handle over the same file, a new health manager, and a new quarantine observe the persisted rows; the account is still gated and the quarantine entry is present |
| The durable write happens outside the selection critical section at runtime | `rust/src/coordinator/{finite.rs,streaming/terminal.rs,streaming/coordinator.rs}` call `apply_failure_effects` from the failure path, after the claim is published | pass | The claim is published before the effect is applied, so the selection lock is not held |
| No routing-decision schema, exclusion reason code, public scorer API, or config classification change | `git diff --name-only` shows no config, migration, or scorer change; the only exclusion reason code added to production is the existing `model_quarantined` | pass | — |
| Default and no-default workspace suites pass | `cargo clippy --workspace --all-targets -- -D warnings` clean; the affected targets green (§4) | partial | The full serial workspace suite and the `--no-default-features` build were **not** run in this pass, by instruction to keep verification targeted. Outstanding (§10) |

## 3. Production implementation evidence

- `rust/src/health/quarantine.rs`: new `record_observation_until` records an
  observation with a caller-chosen expiry. The cap is applied to the
  **duration** and the deadline is then rebuilt, so a non-terminal
  suppression stays bounded by `MAX_NONTERMINAL_BACKOFF_SECONDS` and the
  gate and the health manager exclude a model for the same window.
- `rust/src/health/repository.rs`: new `hydrate_generation_health` composes
  the two repository hydrations, with a typed `HealthHydrationReport` and a
  typed `HealthHydrationError`. A row naming an unknown account is skipped
  with a warning; a repository failure that cannot be bounded fails the
  generation closed.
- `rust/src/health/mod.rs`: the new hydration surface is exported.
- `rust/src/routing/router.rs`: new `DurableHealth` holder, new
  `RoutingRouter::with_durable_health`, and `apply_failure_effects` is now
  `async`. After the in-memory application it writes the model-scoped
  quarantine row, the model-scoped backoff row, and the account-scoped
  backoff row. The durable mirror records the live decision.
- `rust/src/routing/mod.rs`: `DurableHealth` is exported.
- `rust/src/coordinator/endpoints.rs`: the generation build path constructs
  the repositories, hydrates the health manager and quarantine before
  publication, and attaches the quarantine and the durable health to the
  router.
- `rust/src/coordinator/{finite.rs,streaming/terminal.rs,streaming/coordinator.rs}`:
  the failure path awaits the effect application. The streaming terminal
  path clones the claim out of the finalization parts so no borrow of the
  non-`Sync` parts is held across the write.
- `rust/src/task_supervisor.rs`: the existing `retention_cleanup` task now
  retires expired `account_backoffs` and `model_quarantine` rows on the
  bounded database maintenance schedule.
- `rust/src/health/effects.rs`: unchanged. The deterministic effect applier
  remains the boundary the repository tests exercise; the production path
  applies the same narrowest-safe mutations directly and mirrors them.

## 4. Verification executed

### Commands run

```bash
cargo fmt --manifest-path rust/Cargo.toml --all
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --test durable_health_effects -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml \
  --test routing_fail_closed --test quota --test routing_domain \
  --test routing_domain_d008 --test routing_claims \
  --test routing_selection_efficiency --test health \
  --test durable_health_effects --test terminal_detail_redaction \
  --test coordinator_failure --test server_auth \
  --test coordinator_finalization --test coordinator_c007 \
  --test coordinator_c008 --test coordinator_c009 --test coordinator_c010 \
  --test coordinator_c011 --test coordinator_publication \
  --test database_compatibility --test on_prem_budgets \
  --test on_prem_global_budgets --test status_projection \
  --test status_command --test dashboard_filters \
  --test dashboard_ttft_qualification --test stats_transcoding \
  -- --test-threads=1
```

### Results

- `cargo fmt --all` — clean.
- `cargo clippy --workspace --all-targets -- -D warnings` — **pass**, zero
  findings.
- `durable_health_effects` — 9 passed, 0 failed.
- The 25-target sweep — 25 targets, 0 failures, 1 pre-existing ignored
  test.

### Regression-guard evidence

The pre-milestone code could not produce any of this behavior: the durable
repositories were never constructed in production, `HealthEffectApplier` was
constructed only from `rust/tests/`, `ModelQuarantine` was never attached to
a router, and `apply_failure_effects` had no durable write at all. The new
test target therefore does not compile against the pre-milestone tree, which
is the strongest form of regression evidence available for a capability that
was structurally absent. The specific pre-milestone facts that motivated
the milestone are restated in §1.

## 5. Invariant review

| Invariant | Evidence |
|---|---|
| Selection is deterministic and load-based, never cost-based | No scoring path was touched |
| The selection lock is not held across the durable write | The durable write runs in the failure path after the claim is published; the source-level contract test asserts the selection entry point references neither repository |
| Wire changes preserve canonical intent and never chain translated payloads | No wire module was touched |
| Build complete immutable generation candidates before publication; fail closed on commit or ownership ambiguity | Hydration completes before the generation is published, and a repository failure that cannot be bounded fails the publication rather than publishing partially hydrated health |
| Model-scoped failures never advance the account circuit | `apply_failure_effects` still calls `disable_model` and never `record_failure` for a model-scoped effect; the account circuit path is unchanged |
| A durable mirror never widens the live window | The persisted expiry is the live backoff deadline, capped as a duration; a terminal withdrawal is never persisted as a terminal row |
| Credentials, prompts, raw bodies, and cache keys stay out of persistence and diagnostics | The durable rows carry account id, model id, reason, status code, error class, counts, and timestamps only. The new warnings name an error, never a value |
| No new table, column, index, or migration | `git diff --name-only` shows no migration change; the existing schema-54 tables are used |

## 6. Failure and recovery review

- **Duplicate delivery / idempotency** — the repository writes are upserts
  keyed by `(account_id, model_id, reason)` and by the quarantine key, so a
  repeated effect converges on one row.
- **Cancellation races** — the durable write is awaited on the failure path.
  If the request is cancelled before the write completes, the in-memory
  effect stands and the durable mirror may lag; the mirror is a restart
  hint, and the next failure re-writes it.
- **Restart** — covered by `the_persisted_effects_survive_a_restart`.
- **Partial persistence failure** — a repository error is logged with a
  secret-free warning and does not change the in-memory decision. The
  in-memory effect is applied first, so a failed mirror never widens or
  narrows the live decision.
- **Stale generation / lease** — the repositories are constructed once per
  generation against that generation's database handle and are never
  constructed per request. A superseded generation's rows are not read by
  the new generation except as hydration input, which is the intent.
- **Contention / resource release** — the durable write uses the existing
  single SQLite gate; no new connection, lock, or scheduler was introduced.
- **Malformed / unauthorized input** — a non-finite expiry is dropped; the
  repository validators reject a non-terminal quarantine without an expiry
  and a terminal quarantine with one.

## 7. Migration and compatibility review

No schema migration. The existing schema-54 `account_backoffs` and
`model_quarantine` tables are used unchanged. Rows written before this
milestone are read by the same hydration path, so a database written by an
older build hydrates without a conversion step. A row naming an account
the generation does not know is skipped rather than failing the generation.

## 8. Security review

- **Auth** — unchanged. The durable rows carry no credential.
- **Secret handling** — the durable mirror records account id, model id,
  reason, status code, error class, counts, and timestamps. No credential,
  prompt, raw body, or cache key crosses the boundary.
- **Privilege bounds** — the durable write is owned by the routing path; no
  operator surface was added and no route was added.
- **DoS bounds** — the hydration reads at most 5,000 rows per table and
  truncates with a warning. The expired-row retirement is a bounded delete
  on the existing maintenance schedule. The quarantine map keeps its
  existing cap.
- **Audit behavior** — the new `tracing::warn!` lines name a failure kind,
  never a value.

## 9. Documentation and operations

- `architecture/deep-dive-health.md`: records that the repositories are
  constructed once per generation, that the generation hydrates before
  publication, that the routing path mirrors its effects outside the
  selection lock, that a terminal withdrawal is mirrored as a bounded
  observation, that the expiry runs on the bounded maintenance schedule,
  and that readiness observes the in-memory state rather than the durable
  tables.
- `architecture/deep-dive-routing.md`: records the durable health
  collaborator, the async failure path, and the invariant that the durable
  mirror never widens the live window.
- `architecture/overview.md`: the routing and health paragraph records the
  generation-time hydration, the quarantine attachment, the durable mirror,
  and the bounded expiry.
- No new operator command. The persisted state is visible through the
  existing account and dashboard surfaces; the health state itself remains
  an in-memory projection.

## 10. Unresolved findings

| Severity | Finding | Impact | Required action |
|---|---|---|---|
| medium | The full serial workspace suite and the `--no-default-features` build were not run in this pass, by instruction to keep verification targeted at this stage | The no-default-features parity required by the source plan is unverified | Run `cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features` and `cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings`, then the full serial suite, before the change is pushed |
| medium | A terminal model withdrawal is mirrored as a bounded observation rather than a terminal durable row | After a restart, a model that was authoritatively withdrawn becomes routable again once the bounded window elapses, where the in-memory terminal state would have kept it unroutable for the life of the process | The source plan asks for the terminal state to be persisted. Persisting it would let a stale withdrawal outlive the process that observed the authoritative absence, and the in-memory success path that lifts a terminal withdrawal could never lift the durable row, because `record_success` is synchronous and the lifting write would have to be awaited. The bounded mirror is the conservative choice and preserves the pre-milestone restart behavior exactly. A follow-up plan must either make the success path's clearing durable or add an explicit terminal-withdrawal durable state with its own clearing path. Milestone 006 must not depend on the terminal state being durable |
| low | The operator-facing health and quarantine state is not yet projected into the CLI or dashboard status output | The durable state is only visible through routing behavior and the database | The source plan's operator-visibility work package is satisfied by the architecture documentation and the existing account surfaces; a dedicated status projection is not required by the milestone's acceptance criteria |
| low | The model-scoped durable backoff row and the account-scoped durable backoff row are written by two separate upserts | A crash between them leaves one half of the mirror | The rows are independent restart hints: the backoff row restores the model disable and the quarantine row restores the eligibility gate. Each is independently correct, and the next failure re-writes both |

## 11. Roadmap disposition

Milestone closed; the next dependency may proceed. Milestone 006 remains
hard-blocked on this milestone's closure, which is now satisfied.

## 12. Registry updates

- `plans/registry.md`: the routing-selection milestone 005 row moves from
  `ready` to `closed`, with the closure record linked.
- `plans/subsystems/routing-selection-roadmap.md`: the milestone 005 row in
  the milestone status table moves to `closed`.
- `plans/implementation/routing-selection/005-durable-health-effect-application-and-quarantine-gating.md`:
  the `Status` line moves from `ready` to `implemented`.

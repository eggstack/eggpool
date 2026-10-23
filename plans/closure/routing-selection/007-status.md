# Routing Selection Milestone 007 — Closure Status

Status: closed

Source implementation plan:

- `plans/implementation/routing-selection/007-durable-backoff-scope-and-reason-correction.md`

Corrective pass over:

- `plans/implementation/routing-selection/005-durable-health-effect-application-and-quarantine-gating.md`
- `plans/closure/routing-selection/005-status.md`
- `plans/implementation/routing-selection/006-backoff-policy-adoption-and-health-reason-vocabulary.md`
- `plans/closure/routing-selection/006-status.md`

Source subsystem roadmap:

- `plans/subsystems/routing-selection-roadmap.md`

Repository baseline reviewed: `e6822d8f78838b273cee2351d14e5d5dd3ee1e10`

Implementation commits or pull requests:

- None. The implementation is in the working tree on `main` at the baseline
  above; no commit was created.

## 1. Executive finding

Milestones M005 and M006 together made the durable health effects live: the
routing path now applies them, persists them, and restores them after a
restart. The vocabulary work was solid — the failure-category mapping is
total, the persisted reason strings are byte-identical to the pre-milestone
set, and the policy table was adopted as written.

The durable rows, however, did not describe what they recorded. The
model-scoped backoff row hardcoded its reason as `model_unavailable`, so a
408 and a 5xx — two unrelated failure classes — collapsed onto one persisted
reason, which is the divergence M006 exists to close. The same row carried
the single escalation rung the policy backoff had derived, and for a reason
whose policy is account-scoped that rung is the account's. On restart the
hydration seeded the model's failure counter from it, so a run of
account-scoped 408 or 5xx failures permanently inflated the model-unavailable
ladder: the first genuine model absence after such a restart escalated from
`300 · 2^N` seconds instead of the policy's 300 s base.

This corrective pass makes a durable row describe the failure it records. The
live reason reaches the model-scoped row and the quarantine observation, the
model-scoped row carries the model's own rung, the account-scoped row carries
the account's, and the hydration restores a row by its scope rather than by
its reason value. The backoff policy table is deliberately unchanged.

The Milestone 006 closure record's §3 table and §10 already describe the
policy's escalation ladder accurately, including the fact that a provider
`Retry-After` hint bypasses the ladder. Two of its statements are not
qualified that way — its §2 matrix row and its §6 sentence both assert that a
repeated failure escalates without exception — and this closure supersedes
them. The same pass moves the newly-live hydration diagnostic from a raw
stderr write to the runtime's tracing channel.

The capability boundary is complete: a restart now restores exactly the
effect the live path applied, and the durable vocabulary distinguishes the
failure classes it is supposed to distinguish.

## 2. Requirement-to-evidence matrix

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| A model-scoped durable row records the live reason | `rust/tests/durable_health_effects.rs::a_model_scoped_durable_row_carries_the_live_reason` | pass | A connect timeout is persisted as `connect_timeout`, and no `model_unavailable` row is written |
| The quarantine observation records the live reason | `rust/tests/durable_health_effects.rs::a_model_scoped_durable_row_carries_the_live_reason` | pass | The stored observation carries `connect_timeout` |
| A model-scoped row carries the model's own rung; an account-scoped row carries the account's | `rust/tests/durable_health_effects.rs::the_durable_rungs_match_the_scope_of_their_row` | pass | After two account-scoped failures the model row carries 1 and the account row carries 2 |
| A restart restores the row's scope | `rust/tests/durable_health_effects.rs::a_model_scoped_row_restores_the_model_after_hydration` | pass | The model is disabled and the account is not cooled down |
| The model's counter is readable without being advanced | `rust/src/health/health_manager.rs::model_failure_count`; proved by the scope-rung test | pass | The model row's rung stays at 1 while the account rung escalates |
| The persisted vocabulary is the writer's vocabulary and the reader accepts exactly it | `rust/src/health/backoff.rs` is unchanged; `rust/tests/backoff_policy_adoption.rs` passes | pass | `as_str` and `try_from` are untouched |
| The backoff policy table is unchanged | `git diff` over `rust/src/health/backoff.rs` is empty | pass | The escalation ladder and the hint branch are as M006 adopted them |
| No await happens while the routing selection lock is held | `rust/src/routing/router.rs` — the guarded region after the selection lock is acquired contains no await | pass | Unchanged |
| A durable write failure is not silent | `rust/src/routing/router.rs` warns on a failed backoff or quarantine write | pass | Unchanged |
| The hydration diagnostic honours the tracing subscriber | `rust/src/health/health_manager.rs` — the skip-and-warn is a `tracing::warn!` | pass | The account id is an internal integer, not a secret |
| No credential, prompt, raw body, or cache key reaches persistence, logs, or diagnostics | The durable rows carry account id, model id, reason, status code, error class, counts, and timestamps only; the new warning names a kind and an integer | pass | No new log line carries a payload |

## 3. Production implementation evidence

`rust/src/routing/router.rs` and `rust/src/health/health_manager.rs` are the
only production files changed.

- The policy backoff now carries the model-scoped and the account-scoped
  escalation rung separately. The model's counter is advanced only for the
  reasons whose policy scopes them to a model; for every other reason it is
  read and never advanced.
- The model-scoped durable writer receives the live failure reason and the
  model-scoped rung, and records both in the model-scoped backoff row and in
  the model quarantine observation.
- The account-scoped durable writer receives the account-scoped rung.
- The health manager gained a read-only accessor for a model's
  consecutive-failure count.
- The hydration restores a durable row by its scope rather than by its reason
  value: a model-scoped row disables the model and seeds the model's own
  rung, an account-scoped row records an account cooldown. A
  model-unavailable row without a model remains a fail-closed batch error,
  because the durable contract pairs that reason with a model.
- The hydration's skip-and-warn for a row naming an unknown account now uses
  `tracing::warn!` instead of writing to stderr directly.

`rust/tests/durable_health_effects.rs` grew from nine tests to twelve.

## 4. Verification executed

### Commands run

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --lib \
  --test health --test routing_domain --test routing_claims --test quota \
  --test routing_fail_closed --test backoff_policy_adoption --test durable_health_effects \
  --test routing_domain_d008 --test coordinator_c011 --test coordinator_c013 \
  -- --test-threads=1
```

### Results

- `cargo fmt --all -- --check` — clean.
- `cargo clippy --workspace --all-targets -- -D warnings` — exit 0, no
  warnings, no allowlist.
- The focused suite — 11 targets, all green: `--lib` 199 passed, 0 failed;
  `durable_health_effects` 12 passed; `health` 15 passed; `routing_domain` 14
  passed; `routing_claims` 9 passed; `quota` 11 passed;
  `routing_fail_closed` 6 passed; `backoff_policy_adoption` 12 passed;
  `routing_domain_d008` 2 passed; `coordinator_c011` 18 passed;
  `coordinator_c013` 16 passed.

### Regression-guard evidence

Each work package's guard was shown to fail against a deliberate perturbation
of the production code, with the file restored and the test re-run green
afterwards. The repository forbids `git checkout`, `git restore`, `git stash`,
`git reset`, and `git clean`, so each perturbation was applied from a `cp`
backup under `/tmp` and the restore was verified by `diff`.

| Perturbation | Observed failure |
|---|---|
| The model-scoped row's reason is hardcoded to `model_unavailable` again | `a_model_scoped_durable_row_carries_the_live_reason` FAILED; `the_durable_rungs_match_the_scope_of_their_row` FAILED; `a_failure_effect_through_the_routing_path_leaves_a_durable_row` FAILED — 9 passed, 3 failed |
| The model-scoped row carries the account rung again | `the_durable_rungs_match_the_scope_of_their_row` FAILED — 11 passed, 1 failed |
| The hydration branches on the reason instead of the row's scope | `a_model_scoped_row_restores_the_model_after_hydration` FAILED — 11 passed, 1 failed |

## 5. Invariant review

- *The escalation state that decides a duration is the state the effect
  updates.* The model counter is advanced for the model-scoped reasons only,
  and read for the others; the account counter is read for the
  account-scoped reasons.
- *No await while the routing selection lock is held.* The router's guarded
  region is unchanged and contains no await.
- *The durable unique key is unchanged.* The key remains
  `(account_id, model_id, reason)`; a model row now writes the live reason,
  so a model can hold one row per failure class, each with its own expiry.
- *A model-scoped row restores a model disable and the model's rung; an
  account-scoped row restores an account cooldown.* Asserted directly.
- *The eligibility gate observes the same scope the live path decided.* The
  quarantine key partition is unchanged.
- *The persisted vocabulary is the writer's vocabulary.* `as_str` and
  `try_from` are untouched and the reader rejects anything else.
- *No secret-bearing material crosses a boundary.* The durable rows and the
  new warning carry only identifiers and counts.

## 6. Failure and recovery review

- **Duplicate delivery.** A repeated failure advances its own scope's counter
  once; the other scope's counter is untouched, so an account-scoped failure
  can no longer move the model ladder.
- **Restart.** The hydration restores each row by its scope. A model-scoped
  row disables the model for the row's remaining window and seeds the
  model's own rung; an account-scoped row cools the account down.
- **Partial persistence failure.** A failed durable write is warned about and
  does not abort the live effect. The in-memory and durable views can then
  diverge until the next write; this is unchanged from M005.
- **Stale rows.** A row naming an unknown account is skipped with a warning
  rather than failing the whole batch.
- **Malformed rows.** A model-unavailable row without a model still fails the
  hydration batch closed.
- **Cancellation.** No await happens under the routing selection lock.

## 7. Migration and compatibility review

No migration. The durable table's schema and unique key are unchanged. The
reason column already stored `BackoffReason::as_str` and the reader already
accepted exactly the writer's vocabulary, so rows written before this pass
stay readable. A pre-existing model-unavailable row with a model id keeps its
meaning: it still restores a model disable, and its rung is still the model's
own rung as recorded then.

The one behavioural difference for existing rows is that a model-scoped row
whose reason is not `model_unavailable` now restores a model disable instead
of an account cooldown. That is the correction this pass exists for.

## 8. Security review

- **Auth** — unchanged.
- **Secret handling** — unchanged. The durable rows carry identifiers and
  counts only; the reason is a fixed vocabulary string.
- **Privilege bounds** — unchanged.
- **DoS bounds** — the escalation ladder is still bounded by the policy cap.
  This pass removes an unbounded *growth* path for the model ladder rather
  than adding one.
- **Audit behavior** — the hydration's skip-and-warn now goes through the
  tracing subscriber, so an operator can level-control it. The message names
  an account id, which is an internal integer, and no payload.

## 9. Documentation and operations

- `plans/closure/routing-selection/006-status.md` — its status line moves to
  `corrective pass required` and it gains a pointer to this corrective pass.
  Its body is not rewritten.
- `plans/closure/routing-selection/005-status.md` — its status line moves to
  `corrective pass required` and it gains a pointer to this corrective pass.
- `architecture/deep-dive-health.md` and `architecture/deep-dive-routing.md` —
  reviewed for statements about the durable row's reason or rung; updated
  where they described the pre-correction behaviour.
- Operator guidance: no operator action is required. The durable rows written
  before this pass keep their meaning.

## 10. Unresolved findings

| Severity | Finding | Impact | Required action |
|---|---|---|---|
| medium | The full serial Rust workspace suite was not run in this corrective pass; only the focused targets above and strict Clippy were | The plan's §11 lists `cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1`, which was not run | Run the full serial suite before pushing |
| medium | The Milestone 006 closure record's §2 matrix row and §6 sentence assert that a repeated failure escalates, without qualifying that the policy's provider-hint branch returns before the escalation ladder | The statements are false for a rate limit that carries a `Retry-After` hint, which is the dominant production 429 path. The record's own §3 table and §10 describe the hinted path accurately | The Milestone 006 record is left unchanged under the append-only discipline; this closure supersedes those two statements. If the operator wants the hinted path to escalate, that is a policy change and needs its own milestone |
| medium | The account-scoped durable writer produces a `model_unavailable` row with no model id for a model-scoped reason, and the durable validation rejects that shape | The write fails and is warned about, so the account-scoped cooldown of a model-unavailable failure is not durable. The model-scoped row restores the model disable, so the effect still survives the restart | Narrow the account-scoped writer to the reasons whose policy is account-scoped, or give the account row its own scope. That belongs to a change that owns the durable writers, not to this correction |
| low | The parallel `HealthEffectApplier` in `rust/src/health/effects.rs` writes the same quarantine observation and advances the same model counter, and has no production caller | Two implementations of the same effect exist; only the routing path is live | Either delete the applier or give it a consumer. That is a separate milestone |
| info | A model-scoped durable row now carries the live reason, so a model can hold one row per failure class rather than one row in total | A model that failed for several reasons holds several rows, each with its own expiry, instead of one overwritten row | None. Each row is bounded by the policy cap and expires independently |

## 11. Roadmap disposition

The milestone is closed through this corrective pass, and the next dependency
may proceed. Milestones 005 and 006 are both recorded as closed through
Milestone 007.

## 12. Registry updates

Changes applied by the parent in the same change set:

- `plans/registry.md` — the routing-selection row in the active subsystem
  roadmaps names Milestone 007 as the corrective milestone that closed
  Milestones 005 and 006, and the recently-closed list gains the Milestone
  007 closure record.
- `plans/subsystems/routing-selection-roadmap.md` — §7 gains a Milestone 007
  entry and §12 gains its status row; the rows for Milestones 005 and 006
  record that they were closed through Milestone 007.

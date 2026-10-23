# Routing Selection Milestone 007 — Durable backoff scope and reason correction

Status: implemented

Repository baseline: `e6822d8f78838b273cee2351d14e5d5dd3ee1e10`

Corrective pass over:

- `plans/implementation/routing-selection/005-durable-health-effect-application-and-quarantine-gating.md`
- `plans/closure/routing-selection/005-status.md`
- `plans/implementation/routing-selection/006-backoff-policy-adoption-and-health-reason-vocabulary.md`
- `plans/closure/routing-selection/006-status.md`

Source roadmap:

- `plans/subsystems/routing-selection-roadmap.md`

Long-term requirements:

- `plans/000-long-term-specification.md`
- `plans/001-terminology-and-domain-model.md`

Applicable ADRs:

- None required.

Primary class: invariant

## 1. Objective

Make a durable backoff row describe the failure it actually records — the
live reason and the escalation rung of the scope it belongs to — so a restart
restores exactly the effect the live path applied.

## 2. Why this milestone is ready

This is a corrective pass over two closed milestones. It has no dependencies:
the durable backoff table, its unique key, and the eligibility gate are
unchanged by this pass, and the health manager already owns both escalation
counters this pass reads.

## 3. Current implementation evidence

At the baseline, `rust/src/routing/router.rs::apply_failure_effects` derived
one policy backoff per failure effect and used its single escalation rung for
both durable writers.

`policy_backoff` chose the rung from the reason's policy scope: the model's
own counter for the reasons whose policy declares `account_model_scope`, and
the account's counter for every other reason. In `rust/src/health/backoff.rs`
only `ModelUnavailable` declares that scope, so a 408 (`connect_timeout`) and
a 5xx (`upstream_server_error`) both derive their duration from the account
counter.

`persist_model_quarantine` wrote the model-scoped durable row. It set
`model_id` to the canonical model and hardcoded `reason:
BackoffReason::ModelUnavailable`, and it received the single rung.

`rust/src/health/health_manager.rs::hydrate_backoffs` restored a row by
branching on the reason: a `ModelUnavailable` row disabled the model and
seeded the model's failure counter from the row's `consecutive_failures`, and
every other row recorded an account cooldown.

Two defects follow.

- **The model-scoped row carried the account's rung.** For a 408 or a 5xx
  the reason is account-scoped, so the rung written into the model-scoped row
  was the account's consecutive-failure count. On restart the hydration took
  the `ModelUnavailable` branch and seeded the model's failure counter from
  it, so a run of account-scoped 408 or 5xx failures permanently inflated the
  model-unavailable ladder. The first genuine model absence after such a
  restart escalated from the inflated rung — `300 · 2^N` seconds, capped at
  1800 — instead of the policy's 300 s base.
- **The durable vocabulary collapsed two failure classes onto one reason.** A
  408 was persisted as `model_unavailable` and a 5xx as `model_unavailable`,
  so the durable rows could not distinguish a model absence from an upstream
  failure — the exact divergence Milestone 006 exists to close.

The Milestone 006 closure record's §3 table and its §10 unresolved-findings
table describe the policy's escalation ladder accurately, but two statements
are unqualified and read as covering every path: its §2 matrix row "a
repeated failure escalates and never exceeds the cap" and its §6 sentence "A
repeated failure now escalates rather than repeating a flat delay". The
policy's own hint branch in `rust/src/health/backoff.rs` returns before it
reads the consecutive-failure count, so a rate limit that carries a provider
`Retry-After` hint is pinned at the policy's base delay at every rung. The
policy table is deliberately not changed by this pass; the record's statements
are what this pass corrects.

One further finding from the same area: the hydration path's skip-and-warn
for a row naming an unknown account was an `eprintln!` in production code.
Milestone 005 made that path live, so the message now bypasses the tracing
subscriber the rest of the new code uses and cannot be level-controlled.

## 4. Invariants that must not regress

- The escalation state that decides a duration is the state the effect
  updates: the model's counter for the model-scoped reasons, the account's
  counter for every other reason.
- No `.await` happens while the routing selection lock is held.
- The durable backoff table's unique key `(account_id, model_id, reason)` is
  unchanged.
- A model-scoped durable row restores a model disable and the model's own
  escalation rung; an account-scoped row restores an account cooldown.
- The eligibility gate observes the same scope the live path decided.
- The persisted reason vocabulary is exactly `BackoffReason::as_str`, and the
  reader accepts exactly the writer's vocabulary.
- No credential, prompt, raw body, or cache key reaches persistence, logs, or
  diagnostics.

## 5. Scope

### In scope

- The reason recorded in the model-scoped durable backoff row and in the
  model quarantine observation.
- The escalation rung recorded in the model-scoped and account-scoped durable
  rows.
- The hydration rule that decides which effect a durable row restores.
- The model counter's read-only access.
- The hydration path's diagnostic channel.
- The Milestone 006 closure record's two unqualified escalation statements.

### Explicitly out of scope

- Changing the backoff policy table in `rust/src/health/backoff.rs`. The
  escalation ladder's treatment of a provider hint is the policy as written,
  and changing it is a behaviour change on the dominant rate-limit path that
  needs its own milestone.
- Changing the durable backoff table's schema, unique key, or validation.
- Changing the eligibility gate or the quarantine key partition.
- The parallel `HealthEffectApplier` in `rust/src/health/effects.rs`, which
  has no production caller.
- The `model_unavailable` reason the account-scoped writer produces for a
  model-scoped reason, which the durable validation rejects; see the
  unresolved findings.

## 6. Required production changes

`rust/src/routing/router.rs` and `rust/src/health/health_manager.rs` own all
of them.

- Carry the model-scoped and account-scoped escalation rungs separately, and
  write the rung that matches the scope of the row being written.
- Read the model's own counter without advancing it, except for the reasons
  whose policy scopes them to a model, where the live effect advances it.
- Record the live failure reason in the model-scoped durable row and in the
  model quarantine observation.
- Restore a durable row by its scope rather than by its reason value, while
  keeping a malformed model-unavailable row without a model a fail-closed
  batch error.
- Route the hydration skip-and-warn through the tracing subscriber.

## 7. Ordered work packages

### Work package A — Scope-matched escalation rungs

Intent: a durable row must carry the rung of the scope it belongs to.

Required changes: split the single rung into the model-scoped and the
account-scoped rung; read the model's counter without advancing it when the
reason is not model-scoped; write the model rung into the model-scoped row
and the account rung into the account-scoped row.

Acceptance evidence:
`rust/tests/durable_health_effects.rs::the_durable_rungs_match_the_scope_of_their_row`.

### Work package B — The live reason reaches the durable rows

Intent: the durable vocabulary must not collapse two failure classes.

Required changes: pass the live reason into the model-scoped writer and
record it in the model-scoped durable row and in the model quarantine
observation.

Acceptance evidence:
`rust/tests/durable_health_effects.rs::a_model_scoped_durable_row_carries_the_live_reason`.

### Work package C — Hydration restores the row's scope

Intent: a restart must restore the effect the row describes.

Required changes: branch the hydration on the row's scope rather than on the
reason value, keeping the malformed-row check that fails the batch closed.

Acceptance evidence:
`rust/tests/durable_health_effects.rs::a_model_scoped_row_restores_the_model_after_hydration`.

### Work package D — A read-only model counter

Intent: the model rung must be observable without being advanced.

Required changes: add a read-only accessor for the model's consecutive-
failure count to the health manager.

Acceptance evidence: work package A's test, which requires the model row to
carry a rung of one while the account row carries the escalated account rung.

### Work package E — The hydration diagnostic honours the subscriber

Intent: a newly-live production path must use the runtime's logging channel.

Required changes: replace the hydration skip-and-warn's `eprintln!` with a
`tracing::warn!` carrying the same account id, which is an internal integer
and not a secret.

Acceptance evidence: the change is visible in the diff and the strict Clippy
run; the message names a kind and a value, never a payload.

## 8. Failure, cancellation, restart, contention semantics

- **Duplicate delivery.** A repeated failure re-enters the same scope and
  advances that scope's counter once; the other scope's counter is untouched.
- **Restart.** A restart hydrates the durable rows. A model-scoped row
  restores the model disable and the model's own rung; an account-scoped row
  restores the account cooldown. A row naming an unknown account is skipped
  with a warning so one stale row cannot fail the whole batch.
- **Partial persistence failure.** A failed durable write is logged as a
  warning and does not abort the live effect; the live in-memory state and
  the durable state can then diverge until the next write, which is the
  pre-existing behaviour of this milestone.
- **Contention.** The model counter and the account counter are separate
  maps under the health manager's own lock; reading one does not advance the
  other.
- **Cancellation.** No await happens under the routing selection lock, so a
  cancelled request cannot leave the lock held.

## 9. Compatibility and migration

No migration. The durable backoff table's schema and unique key are unchanged.
The reason column already stores `BackoffReason::as_str`, and the reader
already accepts exactly the writer's vocabulary, so a row written before this
pass stays readable. A row written before this pass that carries
`model_unavailable` with a model id keeps its meaning: it still restores a
model disable, and its rung is still the model's own rung as recorded then.

The one behavioural difference for existing rows is that a model-scoped row
whose reason is not `model_unavailable` now restores a model disable instead
of an account cooldown. That is the correction this pass exists for.

## 10. Required tests

`rust/tests/durable_health_effects.rs` — serial `--test-threads=1`.

- `a_model_scoped_durable_row_carries_the_live_reason`
- `the_durable_rungs_match_the_scope_of_their_row`
- `a_model_scoped_row_restores_the_model_after_hydration`

The existing targets that must keep passing:
`rust/tests/health.rs`, `rust/tests/routing_domain.rs`,
`rust/tests/routing_claims.rs`, `rust/tests/quota.rs`,
`rust/tests/routing_fail_closed.rs`, `rust/tests/backoff_policy_adoption.rs`.

No fixed millisecond sleeps and no fixed `yield_now()` count loops are used
as readiness or cleanup proofs.

## 11. Required verification commands

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --test durable_health_effects -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test health -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test routing_domain -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test routing_claims -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test quota -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test routing_fail_closed -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test backoff_policy_adoption -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_c011 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_c013 -- --test-threads=1
```

## 12. Documentation updates

- `plans/closure/routing-selection/006-status.md` — its status line moves to
  `corrective pass required` and it gains a pointer to this corrective pass.
  Its body is not rewritten; the corrective closure supersedes its two
  unqualified escalation statements.
- `plans/closure/routing-selection/005-status.md` — its status line moves to
  `corrective pass required` and it gains a pointer to this corrective pass.
- `architecture/deep-dive-health.md` and `architecture/deep-dive-routing.md` —
  update only if they describe the durable row's reason or rung in a way this
  pass changes.

## 13. Acceptance criteria

- A model-scoped durable row records the reason the live path classified, not
  a fixed reason.
- A model-scoped durable row records the model's own escalation rung, and an
  account-scoped row records the account's.
- After a restart, a model-scoped row disables the model and leaves the
  account usable, and an account-scoped row cools the account down.
- A run of account-scoped 408 or 5xx failures no longer changes the
  model-unavailable ladder a later model absence escalates along.
- The durable reason vocabulary is exactly the writer's vocabulary and the
  reader accepts exactly it.
- The backoff policy table in `rust/src/health/backoff.rs` is unchanged.
- No credential, prompt, raw body, or cache key reaches persistence, logs, or
  diagnostics.

## 14. Stop conditions

Stop and report rather than improvise when: the correction would require
changing the backoff policy's values; the durable table's unique key would
have to change; the eligibility gate would have to observe a different scope
than the live path; the scope of the correction expands into the coordinator
or the server.

## 15. Closure evidence required

The closure record must contain: the requirement-to-evidence matrix with one
row per work package; the exact commands run and their real counts; the
deliberate-failure demonstration for each work package, showing the owning
test failing when the fix is reverted and passing when it is restored; the
invariant review; the failure and recovery review; the compatibility review;
the security review; the documentation evidence; and the unresolved findings,
including the unrun full serial workspace suite, the two unqualified
Milestone 006 statements, and the account-scoped row the durable validation
rejects.

## 16. Handoff notes

Rust tests run serial with `--test-threads=1`. Strict Clippy with
`-D warnings` is a repository invariant: no baseline allowlist and no broad
suppression. The policy table is frozen for this pass — a correction there is
a behaviour change on the hot path and needs its own milestone. The
repository forbids `git checkout`, `git restore`, `git stash`, `git reset`, and
`git clean`; use a `cp` backup under `/tmp` for any reversible experiment and
verify the restore with `diff`.

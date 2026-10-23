# Routing Selection Milestone 004 — Closure Status

Status: closed

Source implementation plan:

- `plans/implementation/routing-selection/004-quota-health-recovery-fail-closed-corrections.md`

Source subsystem roadmap:

- `plans/subsystems/routing-selection-roadmap.md#milestone-004--quota-recovery-interval-and-health-absence-fail-closed-corrections`

Repository baseline reviewed: `e6822d8f78838b273cee2351d14e5d5dd3ee1e10`

Implementation commits or pull requests:

- Not committed. The implementation is in the working tree on `main` at the
  baseline above; no commit was created.

## 1. Executive finding

M004 is complete and **closed**. Four fail-open conditions that could admit
a candidate the routing path could not justify are now closed at their own
boundary:

- the `LocalQuotaMode::HardCap` gate excludes an account that has no quota
  record instead of admitting it unchecked;
- an absent `HealthManager` is no longer read as a healthy account;
- the missing-account recovery interval rejects a non-finite or non-positive
  value instead of coercing it, and its two readers now read one value;
- a non-finite quota observation timestamp is rejected before any counter
  moves.

The production selection result is unchanged. The generation build path
always supplies a health manager, and the production quota estimator is
built from the same account registry the router selects from, so the
production account always has a quota record. The corrections change the
behavior of construction variants that omit one of those facts, which is
what the milestone's own acceptance criteria require.

## 2. Requirement-to-evidence matrix

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| HardCap excludes an account with no quota record | `rust/tests/routing_fail_closed.rs::hard_cap_excludes_an_account_without_a_quota_record` | pass | Exclusion reason code is the existing `quota_exhausted`; the pre-change code produced `malformed_score` from the later scorer gate |
| Score-only mode never applies the quota gate | `rust/tests/routing_fail_closed.rs::score_only_never_applies_the_quota_gate` | pass | The reason code is not `quota_exhausted`; the over-limit candidate contract stays covered by `routing_domain_d008::local_quota_mode_only_changes_scoring_gate_not_authoritative_health` |
| An absent health manager is not evaluated as healthy | `rust/tests/routing_fail_closed.rs::absent_health_manager_is_not_evaluated_as_healthy` | pass | The claim path selects nothing; the pre-change code claimed `account-a` |
| A present manager with a registered healthy account selects that account exactly as before | `rust/tests/routing_fail_closed.rs::present_health_manager_claims_a_registered_healthy_account` | pass | Non-regression guard; passes against both the pre-change and post-change code |
| A NaN or infinite recovery interval is rejected | `rust/tests/routing_fail_closed.rs::non_finite_recovery_interval_is_rejected_and_keeps_the_previous_value` | pass | The previously configured interval is retained; the pre-change code accepted NaN as `0.0` and infinity unchanged |
| Both recovery readers apply the same floor | `rust/tests/routing_fail_closed.rs::recovery_readers_apply_the_same_floor` | pass | One helper backs both readers; the pre-change allowance reader used the raw value |
| A NaN quota observation timestamp does not advance the quota window | `rust/tests/quota.rs::non_finite_observation_timestamp_is_rejected_without_mutating_the_window` | pass | The window's usage stays `(0, 0)` and a later finite observation is still accepted |
| No routing-decision schema, exclusion reason code, public scorer API, or config classification change | `git diff --name-only` shows no config, migration, or scorer change; every exclusion reason code asserted by the new tests already existed | pass | `rust/src/quota/scorer.rs` untouched |
| Default and no-default workspace suites pass | `cargo clippy --workspace --all-targets -- -D warnings` clean; the affected targets green (§4) | partial | The full serial workspace suite and the `--no-default-features` build were **not** run in this pass, by instruction to keep verification targeted. `cargo check --workspace --all-targets --no-default-features` remains outstanding (§10) |

## 3. Production implementation evidence

- `rust/src/routing/eligibility.rs`: the `LocalQuotaMode::HardCap` branch now
  computes `within_limits` and excludes on `!within_limits`. A missing
  record is the same verdict as a record over its limits. The reason code
  `quota_exhausted` is unchanged. `LocalQuotaMode::ScoreOnly` is untouched.
- `rust/src/routing/router.rs`: `probe_available_read_only` and the
  missing-account recovery filter now match explicitly on an absent health
  manager and return "not healthy" rather than reading absence as health.
- `rust/src/routing/router.rs`: `with_missing_account_recovery` accepts only
  a finite, positive interval and otherwise leaves the previously configured
  value in place. A new `recovery_min_interval_s()` helper returns the one
  effective value (`max(configured, 1.0)`) and both recovery readers use
  it, so they cannot disagree.
- `rust/src/quota/state.rs`: `QuotaWindow::add_observation` returns before
  any state mutation when the timestamp is not finite, so no counter moves
  and `last_observation_timestamp` does not advance.
- `rust/src/coordinator/endpoints.rs`: the stale comment claiming that "the
  router is otherwise constructed without a manager and no cooldown,
  circuit-breaker, or model-withdrawal effect ever runs in production" was
  corrected; the manager is handed to the router.

Test-fixture consequence: the fifteen `RoutingRouter` constructions across
twelve test targets that passed `None` for the health manager now construct
one and register their accounts, because a router without a manager
deliberately selects nothing. The behavior those targets assert is
unchanged.

## 4. Verification executed

### Commands run

```bash
cargo fmt --manifest-path rust/Cargo.toml --all
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --test routing_fail_closed -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test quota -- --test-threads=1
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

- `cargo fmt --all` — clean, no diff after formatting.
- `cargo clippy --workspace --all-targets -- -D warnings` — **pass**, zero
  findings.
- `routing_fail_closed` — 6 passed, 0 failed.
- `quota` — 11 passed, 0 failed.
- The 25-target sweep — 25 targets, 0 failures, 1 pre-existing ignored
  test.

### Regression-guard evidence

The four production corrections were temporarily reverted in the working tree
and the new tests re-run:

| Test | Pre-change result |
|---|---|
| `hard_cap_excludes_an_account_without_a_quota_record` | FAILED — reason code was `malformed_score`, not `quota_exhausted` |
| `absent_health_manager_is_not_evaluated_as_healthy` | FAILED — a claim was published |
| `non_finite_recovery_interval_is_rejected_and_keeps_the_previous_value` | FAILED — 4 recovery probes instead of 2 |
| `recovery_readers_apply_the_same_floor` | FAILED — 4 recovery probes instead of 2 |
| `non_finite_observation_timestamp_is_rejected_without_mutating_the_window` | FAILED — the window counted the rejected observation |
| `present_health_manager_claims_a_registered_healthy_account` | passed (non-regression guard) |
| `score_only_never_applies_the_quota_gate` | passed (non-regression guard) |

The corrections were restored from a backup copy and the two targets were
re-run green.

## 5. Invariant review

| Invariant | Evidence |
|---|---|
| Candidate ordering, exclusion reason codes, fairness mode and scope, pending-claim accounting, and routing-decision traces remain equivalent for every previously handled input | The only changed reason code is the hard-cap verdict for an account with no quota record, which previously reached the scorer and was excluded there as `malformed_score`. Production accounts always have a quota record, so no production input changes. `routing_domain`, `routing_domain_d008`, `routing_claims`, and `routing_selection_efficiency` are green |
| The public `QuotaFairScorer` methods and exported routing and request types remain source compatible | `rust/src/quota/scorer.rs` is untouched; no exported signature changed in `rust/src/routing/` |
| Selection is deterministic and load-based, never cost-based | No scoring path was touched |
| The selection lock is not held across new work | The corrections are pure predicates inside the existing critical section and add no `await` |
| No new configuration key, dependency, schema, or migration | `git diff --name-only` shows none |

## 6. Failure and recovery review

- **Idempotency** — not applicable; the corrections change a verdict, not a
  write.
- **Cancellation races** — not applicable; no await was added.
- **Restart** — the recovery interval is a router construction input, not
  persisted state.
- **Malformed input** — this milestone is the malformed-input path: a
  non-finite interval and a non-finite observation timestamp are now
  rejected at their boundary instead of being coerced.
- **Contention** — unchanged; the recovery reader now takes one lock-held
  read of the effective interval instead of two independent reads.

## 7. Migration and compatibility review

No schema migration, no config change, no protocol change. The correction is
in-memory only and is not reversible by configuration.

## 8. Security review

No secret, credential, prompt, raw body, or cache key is introduced. The
diagnostics added are a `tracing::debug!` and `tracing::warn!` that name a
reason, never a value. No new authorization or privilege surface.

## 9. Documentation and operations

- `architecture/deep-dive-routing.md`: records that the health manager is
  not optional in the production generation, that an absent manager is
  fail-closed, that the hard-cap gate requires a quota record, that a
  non-finite quota observation is rejected, and that the recovery interval
  is validated and read through one helper.
- `architecture/overview.md`: the routing and health paragraph records the
  durable-health wiring added by milestone 005.
- No new operator command, no new diagnostic surface, no operator action is
  required.

## 10. Unresolved findings

| Severity | Finding | Impact | Required action |
|---|---|---|---|
| medium | The full serial workspace suite and the `--no-default-features` build were not run in this pass, by instruction to keep verification targeted at this stage | The no-default-features parity required by the source plan is unverified | Run `cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features` and `cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings`, then the full serial suite, before the change is pushed |
| low | `build_routing_plan` (the read-only plan path used by readiness) still treats an absent health manager as "no health information" and lists the account as a candidate, while the claim path now fails closed | A router built without a manager would report a candidate it cannot claim | Production always supplies a manager, so there is no production divergence. The eligibility gate has no reason code for "health unavailable", and adding one would change the exclusion-reason schema this milestone must not change. Documented in `architecture/deep-dive-routing.md` |
| low | The score-only acceptance criterion is exercised as "the quota gate did not fire" rather than "the account remains a candidate" | The account with no estimator entry is excluded by the scorer as `malformed_score` under score-only mode | Pre-existing scorer behavior, not introduced here, and unchanged by this milestone. The score-only over-limit candidate contract is covered by the existing `routing_domain_d008` test |

## 11. Roadmap disposition

Milestone closed; the next dependency may proceed. Milestone 005 was
implemented against the same baseline and is closed in its own record.

## 12. Registry updates

- `plans/registry.md`: the routing-selection milestone 004 row moves from
  `ready` to `closed`, with the closure record linked.
- `plans/subsystems/routing-selection-roadmap.md`: the milestone 004 row in
  the milestone status table moves to `closed`.
- `plans/implementation/routing-selection/004-quota-health-recovery-fail-closed-corrections.md`:
  the `Status` line moves from `ready` to `implemented`.

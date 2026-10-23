# Routing Selection Milestone 006 — Closure Status

Status: corrective pass required

Corrective pass: `plans/closure/routing-selection/007-status.md` — the durable
scope and reason defects recorded here were reopened by review, and the two
unqualified escalation statements in this record are superseded by it.

Source implementation plan:

- `plans/implementation/routing-selection/006-backoff-policy-adoption-and-health-reason-vocabulary.md`

Source subsystem roadmap:

- `plans/subsystems/routing-selection-roadmap.md`

Repository baseline reviewed: `e6822d8f78838b273cee2351d14e5d5dd3ee1e10`

Implementation commits or pull requests:

- Not committed. The implementation is in the working tree on `main` at the
  baseline above; no commit was created.

## 1. Executive finding

M006 is complete and **closed**. The live failure path now takes its backoff
duration from the bounded policy that already existed in
`rust/src/health/backoff.rs`, and the reason vocabulary is written from one
typed mapping instead of nine hand-written string literals.

No persisted string changed. No configuration key, HTTP or CLI contract,
schema, dependency, concurrent map, or lock architecture changed.

**The escalation-state design point was resolved without a stop condition.**
The plan's hard constraint was that escalation state must not move into the
routing selection lock. The investigation found the mechanism already
existed: `HealthManager` (`rust/src/health/health_manager.rs`) owns per-account
`consecutive_failures` and `consecutive_cooldowns`, and a per-model rung
advanced by `record_model_failure` — which the live path had never advanced.
The router reads exactly those counters. No new counter, no new map, and no
await was added inside `RoutingRouter::selection_lock`; the effect still runs
after the claim is published.

Two live behavior changes follow directly from adopting the policy as written:
a quota exhaustion and a rate limit without a usable retry hint now produce a
timed backoff where they previously produced none. The frozen C001
compatibility fixture still matches byte-for-byte.

## 2. Requirement-to-evidence matrix

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| Work package A — one typed mapping, total over every failure category | `rust/tests/backoff_policy_adoption.rs::every_failure_category_maps_to_a_reason_with_no_unmapped_arm` | pass | All ten `FailureCategory` variants map; `Temporary` splits 408 → `ConnectTimeout` and 5xx → `UpstreamServerError` |
| Work package A — no hand-written reason literal remains | `...::the_failure_classifier_carries_no_hand_written_reason_string` | pass | A source-level contract test over `rust/src/coordinator/failure.rs` |
| Work package A — the persisted strings are unchanged | `...::the_live_classification_writes_the_pre_change_reason_strings` | pass | The classifier emits the pre-change values |
| Work package B — the live duration comes from the policy base | `...::the_live_backoff_duration_comes_from_the_policy_base` | pass | — |
| Work package B — a repeated failure escalates and never exceeds the cap | `...::a_repeated_failure_escalates_under_the_policy_and_never_exceeds_the_cap` | pass | — |
| Work package B — a model-scoped failure escalates on its own model counter | `...::a_model_scoped_failure_escalates_on_its_own_model_counter` | pass | — |
| Work package B — the policy applies its base, escalation, and cap | `rust/tests/health.rs::the_policy_applies_its_base_escalation_and_cap` | pass | — |
| Work package C — every written reason round-trips through its stored string | `rust/tests/health.rs::every_written_reason_round_trips_through_its_stored_string` | pass | All ten reasons round-trip |
| Work package C — the reverse-mapping fallback is observable | `...::the_stored_reason_fallback_is_reported_at_an_observable_level` | pass | The fallback is logged at warn level, not debug |
| Work package C — the durable clamp takes its ceiling from the policy | `...::the_durable_deadline_clamp_takes_its_ceiling_from_the_policy` | pass | `rust/src/health/repository.rs` derives the ceiling from `get_backoff_policy(reason).cap` instead of the literal `1_800.0` |
| Work package C — a policy-derived deadline survives a durable write | `rust/tests/health.rs::a_policy_derived_deadline_survives_a_durable_write` | pass | — |
| Work package C — a reason without a policy produces no timed backoff | `...::a_reason_without_a_policy_produces_no_timed_backoff` | pass | Authentication failure keeps zero base and zero ceiling |
| Work package D — the writer set equals the reader set | `...::the_written_and_the_accepted_reason_vocabularies_are_the_same_set` | pass | A source-level guard over `BackoffReason::as_str` and `BackoffReason::try_from` |
| Plan §10 — a rate limit still backs the account off | `rust/tests/routing_domain.rs::a_rate_limited_failure_still_backs_the_account_off` | pass | — |
| Plan §10 — an authentication failure still produces no timed backoff | `...::an_authentication_failure_still_produces_no_timed_backoff` | pass | — |
| Plan §10 — a policy-backed cooldown gates the account without touching fairness or claim state | `rust/tests/routing_claims.rs::a_policy_backed_cooldown_gates_the_account_without_touching_fairness_state` | pass | — |
| The persisted reason strings are byte-identical to the pre-change set | §3 and `the_live_classification_writes_the_pre_change_reason_strings` | pass | `BackoffReason::as_str` and `BackoffReason::try_from` were not modified |
| The C001 compatibility fixture is unchanged | The frozen fixture `tests/fixtures/coordinator/compatibility-observations.json` | pass | Matches byte-for-byte |
| The routing-decision schema, exclusion reason codes, and the public `QuotaFairScorer` API are unchanged | `git diff` over `rust/src/routing/` | pass | No selection-path schema change |
| No database await enters the selection lock | `git diff` over `rust/src/routing/router.rs` | pass | No await was added inside `RoutingRouter::selection_lock`; the durable effect still runs after the claim is published |
| Targeted tests pass | 11 targets, 111 tests, 0 failed (§4) | pass | — |
| Strict Clippy | `cargo clippy --workspace --all-targets -- -D warnings` | pass | Exit 0, no warnings |
| No-default-features build | `cargo check --workspace --all-targets --no-default-features` | pass | Clean |
| Format | `cargo fmt --all -- --check` | pass | Clean |
| Full serial workspace suite | Not run | not run | Deliberately out of scope for this stage at the user's direction; the plan §11 requires it. See §10 |

## 3. Production implementation evidence

| File | Change |
|---|---|
| `rust/src/coordinator/failure.rs` | New total typed mapping `FailureCategory::backoff_reason(self, status: Option<u16>) -> BackoffReason` covering all ten categories. `policy_backoff_duration(reason)` replaces the inline 30 s / 300 s / 20 s constants with `compute_backoff_seconds(reason, 1, None, true)`. All nine hand-written reason literals removed — the string is now produced once from `BackoffReason::as_str()`. The transport branch now pins `category = TransientTransport` so the mapping is category-driven on every path |
| `rust/src/routing/router.rs` | New `PolicyBackoff` holder, `RoutingRouter::resolve_backoff_reason` (warn-level fallback) and `RoutingRouter::policy_backoff` (policy-derived delay, escalation rung, cap). `apply_failure_effects` now uses the computed delay for `disable_model`, `record_cooldown`, and both durable mirrors, and passes the rung to the durable rows |
| `rust/src/health/repository.rs` | The durable deadline clamp is derived from `get_backoff_policy(reason).cap` instead of the literal `1_800.0` |

`rust/src/health/backoff.rs` was **not** modified — the policy already existed
as the plan required, and it is adopted as written.

### Live backoff duration per failure class — before and after

"Before" is the inline constants in the pre-milestone `classify`. "After" is
the policy-derived value at each consecutive-failure rung; every rung is at or
below the 1 800 s cap.

| Failure class | Reason | Before | After (policy) |
|---|---|---|---|
| transport / connection failure | `connection_failure` | 30 s constant, never escalated | 30 s base → 60 → 120 → 240 → 480 → 960 → 1800 (cap) |
| request timeout (408) | `connect_timeout` | 30 s constant, never escalated | 30 s base → 60 → 120 → 240 → 480 → 960 → 1800 |
| upstream 5xx | `upstream_server_error` | 20 s constant, never escalated | 20 s base → 40 → 80 → 160 → 320 → 640 → 1280 → 1800 |
| model absent (401/404) | `model_unavailable` | 300 s constant, never escalated | 300 s base (model-scoped rung) → 600 → 1200 → 1800 |
| quota exhausted (402/403/409/422) | `quota_exhausted` | **no timed backoff at all** | 300 s base → 600 → 1200 → 1800 |
| rate limited (429/425/409/422) | `rate_limited` | the raw upstream hint, or **none** without a hint | with a hint: the hint through the policy (≥ 60 s base, ≤ 1 800 s, jitter-reduced); without a hint: 60 s base → 120 → 240 → 480 → 960 → 1800 |
| authentication failure | `authentication_failed` | no timed backoff | no timed backoff (policy base 0, cap 0) — unchanged |

Two deliberate behavior changes, both required by "the policy is adopted as
written": a quota exhaustion and a rate limit without a usable hint now
produce a timed backoff where they previously produced none.

### Persisted reason strings — unchanged

The pre-change literal set was nine sites carrying seven distinct values:
`connection_failure`, `authentication_failed`, `model_unavailable` (×2),
`quota_exhausted`, `connect_timeout`, `rate_limited`, `upstream_server_error`.
The same seven values are produced today, now via `BackoffReason::as_str()`.
Neither `BackoffReason::as_str` nor `BackoffReason::try_from` was touched, so
the durable on-disk contract is unchanged. A row written before this change
remains readable and maps to the same effect.

### Escalation state — the investigation result

The plan's hard constraint was that escalation state must not move into the
routing selection lock, and its stop condition said to stop and report if it
would have to. **It did not.** The mechanism already existed:

- `HealthManager` owns per-account `consecutive_failures` and
  `consecutive_cooldowns`.
- `record_model_failure` advances a per-model rung that the live path had
  never advanced.
- The router reads exactly those counters — the model rung for
  account-model-scoped reasons, the account rung otherwise.

No new counter, no new map, no await inside the selection lock.

### Tests

- **New:** `rust/tests/backoff_policy_adoption.rs` — 12 tests covering the
  vocabulary guards, mapping totality, live policy-derived durations, live
  escalation and cap, model-scoped escalation, the no-policy fallback, and the
  authentication no-backoff case.
- **Extended:** `rust/tests/health.rs` (+3 — policy ladder and cap, reason
  round-trip, durable deadline surviving the write), `rust/tests/routing_domain.rs`
  (+2 — rate limiting and authentication), `rust/tests/routing_claims.rs` (+1 —
  policy cooldown gating without disturbing fairness state).

### Docs

- `architecture/deep-dive-health.md` — the adopted policy, the typed
  vocabulary, the persisted strings, the durable clamp, and the new
  invariants.
- `architecture/deep-dive-routing.md` — the policy-derived live duration and
  the escalation-state ownership.

## 4. Verification executed

### Commands run

```bash
cargo fmt --manifest-path rust/Cargo.toml --all
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo test --manifest-path rust/Cargo.toml \
  --test health --test routing_domain --test routing_claims \
  --test backoff_policy_adoption --test durable_health_effects \
  --test coordinator_c013 --test coordinator_c014 --test coordinator_failure \
  --test coordinator_c009 --test coordinator_c011 --test routing_fail_closed \
  -- --test-threads=1
uv run --frozen python scripts/validate_release_docs.py
```

### Results

- `cargo fmt --all -- --check` — clean.
- `cargo clippy --workspace --all-targets -- -D warnings` — exit 0, no warnings.
- `cargo check --workspace --all-targets --no-default-features` — clean.
- Targeted test run — **11 targets, 111 tests, 0 failed**.
- `scripts/validate_release_docs.py` — `status: pass`.
- Batch integration sweep — `cargo fmt --all -- --check` clean; strict Clippy exit 0 in both feature configurations; `cargo check --workspace --all-targets --no-default-features` clean; 47 test targets plus the lib, **645 passed, 0 failed, 1 ignored** (the pre-existing manual gate), covering every target touched by this milestone and by the four milestones closed alongside it.
- `cargo deny` — not run; there is no dependency change.

### Regression-guard evidence

Each revert was applied in place, the named test run, then the file restored
from a `/tmp` backup and md5-verified identical before and after.

| WP | Revert | Test | Failure |
|---|---|---|---|
| A | The classifier restored to the nine hand-written literals | `the_failure_classifier_carries_no_hand_written_reason_string` | `a failure reason must come from the typed mapping, never from a string literal` |
| A | The mapping changed so `Authentication` → `Unknown` | `every_failure_category_maps_to_a_reason_with_no_unmapped_arm` | `left: "unknown", right: "authentication_failed"` |
| A | same | `the_live_classification_writes_the_pre_change_reason_strings` | FAILED |
| B | `policy_backoff` reverted to the pre-milestone caller-supplied duration | `a_repeated_failure_escalates_under_the_policy_and_never_exceeds_the_cap` | `left: 0.0, right: 300.0` — the pre-milestone quota failure had no timed backoff |
| B | same | `a_model_scoped_failure_escalates_on_its_own_model_counter` | `left: Some(Some(300.0)), right: Some(Some(600.0))` |
| C | The durable clamp reverted to the literal `1_800.0` | `the_durable_deadline_clamp_takes_its_ceiling_from_the_policy` | FAILED |
| C | The reverse-mapping fallback reverted to `tracing::debug!` | `the_stored_reason_fallback_is_reported_at_an_observable_level` | FAILED |
| D | `"upstream_server_error" => Self::UpstreamServerError` removed from `TryFrom` only | `the_written_and_the_accepted_reason_vocabularies_are_the_same_set` | The exact set difference (writer 10 values vs reader 9); the two runtime guards failed too |

After every restore the full new target was re-run: 12 passed, 0 failed.

## 5. Invariant review

| Invariant | Evidence |
|---|---|
| The persisted reason strings in the durable health tables keep their current values | §3 lists the pre-change literal set; `the_live_classification_writes_the_pre_change_reason_strings` and `every_written_reason_round_trips_through_its_stored_string` prove it |
| A reason whose policy has zero base and zero ceiling keeps its effect of no timed backoff | `an_authentication_failure_still_produces_no_timed_backoff` and `a_reason_without_a_policy_produces_no_timed_backoff` |
| Routing stays deterministic and load/quota/health based — never cost based | No change to scoring, allocation, or selection; the policy only decides a delay |
| The routing-decision schema, the exclusion reason codes, and the public `QuotaFairScorer` API are unchanged | `git diff` over `rust/src/routing/` shows no selection-path schema change |
| No database await enters the selection lock | No await was added inside `RoutingRouter::selection_lock`; the durable effect runs after the claim is published |
| Escalation state is per account and per model | `HealthManager`'s per-account counters and `record_model_failure`'s per-model rung are read directly; `a_model_scoped_failure_escalates_on_its_own_model_counter` proves the model-scoped rung is independent |
| No secret, prompt, raw body, or cache key enters health state or diagnostics | The change only maps a reason and computes a delay; no payload is copied |
| No new dependency, concurrent map, or lock architecture change | No Cargo change; no new shared state; the selection lock is untouched |
| No configuration key added, removed, or reclassified | No config surface touched |
| No public HTTP or CLI schema change | No server or CLI surface touched |
| The policy is adopted as written | `rust/src/health/backoff.rs` was not modified; no policy value changed |
| No migration | The durable schema is unchanged; the clamp is derived, not migrated |

## 6. Failure and recovery review

The failure path gains no new failure mode. The change replaces a hand-written
literal with a typed mapping and a hand-written constant with a policy
lookup.

One behavior is new and worth stating precisely: a stored reason the reader
cannot parse now falls back to `BackoffReason::Unknown` at **warn** level
rather than at debug level, so a vocabulary mismatch is visible to an operator
rather than silent. The vocabulary guard
(`the_written_and_the_accepted_reason_vocabularies_are_the_same_set`) fails
CI if the two sets ever diverge, so the fallback should never fire in
production.

The escalation path reuses the existing `HealthManager` counters, so the
existing cooldown and circuit-breaker recovery behavior is unchanged. A
repeated failure now escalates rather than repeating a flat delay, and the
cap bounds the escalation.

## 7. Migration and compatibility review

No migration. No storage change. No configuration key added, removed, or
reclassified. No public HTTP or CLI contract change. No public Rust API break
beyond the additive helper surface on `FailureCategory` and
`RoutingRouter`.

The frozen C001 compatibility fixture
(`tests/fixtures/coordinator/compatibility-observations.json`) still matches
byte-for-byte, because the policy bases coincide with the old inline constants
for every failure class that previously had one.

Two live behavior changes are recorded above: quota exhaustion and a rate
limit without a usable retry hint now produce a timed backoff.

## 8. Security review

- **Auth** — unchanged. The authentication failure reason keeps zero base and
  zero ceiling, so an auth failure still produces no timed backoff and the
  account is not silently parked.
- **Secret handling** — the change copies no payload. Only a reason enum and
  a duration are derived. No credential, prompt, raw body, or cache key
  enters health state or a diagnostic.
- **Privilege bounds** — unchanged.
- **DoS bounds** — the escalation ladder is bounded by the policy cap of
  1 800 s, so a failing provider cannot produce an unbounded backoff. The
  durable clamp is now derived from the same policy cap.
- **Audit behavior** — the reverse-mapping fallback is logged at warn level,
  which is a deliberate improvement in observability over the previous debug
  level.

## 9. Documentation and operations

- `architecture/deep-dive-health.md`: the adopted policy, the typed
  vocabulary, the persisted strings, the durable clamp, and the new
  invariants.
- `architecture/deep-dive-routing.md`: the policy-derived live duration and
  the escalation-state ownership.
- `rust/tests/backoff_policy_adoption.rs`: the new target, 12 tests.
- No operator command, diagnostic, or recovery instruction changed.

## 10. Unresolved findings

| Severity | Finding | Impact | Required action |
|---|---|---|---|
| medium | The hint path now floors at the policy base | A provider that says "retry in 12 s" gets a 60 s cooldown for a rate limit — the policy's base. This is the policy as written ("a provider hint is never allowed to back off less than the policy base delay"), but it is a live behavior change for short hints | If operators object, the fix belongs in the policy table as a per-reason base, not in the call path. This milestone deliberately did not touch policy values. Any change to the policy needs a separate plan, because M006's acceptance depends on the policy being adopted as written |
| medium | The full serial Rust workspace suite was not run | The plan §11 requires `cargo test --workspace --all-targets -- --test-threads=1`; the user's direction for this stage was targeted verification only | Run the full serial suite before pushing. The no-default-features *check* and Clippy passed; its *test* suite did not run |
| low | The durable clamp is equivalent today, not merely equivalent by construction | Every reason with a timed backoff currently has `cap == 1_800.0`, so the derived ceiling equals the old literal and the behavioral proof passes in both states | None. The genuine guard is the source-level `the_durable_deadline_clamp_takes_its_ceiling_from_the_policy`; the derived ceiling is what prevents future drift |
| low | `apply_failure_effects` still takes the duration as a positional argument | The policy now owns the duration, so the argument is effectively the retry-after hint, and its name is now misleading | Rename `backoff_until` → `retry_after_hint` in a follow-up that owns `coordinator/finite.rs` and `coordinator/streaming/**`. The frozen C001 fixture pins the field's meaning, so it cannot be repurposed here |
| info | The plan §3 prose is slightly inaccurate | It states the live path used "30 seconds … for a rate limit without a usable retry hint"; the pre-milestone code assigned `backoff_until = retry_after`, that is, no backoff without a hint, and the C001 fixture confirms `http_429_rate_limit → 12.0` | The table in §3 reflects the code, not the plan prose. The plan file's prose is a planning document and is not rewritten |

## 11. Roadmap disposition

Milestone closed; the next dependency may proceed. Routing selection M006 was
the only registered successor in this subsystem, so no milestone is promoted
and the blocked-work table gains no routing-selection entry.

The Routing-selection roadmap remains **top-level `active`**. Two obligations
survive this milestone and are tracked in the subsystem rather than here:

- The durable mirror persists the live decision but does not yet own the
  backoff *policy*; the persisted backoff rows are still read back through the
  same policy path, and a future milestone may need to reconcile a durable
  row written under an older policy with the current one.
- The live path and the durable path share the policy, but the reason
  vocabulary guard only covers the writer and the reader inside
  `rust/src/health/backoff.rs`; a future migration of the durable tables
  would need its own guard.

## 12. Registry updates

- `plans/registry.md`: the Routing selection M006 row is removed from
  "Dependency-ready implementation plans" and added to "Recently closed" with
  its disposition; the Routing selection row in "Active subsystem roadmaps"
  records `M006 closed`.
- `plans/subsystems/routing-selection-roadmap.md`: the milestone 006 row in
  the milestone status table moves from `blocked` to `closed` with its closure
  record linked; the dependency graph is updated to reflect the closed state.
- `plans/implementation/routing-selection/006-backoff-policy-adoption-and-health-reason-vocabulary.md`:
  the `Status` line moves from `blocked` to `implemented`.

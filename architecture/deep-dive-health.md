# Deep Dive: Health, Circuit Breakers, and Quarantine

Back to [Architecture](README.md)

See also the review index in [overview.md](overview.md) (§8).

## Ownership

`rust/src/health/` owns account health, bounded backoff, circuit breaking, narrow effect
application, per-model quarantine, and restart-safety persistence. Authority files are
`mod.rs`, `health_manager.rs`, `backoff.rs`, `circuit_breaker.rs`,
`quarantine.rs`, and `repository.rs`. The module never retries requests, chooses
destinations, or finalizes durable request rows.

## Health manager

`HealthManager` is a process-local synchronous map (`AccountHealth` live, projected as
`AccountHealthSnapshot`) with an injectable clock (`with_clock`). `register_account`
seeds one entry per configured account. Routing gates use only the read-only pair
`is_account_healthy_read_only` / `is_model_healthy_read_only`; `try_acquire_request` is
the sole mutating claim operation and `release_request` frees the breaker probe slot.
Mutations are `record_success`, `record_failure`, `record_cooldown`, `disable_account`,
`enable_account`, `disable_model`, `enable_model`, and `prune_disabled_models`.
`snapshot` lazily normalizes expired timed disables so diagnostics agree with gating.
`hydrate_backoffs` converts validated wall-clock `AccountBackoffRecord` rows into monotonic
remaining durations; unknown accounts are skipped with a warning, never fail-closed. A legacy
row whose reason is model-scoped but whose `model_id` is NULL carries no scope this runtime can
restore, so it is skipped and counted rather than failing the batch, and it grants neither an
account-wide nor a model-wide suppression. The count reaches the hydration report
(`HealthHydrationReport::skipped_legacy_backoffs`) and is surfaced as a warning that names the
count and the reason vocabulary only.

## Backoff and circuit breaker

`backoff.rs` defines `BackoffReason` (`AuthenticationFailed`, `QuotaExhausted`,
`RateLimited`, `ModelUnavailable`, `ConnectTimeout`, `ConnectionFailure`,
`UpstreamServerError`, `ProtocolError`, `ContextLimitExceeded`, `Unknown`),
`classify_failure_category`, `BackoffPolicy`, `get_backoff_policy`,
`compute_backoff_seconds`, and `MAX_NONTERMINAL_BACKOFF_SECONDS` (1,800 s).
Authentication yields no delay (sticky disable via the zero `BackoffPolicy`); quota/rate-limit honor
`retry_after` when finite, never below the policy base delay and never for a zero hint, so a
provider cannot bypass escalation; model-unavailable is account/model-scoped; unknown and
context-limit carry no delay (`get_backoff_policy` returns `None`).

The live failure path adopts this policy. The classifier
(`coordinator/failure.rs::FailureCategory::backoff_reason`) maps every classified failure
to a typed `BackoffReason` — a total mapping over `FailureCategory`, with the categories that
carry no provider-attributable backoff resolving to `Unknown` rather than to an unparsable
string — and the durable writer serializes the reason with `BackoffReason::as_str`. The
persisted reason strings are a durable on-disk contract and keep their current values:
`authentication_failed`, `quota_exhausted`, `rate_limited`, `model_unavailable`,
`connect_timeout`, `connection_failure`, and `upstream_server_error`. The reader parses a
stored reason back with `BackoffReason::try_from`; the written set and the accepted set are
the same set, and `rust/tests/backoff_policy_adoption.rs` guards that equality at the source
level so the two cannot drift again.

The duration is the policy's, never an inline constant. `compute_backoff_seconds` takes the
reason's own policy (base delay, doubling multiplier, cap, jitter fraction, consecutive
failure ceiling, account-versus-model scope) and the consecutive-failure rung, so the first
failure takes the base delay and every consecutive failure doubles it up to the cap. The
escalation state is the health manager's existing counters: the per-account
`consecutive_failures`/`consecutive_cooldowns` pair, and the per-model rung advanced by
`record_model_failure`. No second counter and no new map exists.

`circuit_breaker.rs` implements a synchronous three-state breaker (`CircuitState::Closed`,
`Open`, `HalfOpen`) with one half-open probe. `can_request` is the gating view,
`allow_request` acquires the probe, `record_success` / `record_failure` consume it, and
`release_probe` frees it. `try_acquire_probe` returns a cancel-safe `ProbeGuard` so a
dropped request cannot stall half-open forever; stale probes are reclaimed past the
recovery timeout. Poisoned locks deny new work while `stats` reports a fail-closed `Open`
diagnostic (`CircuitStats`).

## Narrow effects

`routing/router.rs::RoutingRouter::apply_failure_effects` is the one effect-application path
the runtime uses. It maps one classified failure to the narrowest safe mutation.
Account-wide failures advance the breaker/cooldown; `ModelUnavailable` with a model key
writes the exact quarantine partition and calls `disable_model` only, never advancing the
account circuit, and escalates its own per-model rung (`record_model_failure`, seeded from
the durable rung on hydration, cleared by a success for the same model) so a persistently
failing model reaches the later policy rungs; `ContextLimitExceeded` and `Unknown` only release the probe.
The applied effect is mirrored into the durable repositories, so the live decision and the
persisted row cannot disagree about the delay that was applied. No credentials, prompts,
bodies, or cache keys cross this boundary.

There is no second applier. The parallel `HealthEffectApplier` module that used to sit
beside this path had no production caller and was removed, so the repository carries one
design of the health subsystem rather than two contradictory ones.

The router derives the duration from the policy rather than accepting one from the caller:
`apply_failure_effects` resolves the typed reason, reads the escalation rung the health
manager already tracks for that account or model, and asks `compute_backoff_seconds` for the
bounded delay. The duration it receives from the classifier is the policy's retry-after hint
input — for the reasons whose policy consumes a provider hint it is the upstream hint, and
for every other reason the policy ignores it. The result never exceeds the cap the reason's
policy declares. A stored reason the reader cannot parse is corrupt state rather than a new
vocabulary: it normalizes to `Unknown` and is reported at warn level rather than silently
dropped.

## Quarantine

`quarantine.rs::ModelQuarantine` keys exact partitions (`QuarantineKey`: provider, account,
canonical model, optional upstream model, upstream protocol, SHA-256 digest).
`is_model_quarantined` / `is_model_quarantined_for` gate routing; missing protocol uses the
`unknown` partition so unclassifiable candidates cannot bypass an exact quarantine.
`record_observation` moves `Suspected` to `Quarantined` at the promotion threshold with
bounded TTLs; `record_observation_until` takes the caller's expiry instead, which is what
the routing path uses so the gate and the health manager exclude a model for the same
window; `set_terminal_withdrawn` requires authoritative provenance
(`ProviderCatalog`, `ManualOverride`, `OperatorAction`) and never expires. The terminal
vocabulary has no producer in the runtime: `TerminalWithdrawn`, `set_terminal_withdrawn`,
and the `ProviderCatalog` and `MigrationLegacy` provenances are retained as the reader for
a durable state the schema admits, so an existing `terminal_withdrawn` row keeps being
parsed and keeps being enforced by the gate above.
`clear_exact_key`, `clear_authoritative_reappearance`, `manual_clear`, `hydrate_entry`,
`prune_expired`, and `list_entries` keep clearing exact-key and never silently disable
unrelated models. Entries are LRU-capped at 4,096 with terminal rows evicted last.
`EvidenceProvenance` and `entry_from_row` validate durable rows before hydration.

## Restart-safety persistence

`repository.rs` persists only restart hints in the `account_backoffs` and
`model_quarantine` tables: `AccountBackoffRepository` (`list_all`, `hydrate_into`,
`list_active`, `upsert`, `clear_success`, `clear_authentication`, `expire_old`) and
`ModelQuarantineRepository` (`list_all`, `hydrate_into`, `list_active`, `upsert_entry`,
`mark_cleared`, `expire_old`). Reads truncate at 5,000 rows with a warning instead of
failing open or closed; writes validate identity, reason, counts, and finite timestamps.

Both repositories are constructed once per generation in the generation build path
(`coordinator/endpoints.rs`), never per request. The build path hydrates the generation's
`HealthManager` from `account_backoffs` and its `ModelQuarantine` from `model_quarantine`
before the generation is published, and attaches the quarantine to the router with
`with_quarantine` so the eligibility gate is live. A row naming an account the generation
does not know is skipped with a warning and never fails the build; a legacy row that carries
no recoverable scope is skipped and counted, so one row this runtime cannot interpret never
decides whether the process starts; a repository failure that cannot be bounded fails
generation publication closed, because a generation must not become reachable with partially
hydrated health.

The durable scope convention is unchanged: a NULL `model_id` means account-wide suppression
and a non-NULL value scopes the row to one (account, model) pair. The table-level
`UNIQUE(account_id, model_id, reason)` does not cover the NULL scope, because SQLite treats
NULLs as distinct in a unique index, so migration v64 adds the partial unique index
`uq_account_backoffs_null_model` over `(account_id, reason)` where `model_id IS NULL`, after
the duplicate-row cleanup that must precede it. The lookup key stays the NULL-safe
`model_id IS ?2` form. The writer still refuses to create a model-unavailable row without a
model identity, so no new malformed row can be written by this runtime; the hydration's
skip-and-count path exists for a restored backup or a writer this runtime does not own.

The routing path's failure effects are mirrored into the same tables after the in-memory
application, outside the selection lock. The mirror records the live decision rather than
a recomputed one: the account backoff carries the applied delay, and the model-scoped
quarantine uses the canonical-only partition the eligibility gate always evaluates with
the live backoff deadline as its expiry. The live path only ever writes a bounded
observation, because the runtime has no producer for the terminal state; the durable
terminal vocabulary stays as the reader for a row this runtime does not write. Expired rows
are retired by the `retention_cleanup` task on the bounded database maintenance schedule.

The credential rejection is the one durable backoff shape without a deadline. The
authentication policy declares no delay, so the live effect is the account disable and the
durable row is written on the `(account_id, NULL, 'authentication_failed')` key with no
expiry; the hydration restores it with `disable_account(name, reason, None)`, so the account
stays disabled across a restart until the credential-change path clears the row or
`clear_authentication` deletes it. The in-memory disable and its timing are unchanged: the
durable row is an addition that makes the suppression survive a restart, and a failed
durable write is warned about and leaves the live disable standing.

The durable write clamps a persisted deadline to the cap the reason's own policy declares
(`get_backoff_policy(reason).cap`, never a literal), so a policy-derived deadline within the
cap is never silently shortened by the write and the durable ceiling cannot drift away from
the policy.

Readiness observes the in-memory health state only. `operations/status.rs::evaluate_readiness`
reads cached snapshots with no outbound provider probes and no writes; it does not query
the durable tables. A restart therefore observes the persisted backoff and quarantine
because generation build hydrated them into the in-memory state, not because readiness
reads them.

## Invariants

- Model-scoped failures never advance the account circuit; transport/auth failures never
  quarantine unrelated models.
- Gating is read-only until `try_acquire_request`; probes are always released.
- Non-terminal suppression is bounded by `MAX_NONTERMINAL_BACKOFF_SECONDS`.
- The live backoff duration is the policy's, and the escalation rung is the health
  manager's existing per-account and per-model counter.
- The written reason values and the accepted reason values are the same set; a reason the
  reader cannot parse is reported, not silently normalized.
- Only authoritative provenance creates terminal withdrawal; only exact keys clear.
- Persistence holds restart hints only, validated on read, with secret-free diagnostics.
- A durable mirror never widens the live window: the persisted expiry is the deadline the
  live path applied, and a terminal withdrawal is never persisted as a terminal row.
- A row this runtime cannot interpret is skipped and reported, never fatal, and grants no
  suppression.
- The durable account-scoped scope is unique: one row per `(account_id, reason)` where
  `model_id IS NULL`.

## Verification

```bash
cargo test --manifest-path rust/Cargo.toml --test health -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test routing_domain -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test backoff_policy_adoption -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test quota -- --test-threads=1
```

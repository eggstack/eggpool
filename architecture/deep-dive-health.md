# Deep Dive: Health, Circuit Breakers, and Quarantine

Back to [Architecture](README.md)

See also the review index in [overview.md](overview.md) (§8).

## Ownership

`rust/src/health/` owns account health, bounded backoff, circuit breaking, narrow effect
application, per-model quarantine, and restart-safety persistence. Authority files are
`mod.rs`, `health_manager.rs`, `backoff.rs`, `circuit_breaker.rs`, `effects.rs`,
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
remaining durations; unknown accounts are skipped with a warning, never fail-closed.

## Backoff and circuit breaker

`backoff.rs` defines `BackoffReason` (`AuthenticationFailed`, `QuotaExhausted`,
`RateLimited`, `ModelUnavailable`, `ConnectTimeout`, `ConnectionFailure`,
`UpstreamServerError`, `ProtocolError`, `ContextLimitExceeded`, `Unknown`),
`classify_failure_category`, `BackoffPolicy`, `get_backoff_policy`,
`compute_backoff_seconds`, and `MAX_NONTERMINAL_BACKOFF_SECONDS` (1,800 s).
Authentication has no backoff policy (sticky disable); quota/rate-limit honor
`retry_after` when finite; model-unavailable is account/model-scoped; unknown and
context-limit carry no delay.

`circuit_breaker.rs` implements a synchronous three-state breaker (`CircuitState::Closed`,
`Open`, `HalfOpen`) with one half-open probe. `can_request` is the gating view,
`allow_request` acquires the probe, `record_success` / `record_failure` consume it, and
`release_probe` frees it. `try_acquire_probe` returns a cancel-safe `ProbeGuard` so a
dropped request cannot stall half-open forever; stale probes are reclaimed past the
recovery timeout. Poisoned locks deny new work while `stats` reports a fail-closed `Open`
diagnostic (`CircuitStats`).

## Narrow effects

`effects.rs::HealthEffectApplier::apply` maps one classified `HealthEffect` (built via
`HealthEffect::account` plus optional `model`) to the narrowest safe mutation.
Account-wide failures advance the breaker/cooldown; `ModelUnavailable` with a model key
writes the exact quarantine partition and calls `disable_model` only; `ContextLimitExceeded`
and `Unknown` only release the probe. The returned `HealthEffectOutcome` records
`account_changed`, `model_changed`, `circuit_penalized`, `probe_released`,
`backoff_seconds`, and `terminal_withdrawal`. Optional repositories persist the same
bounded facts; no credentials, prompts, bodies, or cache keys cross this boundary.

## Quarantine

`quarantine.rs::ModelQuarantine` keys exact partitions (`QuarantineKey`: provider, account,
canonical model, optional upstream model, upstream protocol, SHA-256 digest).
`is_model_quarantined` / `is_model_quarantined_for` gate routing; missing protocol uses the
`unknown` partition so unclassifiable candidates cannot bypass an exact quarantine.
`record_observation` moves `Suspected` to `Quarantined` at the promotion threshold with
bounded TTLs; `set_terminal_withdrawn` requires authoritative provenance
(`ProviderCatalog`, `ManualOverride`, `OperatorAction`) and never expires.
`clear_exact_key`, `clear_authoritative_reappearance`, `manual_clear`, `hydrate_entry`,
`prune_expired`, and `list_entries` keep clearing exact-key and never silently disable
unrelated models. Entries are LRU-capped at 4,096 with terminal rows evicted last.
`EvidenceProvenance` and `entry_from_row` validate durable rows before hydration.

## Restart-safety persistence

`repository.rs` persists only restart hints in the schema-54 `account_backoffs` and
`model_quarantine` tables: `AccountBackoffRepository` (`list_all`, `hydrate_into`,
`list_active`, `upsert`, `clear_success`, `clear_authentication`, `expire_old`) and
`ModelQuarantineRepository` (`list_all`, `hydrate_into`, `list_active`, `upsert_entry`,
`mark_cleared`, `expire_old`). Reads truncate at 5,000 rows with a warning instead of
failing open or closed; writes validate identity, reason, counts, and finite timestamps.
Readiness reuses this state through `operations/status.rs::evaluate_readiness` with cached
snapshots only: no outbound provider probes and no writes.

## Invariants

- Model-scoped failures never advance the account circuit; transport/auth failures never
  quarantine unrelated models.
- Gating is read-only until `try_acquire_request`; probes are always released.
- Non-terminal suppression is bounded by `MAX_NONTERMINAL_BACKOFF_SECONDS`.
- Only authoritative provenance creates terminal withdrawal; only exact keys clear.
- Persistence holds restart hints only, validated on read, with secret-free diagnostics.

## Verification

```bash
cargo test --manifest-path rust/Cargo.toml --test health -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test routing_domain -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test quota -- --test-threads=1
```

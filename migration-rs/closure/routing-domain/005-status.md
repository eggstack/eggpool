# D005 Closure — Health, Backoff, Circuit Breaker, and Quarantine

Status: closed

Recommendation: closed; D006 is now the sole dependency-ready M5 implementation plan. D007 remains queued behind D006, D008 remains queued behind D001-D007, and M6 implementation handoff remains blocked on integrated D008 closure.

Implementation commit: [`d5dd16d`](https://github.com/eggstack/eggpool/commit/d5dd16d)

Plan: [D005 — health, backoff, circuit, and quarantine](../../implementation/routing-domain/005-health-backoff-circuit-and-quarantine.md)

Contract and oracle: [M5 routing-domain contract](../../routing-domain-contract.md), D001's [Python observations](../../fixtures/routing-domain/d001-python-observations.json), and the canonical Python modules `health.backoff`, `health.circuit_breaker`, `health.health_manager`, `failure.quarantine`, and the `account_backoffs`/`model_quarantine` repositories.

## Outcome

D005 adds the Rust health boundary under `rust/src/health/`. It provides the
normalized failure vocabulary and exact authentication classification rules,
reason-specific bounded backoff with downward-only Retry-After jitter,
lock-protected three-state circuit breakers with one reclaimable half-open
probe, account/model health state, typed restart hydration, and the exact-key
model-quarantine state machine.

The typed `AccountBackoffRepository` and `ModelQuarantineRepository` operate on
the existing schema-54 tables without a new migration. Durable wall-clock
timestamps are parsed and written as UTC SQLite timestamps; hydration converts
remaining wall duration to the process-local monotonic health clock and clamps
nonterminal state to 1,800 seconds. Invalid mandatory identity, enum, count,
timestamp, scope, or expiry state is rejected.

`HealthEffect` and `HealthEffectApplier` are the narrow application boundary.
They update health/quarantine state and optional durable repositories only;
they do not retry, select an account, submit network traffic, create request or
attempt rows, map downstream status, or finalize requests.

## Requirement-to-evidence matrix

| D005 requirement | Evidence | Result |
|---|---|---|
| Stable category boundary | `health/backoff.rs` preserves auth exact-match vocabulary, 402 quota, 408 timeout, 409/422 unknown, 429 rate, 5xx, transport-like, model, context, and unknown distinctions. | Pass |
| Bounded backoff | `compute_backoff_seconds` implements all reason schedules, exponent caps, invalid Retry-After fallback, 1,800-second normalization, and downward-only provider-wait jitter through `JitterSource`. | Pass |
| Circuit semantics | `CircuitBreaker` exposes CLOSED/OPEN/HALF_OPEN, read-only `can_request`, mutating `allow_request`, one probe, explicit release, stale probe reclaim, and deterministic clock injection. | Pass |
| Account health | `HealthManager` tracks health state, timestamps/categories, generic/cooldown counters, account disable, model deadlines/terminal markers, and separate read-only/claim APIs. | Pass |
| Cooldown isolation | Quota/rate effects use `record_cooldown` and release the probe without advancing breaker failures; generic effects record breaker failures. | Pass |
| Exact model scope | `QuarantineKey` hashes provider/account/canonical/upstream/protocol identity; sibling providers, accounts, aliases, and protocols remain isolated. | Pass |
| Quarantine lifecycle | Suspected → quarantined, expiry, exact success clear, authoritative reappearance, manual clear, terminal provenance gate, and bounded pruning are implemented. | Pass |
| Durable backoffs | Typed schema-54 read/write/clear/expiry operations validate rows, deduplicate NULL model scopes, preserve model scope, and hydrate remaining durations. | Pass |
| Durable quarantine | Typed schema-54 read/write/clear/expiry operations preserve provenance, identity, counters, clear audit fields, and NULL upstream identity matching. | Pass |
| Stale/corrupt state | Hydration never demotes resident healthy/terminal state; corrupt state fails the repository operation instead of becoming eligible. | Pass |
| Boundary ownership | The health module has no request retry, provider client, request/attempt persistence, selection, or finalization dependency. | Pass |

## Verification commands actually run

All commands below completed successfully:

```text
rtk cargo fmt --manifest-path rust/Cargo.toml
rtk cargo clippy --manifest-path rust/Cargo.toml --all-targets -- -D warnings
rtk cargo test --manifest-path rust/Cargo.toml --test health -- --test-threads=1  # 8 passed
rtk cargo test --manifest-path rust/Cargo.toml --test database_compatibility -- --test-threads=1  # 6 passed
rtk cargo test --manifest-path rust/Cargo.toml --test quota -- --test-threads=1  # 7 passed
rtk cargo test --manifest-path rust/Cargo.toml --test routing_domain -- --test-threads=1  # 12 passed
rtk cargo test --manifest-path rust/Cargo.toml --test provider_transport -- --test-threads=1  # 29 passed
rtk cargo test --manifest-path rust/Cargo.toml --lib -- --test-threads=1  # 16 passed
rtk uv run pytest tests/migration_rs/test_d001_routing_domain.py -q --tb=short --maxfail=1  # 7 passed
rtk git diff --check
```

The focused health suite includes Python SQLite readback of Rust-written
backoff and quarantine rows. The existing Rust database compatibility suite
also applies the schema-54 routing seed and verifies the two durable health
tables. A repository-wide `cargo test --all-targets` wrapper was attempted but
does not terminate in this workspace at the existing catalog-refresh fixture;
the affected catalog-refresh suite is therefore retained as a known
workstation limitation, consistent with the D004 closure record. No live
provider, inference request, network, or performance gate is required for this
state-only milestone.

## Acceptance and stop-condition review

All D005 acceptance criteria pass: nonterminal state is bounded to 30 minutes,
Retry-After never extends a provider-specified wait, ordinary success cannot
clear auth/operator terminal state, cooldowns do not poison the breaker,
read-only checks do not consume probes, exactly one half-open probe is owned,
restart state uses remaining durations, runtime quarantine cannot create a
terminal withdrawal, and stale durable quarantine cannot resurrect a cleared
entry. Explicit `release_request` covers claim rollback, cancellation, client
errors, cooldowns, and model-disabled outcomes.

## Future-plan state

D005 is removed from the dependency-ready table and recorded in the completed
implementation table and this closure record. D006 is promoted from queued to
dependency-ready because D004 and D005 are both closed. D007 remains queued on
D006, D008 remains queued on D001-D007, and M6 implementation handoff remains
blocked on accepted integrated D008 closure. No other future plan can be safely
unblocked by D005 under the repository's serial handoff policy.

Unresolved mandatory findings by severity: none.

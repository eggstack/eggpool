# C003 — Runtime Wire Resolution and Negotiation Ownership

Status: ready for handoff; C002 accepted closure

Source roadmap: `migration-rs/subsystems/coordinator-roadmap.md`

Primary class: capability/invariant

Hard dependency: C002.

## Objective

Port the process-owned dynamic wire resolver deliberately excluded from M6: candidate ordering, learned preference, deterministic rejection suppression, negotiation single-flight, provider admission gating, and alternate-wire ownership. Keep HTTP failure interpretation outside the resolver; C005 supplies authorized transitions.

## Python oracle

Use C001 plus `wire/resolver.py`, wire registry/config preferences, coordinator integration points, negotiation metrics/tests, and M6 static profile identity.

## Required behavior

Implement bounded resolver state keyed by provider/model/candidate fingerprint. Preserve:

- operator-fixed preference;
- metadata/bundled hints;
- learned preference with TTL;
- per-candidate deterministic rejection cooldown;
- fingerprint invalidation when candidate/config/request constraints change;
- bounded LRU/cache capacity;
- per-provider negotiation concurrency cap;
- minimum negotiation interval and delayed provider negotiation after rate limiting;
- leader/follower/throttled roles;
- follower cancellation that does not cancel the leader/shared result;
- leader cancellation that releases only its owned gate and resolves followers with a bounded rejection result;
- accepted candidate learning only after caller-authorized success evidence.

The resolver returns ordered concrete M6 profiles and a negotiation handle; it performs no provider I/O and does not inspect status/body itself.

## Concurrency and cancellation

Use Tokio synchronization with explicit permit ownership. Do not hold a mutex across provider I/O. Cancellation while waiting for a gate must not release another request's permit. Completion/accept/reject must be idempotent enough for duplicate cleanup paths.

## Bounded state

Default capacities/TTLs/intervals must come from the frozen Python/config contract. Candidate rejection maps, flights, provider gates, metrics labels, and learned entries must be bounded or lifecycle-scoped. Do not retain request bodies or credentials.

## Tests

Differential fixtures must cover fixed/learned/hint ordering, TTL expiry, rejection cooldown, fingerprint changes, LRU eviction, leader/follower, throttling, concurrent providers, leader/follower cancellation, rate-limited delay capped at the configured suppression ceiling, and repeated accept/reject/finish calls.

## Dependencies

No actor framework/task queue. Tokio primitives already suffice. No new Cargo dependency expected.

## Acceptance criteria

C003 closes when candidate order/learning/suppression and negotiation concurrency match the Python oracle, all cancellation paths return capacity, state is bounded/redacted, and the resolver still performs zero network/DB writes.

## Closure

Create `migration-rs/closure/coordinator/003-status.md`. Accepted closure promotes C004.

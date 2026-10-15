# On-Prem User Access M023 — Short-window team fairness and admission controls

Status: blocked

Repository baseline: `88f58bbf2e9095cf46437945edf83e580270bdb6` (`main`, post-PR #7).

Source roadmap: `plans/subsystems/on-prem-user-access-roadmap.md#m023--short-window-team-fairness-controls`

Long-term requirements: `plans/000-long-term-specification.md#1-product-definition`, `#2-end-state-invariants-normative`; `plans/002-long-term-roadmap.md#phase-5--optional-on-prem-small-team-access-and-budgeting`.

Applicable ADR: `plans/adrs/ADR-0002-on-prem-user-identity-and-policy-boundaries.md` (accepted); require new ADR if durable budget semantics or fairness ownership changes.

Primary class: capability

## 1. Objective

Prevent one user's bursty coding agents from monopolizing shared provider capacity on a small on-prem team server while retaining minimal solo/SBC overhead. Complement existing daily/monthly request, token, cost and concurrency ceilings with optional bounded short-window admission fairness; do not replace provider-health/quota routing.

## 2. Dependencies

Hard: M022 client/budget interoperability closure. Interface: existing principal authentication, M008/M009 durable reservations and `coordinator/endpoints.rs` admission order. Operational: qualify on representative ordinary on-prem Linux server; Pi/microSD is a solo low-end reference, not the team load-release gate.

## 3. Current evidence

`rust/src/user_budget.rs` and `rust/src/instance_budget.rs` already handle daily/monthly and max-concurrent rules transactionally. `routing/` owns provider-account quota fairness, not downstream-user fairness. An authorized user's burst may exhaust provider throughput or dominate latency without violating day/month budgets. Existing single-gate SQLite performance, particularly Pi microSD write tails, argues against persisting per-request rolling-window rows by default.

## 4. Invariants

Do not let short-window counters substitute for durable daily/monthly budget holds, global ceilings, typed principals or model restrictions. No extra SQLite writer, no queue with unbounded request-body retention, no hidden priority routing or provider-health penalty on user refusal. Single-user instance has zero new operational setup and negligible idle footprint.

## 5. Scope

In: opt-in per-principal short-window requests/sec or requests/minute, optional bounded global admission burst, actionable 429/Retry-After and lightweight visibility. Out: token-throughput shaping based on unknown generated output, tiered billing, weighted-fair queuing, SSO, a durable distributed rate limiter, 50-simultaneous inference promise.

## 6. Production changes

Implement a bounded in-memory per-user token bucket or sliding-window admission guard in the single daemon. Limit number of retained principals and stale-window lifetime, prevent key spoofing and excessive hash-map growth, and reject before body/upstream reservation where principal is known. Consider typed admission result with exact distinction between short-window throttle, hard budget, and provider 429. Define restart semantics explicitly: ephemeral limiter resets on restart, but durable budgets do not.

## 7. Ordered work packages

### A — Define fairness contract and defaults

Freeze absent-rule behavior (no additional limit), operator-specified policy and best-effort limiter semantics. Choose a monotonic clock and deterministic Retry-After calculation. Separate identity from token: several agent credentials for one human share the same principal bucket.

Acceptance: documented no-rule solo fast path and bounded memory by unique principals.

### B — Implement bounded admission guard

Insert at an existing authenticated pre-dispatch boundary without blocking SQLite or current-thread execution; avoid background process and request-body buffering. Handle simultaneous last-slot requests and configured per-user/global rate intersection. Reject with stable 429 plus Retry-After and no upstream attempt.

Acceptance: concurrent bursts enforce the declared bucket limits without impacting provider-account health or durable budget counters.

### C — Verify ordinary server throughput and UX

Use a synthetic provider and multiple principals on ordinary server/storage. Measure admitted/rejected rates and p50/p95/p99 local overhead against no-rule baseline; add sanitized member/admin diagnostic indication and documentation. Run an inexpensive Pi microSD smoke only as compatibility, not team sizing.

Acceptance: no material no-rule regression and no unbounded memory under many bogus credentials or clients.

## 8. Failure, cancellation, restart, contention

Limiter state is ephemeral and resettable after process restart; durable user/global budget state remains authoritative. Cancellation before upstream dispatch should not corrupt long-lived counters; clearly define whether attempts consume an admission slot. No per-client unbounded wait queue. Under max cached users, fail or evict only with a documented safe rule, never silently grant excess throughput.

## 9. Compatibility/migration

Opt-in defaults to disabled; no SQLite schema needed unless an existing admin policy table is minimally extended, which requires checksums and restore evidence. Preserve existing 429 provider/budget errors and avoid breaking client retries. Additional operator config classified by existing reload policy; respect immutable generation transitions.

## 10. Tests

New focused fairness/admission test target plus `rust/tests/on_prem_budgets.rs`, `on_prem_global_budgets.rs`, `on_prem_attribution.rs`, `coordinator_boundaries.rs`. Cover 1/5/25/49 named users with explicitly declared concurrency, many credentials one principal, clock edges, collision/race, absent config, restart, fake provider 429 and maximum cache cardinality.

## 11. Verification commands

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
uv run pytest tests/tooling/ -q --tb=short --maxfail=1
git diff --check
```

## 12. Documentation

`docs/model-limits.md`, `docs/deployment.md`, `docs/api-reference.md`, `architecture/deep-dive-request-lifecycle.md` and `architecture/deep-dive-security.md`; provide ordinary server team recommendations, no SBC SLA.

## 13. Acceptance criteria

With rate limits absent, same solo behavior; with limits present, one user's burst does not exhaust all shared admission slots, a 429 is actionable and distinct from provider failure, and durable user/global budgets remain exactly enforced.

## 14. Stop conditions

Do not start before M022; stop if needed design changes durable reservation or provider fairness without ADR, introduces unbounded queues, adds significant per-request SQLite writes, or has no measured on-prem server baseline.

## 15. Closure evidence

`plans/closure/on-prem-user-access/023-status.md` with exact commit, contention/rate matrix, ordinary Linux hardware/storage metadata and baseline deltas, serial/no-default checks, failure/restart cases, compatibility notes and unblock audit.

## 16. Handoff

P3 optional small-team capability. Ordinary server is the primary team target; SBC microSD remains a lightweight solo regression probe, never the release throughput requirement.

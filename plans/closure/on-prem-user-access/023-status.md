# On-Prem User Access M023 — Closure Status

Status: conditionally closed

Source implementation plan:

- `plans/implementation/on-prem-user-access/023-short-window-fairness-controls.md`

Source subsystem roadmap:

- `plans/subsystems/on-prem-user-access-roadmap.md#m023--short-window-team-fairness-controls`

Repository baseline reviewed: `deda832d` (M022 closed and M002 ordinary-server
qualification closed; M023 dependency gates satisfied).

Implementation commits:

- `84482e91b84fe7424144dcf916441479c5b492d1` — implement bounded short-window admission fairness.
- `3722ae64ede200da69415c1da711dd6c5248c519` — move M023 from ready to active.
- `472bef9f5dd8575f66416ac9cf07c1a766efc722` — move M023 from active to closing.

## 1. Executive finding

M023's production capability is implemented. Optional per-principal and
instance-global 60-second token buckets now run after authentication and
generation acquisition, before body collection and durable budget admission.
They share allowance across credentials owned by one user, bound retained
principal state, fail closed at capacity, return a distinct 429 with
`Retry-After`, and expose remaining allowance headers on admitted responses.
No-rule behavior allocates no limiter state. No queue, database write,
migration, provider-routing change, or provider-health effect was added.

The full local default/no-default Rust suites, strict Clippy, focused HTTP and
reload tests, and tooling suite pass. The plan's post-change p50/p95/p99
comparison on an ordinary Linux server with durable SSD/NVMe storage could not
be run here: this checkout is executing on Darwin. The available Ubuntu M002
measurements are a pre-change reference and cannot stand in for a measurement
of the M023 binary. M023 is therefore **conditionally closed** pending that
external operational comparison; no general capacity or latency claim is
made.

## 2. Requirement-to-evidence matrix

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| Keep no-rule behavior unchanged and avoid state allocation | `request::admission_fairness::tests::absent_policy_keeps_the_no_rule_fast_path`; `AdmissionFairnessLimiter::admit_at` | pass | Disabled policy returns before locking the shared limiter. |
| Share per-principal allowance across credentials and isolate 1/5/25/49 users | `all_credentials_for_one_principal_share_a_bucket`; `qualification_user_counts_keep_buckets_separate` | pass | Principal identity is the authenticated database-owned user ID, never a credential or request header. |
| Intersect per-principal and optional global limits | `global_and_principal_rules_intersect_without_consuming_denied_slots` | pass | A denied request does not consume the other bucket. |
| Bound concurrent admission and use a monotonic window | `simultaneous_last_slot_is_granted_once`; `monotonic_refill_admits_again_at_the_window_boundary` | pass | Thirty-two racing callers receive the single remaining token exactly once; refill uses `Instant`. |
| Bound principal state and define full-table behavior | `principal_table_is_bounded_and_evicts_only_fully_stale_state`; `many_principals_remain_within_declared_capacity` | pass | Stale buckets are eligible only after the 60-second bucket can fully refill (120-second retention); otherwise new principals fail closed with a computed retry delay. |
| Return an actionable throttle before reading or dispatching the body | `server_transport::short_window_fairness_rejects_before_dispatch_and_reports_retry_scope` | pass | The second request declares a body but sends none; it receives 429, `Retry-After: 60`, `short_window_rate_limited`, and the principal scope without body collection. |
| Keep policy bounded and live-generation consistent | `config::tests::short_window_fairness_values_are_bounded_and_optional`; `runtime_lifecycle_r005::admission_fairness_policy_is_live_reloaded_as_one_generation_contract` | pass | Optional rates validate from 1–60,000/minute; tracked principals validate from 1–65,536. Policy is read from the acquired request generation. |
| Measure post-change overhead and tails on the ordinary Linux target | M002 reference: `plans/closure/on-prem-concurrency-performance/002-status.md`; current local host: `uname -a` reports Darwin | partial | No post-change Linux p50/p95/p99 run or baseline delta is available from this host. This is the remaining condition. |

## 3. Production implementation evidence

`rust/src/request/admission_fairness.rs` owns the process-local token buckets.
Per-principal state is held in a bounded map; an optional global bucket is a
single value. Stale state is reclaimed only after it has had more than one
full refill window to become idle. A full table with no stale entry refuses a
new principal rather than evicting an active user's allowance. A new limiter
is created with each server AppState, so restart resets these ephemeral
counters.

`server/middleware.rs::admit_inference_body` consults the limiter after auth
and generation acquisition but before `Content-Length` checks, body
collection, and the existing raw-body reservation. A denial returns a
protocol-shaped 429 with `Retry-After`, code `short_window_rate_limited`, and
`X-EggPool-Admission-Scope`. Admitted responses carry
`X-EggPool-Admission-Remaining`, scope, and the 60-second window. Requests
that later fail body validation consume one short-window attempt; cancellation
before upstream dispatch likewise consumes only that ephemeral attempt, which
refills normally. This behavior is documented.

Configuration lives under `[admission_fairness]`. Both rates default to
disabled; changes are live-reloadable and apply according to the acquired
generation. The configuration template's runtime-manifest digest and the
R005 compatibility oracle were updated. There is no schema or migration.

## 4. Verification executed

### Commands run

```bash
rtk cargo fmt --manifest-path rust/Cargo.toml --all -- --check
rtk cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
rtk cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
rtk cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
rtk cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
rtk cargo test --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- --test-threads=1
rtk cargo test --manifest-path rust/Cargo.toml --test runtime_lifecycle_r005
rtk cargo test --manifest-path rust/Cargo.toml --test server_transport short_window_fairness_rejects_before_dispatch_and_reports_retry_scope -- --test-threads=1
rtk cargo test --manifest-path rust/Cargo.toml --test build_manifest
rtk uv run pytest tests/tooling/ -q --tb=short --maxfail=1
rtk git diff --check
```

### Results

- Default workspace: 1,050 passed, 1 ignored across 80 suites.
- No-default workspace: 1,051 passed, 1 ignored across 80 suites.
- Strict Clippy passed with default and no-default features; no-default check passed.
- Focused config/reload target: 11 passed. Focused loopback HTTP target: 1 passed. Runtime asset manifest target: 4 passed.
- Tooling pytest: 199 passed, 1 skipped.
- Formatting and `git diff --check` passed.
- Verification is local. Hosted CI and post-change Linux hardware qualification were not run in this closure pass.

## 5. Invariant review

- Durable daily/monthly user and instance budgets remain authoritative and are
  not read or modified by the short-window limiter.
- Requests rejected by this limiter do not reach endpoint parsing, provider
  selection, provider health, quota, or upstream submission.
- Principal keys come from authenticated identity state. Tokens, credentials,
  request bodies, prompts, IPs, and provider outcomes are not retained.
- Limiter state and critical sections are bounded. The no-rule fast path does
  not acquire the limiter mutex. There is no per-client wait queue or extra
  SQLite writer.
- Requests use one immutable configuration generation for body admission and
  the downstream execution path. The limiter itself is process-local and
  shared across generations.

## 6. Failure and recovery review

- User/global throttles return a stable, distinct 429 with a positive
  `Retry-After`; neither is confused with provider or durable-budget 429s.
- If both buckets apply, a denial does not consume a token from the other
  bucket. Concurrent callers serialize only the bounded bucket decision, so a
  final token is granted once.
- If principal-state capacity is reached, active entries are not evicted to
  grant a new caller excess traffic. Stale state becomes evictable only after
  refill has completed.
- Restart discards short-window state. Durable holds, settled budgets,
  provider retry state, and request recovery are unaffected.
- Body validation failure or cancellation after the admission decision may
  consume one ephemeral token; the token then refills normally. No durable
  counter or reservation is left behind by the limiter.

## 7. Migration and compatibility review

No migration was added. Existing configurations omit `[admission_fairness]`
and retain disabled throttling. Configured rate and table-bound changes are
classified live in the canonical reload policy. The HTTP error is distinct
from hard-budget, exhausted-budget, and provider rate-limit responses.

## 8. Security review

Authentication runs before this check. Bucket identity cannot be selected by
an API token value, header, model name, IP address, or body field. Unknown
authenticated principals cannot grow state without limit. Capacity exhaustion
fails closed for new principals, and response diagnostics contain only the
bounded scope and remaining-count facts.

## 9. Documentation and operations

Updated `docs/model-limits.md`, `docs/api-reference.md`, `docs/deployment.md`,
`architecture/deep-dive-request-lifecycle.md`, and
`architecture/deep-dive-security.md`. `rust/config.example.toml` documents the
disabled-by-default options. Operators can rehash the policy; restarting
clears its in-memory buckets.

The M002 host reference is Ubuntu 24.04.5 with ext4 on Samsung SSD 980 NVMe.
It recorded the pre-M023 synthetic envelope and remains useful as the
comparison baseline. This checkout reports Darwin and did not produce
post-change measurements for that Linux host.

## 10. Unresolved findings

| Severity | Finding | Impact | Required action |
|---|---|---|---|
| medium | Post-change p50/p95/p99 overhead and admitted/rejected-rate comparison on durable ordinary Linux hardware was unavailable locally | Functional behavior is verified, but no measured no-rule overhead delta or ordinary-server fairness capacity result is claimed | Run the M023 fairness matrix and no-rule comparison on an ordinary Linux SSD/NVMe host against the M002 reference; record host/storage metadata, 1/5/25/49 principal and concurrency results, p50/p95/p99 overhead, and any envelope changes. |
| low | Hosted CI was not run in this local pass | The local default/no-default matrix passes; branch CI remains the hosted gate | Review hosted CI for the final pushed commit. |

No high-severity correctness or security finding remains.

## 11. Roadmap disposition

M023's implementation is complete and may be used with operator-selected
limits. It is conditionally closed until the named ordinary-Linux performance
comparison is recorded. No capacity, latency-SLA, or universal team-size claim
is made. M023 has no registered successor, so this closure unblocks no future
on-prem user-access milestone.

## 12. Registry updates

The closure commit marked the implementation plan `implemented`, removed
M023 from the active implementation table, marked the roadmap milestone
`conditionally closed`, added this record to recent closures, and recorded
the ready/active/blocked audit. No successor was promoted or blocked; M023 is
terminal in the registered line.

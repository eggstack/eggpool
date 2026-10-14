# On-Prem User Access Milestone 005 — Durable user/model budgets

Status: ready

Repository baseline: `main` at `9a0a903c9944b69a6c1ae9fa969bb8603d5c65d9` (accepted new architecture on `plans/on-prem-small-team-user-access`).

Source roadmap: `plans/subsystems/on-prem-user-access-roadmap.md`.

Long-term requirements:
- `plans/000-long-term-specification.md#2-end-state-invariants-normative`
- `plans/001-terminology-and-domain-model.md#identity-and-lifecycle-terms`
- `plans/002-long-term-roadmap.md#phase-5--optional-on-prem-small-team-access-and-budgeting`

Applicable ADR: `plans/adrs/ADR-0002-on-prem-user-identity-and-policy-boundaries.md` (accepted).

Primary class: capability

## 1. Objective

Enforce administratively configured per-user and per-model consumption
budgets independently from existing upstream provider quotas. A budget is
checked and durably reserved **before** provider dispatch and safely
settled/compensated across finite/streaming, retries and crash recovery.
Support request count, token, estimated cost and concurrent-request
dimensions within explicit enforceability limits; handle self-hosted
unpriced LLMs meaningfully. **Hard dependency:** accepted M004 policy
authorization closure. No dashboard editor in this milestone.

## 2. Readiness

M004 is closed with durable policy profiles, revision invalidation, and
concrete-model checks at coordinator admission. M003 established stable
principal ownership and request attribution. Reusing `QuotaPolicy` provider
scoring without an independent user admission authority remains out of scope.

## 3. Baseline evidence

`rust/src/quota/state.rs::AccountQuota` and
`routing/eligibility.rs::LocalQuotaMode` represent provider-account
capacity (default score-only). `coordinator/publication.rs`
creates durable request/attempt/provider reservations;
`coordinator/finalization.rs` settles terminal state;
`coordinator/reconciliation.rs` recovers pending requests;
`rust/assets/db/migrations/0032_usage_rollups.sql` is provider-model-
account aggregated. `db/connection.rs` serializes transactions and
preserves single-gate/WAL constraints. Those mechanisms are references for
ownership, not reusable user budget objects by renaming.

## 4. Non-regressing invariants

- This is one LAN/on-prem instance, not hosted SaaS. The first-admin SBC flow remains canonical; no mandatory external service.
- TOML/environment retain provider topology, provider credentials, outbound routing, model overrides and self-hosted LLM connection details. SQLite holds downstream users, policies, budgets, sessions and usage. Never make the web dashboard a general provider configuration editor.
- Existing Chat Completions/Responses/Messages/compact wire, streaming termination, auth-header compatibility, metadata redaction, recovery, provider scoring and health remain intact.
- Single SQLite gate/worker (WAL/NORMAL), `GenerationLease` and immutable provider topology remain authoritative. No secret/body logging, second retry/finalization owner or unbounded in-memory cache.
- A denied user cannot transform a permitted virtual model or provider qualifier into an unauthorized underlying dispatch.

## 5. Required production changes

### A — Budget definition and units

Add bounded SQLite-managed `user_budget_rules` plus optionally
`profile_budget_rules`, per-user and per-model daily/monthly request,
token and microdollar limits, and a small explicit concurrency ceiling.
Choose and document calendar UTC vs rolling windows; prohibit ambiguous
time-zone reset rules, zero/negative limits and overflows. Distinguish
request **logical count** from upstream attempts; retry/failover may
incur actual upstream token/cost use without counting as another logical
request. Support hard global instance ceilings as intersection of limits
and profile/user rules. Store money as integers; differentiate modeled/
reported/provider-local cost; do not call flat-rate subscription consumption
an actual incremental API charge.

### B — Atomic pre-dispatch reservation

Define `user_budget_reservations` with stable logical request/user/
canonical model ID, applicable policy version and budget window, limits
and projected holds, status, expiry and idempotency token. Use the existing
SQLite transactional single-owner lifecycle to atomically check
`used + live_reserved + request_estimate <= limit` against *every*
applicable aggregate/model window. Concurrent clients cannot both
pass a final remaining slot. In admission, bind the resolved model and
user principal first; no separate model authorization bypass.
Integrate reservation transaction into durable publication or a fully
specified compensation handshake that cannot leave provider submission
without an owned user hold. Never hold locks during network requests.

### C — Settlement, retries and recovery

On terminal outcome, settle one logical request exactly once with
observed token/cost totals, release unused estimates and concurrency
slot, and preserve any actual upstream use due to failed attempts.
Avoid retries counting request quota twice or releasing user reservation
after a nonterminal attempt. Account for delayed streams,
partial usage, disconnect, process kill, finalization supervisor retry,
replayed publication and active provider reservation compensation.
Integrate crash/reconciliation cleanup with explicit cutoff and observed
vs unknown-use treatment. A terminal publish failure must not leave a
phantom hard hold indefinitely; recovery must not grant spent budget
prematurely. Use trusted durable state, not volatile dashboard counters,
for enforcement.

### D — Exactness and unknown costs

Before dispatch, enforce maximum output tokens/request-size bounds where
protocols support them; if a requested hard bound cannot be guaranteed,
reject or conservatively limit admission rather than claim mathematically
exact post-hoc spend enforcement. If provider prices are absent,
token/request budgets still operate; hard USD budgets fail closed for
unpriced usage unless an admin configures a documented conservative
local pricing estimate. No implicit `$0` cost for self-hosted models.
Report provisional vs provider-reported cost and whether a hard
cost cap can be proved for a particular model. Ensure selector/internal
calls are authorized and charged or explicitly blocked.

### E — Usage projection

Add bounded user/model/time usage rollups and authenticated user/admin
read APIs; do **not** inflate `0032_usage_rollups` composite keys
prematurely. Reuse existing retained metadata and cost correction semantics
without retrospectively misbilling user quotas; define what happens on
historical price recompute. Track token, request, cost estimate, and
reservation counters. Queries must use indexed range scans, not unbounded
request history. Include audit on admin quota changes.

## 6. Out of scope

No cloud billing service, charged invoices, provider routing-score changes,
automatic model downgrade when a limit hits, complete fiscal cost
reconciliation for personal subscriptions, user budget UI (M006) or
multi-instance quota coordination.

## 7. Ordered work packages

1. Specify exact budget windows/units and reservation transition table;
   add checked SQL migration/indexes and model/profile rule merge.
2. Implement atomic user-hold admission co-owned by durable publication,
   concurrent last-slot negative fixtures and idempotent replay.
3. Integrate finite, streaming, selector and compact settlement with
   finalizer/recovery; fault inject at every commit/terminal boundary.
4. Add user rollups and bounded read APIs; perform single-owner baseline
   admission/TTFT/SQLite-gate measurements before accepting.

## 8. Failure, cancellation, restart and contention

Define explicit transitions `pending -> active -> settled/released`,
with invariant that durable request admission and budget reservation
cannot disagree after recoverable committed stages. Keep an exactly-once
settlement identity and compensation idempotency even on duplicate
callbacks or server shutdown. If DB unavailable while protected budget
is evaluated, fail closed. An already-started stream is charged to its
snapshot user/profile; administrative limit decreases affect new work
immediately and cannot silently underflow existing holds. In-memory
fast-path counters may accelerate reads but cannot replace the durable
transaction at a hard-cap boundary.

## 9. Compatibility and migration

Do not re-interpret upstream `accounts`, `reservations` or provider
quota `score_only`. No change to API response streaming semantics,
provider health or model attribution. Existing database usage is historical
legacy/unattributed and must not be silently retroallocated. Backup and
restore conserve remaining budget and the authoritative state of holds.

## 10. Tests

Add `rust/tests/on_prem_budgets.rs`. Cover exact simultaneous last-slot
requests, aggregate+model caps, UTC window boundaries, depleted token cap,
inflight concurrency, cost unknown/overflow, private LLM unpriced usage,
pre-dispatch rejection, retry paid upstream attempts, stream cancellation,
EOF without usage, crash after publication commit, crash after provider
response but before finalization, duplicate finalization, DB busy/outage,
policy update midstream, restored database and recovery reconciliation.
Extend `rust/tests/quota.rs`, `coordinator_publication.rs`,
`coordinator_finalization.rs`, `coordinator_c008.rs`,
`coordinator_c009.rs`, `database_compatibility.rs` and
`rust/tests/persistence_projection_architecture.rs`.
```bash
cargo test --manifest-path rust/Cargo.toml --test on_prem_budgets -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_publication -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_finalization -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test quota -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test database_compatibility -- --test-threads=1
```
## 11. Required verification commands

Run the following after focused tests; preserve exact results for closure:
```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
git diff --check
```
If new dependencies/feature flags: `cargo deny --manifest-path rust/Cargo.toml check`,
`cargo tree --manifest-path rust/Cargo.toml -e features`,
`cargo build --manifest-path rust/Cargo.toml --locked --release`.
No test may be declared passed unless actually run.

## 12. Documentation

Update `architecture/deep-dive-database.md`,
`architecture/deep-dive-request-lifecycle.md`,
`architecture/deep-dive-routing.md`,
`docs/model-limits.md`, `docs/api-reference.md` and
`docs/backup-restore.md` with exact units and guarantees.
Do not label estimated USD consumption as real billed charges.

## 13. Acceptance criteria

No user can over-admit a defined hard concurrent request/token hold in a
reproducible race, cross all allowed model budgets by alias/failover,
or free a hold twice. All recoverable crashes converge with correct
attribution and no leak of another user's usage. Unknown-price and
partial-usage status are explicit rather than successfully counted as
free. Latency/DB costs are measured and documented.

## 14. Stop conditions

Stop on unsafe cross-transaction gap between user reservation and provider
publication, inexact quotas presented as exact, unbounded SQLite gate
occupation, or any ambiguity concerning compensation authority. Split
ownership/failure research into corrective ADR/plan rather than widening
scope.

## 15. Closure evidence required

`plans/closure/on-prem-user-access/005-status.md`: state transition
matrix, adversarial overlap/crash fixtures, cost-metering examples,
SQL/index tests, backup/restart results, measured publish/settle overhead,
actual commands/results, severity-tagged residuals and registry audit.

## 16. Handoff

Single DB remains authoritative. Do not alter existing quota-scoring
semantics to enforce user budgets; keep no-default builds working.

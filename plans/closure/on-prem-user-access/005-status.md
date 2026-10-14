# On-Prem User Access Milestone 005 — Closure Status

Status: conditionally closed

Source implementation plan:

- `plans/implementation/on-prem-user-access/005-durable-user-budgets.md`

Source subsystem roadmap:

- `plans/subsystems/on-prem-user-access-roadmap.md`

Repository baseline reviewed: `03f6bbfe` (M005 activation)

Implementation commits:

- `49dc6ce` — durable budget rules, transactional holds, settlement, CLI and read API
- `0bf3e71` — move M005 into closure review

## 1. Executive finding

SQLite-backed user/model budget capability is implemented for logical request
count, request-size token estimates, estimated microdollars, and concurrency.
Publication owns the hold in the same transaction as request and provider
reservation state; terminal finalization owns idempotent settlement. UTC daily
and monthly usage is queryable through an authenticated bounded API. M005 is
conditionally closed because token and cost dimensions are estimates rather
than provable hard ceilings, crash/restore-specific budget fixtures and gate
overhead measurements were not collected, and global-instance budget rules
are not implemented. M006 may proceed against this durable contract, with
these conditions visible in its UI and the later qualification audit.

## 2. Requirement-to-evidence matrix

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| UTC windows, integer units, bounded rule validation and change audit | `rust/assets/db/migrations/0059_user_budgets.sql`; `user_budget_rules_validate_admin_scope_and_usage_reads_are_bounded` | pass | Requests/tokens/microdollars/concurrency are positive bounded integers; day/month are UTC calendar periods. |
| Atomic aggregate and exact-model pre-dispatch holds | `rust/src/coordinator/publication.rs`; `concurrent_last_request_reservations_cannot_over_admit` | pass | Hold and durable publication commit together; concurrent final slot has one winner. |
| Concurrency release after terminal settlement | `concurrent_limit_releases_only_after_terminal_settlement` | pass | All active requests count across day/month boundaries; terminal settlement releases the slot. |
| Retry accounting, partial/unknown usage, duplicate finalization | `rust/src/coordinator/finalization.rs`; `on_prem_budgets` retry and unknown-cost cases | pass | One logical request is charged; transitioned attempts retain usage; unknown values consume reserved fallback; duplicate finalization is idempotent. |
| Authenticated bounded usage projection | `rust/src/server/health.rs`, `middleware.rs`; `on_prem_attribution` | pass | Members read only their own rules/usage; admins may select a target; query limit is capped. |
| CLI rule management | `rust/src/cli.rs`, `runtime.rs`, `rust/tests/cli_contract.rs` | pass | Admin scope and UTC period are explicit; no dashboard editor is added in M005. |
| Migration/checksum/runtime asset registration | migration 0059, `checksums.json`, `runtime-manifest.json`; full serial workspace suite | pass | Migration is append-only and backup includes the authoritative SQLite state. |
| Exact output-token cap for every supported protocol | `request/limits.rs` and publication reservation currently use a bounded request-size estimate | partial | The estimate does not reserve a guaranteed requested output maximum or reject every unbounded-output request. |
| Hard USD cap for unpriced models | routing estimator fallback and budget settlement fallback | partial | Unknown usage is not counted as zero, but a configured user microdollar rule can still rely on the generic estimator fallback; no explicit local-price requirement or fail-closed unknown-price gate exists. |
| Hard global instance budget intersection | no global rule scope in migration/service | not implemented | Profile budget rules were optional; global instance ceilings were required by the source plan. |
| Budget-specific process-crash/restore campaign and measured publish/settle overhead | not collected in this milestone | not run | Generic lifecycle/migration tests ran in the workspace suite; they do not substitute for budget-specific crash/restore fixtures or measurements. |

## 3. Production implementation evidence

Migration 0059 adds bounded user rules, active per-request holds, per-attempt
usage, audit history, and indexed user/model/day/month rollups. The publication
transaction validates current user/credential ownership, evaluates every
matching aggregate and exact-model rule, then inserts or extends the hold with
the durable request/attempt/provider reservation. Retry attempts do not add a
second logical request. Terminal finalization records each transitioned
attempt and settles the request once in the same SQLite transaction. Unknown
token/cost use consumes the held estimate and increments explicit unknown
counters. `/api/user-budget` provides authenticated member/admin reads, and
`eggpool users budget set|remove` manages rules locally.

## 4. Verification executed

### Commands run

```bash
cargo test --manifest-path rust/Cargo.toml --test on_prem_budgets -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
git diff --check
```

### Results

- `on_prem_budgets`: 4 passed.
- Full local serial Rust workspace/all-target suite: 962 passed, 1 ignored,
  77 suites.
- Formatting, default strict Clippy, no-default check, no-default strict
  Clippy, and whitespace checks passed.
- No CI run or physical SBC measurement was performed for this closure.

## 5. Invariant review

- User budgets remain separate from upstream provider quota scoring and do not
  affect load-based account selection.
- Publication has one SQLite transaction owner; a user hold cannot commit
  separately from the durable request/provider reservation.
- Holds survive nonterminal attempts and retries; terminal settlement and
  duplicate finalization are idempotent.
- User usage reads are bounded and owner-fenced; diagnostics do not include
  request bodies, prompts, or credentials.
- Unsupported exactness is documented in `docs/model-limits.md` and the
  request/routing/database deep dives. Estimated USD is not labeled as an
  invoice or exact cost.

## 6. Failure and recovery review

Concurrent last-slot publication and terminal concurrency release are covered.
Retry partial usage, unknown usage fallback, and duplicate terminal settlement
are covered. Database errors propagate through publication/finalization rather
than being treated as permission to dispatch. A budget-specific process kill
between publication and settlement, post-provider-response recovery, and
backup/restore round-trip were not directly injected; those remain evidence
conditions rather than inferred passes.

## 7. Migration and compatibility review

Migration 0059 is additive and checksummed; the full workspace migration and
database compatibility tests passed. Backups include the new tables as part of
the SQLite database, while provider credentials remain config/environment
owned. Older executables still require a matching pre-upgrade backup to roll
back across the schema version.

## 8. Security review

Rule mutation requires an active admin and records actor/action/version. Member
reads are scoped to the authenticated owner; admin target selection is
role-checked. Usage endpoints cap row counts. Cost, token, and request counters
are bounded integers; hold evaluation uses checked wide arithmetic. No secret
or body fields are included in usage projections.

## 9. Documentation and operations

Updated `architecture/deep-dive-database.md`,
`architecture/deep-dive-request-lifecycle.md`,
`architecture/deep-dive-routing.md`, `docs/api-reference.md`,
`docs/backup-restore.md`, `docs/cli-reference.md`, and `docs/model-limits.md`.
The API exposes UTC windows and separates provider-reported, local-estimated,
and reservation-fallback microdollars.

## 10. Unresolved findings

| Severity | Finding | Impact | Required action |
|---|---|---|---|
| Medium | Output-token reservations are request-size estimates and do not enforce a guaranteed maximum output for every protocol. | A token rule is not a mathematically hard spend ceiling when the provider can emit more output than the admitted estimate. | Add protocol-aware maximum-output admission and fail closed when a hard token rule cannot be bounded. |
| Medium | USD rules can use generic fallback pricing for an unpriced model. | A configured USD limit is an estimate and cannot prove a hard cap for self-hosted/unpriced usage. | Require a documented operator-configured conservative local price or reject hard-cost admission when price is unknown. |
| Medium | No global-instance budget scope/intersection exists. | Administrators cannot enforce the source plan's instance-wide hard ceiling independently of user rules. | Add global rule scope and intersection tests before claiming full M005 hard-cap completion. |
| Medium | M005-specific crash/restart/backup-restore tests and publish/settle overhead measurements were not run. | Recovery correctness and SQLite gate cost for new budget writes are not directly quantified. | Add restart/restore fault fixtures and reproducible overhead measurement during qualification. |

## 11. Roadmap disposition

M005 is conditionally closed. The durable user-level request/usage capability
is available for M006 integration, while the four findings above remain
explicit conditions. M006 is promoted to ready because its UI and API
integration can consume the implemented rule/usage interfaces; M007 remains
blocked on M006 closure and physical target qualification.

## 12. Registry updates

The registry and roadmap mark M005 conditionally closed, promote M006 to ready,
and retain M007 as blocked on M006 plus operational hardware evidence.

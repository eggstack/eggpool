# On-Prem User Access M009 — Global-instance budget ceiling and intersection

Status: implemented

Repository baseline: `c18dcc2051ae4987a2cb95a7cd046b8aff2095ff` on `plans/on-prem-small-team-user-access` (PR #7), schema 60, M001–M007 closure records as of 2026-10-13.

Source roadmap:
- `plans/subsystems/on-prem-user-access-roadmap.md#m009--instance-global-budget-and-scope-intersection-corrective`

Long-term references:
- `plans/000-long-term-specification.md#1-product-definition`
- `plans/000-long-term-specification.md#2-end-state-invariants-normative`
- `plans/001-terminology-and-domain-model.md#identity-and-lifecycle-terms`
- `plans/002-long-term-roadmap.md#phase-5--optional-on-prem-small-team-access-and-budgeting`

Applicable ADR:
- `plans/adrs/ADR-0002-on-prem-user-identity-and-policy-boundaries.md` (accepted)

Primary class: capability

## 1. Objective

Global-instance budget ceiling and intersection. This is a **new corrective** to the conditionally closed M005/M006/M007 records, not a rewrite of historical closure.

## 2. Readiness and dependencies

**Hard:** accepted M008 closure. M005's durable accounting is present,
but global-instance scope must consume the final M008 enforcement-mode and
conservative reservation contract before implementation. No physical
hardware prerequisite; SQL contention qualifications are local first.

## 3. Evidence and why prior tests missed it

`plans/closure/on-prem-user-access/005-status.md` explicitly reports no
global-instance budget scope. Current
`rust/src/user_budget.rs::rules_exceeded` queries
`user_budget_rules WHERE user_id=?` and aggregates a user's usage
and active reservations. Those last-slot tests exercise **one user**, so
they do not prove a shared ceiling across two separately authenticated
accounts. Neither the prior M005 schema nor the M006 dashboard UI owns
an all-users hard-cap bound.

## 4. Non-regressing invariants

- Canonical use is **one administrator on one local/SBC instance**, optional <50 named users. No SaaS, organization tenancy, billing platform, centralized IAM, new database writer/service, built-in WAN/VPN/reverse proxy, or mandatory cloud calls.
- Upstream provider topology, API keys, routing and self-hosted model endpoints remain in TOML/environment; SQLite owns users, personal credentials, profiles, policy, downstream budgets, accounting and audit. Do not place upstream keys in user tables or web administration.
- Maintain generation leases, existing coordinator publication/finalization, a **single SQLite connection/gate/worker with WAL/NORMAL**, canonical wire and transparent streaming, bounded hot-path memory and provider fairness. Do not turn downstream user budget refusal into upstream provider unhealthiness.
- Preserve published OpenAI Chat/Responses/compact and Anthropic Messages behavior, permitted client auth headers, existing client profiles, dashboard themes and local legacy upgrade behavior.
- Keep identity and cost metadata redacted, prohibit secret/password/bearer leakage in logs, HTML snapshots or qualification artifacts, and fail closed rather than silently weaken configured security limits.

## 5. Required production changes

### A — One instance-owned budget namespace

Append a checksum-pinned migration (>60) representing exactly one
**instance-wide** daily/monthly request, token, cost and active-concurrency
rule scope, plus optional exact model keys. Do not introduce an
organization/tenant identifier or second database. Define intersection:
an admitted call must satisfy all applicable instance-global, instance-
model, user-global and user-model rules. Retain M008 `hard` vs
`estimated` semantics per dimension. Absent instance rules mean no
additional cap. Reject malformed/overflowed rules. Ownership stays with
the local admin; ordinary member/API tokens never mutate instance rules.

### B — Atomic combined reservation

Check usage **plus active holds** for every matching instance/user/model
window and insert the hold under the same SQLite transaction that
publishes the logical request/provider reservation. Concurrent requests
from different users must not both take a final global slot. Count each
logical request once even if provider failover, internal selector attempt
and stream retry occur; charge attempt costs/tokens correctly per M008.
Global held and settled counters cannot depend on a user still existing
in the users table; disabling/deleting user must not erase historical
spend. Use bounded indexed aggregates or a dedicated aggregate rollup,
with no full request-history scans under the writer gate.

### C — Admin management and observable state

Add instance-rule CRUD to local `users budget` CLI (or similarly scoped
existing admin command), `/api/user-budget` admin projection and
server-rendered `dashboard/admin.rs`; member views may show only their
effective cap without disclosing others' usage. Honor existing CSRF,
admin session requirements, profile revision / cached policy invalidation,
and same-window concurrent update semantics. The UI must identify
instance-global vs per-user and hard vs estimated rules without treating
estimates as billed dollars.

## 6. Non-goals

No multi-organization budgeting, external billing, cloud synchronization,
new per-provider credential quota rules, dynamic model substitution after
quota refusal, or second SQLite pool/writer.

## 7. Ordered work packages

1. Declare global rule schema, intersection and exact window semantics;
   extend `rust/assets/db/migrations/`, checksums, runtime manifest.
2. Implement transactional all-scope reservation + settlement/read model
   including retry, selector and user-removal invariants.
3. Add local admin API/CLI/UI forms and model/instance rule conflict tests.
4. Benchmark DB gate tenure for 1/10/49 stored **identities** with
   controlled concurrency on normal host; do not assert SBC performance.

## 8. Failure, cancellation, restart and contention

Budget-edit commit changes admission for **new** requests only; existing
authoritative holds retain source policy version. If global aggregation
fails, reject requests under a hard rule rather than fall back to
user-only enforcement. On cancellation, terminalization and restart,
recovery must release the same global hold exactly once; M010 supplies
the dedicated fault corpus. Guard races at day/month boundaries, including
requests active across UTC reset; active concurrency counts across windows.

## 9. Compatibility and migration

Existing M005 user rules stay intact and continue to apply by
intersection. Absence of global rules must leave a single-admin workload
identical except measured bounded overhead. Do not rewrite provider
`[routing]` or `[providers]`. Migration has schema version >60
and tests automatic upgrade from 60 and restored older fixtures. A
pre-migration binary requires a matching pre-upgrade DB backup to downgrade.

## 10. Tests

Extend `rust/tests/on_prem_budgets.rs`, `on_prem_admin_ui.rs`,
`database_compatibility.rs`, `coordinator_publication.rs`,
`coordinator_finalization.rs` and `server_transport.rs`.
Include 2/10/49 distinct users simultaneously consuming the last
global request/concurrency slot, two models sharing a global budget,
selector/retry without double count, finalization failure,
day/month rollover with active holds, disable/revoke/delete owner,
global underflow/overflow, 403 cross-user mutation, and no-config
single-admin parity. All DB operations must use the one gate.
```bash
cargo test --manifest-path rust/Cargo.toml --test on_prem_budgets -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test on_prem_admin_ui -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test database_compatibility -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_publication -- --test-threads=1
```
## 11. Required verification commands

Run applicable focused targets in §10, then the full default/no-default gates:
```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
git diff --check
```
Run `cargo deny --manifest-path rust/Cargo.toml check`, the feature tree,
and `cargo build --manifest-path rust/Cargo.toml --locked --release` when
Cargo dependencies/features change. Use the repository's tooling/test commands
when scripts are changed. Closure records must distinguish local vs hosted
vs physical hardware results; never claim unrun tests as passed.
## 12. Documentation

Update `architecture/deep-dive-database.md`,
`architecture/deep-dive-request-lifecycle.md`, `docs/model-limits.md`,
`docs/api-reference.md`, `docs/cli-reference.md` and UI help
text. Document specific ceilings, precedence and provider-side quotas
as unrelated quantities.

## 13. Acceptance criteria

With two disjoint authenticated users and profiles, an instance-wide
last slot admits **exactly one** logical request; no retry, alternate model
or user-switch bypass exists. All hard request/concurrency limits remain
strict, hard token/cost rules honor M008's enforceability contract,
and member-only browser/API credentials cannot edit global rules.
One-admin behavior and wire formats remain compatible.

## 14. Stop conditions

Stop if an all-users rule requires repeated full-scans on the foreground
hot path, introduces a second DB writer, makes the global ceiling
dependent on a user being present, or bypasses M008 proven-versus-estimated
typing. Keep scope within instance budget ownership.

## 15. Closure evidence

Write `plans/closure/on-prem-user-access/009-status.md` with
schema/upgrade evidence, concurrent distinct-user last-slot and rollover
tests, DB gate contention and memory observations, authorization/CSRF
matrix, exact commands and real CI, and unblock audit. Historical M005
closure remains immutable.

## 16. Handoff

Implement only after M008 contract is accepted. M010 owns the
crash/restore campaign; M011 owns visual dashboard parity.

# On-Prem User Access M010 — Budget crash recovery and schema-54 restore

Status: ready

Repository baseline: `c18dcc2051ae4987a2cb95a7cd046b8aff2095ff` on `plans/on-prem-small-team-user-access` (PR #7), schema 60, M001–M007 closure records as of 2026-10-13.

Source roadmap:
- `plans/subsystems/on-prem-user-access-roadmap.md#m010--durable-crash-recovery-and-schema-54-restore-corrective`

Long-term references:
- `plans/000-long-term-specification.md#1-product-definition`
- `plans/000-long-term-specification.md#2-end-state-invariants-normative`
- `plans/001-terminology-and-domain-model.md#identity-and-lifecycle-terms`
- `plans/002-long-term-roadmap.md#phase-5--optional-on-prem-small-team-access-and-budgeting`

Applicable ADR:
- `plans/adrs/ADR-0002-on-prem-user-identity-and-policy-boundaries.md` (accepted)

Primary class: invariant

## 1. Objective

Budget crash recovery and schema-54 restore. This is a **new corrective** to the conditionally closed M005/M006/M007 records, not a rewrite of historical closure.

## 2. Readiness and dependencies

**Hard:** M008 and M009 accepted closure. Their new hold semantics and
instance-global scope must be frozen before fault injection.
**Interface:** existing `operations::backup`/`recover`,
`db::MigrationRunner` and coordinator reconciliation contracts are stable.
Runs on disposable local databases; does not require physical SBC hardware.

## 3. Evidence and why prior tests missed it

`plans/closure/on-prem-user-access/005-status.md` reports missing
budget-specific process-crash/restore testing and DB gate overhead.
`plans/closure/on-prem-user-access/007-status.md` reports the real
schema-54 snapshot restore was not exercised; migrating a synthetic
older fixture through v60 is **not** the same thing. Current
`rust/src/user_budget.rs` and `coordinator/publication.rs` add
budget holds in publication, with settlement in
`coordinator/finalization.rs`; `coordinator/reconciliation.rs`
handles interrupted legacy reservations. The source M007 acceptance
requirements also require authenticated restore/upgrade behavior.
`rust/src/db/migrations.rs` enforces checksum identities.

## 4. Non-regressing invariants

- Canonical use is **one administrator on one local/SBC instance**, optional <50 named users. No SaaS, organization tenancy, billing platform, centralized IAM, new database writer/service, built-in WAN/VPN/reverse proxy, or mandatory cloud calls.
- Upstream provider topology, API keys, routing and self-hosted model endpoints remain in TOML/environment; SQLite owns users, personal credentials, profiles, policy, downstream budgets, accounting and audit. Do not place upstream keys in user tables or web administration.
- Maintain generation leases, existing coordinator publication/finalization, a **single SQLite connection/gate/worker with WAL/NORMAL**, canonical wire and transparent streaming, bounded hot-path memory and provider fairness. Do not turn downstream user budget refusal into upstream provider unhealthiness.
- Preserve published OpenAI Chat/Responses/compact and Anthropic Messages behavior, permitted client auth headers, existing client profiles, dashboard themes and local legacy upgrade behavior.
- Keep identity and cost metadata redacted, prohibit secret/password/bearer leakage in logs, HTML snapshots or qualification artifacts, and fail closed rather than silently weaken configured security limits.

## 5. Required correctness/evidence work

### A — Crash-state transition audit

Inventory the precise authority/transaction invariants across:
credential/policy resolution; selector pre-work; initial
budget/provider publication; retry hold extension; upstream acceptance;
attempt finalization; terminal response; reconciliation; backup; and
normal/forced shutdown. Record an explicit matrix for committed,
ambiguous, and rolled-back transactions. Ensure no dispatched provider
attempt can lack its durable user/global hold and no terminal request
retains a phantom active hold. Use actual branch code to decide whether
existing `reconciliation.rs` needs an additive budget recovery step.
Do not modify historical migration data or alter core
`reconciliation.rs` behavior without narrowly proved gap.

### B — Deterministic fault injection

Extend `rust/tests/on_prem_budgets.rs`,
`coordinator_publication.rs`, `coordinator_finalization.rs`
and new scoped process/test harness to pause at real observable
transaction/transport boundaries and terminate/restart the process
(or safely simulate abrupt failure against an isolated database).
Include crash **before/after** publication commit, selector child,
after upstream acceptance, mid-stream, before/after finalizer commit,
duplicate terminal command, cancel, backup during active reservation
and DB read-only/busy/commit ambiguity. On restart verify bounded
convergence, idempotent adjustment, no double cost, no leaked global
hold, and no bypass of next admitted request. Assertions must
converge on observable rows/awaited channels under bounded timeouts,
not fixed sleeps or arbitrary yield loops.

### C — Historical schema-54 backup and migration

Capture or construct a byte-valid **schema-54** backup independently of
current migration application, with audited legacy upstream-account,
request and token-usage history and appropriate SQLite WAL checkpoint.
Prove source identity/checksum/integrity before applying migration
55+; run current `eggpool migrate` against a disposable restore,
`users bootstrap --password-stdin`, login, personal token revoke,
profile editing, hard/estimated/global rule migration, forced restart,
backup/restore of v61+ state, and compare request ownership/account
quota/usage/audit. Legacy shared-key behavior before activation must
be distinct from fresh-init; never convert historical shared-key
inference to a fabricated human identity. Old binary rollback is via
protected matching old backup, not a destructive down-migration.

### D — Quantify storage pressure

Measure transaction/wait times and write counts for 1/10/49
distinct credential identities, with/without budget rules on
ordinary local hardware; track WAL bytes, checkpoint behavior,
pending/finalized count, RSS, CPU and latency. Record paired baseline
configuration/build and fail if new global rollups unacceptably widen
SQLite gate tenure. This is not yet physical SBC capacity evidence:
M012 owns that proof.

### E — Resolve M003 updater-unit-test condition accurately

Reconcile `plans/closure/on-prem-user-access/003-status.md`'s two
local `operations::update` failures with current hosted CI at the
exact branch SHA. Attempt deterministic local isolated reproduction
(with home/ownership fixtures) and classify as existing platform
regression versus stale historical failure; if still reproducible,
open a **separate bounded deployment-packaging corrective** rather than
changing updater authority in this budget pass. Do not erase or edit
M003 closure history.

## 6. Out of scope

No storage redesign, projection second writer, DB pool, new WAL policy,
cloud persistence, production hardware qualification, UI screenshot
work, or rewriting historical closed records.

## 7. Ordered work packages

1. Baseline current source/closure, define exact identity/budget
   transaction state machine and failpoints.
2. Add reproducible crash/cancel/finalizer restart tests; fix only
   demonstrated budget/reconciliation defects and add regressions.
3. Exercise independent schema-54 restore and forward upgrade +
   backup/restore of current state with sanitizer/manifest assertions.
4. Run paired ordinary-host SQLite/latency observations; report
   update-test condition and either close evidence or register a
   distinct deployment-owned follow-up.

## 8. Failure, cancellation, restart and contention

Committing a durable reservation and request must remain atomic.
After crash, recovery must preserve conservatively committed costs,
not forgive paid attempts when response usage is unknown; unknown
usage must remain marked as such. Holds may be compensated only when
the upstream submission state supports doing so without false credit.
Run concurrency tests under the same SQLite gate; never attempt
reconciliation with an independent writer or asynchronous worker pool.

## 9. Compatibility

Existing schema 54/55/60 fixtures and all legacy data survive the upgrade.
Verify `--no-default-features` and generated checksums/manifest.
No provider TOML changes or network dependency. Forward-only migration
is explicit; never promise a newer DB can be opened by an older binary.

## 10. Test and focused verification commands

```bash
cargo test --manifest-path rust/Cargo.toml --test on_prem_budgets -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_publication -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_finalization -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_c008 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_c009 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test database_compatibility -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test runtime_lifecycle_r009 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --lib operations::backup -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --lib operations::update -- --test-threads=1
```
Add separate documented test commands for the isolated child-process,
schema-54 image and paired performance fixture; preserve captured
SHA-256 and redacted artifact manifest.
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
`architecture/deep-dive-request-lifecycle.md`,
`docs/backup-restore.md`, `docs/deployment.md`,
`docs/cli-reference.md` and any test-owned qualification guide.
Use a reproducible artifact path under `artifacts/qualification/`
without sensitive home paths or credentials.

## 13. Acceptance criteria

Each supported failpoint converges after restart to the same
durable logical request owner, correct user/global holds and
idempotent finalization; no new request can over-admit and no
nonterminal orphan holds remain without documented bounded recovery.
An independent v54 fixture upgrades to current schema and recovers
permissions, usage, revocation and existing provider-account history.
Ordinary-host gate measurements are recorded, including worst-case
outliers. Updater regression receives an evidence-based disposition,
not a silent waiver.

## 14. Stop conditions

Stop if a committed paid attempt would be credited back without
authoritative evidence; crash matrix reveals unbounded orphan rows;
a historical schema-54 migration or checksum is inconsistent;
test runner touches live data; or DB architecture must change.
Write a separate architecture/owner plan for any unbounded fix.

## 15. Closure evidence required

`plans/closure/on-prem-user-access/010-status.md`: state graph and
failpoint matrix with actual results; v54 fixture identity/restore/
upgrade checksums; DB overhead samples; security redaction;
exact CI/local commands and revisions; updater condition disposition;
severity findings, registry unblock audit. Never rewrite previous
conditional M003/M005/M007 closures.

## 16. Handoff

Bounded persistence and recovery owner. The physical target
qualification remains M012.

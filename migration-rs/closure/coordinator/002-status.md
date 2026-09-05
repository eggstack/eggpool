# C002 Closure — Durable Dispatch Publication and Lifecycle Identity

Status: closed

Recommendation: closed; C003 is dependency-ready.

Implementation commit: [`8caae259`](https://github.com/eggstack/eggpool/commit/8caae259ca6b4d45c0a2594a499ed6f2ea6762fd)

Plan: [C002 — durable dispatch publication and lifecycle identity](../../implementation/coordinator/002-durable-dispatch-publication-and-lifecycle-identity.md)

Repository baseline: `d7c878d8`

## Outcome

C002 implements the Rust durable publication boundary after an M5
`SelectionClaim`. `PublicationService` owns one `BEGIN IMMEDIATE` transaction
that creates or observes the canonical `requests` row, creates the attempt and
active reservation rows, and persists the frozen M5 routing trace in the same
commit. A later attempt reuses the existing pending request, while a repeated
attempt number observes the existing durable identity instead of creating row
fan-out. No provider HTTP, wire negotiation, retry, or finalization dispatch
was added.

The result carries an immutable `FinalizationIdentity`, an explicit
`RuntimePublicationReceipt`, and the still-owned converted M5 claim. Pre-commit
failures synchronously roll back only local provisional ownership. A
post-commit interruption retains the durable identity and claim in
`PostCommitInterruption`; `compensate_post_commit` terminalizes the attempt,
releases the reservation, and releases local ownership idempotently. The
publication waiter runs behind a retained worker so cancellation of the
caller cannot strand a claim or committed rows; if the result receiver is
dropped, the worker compensates the committed publication.

## Requirement-to-evidence matrix

| C002 requirement | Evidence | Result |
|---|---|---|
| Typed lifecycle identities and receipt | `rust/src/coordinator/publication.rs`: `FinalizationIdentity`, `RuntimePublicationReceipt`, `PublishedAttempt`, and `PostCommitInterruption` | Pass |
| One explicit durable boundary | `PublicationService::publish_transaction` writes request, reservation, attempt, and routing decision through one `Database::with_transaction` call | Pass |
| Existing schema only | SQL targets canonical schema-54 columns; no migration or Rust-only table was added | Pass |
| Request/attempt/reservation/trace consistency | `rust/tests/coordinator_publication.rs::publication_commits_all_rows_and_converts_the_claim_once` checks all four rows and identity fields | Pass |
| Retry request reuse | `later_attempt_reuses_the_pending_request_without_creating_a_parent` proves attempt 2 shares one parent request | Pass |
| Duplicate publication semantics | `duplicate_attempt_observes_existing_identity_without_row_fanout` proves the existing identity is observed and the incoming claim is rolled back | Pass |
| Pre-commit rollback | `every_precommit_failure_rolls_back_the_complete_publication` faults validation, each write boundary, and the pre-commit boundary; rows and M5 ownership return to zero | Pass |
| Post-commit retained compensation | `postcommit_interruption_retains_identity_and_compensates_idempotently` proves committed rows are retained, then attempt/reservation/local ownership converge | Pass |
| Cancellation safety | `cancelling_the_waiter_cannot_strand_a_claim_or_durable_rows` pauses at the transaction boundary, cancels the waiter, and verifies no local leak and either no rows or released reservation | Pass |
| No provider dispatch | The C002 module imports only DB and M5 routing claim types; the focused and full Rust tests use no provider client or network | Pass |

## Python/Rust compatibility evidence

The implementation uses the schema already qualified by F004 and C001. The
existing Rust database compatibility suite verifies Rust-created request rows
are readable by Python after the database is closed, while the C002 suite
verifies the additional attempt, reservation, and routing-decision rows with
the same canonical column names. The Python migration oracle remains green;
no schema checksum, migration ledger, or repository compatibility behavior
changed.

## Verification commands actually run

```text
rtk cargo fmt
rtk cargo clippy --all-targets -- -D warnings  # no issues found
rtk cargo test --all-targets  # 187 passed (21 suites)
rtk uv run pytest tests/migration_rs -q --tb=short --maxfail=1  # 83 passed, 3 skipped
rtk git diff --check  # passed
```

The first strict Clippy run also found one unrelated pre-existing unused
import in `rust/tests/wire_qualification.rs`; removing that dead import was
included in the implementation commit so the declared quality gate is clean.

No Cargo dependency, database migration, provider credential, external
network call, or production Python path changed. The publication fault
injector and deterministic transaction barrier are inert unless explicitly
provided by a caller/test and retain no request content or secrets.

## Ownership, security, and resource review

- The M5 claim remains the sole owner of pending load, active count, quota
  probe, and health-probe conversion/release; C002 never reconstructs those
  counters from database rows.
- The durable receipt contains IDs, account/provider/model identity, protocols,
  and bounded routing metadata only. Credentials, authorization values,
  request bodies, provider bodies, and error detail are not persisted.
- Transaction rollback is delegated to the canonical serialized SQLite
  boundary. Duplicate observation is guarded by the canonical unique
  `proxy_request_id` index and exact identity checks.
- C002 does not own provider transport, dynamic wire state, response handoff,
  retries, terminal finalization, restart reconciliation, or M8 generation
  lifecycle.

## Future-plan audit and registry transition

C002 is removed from the registry's dependency-ready table and added to its
completed table with implementation commit `8caae259` and this accepted
closure record. The coordinator README, handoff sequence, C002 plan header,
and M7 roadmap now record C002 as closed. C003 is promoted to the sole
dependency-ready implementation plan because its hard dependency is accepted.
C004-C011 remain queued behind their named serial predecessors. M8 runtime
generation/background lifecycle remains blocked on accepted C011 M7 closure
and its separate planning review.

Unresolved mandatory findings: none.

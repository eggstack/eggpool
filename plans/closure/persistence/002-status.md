# Persistence Milestone 002 — Closure Status

Status: closed

Source implementation plan:

- `plans/implementation/persistence/002-single-gate-tenure-and-metrics-flush-allocation-cleanup.md`

Source subsystem roadmap:

- `plans/subsystems/persistence-roadmap.md#milestone-002--single-gate-tenure-and-metrics-flush-allocation-cleanup`

Repository baseline reviewed: `52494140`

Implementation commits or pull requests:

- `52494140` — Implement persistence M002 single-gate tenure and metrics-flush cleanup

## 1. Executive finding

M002 is complete and **closed**. Deterministic routing-decision preparation
now happens before the single database gate is acquired, metric key Strings
are moved instead of cloned, flush batches are consumed into one shared
immutable representation, and the metrics UPSERT is prepared once per
transaction. Durable rows, fault/retry semantics, capacity/drop accounting,
and rebuffer-on-failure behavior are proven equivalent by new
persistence-shape tests; every acceptance criterion is host-verifiable, so
no conditional evidence remains.

## 2. Requirement-to-evidence matrix

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| Private prepared routing-decision row before the gate | `publication.rs::PreparedRoutingDecisionRow::prepare` (borrowed snapshot → exclusions/score JSON, score scalars, counts, TTL modifier) | pass | Only minimal owned facts enter the `'static` worker closure |
| No full `SelectionSnapshot` clone for the transaction | Source inspection: zero `snapshot.clone()` in `publication.rs`; serde calls exist only in `prepare` (pre-gate) | pass | `top_account_name: Option<String>` clone is part of the minimal row, not the snapshot |
| Serialization error category preserved | `map_precompute_failure` → `PublicationError::Database` (existing category, no new variant); failure rolls back the claim without starting a transaction | pass | Same retry/compensation path as in-transaction failure |
| SQLite-dependent checks stay inside the transaction | Request/duplicate/prior-attempt/account checks, inserts, `last_insert_rowid`, and all `fail_sql` stages untouched in order | pass | Fault-injection suite green |
| Metric key ownership moved | `record_buffered` folds one additive `Aggregate` delta, then destructures/moves key Strings into the map key | pass | `entry().or_default().merge(delta)` proven equivalent to `add` (field-by-field, incl. min/max/first-byte) |
| Flush batch consumed, one shared representation | `batch.into_iter()` → `Arc<[MetricFlushRow]>` shared with the worker closure and retained for recovery | pass | No second deep batch clone; `git`-visible structural removal |
| UPSERT prepared once per transaction | `METRICS_UPSERT_SQL` const + `connection.prepare` once, `statement.execute` per row | pass | Identical SQL text and bind order; no dynamic SQL |
| Rebuffer/capacity/drop counters exact | Forced-failure (closed DB) test: exact restore, idempotent retry, concurrent-event convergence, zero drops | pass | New `operations_o007` tests |
| Immediate/low-wear semantics unchanged | Immediate-mode test through the same boundary; existing coalesce test green | pass | `write_mode` branching untouched |
| One gate/worker, schema, config, APIs unchanged | No gate/schema/config/API/dependency change; `git diff --name-only` has no `Cargo.toml`/`Cargo.lock`/migration/config | pass | `cargo deny` not required |

## 3. Production implementation evidence

Landed ownership (all in `52494140`):

- `rust/src/coordinator/publication.rs`: `PreparedRoutingDecisionRow` +
  `map_precompute_failure`; `publish_owned` prepares before the gate with
  claim rollback on precompute failure; `publish_transaction` takes the
  owned prepared row (no snapshot clone); `AttemptRows` carries
  `prepared: &PreparedRoutingDecisionRow`; reservation/insert bodies read
  only prepared facts with identical SQL and fault-stage order.
- `rust/src/operations/metrics.rs`: `MetricFlushRow` + `METRICS_UPSERT_SQL`;
  move-based `record_buffered` (cap-path probe clones only to test key
  presence at the row cap); consuming `flush` with one `prepare` per
  transaction; failure path re-keys from the retained `Arc` batch and
  merges under the same caps.
- `rust/tests/coordinator_publication.rs`:
  `prepared_routing_row_persists_multi_exclusion_and_score_facts` (3-account
  fixture: eligible + `disabled` + `no_model`; every scalar column, parsed
  exclusion reasons, byte-identical score components).
- `rust/tests/operations_o007.rs`:
  `metrics_flush_writes_many_rows_additively_in_one_transaction` (2 keys,
  3 events; full additive/min/max/first-byte/bytes column proof),
  `metrics_failed_flush_rebuffers_exactly_and_keeps_concurrent_events`
  (exact restore, idempotent retry, concurrent convergence),
  `metrics_immediate_mode_flushes_through_the_same_boundary`.

Structural before/after inventory:

- Publication before: `claim.selection_snapshot().clone()` →
  `snapshot.clone()` into the closure → `serde_json::{json,to_string}`
  plus `format!(TTL)` inside `insert_attempt_rows` while the gate was held.
  After: `prepare` (borrowed) before the gate; closure owns only the
  prepared row; zero `snapshot.clone()`; serde confined to `prepare`.
- Metrics enqueue before: 5 key-String clones per event (`bucket_start`,
  `provider_id`, `model_id`, `protocol`, `status`). After: one delta fold
  plus moves; clones remain only on the rare at-cap probe path.
- Metrics flush before: full row `Vec` built by cloning every key String,
  then a second deep `rows.clone()` for recovery; `connection.execute`
  (prepare per row) inside the loop. After: single consumed `Arc` batch,
  one `prepare`, per-row `execute`; clones remain only on the failure
  re-key path.

## 4. Verification executed

### Commands run

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --test coordinator_publication -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_finalization -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test database_compatibility -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test operations_o007 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- --test-threads=1
cargo build --manifest-path rust/Cargo.toml --locked --release
git diff --check
```

### Results

All local (darwin development host; CI truthfully not run):

- Focused: `coordinator_publication` 7 passed (incl. new multi-exclusion
  equivalence); `coordinator_finalization` 10 passed;
  `database_compatibility` 7 passed; `operations_o007` 7 passed (incl.
  3 new metrics tests).
- Full default workspace suite: 792 passed, 0 failed (serial).
- Full no-default workspace suite: 793 passed, 0 failed (serial);
  no-default check + Clippy clean.
- Strict Clippy (default): clean after narrowing test helpers
  (`too_many_arguments` allow on the test event builder; tuple factored
  into a `type` alias).
- Locked release build: ok. `git diff --check`: clean. No `Cargo.toml` /
  `Cargo.lock` change, so the dependency audit matrix was not required.

## 5. Invariant review

- One SQLite connection/gate/worker: unchanged; preparation happens before
  acquisition, transactions are still one per publication/flush.
- Publication atomicity (request/reservation/attempt/routing-decision):
  unchanged SQL in unchanged order; row-count and duplicate/retry suites
  green.
- Fault-injection stages reachable in the same logical order: every
  `fail_sql` site preserved; `every_precommit_failure` suite green.
- Duplicate/prior-attempt/identity checks: untouched; duplicate and
  later-attempt suites green.
- Finalization not delayed/reordered by metrics: metrics path unchanged in
  scheduling; finalization suite green.
- Metrics capacity/drop/additive/rebuffer/immediate/low_wear/BTreeMap
  ordering: proven equivalent by the new tests; existing coalesce test
  green.
- No schema/config/API/dependency change: verified via diff inventory.
- No secret/body/cache-key/diagnostic leakage: preparation moves only the
  already-persisted routing facts; metrics still scalar-only.

## 6. Failure and recovery review

- Publication precompute failure: no transaction starts, no claim converts;
  existing `Database` error category with claim rollback, so caller
  retry/compensation is unchanged. (Serialization is infallible in
  practice; the path is defensive.)
- Post-gate publication failures: unchanged retained-worker + compensation
  boundary; cancellation/compensation suites green.
- Metrics flush failure: single retained `Arc` batch merged back under the
  same caps; concurrent arrivals during the flush converge exactly (test
  asserts all interleavings land on the same totals); retry is idempotent
  (no doubling); drop accounting only under the existing cap rules.
- Restart: buffered analytics remain process memory as before;
  durability-critical state never enters the coalescer.

## 7. Migration and compatibility review

No migration, no config/CLI/HTTP/Rust API change. Persisted JSON ordering
remains deterministic (`preserve_order` serde); the new test asserts
byte-identical score components and semantic exclusion equality. Exact
textual JSON compatibility is preserved, not just semantic equivalence.

## 8. Security review

No auth/secret/privilege surface touched. No new diagnostics. No DoS bound
change: preparation is bounded by the existing snapshot sizes; flush batch
is bounded by the existing row/event caps; statement preparation count went
from per-row to per-flush (strictly less work while the gate is held).

## 9. Documentation and operations

- `architecture/deep-dive-database.md`: pre-gate preparation note.
- `architecture/deep-dive-metrics.md`: move-based ownership + one-prepare
  note.
- Legacy Plans 230–242 untouched. No operator surface changed, so no
  deployment/runbook update was needed.

## 10. Unresolved findings

| Severity | Finding | Impact | Required action |
|---|---|---|---|
| low | At-cap probe in `record_buffered` still clones key Strings to test key presence | Bounded extra clones only on the rare row-cap path; common path moves | None; documented as intentional |
| low | Failure rebuffer re-keys by cloning from the retained batch | One clone per row on the failure path only; no upfront double batch | None; documented as intentional |
| low | Prepared row clones small bounded facts (`Option<String>`, TTL modifier) into the `'static` closure | Required by the worker boundary; orders of magnitude smaller than the removed snapshot clone | None |

No medium-or-higher finding. No corrective pass required.

## 11. Roadmap disposition

Persistence M002 is **closed**. No follow-up milestone is unblocked or
required by this closure: M001 stays conditionally closed on its physical
evidence condition, M003 stays not started behind that same condition, and
no other subsystem depends on this cleanup.

## 12. Registry updates

`plans/registry.md` + `plans/subsystems/persistence-roadmap.md` updated in
the same commit: M002 `ready` → `closed` with this record; roadmap table
and current-milestone notes reflect M001 conditionally closed + M002
closed.

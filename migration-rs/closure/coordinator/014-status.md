# C014 Closure — Finalization Idempotency and Retry-After Closure

Status: closed

Implementation commit: [`7607237d533e5e3f6ae33d2ecd504acff5732959`](https://github.com/eggstack/eggpool/commit/7607237d533e5e3f6ae33d2ecd504acff5732959)

Plan: [C014 — finalization idempotency and Retry-After closure](../../implementation/coordinator/014-finalization-idempotency-and-retry-after-closure.md)

Repository baseline: `4fffd3bb615d47b52043a75a62abd095fd113f06`

## Outcome

C014 closes the four residual coordinator-core findings identified after C013.
Finalization progress now reports completion for durable-only compatible
observations, while a supplied claim remains an explicit runtime-cleanup
obligation. Retained finalization compatibility includes the complete durable
identity plus every authoritative terminal/accounting fact persisted by
`finalize_durable`. Retry-After values are bounded at both parsing and failure
classification boundaries for numeric and HTTP-date forms. Failed-attempt
finalization validates the immutable attempt and reservation identity without
requiring the mutable parent request account/provider to remain unchanged.

The retry publication path also preserves strict same-attempt duplicate
identity checks while allowing a new pending retry to publish a replacement
account/provider selection. No schema, dependency, HTTP-stack, scheduler, or
M8 lifecycle change was introduced.

## Requirement-to-evidence matrix

| C014 requirement | Evidence | Result |
|---|---|---|
| Durable-only duplicate completion | `request_finalization_converges_rows_and_runtime_once` asserts first completion with a claim and a duplicate with `claim=None` has `progress.completed=true` and no runtime cleanup requirement | Pass |
| Failed-attempt duplicate completion | `failed_attempt_cleanup_leaves_request_retryable` repeats the terminal failed attempt without a claim and asserts complete progress while the parent remains pending | Pass |
| Runtime cleanup remains required | First-completion and failed-attempt tests assert claim-backed completion only after quota/active/probe release; `run_command` retains bounded retry ownership for release errors | Pass |
| Retained command compatibility | `finalization_rejects_missing_durable_identity_and_incompatible_jobs` proves compatible coalescing and rejects changes to request scope, outcome, status, error class, release reason, input/output/cost, bytes received/emitted, latency, upstream request ID, proxy request ID, attempt number, and durable identity | Pass |
| `error_detail` compatibility policy | Excluded deliberately: it is sanitized diagnostic text and is not an authoritative terminal fact used to decide durable convergence; this matches the established C006/C012 policy | Pass |
| Retry-After numeric/date parity | `coordinator_c014.rs` covers numeric below/above cap, HTTP-date below/far above cap, equal/earlier dates, invalid date, negative/malformed numeric input, and a custom cap; direct classification input is capped too | Pass |
| Historical retry-attempt idempotency | `historical_retry_finalization_uses_attempt_identity_not_parent_selection` publishes attempt 1 on account/provider A, terminalizes it retryably, publishes attempt 2 on B, re-observes attempt 1 without a claim, verifies attempt 2 remains active and the parent remains selected on B, then completes attempt 2 | Pass |
| Cross-attempt ownership safety | The same regression ends with one completed request, two terminal attempts, two converged reservations, zero active counts, and zero reserved requests on both accounts | Pass |
| Handoff/retry safety and boundedness | Existing C013 no-post-handoff classifier coverage remains green; C014 adds no retained state or new retry budget | Pass |

## Failing-before / passing-after evidence

Before the implementation, the durable-only duplicate path constructed
`completed` from `runtime_released`, so a compatible no-claim observation was
reported incomplete. Retained compatibility omitted bytes, latency, and
upstream request ID (and the remaining authoritative scope fields). HTTP-date
parsing returned the raw 126,230,412-second C001 observation while numeric
parsing was capped. Replaying attempt 1 after attempt 2 changed the parent
selection failed closed on the request account/provider identity.

The initial C014 historical regression reproduced that last failure as an
`Invariant` before the historical-identity change. After implementation, the
focused Rust suites passed 17 tests across C013/C014/finalization, including
the two-account/provider replacement case. The final all-target Rust run
passed 208 tests.

## Retry-After effective-bound evidence

The Python parser observation remains intentionally raw in the committed C001
fixture for differential provenance. Python's operational backoff application
clamps nonterminal suppression to 1,800 seconds. Rust now applies the
configured `RetryPolicy.max_retry_after` cap to numeric and RFC 1123 values in
`parse_retry_after`, and also clamps directly supplied failure observations
before they reach account backoff or wire-negotiation delay. The C014 matrix
proves the default 1,800-second ceiling and a custom 30-second ceiling.

## Verification commands

```text
rtk cargo fmt --all -- --check
rtk cargo clippy --all-targets -- -D warnings
rtk cargo test --all-targets                         # 208 passed
rtk uv run pytest tests/migration_rs -q --tb=short --maxfail=1  # 83 passed, 3 skipped
rtk uv run pytest tests/unit/test_retry_classification.py tests/unit/test_backoff.py tests/unit/test_failure_effects_table.py tests/unit/test_request_finalization_state_machine.py tests/unit/test_request_finalization_supervisor.py tests/unit/test_request_finalizer.py tests/unit/test_finalizer_reservation_regression.py tests/unit/test_accepted_finalization_state_machine.py -q --tb=short --maxfail=1  # 215 passed
rtk uv run ruff check tests/migration_rs
rtk git diff --check
```

No live/paid provider, external network, schema migration, dependency update,
or Python fixture change was required. No existing deterministic fault hook
expresses a partial claim-release failure; no test-only framework was added.
Claim release remains component-idempotent, and retained command retries reuse
the same command after a release error.

## Registry transition and future-plan audit

C014 moves from the dependency-ready table to completed implementation plans
with commit `7607237d533e5e3f6ae33d2ecd504acff5732959` and this accepted
closure record. C007 is promoted as the sole dependency-ready plan because
C014 is its hard dependency. C008 remains queued behind C007, C009 behind
C008, C010 behind C009, and C011 behind C010. No other future plan is
unblocked. M8 remains blocked on accepted C011 M7 closure and its separate
planning review.

Unresolved mandatory findings within C014 scope: none. C014 does not replace
the aggregate C011 M7 closure and does not claim finite-response, streaming,
endpoint, restart-reconciliation, or M8 lifecycle parity.

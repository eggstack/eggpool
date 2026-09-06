# C014 — Finalization Idempotency and Retry-After Closure

Status: ready for handoff

Source roadmap: `migration-rs/subsystems/coordinator-roadmap.md`

Primary class: invariant/corrective

Hard dependencies: accepted C012 and C013 closures.

## Objective

Close the narrow residual coordinator-core findings discovered after C013 before C007 finite-response handoff resumes. C014 is intentionally small: it does not reopen the C003-C006 architecture, add new coordinator capabilities, or pull C007/C008/M8 work forward. It corrects finalization completion/idempotency semantics and makes Retry-After bounding consistent across numeric and HTTP-date forms.

Historical C003-C006, C012, and C013 closure records remain append-only evidence. C014 supersedes their “no unresolved mandatory findings” conclusion only for the findings named below.

## Accepted findings

1. A duplicate/reconciliation finalization with `claim=None` can return `progress.completed = false` even when durable truth is already terminal and no runtime ownership remains.
2. `FinalizationSupervisor` command compatibility omits persisted terminal evidence (`bytes_received`, `bytes_emitted`, `latency_ms`, `upstream_request_id`), allowing incompatible concurrent submissions to share one retained job.
3. Numeric Retry-After values are capped by `RetryPolicy.max_retry_after`, but HTTP-date values are not, allowing an arbitrarily long effective delay despite EggPool's bounded suppression policy.
4. Re-observing/finalizing an earlier already-terminal retry attempt after a later attempt has updated the parent request's current account/provider can fail the parent identity invariant even though the historical attempt/reservation identity is valid and terminal.

If any finding conflicts with the current Python oracle, stop and document the supported difference explicitly rather than weakening the accepted M7 contract to fit Rust behavior.

## 1. Finalization progress semantics

`FinalizationProgress.completed` must mean that every correctness obligation represented by the command has converged, not merely that a runtime `SelectionClaim` was supplied and released during this invocation.

Required behavior:

- when `claim=None`, `runtime_cleanup_required=false` and durable terminal truth is compatible, completion is allowed to be `true` because there is no process-local claim obligation to release;
- when a claim is present, completion becomes true only after quota reservation, active count, and probe ownership have converged according to the claim's actual ownership;
- a durable-only duplicate observation must not manufacture runtime-release work;
- a partial runtime-release failure must remain incomplete and retryable;
- repeated compatible finalization must converge to the same terminal progress without double-release or underflow;
- progress fields must remain internally consistent: `completed` implies durable convergence and every required runtime/effect component is converged.

Add focused tests for first completion with a claim, duplicate completion without a claim, failed-attempt duplicate observation without a claim, and partial runtime-release failure/resume if an existing deterministic fault hook can express it. Do not add a framework solely for this test.

## 2. Retained command compatibility

Two commands for the same `(request_id, attempt_id)` may share retained work only when every immutable fact that can alter durable terminal persistence is compatible.

Extend `CommandCompatibility` (or equivalent) to include at least:

- existing durable identity fields;
- request-vs-failed-attempt terminal scope;
- outcome;
- status code;
- error class;
- release reason;
- input/output tokens;
- cost;
- `bytes_received`;
- `bytes_emitted`;
- `latency_ms`;
- bounded `upstream_request_id`.

If another non-secret `FinalizationData` field is persisted authoritatively by `finalize_durable`, include it as well. `error_detail` may remain excluded if the established policy treats it as sanitized diagnostic text rather than an authoritative compatibility fact; document that decision in the closure record.

Tests must prove that two concurrent compatible commands share one job while changing each authoritative persisted fact causes `IncompatibleCommand` before observing another command's success.

## 3. Retry-After cap parity

Apply the same `RetryPolicy.max_retry_after` bound to both supported Retry-After forms:

- delta-seconds;
- RFC 1123 HTTP-date.

Required cases:

- numeric value below cap;
- numeric value above cap;
- HTTP date below cap;
- HTTP date far above cap;
- date equal to or earlier than the injected current time;
- invalid date;
- malformed/negative numeric value.

The effective value supplied to account backoff and wire-negotiation delay must never exceed the configured policy cap. Keep the current project default ceiling of 1,800 seconds unless the canonical configuration says otherwise. Do not introduce a separate hard-coded date-path cap.

Update the C001/C013 differential fixture expectation only if the Python oracle itself currently returns an unbounded raw HTTP-date duration while the operational suppression contract intentionally clamps later. In that case preserve raw-parser observation separately and add a test at the policy/application boundary proving the effective persisted delay is capped. Prefer semantic parity with the actual Python routing/backoff behavior over parser-shape parity.

## 4. Historical retry-attempt idempotency

A request row represents the current selected account/provider for an in-progress multi-attempt request. An old attempt row represents immutable historical attempt identity. Finalizing or re-observing an already-terminal old attempt after a replacement attempt has been published must validate against the old attempt/reservation rows without requiring the mutable parent request's current `account_id`/`provider_id` to still equal the old attempt.

Preserve these invariants:

- request ID/model/client protocol relationship must still match;
- the historical attempt's request/account/provider/model/upstream protocol must match `FinalizationIdentity`;
- the historical reservation's request/account/model relationship must match that same attempt identity;
- a retryable failed-attempt finalization must leave the parent request pending;
- finalizing attempt N again after attempt N+1 publication is a compatible no-op/observation if N is already terminal and its reservation is converged;
- it must not overwrite the parent request's current account/provider selection;
- it must not release attempt N+1 ownership;
- incompatible historical identity still fails closed.

Add a deterministic two-attempt regression:

1. publish attempt 1;
2. terminalize/release attempt 1 as retryable;
3. publish attempt 2, changing current parent account/provider where the fixture can represent this;
4. re-run attempt-1 finalization with no claim;
5. assert compatible convergence/no parent mutation/no attempt-2 ownership release;
6. complete attempt 2 and assert one terminal request, two terminal attempts, two converged reservations, and zero runtime ownership.

Use two accounts/providers if necessary to make the mutable-parent-vs-historical-attempt distinction observable. Do not loosen identity checks broadly just to make the regression pass.

## Fault, security, and resource review

C014 must additionally assert:

- no selected-claim/quota/active/probe underflow under duplicate historical finalization;
- no retry becomes legal after downstream response start;
- no retained job/effect/resolver state grows because of these changes;
- upstream request IDs remain bounded/redacted and no auth/request body is introduced into compatibility/debug state;
- no schema migration or dependency change is needed.

## Scope boundaries

C014 must not implement:

- C007 finite provider-response adaptation/handoff;
- C008 streaming/timeout/downstream cancellation behavior;
- C009 public inference endpoint wiring;
- C010 restart scanning/scheduling beyond the finalizer semantics required for later reconciliation;
- M8 runtime generation, rehash, shutdown/signal, or recurring background lifecycle.

No new HTTP stack, ORM, task queue, workflow/actor framework, database schema, or broad CI matrix is expected.

## Verification

Required before closure:

```text
cargo fmt --all --check
cargo clippy --all-targets -- -D warnings
cargo test --all-targets
uv run pytest tests/migration_rs -q --tb=short --maxfail=1
uv run pytest <targeted Python retry/finalization oracle tests> -q --tb=short --maxfail=1
uv run ruff check tests/migration_rs
uv run pyright <touched Python fixture/test paths if any>
git diff --check
```

No live/paid provider or external network prerequisite is expected.

## Acceptance criteria

C014 closes only when:

- durable-only duplicate finalization reports complete when no runtime ownership is required;
- required runtime cleanup still prevents premature completion;
- all authoritative persisted terminal facts participate in retained-command compatibility;
- numeric and HTTP-date Retry-After paths obey the same configured maximum effective delay;
- historical attempt finalization remains idempotent after a later retry changes mutable parent selection;
- the two-attempt regression proves no cross-attempt ownership mutation or release;
- full Rust and targeted Python/migration suites pass;
- no schema/dependency/M8 scope expansion is required;
- no new high/medium correctness or security finding remains within this narrow scope.

C014 closure restores C007 readiness directly; no separate requalification plan is required unless implementation exposes a broader architectural defect.

## Closure

On completion write `migration-rs/closure/coordinator/014-status.md` with implementation commit(s), failing-before/passing-after regression evidence, the compatibility-field audit, Retry-After effective-bound evidence, two-attempt idempotency results, full verification commands, and exact registry transition. Only then may C007 return to the dependency-ready table.

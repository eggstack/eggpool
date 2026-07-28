# Upstream Error Isolation and Hot-Path Hardening Roadmap

Date: 2026-07-25
Status: completed — all workstreams implemented and verified, Plan 030 closure evidence at artifacts/plan-030-exact-head-evidence.md

Implementation baseline:

- `0d369d9adf962eb2907a543e2540c863b0eb4d45`

Related completed work:

- `plans/019-accepted-finalization-lifecycle-closure.md`
- `plans/020-accepted-finalization-control-flow-evidence-corrective-pass.md`
- `plans/021-accepted-finalization-terminal-closure-pass.md`

Detailed phase plans:

- `plans/023-error-isolation-reproducer-and-invariant-baseline.md`
- `plans/024-provider-bound-thinking-control-normalization.md`
- `plans/025-failure-effects-and-model-quarantine.md`
- `plans/026-process-owned-request-finalization.md`
- `plans/027-database-recovery-and-transaction-reconciliation.md`
- `plans/028-provider-payload-lifecycle-hotpath-consolidation.md`
- `plans/029-dispatch-writer-and-observability-bounds.md`
- `plans/030-hardening-integration-soak-and-rollout-closure.md`

## Objective

Prevent a request-local upstream validation failure from degrading unrelated proxy traffic, requiring a process restart, or forcing operators to delete the SQLite database. The initial concrete reproducer is MiniMax-M3 routed through OpenCode Go with a client-supplied thinking level that the selected upstream deployment does not accept, while the same model through MiniMax's native endpoint works correctly.

This roadmap also closes the highest-value performance and long-running-process defects found in the same request path. The work must improve failure isolation, cancellation safety, database self-recovery, payload-processing efficiency, diagnostic memory bounds, and measurable dispatch latency without reducing protocol compatibility or provider capability.

## Problem statement

The current tree has substantial hardening already, including typed retry classification, capability-aware routing, retained reload finalization, database invalidation diagnostics, bounded routing-trace pressure, parsed request caching, and fine-grained dispatch spans. Those mechanisms must be preserved.

The remaining failure surface is distributed across several ownership boundaries:

1. Provider/model capability metadata does not fully describe the accepted thinking-control wire shape.
2. Provider-specific thinking adjustment runs primarily as a transcoding concern; native-protocol requests may forward incompatible controls unchanged.
3. Error classification and health effects are inferred in multiple layers instead of being represented by one explicit effects decision.
4. Request finalization remains coupled to request-task lifetime on important non-streaming and capability-rejection paths.
5. SQLite connection invalidation correctly fails closed but has no process-owned replacement and reconciliation controller.
6. A first model-specific failure can become durable, effectively terminal model suppression.
7. Provider-bound payloads and non-stream responses may be decoded and encoded multiple times in one request.
8. Dispatch-writer diagnostic sample lists are unbounded and percentile snapshots become more expensive over process lifetime.
9. Failure-path transactions include avoidable lookups and diagnostic writes that lengthen the single SQLite writer critical section.

## Target architecture

The target request lifecycle is:

```text
client request
  -> parse once
  -> validate client contract
  -> build immutable routing requirement
  -> select provider/account
  -> apply provider-bound request contract
  -> serialize once
  -> dispatch upstream
  -> classify upstream result into explicit failure effects
  -> create process-owned finalization job
  -> reconcile durable and runtime ownership exactly once
  -> render client response
```

The target architecture has five strong boundaries.

### Boundary 1: Client validation versus provider adaptation

Client-invalid requests are rejected before durable dispatch. Provider-specific compatibility adaptation occurs after provider selection and before upstream request construction. A provider adaptation failure is a client-visible compatibility result, not an account-health failure.

### Boundary 2: Failure classification versus shared-state effects

One typed decision determines retry, account cooldown, model quarantine, circuit penalty, durable backoff, credential disablement, and client response handling. Request-local failures default to no shared-state effects.

### Boundary 3: Request task versus correctness ownership

Once durable request/attempt/reservation state exists, cleanup is owned by a retained process task. Client cancellation may stop response delivery, but it cannot cancel or strand finalization.

### Boundary 4: SQLite connection versus process availability

An indeterminate connection is detached and never reused. The process opens a replacement connection, reconciles ambiguous idempotent operations, and restores readiness without requiring a restart or database deletion.

### Boundary 5: Decoded payload versus wire bytes

A request or response is decoded once into a typed lifecycle object, transformed in memory, and encoded once only when mutation is required. Raw bytes remain available for exact passthrough.

## Mandatory global invariants

### Failure isolation

1. A client or provider validation error affects only the request that caused it.
2. Unsupported thinking effort, unsupported optional field, malformed tool schema, context-limit failure, and protocol-shape validation never penalize account health.
3. No request-local validation failure creates account-wide durable backoff.
4. No single runtime 404 creates indefinite model withdrawal.
5. An account/model/provider quarantine key never broadens silently to another provider, account, protocol, or alias.
6. A successful request or authoritative catalog reappearance clears compatible bounded quarantine state.
7. Client cancellation never records an upstream provider failure.

### Ownership and finalization

8. Every selected attempt has exactly one durable reservation owner and exactly one runtime ownership token.
9. Every terminal path invokes idempotent release semantics.
10. Client cancellation, timeout, response-rendering failure, and task teardown cannot cancel retained cleanup.
11. No terminal request leaves a pending reservation, active-request count, quota reservation, or health probe slot.
12. Finalization retry and reconciliation queues are bounded and observable.
13. A failed finalizer cannot cause unrelated requests to inherit the failed request's state.

### Database recovery

14. A connection with indeterminate transaction state is never reused.
15. Database invalidation makes readiness false but does not require process restart.
16. Replacement connection creation is single-flight and bounded.
17. Ambiguous commits are reconciled using durable idempotency keys; they are not blindly replayed.
18. Transaction-body rollback failure follows the same invalidation path as indeterminate commit recovery.
19. Recovery failure is observable and fails closed without corrupting or deleting the database.

### Provider contracts

20. Provider-bound thinking controls are normalized for native and transcoded requests.
21. Capability metadata distinguishes reasoning production from client-controllable effort or budget.
22. An empty effort list cannot mean both “unknown metadata” and “no effort control accepted.”
23. Compatibility retries are allowlisted, pre-body only, bounded to one retry, and never alter health state.
24. Strict semantic-preservation policy never silently drops or changes a client-requested control.

### Performance and long-running behavior

25. Common native request paths decode request JSON once and encode at most once.
26. Common non-stream response paths decode response JSON once and encode only when transcoding or adaptation requires it.
27. Provider-bound transforms do not repeatedly parse serialized bytes.
28. Diagnostic memory use is bounded independently of process uptime.
29. Percentile snapshots have bounded computational cost.
30. Diagnostic instrumentation can be sampled without changing correctness.
31. No optimization weakens protocol behavior, error fidelity, usage accounting, or finalization correctness.

## Roadmap phases

## Phase 1 — Reproducer and invariant baseline

Plan: `023-error-isolation-reproducer-and-invariant-baseline.md`

Build a deterministic mock-upstream reproducer for the OpenCode Go MiniMax-M3 thinking-level failure and characterize every state mutation caused by the request. Add reusable state-audit fixtures, fault-injection seams, parse/encode counters, and baseline latency/resource measurements.

Exit gate: the defect is reproducible without live credentials, and tests can prove which durable and runtime facts changed after each injected failure.

## Phase 2 — Provider-bound thinking-control normalization

Plan: `024-provider-bound-thinking-control-normalization.md`

Extend capability contracts and add a post-selection request-normalization layer that applies to native and transcoded requests. Add explicit OpenCode Go MiniMax-M3 metadata and bounded compatibility behavior.

Exit gate: unsupported controls are locally rejected, explicitly mapped, or dropped only under configured policy; they are never forwarded accidentally and never affect health.

## Phase 3 — Typed failure effects and bounded model quarantine

Plan: `025-failure-effects-and-model-quarantine.md`

Centralize shared-state consequences into one typed `FailureEffects` decision. Replace first-observation terminal model withdrawal with bounded, corroborated quarantine and automatic recovery.

Exit gate: every relevant status/body/error-class combination has one test-pinned effects decision, and request-local errors have zero shared-state effects.

## Phase 4 — Process-owned request finalization

Plan: `026-process-owned-request-finalization.md`

Move correctness cleanup for selected attempts into retained process-owned finalization jobs. Introduce idempotent runtime ownership release, bounded retry/reconciliation, and cancellation-safe completion observation.

Exit gate: exhaustive cancellation and failure injection leaves no durable or runtime leaks, including when every request waiter is cancelled.

## Phase 5 — Database replacement and transaction reconciliation

Plan: `027-database-recovery-and-transaction-reconciliation.md`

Add a process-owned database recovery controller. Detach invalid connections, open replacements, reconcile ambiguous request operations, and restore readiness safely. Cover rollback failure as well as commit indeterminacy.

Exit gate: deterministic commit/rollback fault matrices recover without restart or database deletion, or remain safely unready with precise diagnostics.

## Phase 6 — Provider payload lifecycle and transaction hot-path consolidation

Plan: `028-provider-payload-lifecycle-hotpath-consolidation.md`

Create decoded provider-bound request and response lifecycle objects, remove repeated JSON parsing/encoding, reuse segmentation where safe, and shorten finalization transactions by eliminating avoidable lookups and best-effort work from critical sections.

Exit gate: parse/encode counters and performance tests prove reduced work with byte-for-byte and semantic-equivalence coverage.

## Phase 7 — Dispatch writer and observability bounds

Plan: `029-dispatch-writer-and-observability-bounds.md`

Bound dispatch-writer samples, correct timing semantics, make percentile cost constant with process age, tune batching for latency-sensitive traffic, and sample detailed instrumentation in production.

Exit gate: multi-million-intent synthetic soak has bounded RSS, bounded snapshot latency, accurate metric labels, and no dispatch correctness regression.

## Phase 8 — Integrated hardening, soak, rollout, and closure

Plan: `030-hardening-integration-soak-and-rollout-closure.md`

Combine all phases under realistic concurrency, streaming/non-streaming, provider adaptation, cancellation, database-fault, and long-running workloads. Define compatibility rollout, feature flags, operational runbooks, and exact-head evidence.

Exit gate: all cross-phase invariants pass on Python 3.11 and 3.12, standard and focused CI are green, and exact-head soak evidence demonstrates bounded resource and latency behavior.

## Dependency graph

```text
Phase 1 baseline
  ├─> Phase 2 provider contracts
  ├─> Phase 3 failure effects
  ├─> Phase 4 finalization ownership
  └─> Phase 5 database recovery

Phase 2 + Phase 3 + Phase 4 + Phase 5
  └─> Phase 6 payload and transaction optimization

Phase 1 + Phase 6
  └─> Phase 7 writer and observability bounds

Phases 2–7
  └─> Phase 8 integration and closure
```

Phase 6 must not land before Phase 1 counters and equivalence harnesses exist. Phase 8 must not compensate for missing phase-level acceptance criteria by weakening tests or masking failures.

## Compatibility and migration policy

The implementation must preserve existing configuration behavior unless a phase plan explicitly introduces an additive field. New configuration must use safe defaults and `extra="forbid"` validation consistent with the current Pydantic models.

Provider capability schema changes must support existing serialized metadata. Migrations must distinguish:

- unknown control metadata;
- known reasoning support with fixed/non-configurable behavior;
- effort-controlled behavior;
- explicit-budget behavior;
- unsupported reasoning.

Existing terminal `model_unavailable` rows require a migration or hydration compatibility rule. They must not all be silently cleared. The implementation must identify rows produced by historical first-observation behavior where possible and convert them to bounded quarantine or require fresh catalog corroboration before retaining terminal status.

No migration may delete request, usage, cost, or audit history.

## Rollout strategy

1. Land Phase 1 with no production behavior change.
2. Land provider contracts in observe mode and compare intended versus actual adaptation.
3. Enable local rejection/mapping for explicitly covered provider/model pairs, starting with OpenCode Go MiniMax-M3.
4. Land typed failure effects with shadow comparison against legacy classification before switching authoritative effects.
5. Land retained finalization and database recovery behind process-level diagnostics, then enable by default after fault-matrix stability.
6. Land payload consolidation in small independently benchmarked slices.
7. Enable revised dispatch writer only after bounded-soak evidence.
8. Remove legacy paths only in Phase 8 after exact-equivalence evidence and one release of compatibility telemetry.

Every feature flag introduced during rollout must have a removal criterion and owner in its phase plan. Permanent dual implementations are not acceptable.

## Required cross-phase test matrix

The final matrix must include:

- OpenAI client to OpenAI upstream, native and streaming/non-streaming;
- Anthropic client to Anthropic upstream, native and streaming/non-streaming;
- OpenAI to Anthropic transcoding;
- Anthropic to OpenAI transcoding;
- provider-qualified and collapsed model IDs;
- thinking absent, low, medium/med, high, unsupported, unknown, explicit budget, and historical reasoning content;
- upstream 400, 401, 402, 403 quota, 404 model-like, 404 generic, 408, 409, 422, 429, 500, 502, 503, 504;
- connect, pool, read, write, and protocol transport errors;
- cancellation before selection, after selection, before commit, during commit, after commit, before first byte, midstream, and during finalization;
- SQLite begin, write, commit, rollback, close, reconnect, and reconciliation failures;
- single account, multiple accounts, mixed providers, and constrained concurrency;
- short burst, sustained concurrency, and extended soak.

## Performance gates

Phase 1 records exact baseline artifacts. Phase 8 compares against them.

Minimum gates:

- No statistically meaningful regression on native no-transform request p50 or p95 local pre-upstream latency.
- At least 25% reduction in JSON decode operations for non-stream success paths that currently parse more than once.
- At least 25% reduction in JSON encode operations for provider-bound requests with multiple enabled transforms.
- Finalization transaction p95 does not regress and should improve after lookup removal.
- Dispatch-writer diagnostic memory remains within a fixed bound after one million batches.
- Diagnostic snapshot p95 does not increase with historical batch count.
- Extended soak shows no monotonic increase in pending requests, reservations, active counts, finalization jobs, database lock-wait p95, or RSS after warm-up.

These are minimum closure gates, not permission to optimize benchmarks by disabling capability paths.

## Required implementation evidence

Each phase must leave:

- focused tests named for the plan number;
- a concise implementation note in the plan status or a linked artifact;
- exact commands used for focused verification;
- before/after measurements for performance phases;
- explicit documentation of any deferred item with rationale and owner;
- no unconditional skips or non-strict xfails.

Phase 8 must add an exact-head artifact containing:

- full 40-character implementation SHA;
- implementation tree SHA;
- Python versions;
- focused matrix results;
- standard suite result;
- lint, format, pyright, and xfail/skip audit results;
- resource and latency soak summaries;
- database consistency audit;
- proof that no source/test changes occurred after verification.

## Roadmap completion criteria

This roadmap is complete only when all of the following are true:

- [ ] The OpenCode Go MiniMax-M3 thinking-level reproducer is closed without provider health penalty.
- [ ] Unsupported provider controls cannot poison unrelated traffic.
- [ ] Request-local failures have test-pinned zero shared-state effects.
- [ ] Selected-attempt cleanup survives cancellation and finalizer faults.
- [ ] SQLite invalidation recovers without restart or database deletion.
- [ ] One runtime model failure cannot create indefinite withdrawal.
- [ ] Provider-bound request and non-stream response JSON are parsed once on common paths.
- [ ] Dispatch-writer and instrumentation memory are bounded by configuration, not uptime.
- [ ] Long-running soak shows stable latency, lock contention, queue depth, pending state, and RSS.
- [ ] Existing protocol, transcoding, usage, cost, cache, compression, reload, and dashboard test suites remain green.
- [ ] Exact-head evidence is committed and references the verified tree.

## Explicit non-goals

This roadmap does not:

- replace SQLite with another database;
- replace FastAPI, Granian, HTTPX, or aiosqlite;
- redesign routing strategy or quota fairness;
- add speculative retries after response bytes are emitted;
- silently weaken strict transcoding or capability-loss policy;
- infer unsupported provider contracts from arbitrary error text without an allowlisted contract;
- make diagnostic persistence correctness-critical;
- introduce distributed transaction coordination;
- require live provider credentials for CI closure.

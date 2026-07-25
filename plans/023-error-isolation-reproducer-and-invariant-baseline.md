# Error-Isolation Reproducer and Invariant Baseline

Date: 2026-07-25
Status: completed

Parent roadmap:

- `plans/022-upstream-error-isolation-and-hotpath-hardening-roadmap.md`

Implementation baseline:

- `3562889bc08af10d24376b5b0155f1897950116e`

## Objective

Create a deterministic, credential-free reproduction and measurement harness for the request-local upstream failure that can poison subsequent proxy operations. The first required scenario is MiniMax-M3 through OpenCode Go rejecting a supplied thinking level. The phase must expose every durable and in-memory side effect of the failed request and establish performance/resource baselines used by Plans 024–030.

This phase is observational and test-infrastructure focused. It must not change production routing, failure classification, health policy, finalization ownership, database recovery, or payload semantics except for narrowly scoped diagnostic seams that are inactive by default.

## Required outcomes

1. A mock upstream reproduces the exact class of provider validation failure without live credentials.
2. The reproducer can emit multiple plausible status/body shapes, because the production symptom may involve a 400 validation response, a provider-specific 404, or cancellation/finalization failure after the upstream response.
3. A reusable state-audit fixture captures all runtime and durable ownership before and after a request.
4. Fault injection covers request cancellation and every SQLite transaction boundary relevant to selection and finalization.
5. Parse/encode counters quantify repeated JSON work without altering wire behavior.
6. Baseline latency, lock, queue, memory, and operation-count artifacts are committed for later comparison.

## Scope

### In scope

- Mock OpenCode Go-compatible upstream endpoint.
- MiniMax-M3 request fixtures with and without thinking controls.
- Status/body/error-shape matrix.
- Streaming and non-streaming pre-body failure reproduction.
- State-audit helpers for database and runtime ownership.
- Deterministic cancellation injection.
- Deterministic database begin/write/commit/rollback injection.
- JSON decode/encode operation counters in tests or diagnostic mode.
- Baseline request-path and long-running synthetic measurements.
- Focused CI command and plan-specific tests.

### Out of scope

- Provider-control adaptation.
- New shared-state health policy.
- Automatic database reconnect.
- Permanent schema changes except test-only or diagnostic counters approved by this plan.
- Live requests to OpenCode Go or MiniMax.
- Performance optimization.

## Workstream A — Build the mock upstream contract

Create a reusable mock upstream service, preferably extending the existing mock SSE/provider infrastructure rather than adding a parallel test server framework.

The service must support:

- OpenAI-compatible `/chat/completions`.
- Anthropic-compatible `/messages` where needed for protocol comparison.
- Streaming and non-streaming responses.
- Configurable response status, headers, JSON body, plain-text body, delayed headers, delayed body, and connection termination.
- Per-request capture of received model, thinking/reasoning fields, request body bytes, and request sequence number.
- Deterministic rule matching by model and request field.

Required MiniMax-M3 scenarios:

1. No thinking field: successful response.
2. Accepted thinking field/value: successful response.
3. Unsupported thinking level: HTTP 400 validation error.
4. Unsupported thinking level rendered as provider-specific HTTP 422.
5. Misleading model-like HTTP 404 body containing “unsupported model” plus a thinking-field explanation.
6. Error followed immediately by a successful unrelated model request.
7. Error followed immediately by a successful MiniMax-M3 request without thinking controls.
8. Streaming request rejected before response bytes.
9. Connection dropped after response headers but before body read.

The mock service must expose a structured request log to tests. Tests must not infer what was sent by scraping application logs.

## Workstream B — Define canonical request fixtures

Add fixtures covering:

- OpenAI top-level `reasoning_effort` values: `low`, `med`, `medium`, `high`, `xhigh`, unknown string, null, and omitted.
- OpenAI nested `reasoning` forms currently accepted by Eggpool clients.
- Anthropic `thinking` with explicit `budget_tokens`.
- Historical assistant `reasoning_content` without a new requested thinking level.
- Provider-qualified model IDs and collapsed model IDs.
- Native-protocol and transcoded paths.
- Streaming and non-streaming requests.
- Requests with tools, cache controls, and compression-enabled fixtures so later phases can reuse the same payloads.

Fixtures must be immutable or copied per test. One test may not mutate a shared payload and influence another test.

## Workstream C — Build a complete state-audit snapshot

Introduce a test helper such as `RequestStateAuditSnapshot` that captures only scalar, bounded facts.

Required durable facts:

- Request rows by proxy request ID and database request ID.
- Attempt rows and terminal status.
- Reservation rows and release reason.
- Account-backoff rows, including scope and expiry.
- Account-event rows.
- Model availability or withdrawal facts persisted in SQLite.
- Routing-decision rows when enabled.
- Finalization retry/reconciliation rows or queue diagnostics if represented durably.

Required runtime facts:

- Router active-request count per account.
- Quota estimator reservation count, tokens, and estimated cost per account.
- Health-manager account state, circuit state, cooldown, disabled models, and in-flight probe ownership.
- Account runtime-state failure/cooldown facts.
- Catalog model availability per account/provider.
- Finalization retry queue depth and active entries.
- Dispatch-writer queue depth and state.
- Database connection diagnostics and invalidation state.
- Runtime generation identity, where relevant to ensure no reload interaction.

The helper must provide a deterministic diff method. The diff must distinguish expected request-history additions from forbidden shared-state changes.

Example categories:

```python
StateAuditDiff(
    request_history_changes=[...],
    runtime_ownership_changes=[...],
    health_changes=[...],
    durable_backoff_changes=[...],
    database_connection_changes=[...],
)
```

Tests must assert structured fields, not serialized repr strings.

## Workstream D — Add deterministic cancellation seams

Provide test-only cancellation points at:

1. Before request-row creation.
2. After request-row creation but before account selection.
3. After account/health slot claim.
4. After dispatch bundle commit.
5. Before upstream send.
6. After upstream headers.
7. Before non-retryable finalization.
8. During request finalization transaction.
9. Immediately after finalization commit but before runtime release.
10. During response rendering.
11. Midstream after at least one emitted chunk.

Use named hooks or context-manager seams. Do not use arbitrary sleeps as the primary synchronization mechanism.

Every injected cancellation test must have a bounded completion wait and a final state audit.

## Workstream E — Add deterministic database fault seams

Cover the write connection at these exact points:

- `BEGIN IMMEDIATE` raises.
- A selection-persistence write raises.
- A finalization write raises.
- COMMIT raises while `in_transaction=True`, rollback succeeds.
- COMMIT raises while transaction state is indeterminate.
- ROLLBACK raises after transaction-body failure.
- Connection close during invalidation raises.
- Subsequent transaction attempts observe invalidated state.

Each test must force exactly one outcome. Tests may not accept multiple outcomes such as “rolled back or invalidated.”

## Workstream F — Instrument parse and encode counts

Add a test/diagnostic counting layer around `eggpool.jsonx.loads`, `dumps_bytes`, and request-body encoding.

Requirements:

- Disabled or near-zero overhead in normal production configuration.
- Counts by request direction and lifecycle stage.
- No request body contents in metrics or logs.
- Separate counters for request decode, request encode, response decode, response encode, stream event decode, and stream event encode.
- Tests can reset and snapshot counters deterministically.

The baseline must record counts for:

- Native non-stream success.
- Native non-stream 400.
- Native stream success.
- OpenAI-to-Anthropic non-stream and stream.
- Anthropic-to-OpenAI non-stream and stream.
- Compression observe and safe modes.
- Synthetic cache controls enabled and disabled.

## Workstream G — Establish latency and resource baselines

Extend existing performance/soak infrastructure rather than creating one-off scripts when possible.

Record:

- `local_pre_upstream_ms` p50/p95/p99.
- Coordinator dispatch overhead p50/p95/p99.
- Per-span timings for JSON parse, transcode preflight, segmentation, compression, routing plan, selection claim wait/hold, dispatch persistence, provider-bound transforms, and finalization.
- SQLite lock-wait p50/p95/p99.
- Transaction duration p50/p95/p99.
- Finalization queue depth and age.
- Dispatch-writer queue depth and batch statistics.
- RSS, Python heap if available, file descriptors, task count, and thread count.
- Pending requests, incomplete attempts, unreleased reservations, and active-request counts.

Required profiles:

- Serial native requests.
- 50 concurrent native streams.
- 8 concurrent transcoded streams.
- Mixed success/validation-error workload.
- Cancellation workload.
- File-backed SQLite balanced workload.
- Minimum-footprint single worker-thread configuration.

Artifacts must include environment and configuration. Numbers without the exact profile are not evidence.

## Workstream H — Add the focused test suite

Suggested files:

- `tests/integration/test_plan_023_minimax_thinking_reproducer.py`
- `tests/integration/test_plan_023_error_isolation_matrix.py`
- `tests/unit/test_plan_023_state_audit.py`
- `tests/unit/test_plan_023_cancellation_seams.py`
- `tests/unit/test_plan_023_database_fault_matrix.py`
- `tests/unit/test_plan_023_json_operation_counters.py`
- `tests/perf/test_plan_023_request_path_baseline.py`
- `tests/soak/test_plan_023_error_isolation_baseline.py`

Use existing markers: `request_path`, `integration`, `performance`, `soak`, and `extended_soak` as appropriate.

## Required baseline artifact

Commit `artifacts/plan-023-baseline.md` containing:

- Full 40-character implementation SHA.
- Tree SHA.
- Python and platform versions.
- Exact configuration.
- Focused command results.
- Failure-state mutation table.
- Parse/encode counts.
- Latency and lock measurements.
- Resource plateau measurements.
- Known nondeterminism, if any.

The plan remains “implementation handoff” until the artifact is generated from a committed implementation tree.

## Acceptance criteria

### Reproduction

- [ ] MiniMax-M3/OpenCode Go unsupported-thinking failure reproduces without live credentials.
- [ ] The mock upstream proves the exact request fields Eggpool forwarded.
- [ ] A successful unrelated request immediately after the failure is part of the reproducer.
- [ ] A successful MiniMax-M3 request without thinking controls immediately after the failure is part of the reproducer.
- [ ] 400, 422, misleading 404, and transport-interruption variants are independently testable.

### State audit

- [ ] State snapshots include every durable and runtime ownership surface listed above.
- [ ] Diffs distinguish request-history changes from forbidden shared-state effects.
- [ ] Every failure/cancellation test asserts zero leaked reservations and active counts after bounded cleanup.
- [ ] Health, catalog, and durable backoff changes are explicitly asserted rather than omitted.
- [ ] Database invalidation state is captured and test-pinned.

### Fault injection

- [ ] All eleven cancellation points are deterministic and covered.
- [ ] Begin, write, commit, rollback, invalidation-close, and subsequent-use failures are deterministic and covered.
- [ ] No fault test accepts multiple possible terminal outcomes.
- [ ] Hooks are test-only or disabled by default.

### Baselines

- [ ] Parse/encode counts exist for every required path.
- [ ] Latency and lock baselines exist for every required profile.
- [ ] RSS/task/thread/file-descriptor baselines include early and late windows.
- [ ] Baseline artifacts identify exact configuration and commit tree.
- [ ] Existing tests remain behaviorally unchanged.

### Verification

- [ ] `uv run ruff format --check src/ tests/ scripts/` passes.
- [ ] `uv run ruff check src/ tests/ scripts/` passes.
- [ ] `uv run pyright src/ scripts/` passes.
- [ ] Focused Plan 023 tests pass on Python 3.11 and 3.12.
- [ ] Standard non-slow test suite passes.
- [ ] `uv run python scripts/audit_xfail_skips.py` passes.
- [ ] No live/network credential is required for closure.

## Closure evidence required for handoff

The implementing agent must update this plan to `Status: completed` only after linking the exact-head baseline artifact and listing the focused test files added. A statement that the bug “could not be reproduced live” is not closure; the deterministic mock contract is the authoritative reproducer for later phases.

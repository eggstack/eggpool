# Provider Payload Lifecycle and Hot-Path Consolidation

Date: 2026-07-25
Status: implementation handoff

Parent roadmap:

- `plans/022-upstream-error-isolation-and-hotpath-hardening-roadmap.md`

Depends on:

- `plans/023-error-isolation-reproducer-and-invariant-baseline.md`
- `plans/024-provider-bound-thinking-control-normalization.md`
- `plans/025-failure-effects-and-model-quarantine.md`
- `plans/026-process-owned-request-finalization.md`
- `plans/027-database-recovery-and-transaction-reconciliation.md`

## Objective

Reduce request-path CPU, allocations, serialization work, and SQLite writer-lock duration without changing protocol behavior. Consolidate provider-bound request transformations around one decoded payload lifecycle, consolidate non-stream response processing around one decoded response lifecycle, reuse previously computed segmentation where safe, and move avoidable lookups and best-effort work outside correctness-critical transactions.

The phase must be driven by Plan 023 operation counters and baselines. It may not claim improvement solely from code inspection.

## Performance principles

1. Parse once, transform in memory, encode once.
2. Preserve raw bytes for exact passthrough.
3. Do not mutate shared client payload objects.
4. Avoid work when no consumer needs it.
5. Keep provider-bound transforms ordered and explicit.
6. Shorten global SQLite writer critical sections.
7. Preserve all usage, cost, cache, compression, thinking, and error semantics.
8. Optimize common paths without creating untested divergent implementations.

## Scope

### In scope

- Typed request payload lifecycle.
- Typed non-stream response payload lifecycle.
- Shared provider-bound transform pipeline.
- Prepared-transcode reuse without repeated parse/encode.
- Streaming usage-option injection without reparsing bytes.
- Segmentation and stable-prefix reuse with validity keys.
- Non-stream usage extraction from one decoded object.
- Error transcoding from one decoded object.
- Finalization transaction shortening.
- Account/provider/model identity reuse.
- Import/cache and small allocation reductions supported by measurement.
- Benchmarks, semantic equivalence, and byte-for-byte tests.

### Out of scope

- Replacing Python with Rust.
- Changing JSON wire formatting or number semantics.
- Changing compression transforms or capability policy.
- Replacing HTTPX or aiosqlite.
- Removing diagnostics needed for Plans 023–027.
- Optimizing streaming event transcoding beyond measured, independently safe changes.

## Workstream A — Define `ProviderBoundRequest`

Create a lifecycle object owned by one proxy request. Suggested shape:

```python
@dataclass(slots=True)
class ProviderBoundRequest:
    client_bytes: bytes
    client_payload: Mapping[str, Any]
    client_protocol: str
    model_id: str
    provider_id: str | None = None
    upstream_protocol: str | None = None
    provider_payload: Mapping[str, Any] | None = None
    provider_bytes: bytes | None = None
    mutated: bool = False
    mutation_generation: int = 0
    segmentation: SegmentationResult | None = None
    segmentation_key: SegmentationValidityKey | None = None
```

Required semantics:

- `client_payload` is treated as immutable.
- `provider_payload` initially aliases the client payload only when no mutation will occur; copy-on-write or immutable mapping semantics prevent accidental mutation.
- `provider_bytes` is produced once after all provider-bound transforms.
- A transform that changes the payload increments a generation/version.
- Exact original bytes are used when model/provider fields and all transforms are unchanged.
- Callers do not independently call `jsonx_loads(context.upstream_body)`.

`ProxyRequestContext` may own this object or be refactored to expose equivalent fields. Avoid duplicating both old and new authoritative payload state indefinitely.

## Workstream B — Build one ordered provider transform pipeline

Create an explicit ordered pipeline after account selection:

1. Resolve upstream protocol and model identity.
2. Materialize/reuse prepared transcode output.
3. Apply selected-provider thinking-control normalization from Plan 024.
4. Apply selected-provider model/request aliases.
5. Resolve post-route compression/cache policy.
6. Apply synthetic cache controls when enabled.
7. Inject OpenAI `stream_options.include_usage` when required.
8. Apply any provider-specific static request shaping.
9. Validate final provider payload invariants.
10. Serialize once.

Each transform must declare:

- whether it requires decoded payload;
- whether it can return the original mapping unchanged;
- whether it invalidates segmentation;
- whether it changes token/context estimates;
- whether it is allowed to fail the request or must fail open;
- diagnostic decision category.

The pipeline must be shared by streaming and non-streaming execution.

## Workstream C — Prepared transcode reuse

The current preflight can produce a translated payload and encoded body. Rework reuse so:

- prepared transcode stores decoded translated payload as the primary artifact;
- encoded bytes are optional cache output, valid only for a specific transform generation;
- selected-provider thinking normalization can modify decoded output without parsing bytes;
- compatibility/loss warnings are retained once;
- tool-token padding and context checks use the same decoded payload;
- feature/policy changes or selected-provider overrides invalidate reuse deterministically.

Add a `PreparedTranscodeValidityKey` including client/upstream protocols, policy generation, feature flags, and relevant capability generation.

Do not silently reuse preflight output created for a collapsed capability when selected-provider constraints require recomputation.

## Workstream D — Segmentation reuse and invalidation

Segmentation currently may run before routing and again for synthetic cache controls. Introduce a validity key, for example:

```python
@dataclass(frozen=True, slots=True)
class SegmentationValidityKey:
    payload_generation: int
    protocol: str
    segmentation_policy_version: int
```

Reuse segmentation only when:

- payload generation is unchanged;
- protocol interpretation is unchanged;
- segmentation policy/version is unchanged;
- no transform changed protected/cache-control boundaries.

When only a provider-neutral model string changes, document whether segmentation remains valid and test it. When transcode changes message/block structure, recompute once after transcode rather than retaining client-protocol segmentation.

Synthetic cache controls must consume the current valid segmentation instead of unconditionally walking the payload again.

## Workstream E — Define `ParsedUpstreamResponse`

For non-stream responses, decode once into:

```python
@dataclass(slots=True)
class ParsedUpstreamResponse:
    status_code: int
    headers: list[tuple[str, str]]
    raw_body: bytes
    parsed_json: object | None
    parse_status: Literal["not_attempted", "parsed", "invalid_json", "non_object"]
```

Consumers:

- upstream error signal extraction;
- retry/failure-effects classification;
- protocol error re-encoding;
- usage extraction;
- normalized usage construction;
- provider-reported cost extraction;
- success response transcoding.

Rules:

- Parse only when a consumer needs JSON.
- Parse at most once.
- Preserve invalid/non-object distinction.
- Preserve original bytes for pass-through.
- Encode only when response transcoding/adaptation changes the body.
- Do not convert an invalid upstream JSON body into a fabricated success.

Refactor usage extractors to accept a decoded mapping plus provider ID. Keep byte-accepting compatibility wrappers only temporarily and remove them after call-site migration.

## Workstream F — Streaming path operation accounting

Streaming events are inherently incremental, but avoid duplicate decode/encode within each event.

Requirements:

- One event parser owns frame/SSE decode.
- Usage observer and transcoder share the decoded event representation when both need it.
- Original chunk bytes pass through untouched when no transform is required.
- Event re-encoding occurs only after mutation.
- Final usage-bearing events are not decoded independently by multiple consumers.
- Buffering remains bounded and does not delay first-byte delivery beyond existing policy.

This workstream should be scoped to measured duplicate operations. Do not redesign the whole streaming parser without baseline evidence.

## Workstream G — Shorten dispatch persistence and finalization transactions

Audit every SQL operation executed under `BEGIN IMMEDIATE` in selection and finalization.

Mandatory changes when applicable:

- Use `SelectedAttempt.account_id`; do not query account ID by name inside finalization.
- Reuse cached provider/model/account identities already computed before the lock.
- Move best-effort account event enrichment outside the correctness transaction or use the known account ID directly.
- Move diagnostic JSON construction outside the transaction.
- Precompute serialization summaries before entering the transaction when they are immutable and bounded.
- Avoid repository object construction inside hot transactions when stable instances can be injected.
- Combine dependent writes with `RETURNING`/batch methods only when semantics remain exact.
- Do not hold the database lock while awaiting unrelated in-memory metrics or provider work.

Document the minimal correctness write set for dispatch and finalization.

## Workstream H — Reduce small repeated work

Measure before changing. Candidate targets include:

- Repeated construction/lowercasing of header dictionaries.
- Repeated provider URL composition for stable provider/protocol pairs.
- Function-local imports executed on every request.
- Repeated creation of repository wrappers.
- Repeated list/set copies in routing and response headers.
- Repeated capability Pydantic conversion from stable cached dictionaries.
- Repeated model/provider resolution for the same request.

Any cache must have:

- bounded size;
- generation-aware invalidation on rehash/catalog refresh;
- no API keys or request content;
- deterministic behavior under concurrent access.

Do not trade small CPU savings for stale provider configuration.

## Workstream I — Preserve exact semantics

Required equivalence dimensions:

- status code;
- response headers after existing filtering;
- raw body bytes on pass-through paths;
- decoded semantic JSON on transformed paths;
- upstream request body semantic JSON;
- field omission versus explicit null;
- integer/float/string behavior;
- Unicode escaping behavior;
- stream frame boundaries where currently guaranteed;
- usage and cache counters;
- provider-reported and local cost;
- thinking trace;
- compression and synthetic cache audit fields;
- finalization outcome and retry count.

Use golden fixtures and differential tests running legacy/reference helpers against the consolidated pipeline during implementation. Remove the reference path only after exact closure.

## Workstream J — Benchmarks and tests

Suggested files:

- `tests/unit/test_plan_028_provider_bound_request.py`
- `tests/unit/test_plan_028_transform_pipeline.py`
- `tests/unit/test_plan_028_prepared_transcode_reuse.py`
- `tests/unit/test_plan_028_segmentation_reuse.py`
- `tests/unit/test_plan_028_parsed_upstream_response.py`
- `tests/unit/test_plan_028_response_equivalence.py`
- `tests/unit/test_plan_028_transaction_scope.py`
- `tests/perf/test_plan_028_json_operation_counts.py`
- `tests/perf/test_plan_028_hotpath_latency.py`
- `tests/integration/test_plan_028_protocol_matrix.py`

Required profiles:

- Native no-transform non-stream request/success.
- Native non-stream 400.
- Native stream success.
- OpenAI-to-Anthropic non-stream/stream.
- Anthropic-to-OpenAI non-stream/stream.
- Thinking normalization with and without mutation.
- Compression off/observe/safe.
- Synthetic cache off/dry-run/apply.
- Large tools payload.
- Large message history.
- Mixed 8-stream transcode concurrency.
- 50-stream native concurrency.

## Performance acceptance criteria

Relative to Plan 023 exact baseline on equivalent hardware/configuration:

- [ ] Native non-stream success request JSON decode count is exactly one.
- [ ] Native non-stream pass-through request encode count is zero when original bytes remain valid, otherwise exactly one.
- [ ] Provider-bound request with multiple transforms is encoded exactly once after selection.
- [ ] Non-stream success response JSON decode count is at most one.
- [ ] Non-stream error response JSON decode count is at most one.
- [ ] Non-stream response encode count is zero on raw pass-through and exactly one when transformed.
- [ ] Synthetic cache control does not cause a second segmentation when the validity key matches.
- [ ] Usage extraction and normalized usage share one decoded response.
- [ ] Finalization transaction removes the account-ID lookup and other identified avoidable SQL.
- [ ] Correctness transaction p95 does not regress; target improvement is at least 10% under mixed workload when baseline noise permits.
- [ ] Native no-transform local-pre-upstream p50/p95 does not regress beyond the documented noise threshold.
- [ ] Transcoded multi-transform operation counts decrease by at least 25% for decode and encode operations where Plan 023 showed duplication.

## Correctness acceptance criteria

### Request pipeline

- [ ] Streaming and non-streaming use one ordered provider transform pipeline.
- [ ] Native and transcoded requests use the same provider-bound representation.
- [ ] Original client payload is not mutated.
- [ ] Prepared transcode invalidation is deterministic.
- [ ] Thinking-control adaptation from Plan 024 remains provider-specific and correct.
- [ ] Context-limit and reservation estimates remain conservative and use the correct payload stage.

### Response pipeline

- [ ] All required consumers use one parsed response object.
- [ ] Invalid JSON and non-object JSON behavior remains unchanged.
- [ ] Raw pass-through body bytes are byte-for-byte identical.
- [ ] Transcoded response JSON is semantically equivalent to the pre-refactor behavior.
- [ ] Usage, cost, cache, reasoning, and thinking-character accounting remains exact.

### Segmentation and cache/compression

- [ ] Segmentation reuse occurs only with a matching validity key.
- [ ] Protocol or structural mutation invalidates segmentation.
- [ ] Stable-prefix and cache-protected invariants remain green.
- [ ] Compression observe/safe and synthetic cache audit fields remain accurate.
- [ ] No optimization bypasses strict loss/capability policy.

### Transactions

- [ ] Minimal correctness write sets are documented and test-guarded.
- [ ] No provider/network await occurs under the database write transaction.
- [ ] Known account ID is reused.
- [ ] Best-effort diagnostic work cannot fail the correctness transaction.
- [ ] Plan 026 finalization ownership remains exact under transaction failures.
- [ ] Plan 027 reconciliation metadata remains available before commit.

### Verification

- [ ] Plans 023–027 focused suites remain green.
- [ ] Differential/golden tests pass on Python 3.11 and 3.12.
- [ ] Performance tests report operation counts and latency distributions, not only wall-clock totals.
- [ ] Standard non-slow suite passes.
- [ ] Ruff format, Ruff check, Pyright, and xfail/skip audit pass.

## Closure evidence

Commit an exact-head artifact with before/after parse and encode counts for every required profile, transaction statement counts and duration distributions, local-pre-upstream/dispatch measurements, and differential equivalence results. The artifact must list any path that still parses or encodes more than once and explain why it is structurally necessary. Update this plan to completed only after the verified implementation tree is committed.

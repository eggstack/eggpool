# Provider-Bound Request Pipeline Completion

Date: 2026-07-28
Status: implementation handoff

Parent roadmap:

- `plans/031-upstream-hardening-corrective-roadmap.md`

Depends on:

- `plans/033-real-eggpool-runtime-test-harness.md`

May be implemented after or alongside:

- `plans/034-runtime-error-isolation-finalization-recovery-matrix.md`

Implementation baseline:

- completion commit of Plan 033

## Objective

Complete the request-side payload lifecycle introduced by Plan 028 so `ProviderBoundRequest` is the actual authoritative decoded and serialized provider payload for every post-selection transform.

The current pipeline is structurally present but creates a no-op `ProviderBoundRequest` and delegates to legacy coordinator helpers that parse and mutate `ProxyRequestContext.upstream_body` independently. This leaves duplicate JSON work, split ownership, and a false architecture claim.

The completed design must maintain one provider-bound payload object, mutate it through ordered pure or narrowly stateful transforms, and serialize it once after the final mutation before dispatch.

## Scope

### In scope

- `ProviderBoundRequest` ownership and lifecycle.
- Post-selection transform pipeline implementation.
- Provider protocol translation/reuse handoff into the provider-bound object.
- Selected-provider thinking-control normalization.
- Synthetic cache-control transformation.
- streaming usage-option injection.
- segmentation/prepared-transcode cache validity and invalidation.
- exact JSON decode/encode operation counts.
- streaming/non-streaming equivalence.
- removal of legacy duplicate request-body parsing and serialization paths.
- focused runtime and structural tests.

### Out of scope

- Response-side `ParsedUpstreamResponse`, except regression tests.
- Failure-effects behavior.
- Database recovery/finalization logic.
- New compression or cache algorithms.
- Routing strategy.
- Reworking pre-route context-limit validation.
- Broad request model/schema redesign.
- Performance soak; Plans 036 and 037 own measured closure.

## Required end-state architecture

### One payload owner

For every accepted proxy request:

1. `ParsedRequestPayload` owns the decoded client payload at ingress.
2. `ProviderBoundRequest` is constructed from that payload without a second client decode.
3. Preflight/protocol translation initializes or replaces `ProviderBoundRequest.provider_payload`.
4. After provider selection, all provider-specific transforms read and mutate `ProviderBoundRequest`.
5. Each mutation increments `payload_generation` and invalidates only the caches declared by transform metadata.
6. The final payload is serialized exactly once for the current generation.
7. `context.body_for_upstream` returns only `ProviderBoundRequest.provider_bytes` once the provider-bound object exists.
8. `client.build_request(..., content=...)` receives those exact bytes.

`ProxyRequestContext.upstream_body` may remain temporarily for compatibility during the implementation commit, but it must not remain a second authoritative mutable payload at plan completion.

### One pipeline entry point

Both streaming and non-streaming dispatch must call one function exactly once, preferably:

```python
result = run_provider_transforms(
    request=context.provider_bound,
    context=transform_context,
)
```

The pipeline must operate on the actual request object. It must not construct an empty/no-op `ProviderBoundRequest`.

### Ordered transform stages

Required logical order:

1. Establish provider protocol payload from prepared transcode or actual translation.
2. Normalize selected-provider thinking controls.
3. Apply provider-specific synthetic cache controls.
4. Inject provider-required stream usage options when streaming.
5. Perform any final provider-safe request annotations already present in production.
6. Serialize final provider payload.

If current behavior requires a different order, document the invariant and add tests proving why. Do not allow streaming and non-streaming orders to diverge.

## Workstream A — ProviderBoundRequest API hardening

Review and adjust `ProviderBoundRequest` so it supports:

- immutable client bytes and client payload reference/copy policy;
- current provider payload;
- provider protocol;
- payload generation;
- serialized bytes plus generation at serialization;
- segmentation validity key/data;
- prepared-transcode validity key/data;
- mutation reason/history as bounded diagnostic metadata;
- `set_provider_payload(...)` with exact invalidation flags;
- `serialize_if_needed()` or equivalent idempotent serialization.

Recommended invariants:

```python
assert provider_bytes is None or serialized_generation == payload_generation
assert client_payload is never mutated
assert set_provider_payload(same_object_or_equal_payload, changed=False) does not increment
assert every declared mutation invalidates serialized bytes
```

Do not store unbounded copies of historical payloads. Diagnostic mutation history, if retained, must be a small bounded tuple/deque of stage names only.

## Workstream B — Remove no-op pipeline adapter

Delete the pattern that creates:

```python
ProviderBoundRequest(client_bytes=b"", client_payload={}, ...)
```

The pipeline must receive `context.provider_bound`; absence is a programming error after ingress construction, except in explicitly supported legacy unit tests.

For legacy callers that instantiate `ProxyRequestContext` directly:

- add a narrow lazy initializer from `parsed_payload`/`original_body`, or
- update those tests/callers to construct the required object.

Do not silently fall back to a second ad-hoc parsing path in production.

Add a structural test that fails if `run_provider_transforms` constructs `ProviderBoundRequest` internally.

## Workstream C — Protocol translation ownership

Move the result of prepared-transcode reuse or fresh translation into `ProviderBoundRequest.provider_payload`.

Requirements:

- reuse prepared translated payload/bytes only when its validity key matches current client payload, feature policy, target protocol, and any fields known to affect translation;
- if prepared result exposes only bytes, decode at most once into the provider-bound payload, or change prepared representation to retain the translated dict from the existing preflight decode;
- do not serialize translated output before later provider-specific transforms;
- preserve transcode warnings and diagnostics in `TranscodeContext` without mutating the original client payload;
- strict transcode errors remain request-local.

The implementer should prefer carrying the preflight translated mapping alongside bytes rather than decoding translated bytes again after selection.

## Workstream D — Thinking-control transform

Replace the coordinator adapter that mutates `context.upstream_body` with a transform that:

1. Reads `request.provider_payload`.
2. Resolves the selected-provider contract using Plan 032 identity behavior.
3. Uses the immutable original `ThinkingRequestIntent`.
4. Calls the existing pure adaptation helper.
5. If rejected, returns/raises the canonical `CapabilityError` without mutating the provider payload.
6. If changed, calls `request.set_provider_payload(...)` once with appropriate invalidation flags.
7. Records decision/warnings in the thinking trace.
8. Does not serialize.

The transform must return `PASSTHROUGH` when unchanged rather than always reporting `MUTATED`.

Budget recomputation for transcoded Anthropic payloads must update the same provider payload object and must not parse bytes.

## Workstream E — Synthetic cache transform

Refactor synthetic cache controls to operate on the provider payload mapping.

Requirements:

- segmentation is computed from the current payload generation;
- cached segmentation is reused only when the full validity key matches;
- a thinking-control mutation invalidates segmentation only when it changes fields relevant to segmentation;
- synthetic cache mutation invalidates serialized bytes and updates generation;
- safety diff validation compares pre/post mappings without reparsing;
- fallback leaves the pre-transform payload intact;
- provider-specific policy resolution uses selected provider/account/protocol context;
- no serialization occurs inside the transform.

Tests must cover no-op, transformed, validation-fallback, and provider-policy mismatch cases.

## Workstream F — Streaming usage-option transform

Move `stream_options.include_usage` injection into the common provider transform pipeline.

Requirements:

- runs only for streaming OpenAI-compatible upstream requests where current policy requires it;
- preserves an explicitly supplied compatible `stream_options` mapping;
- does not duplicate or overwrite unrelated stream options;
- returns passthrough for Anthropic upstreams and non-streaming requests;
- applies before final serialization;
- streaming path does not parse/serialize the body after the pipeline.

This closes a remaining post-pipeline mutation path.

## Workstream G — Final serialization and dispatch

Add one serialization boundary after all transforms:

```python
provider_bytes = request.serialize_if_needed()
```

Then:

- assign no second encoded copy to `context.upstream_body`;
- build streaming and non-streaming upstream requests from `provider_bytes`;
- ensure retry attempts for the same selected provider can reuse bytes when no transform input changes;
- if a retry selects a different provider, re-run provider-specific transforms from the correct provider payload base rather than mutating the previous provider's payload cumulatively.

The last requirement is critical. A request retried from Provider A to Provider B must not carry Provider A-specific thinking/cache annotations.

Recommended design: retain a provider-neutral translated base payload and clone/copy-on-write it for each selected provider attempt.

## Workstream H — Remove legacy ownership

At completion:

- remove or make private/deprecated any helper whose sole purpose is parsing `context.upstream_body` for a transform now represented in the pipeline;
- eliminate direct `encode_json_body`/`jsonx_loads` calls from those transform paths;
- remove comments claiming native thinking controls pass unchanged when provider normalization now applies;
- update architecture documentation to describe actual ownership;
- add source-level tests preventing reintroduction of the no-op wrapper and known duplicate parse sites.

Do not remove general-purpose JSON helpers used elsewhere.

## Required tests

Create or replace with:

- `tests/unit/test_plan_035_provider_bound_lifecycle.py`
- `tests/unit/test_plan_035_transform_mutation_semantics.py`
- `tests/unit/test_plan_035_provider_retry_reset.py`
- `tests/unit/test_plan_035_stream_usage_transform.py`
- `tests/integration/test_plan_035_proxy_payload_equivalence.py`
- `tests/perf/test_plan_035_json_operation_contract.py`
- `tests/unit/test_plan_035_architecture_guards.py`

### Lifecycle tests

- construct from `ParsedRequestPayload` without another client decode;
- provider payload mutation increments generation once;
- passthrough does not increment generation;
- serialized bytes cache is reused for unchanged generation;
- mutation invalidates bytes;
- segmentation/prepared caches invalidate according to declared flags;
- client payload remains unchanged.

### Transform tests

For each transform assert:

- exact input mapping;
- exact output mapping;
- decision (`PASSTHROUGH`, `MUTATED`, `REJECTED`);
- generation delta;
- cache invalidation delta;
- no JSON encode occurs inside the transform;
- warning/trace output.

### Retry-reset tests

- Provider A transform adds/removes provider-specific fields.
- Retry selects Provider B.
- Provider B starts from provider-neutral base, not Provider A output.
- Final bytes contain only Provider B-valid fields.
- Original client payload remains unchanged.

### Runtime equivalence matrix

Through the Plan 033 harness cover:

- OpenAI -> OpenAI native non-stream;
- OpenAI -> OpenAI native stream;
- OpenAI -> Anthropic transcoded non-stream;
- OpenAI -> Anthropic transcoded stream;
- Anthropic -> Anthropic native non-stream;
- Anthropic -> OpenAI transcoded non-stream;
- tools/system messages;
- historical reasoning content;
- thinking strict reject and warn-drop;
- synthetic cache enabled/disabled;
- dispatch retry to a different provider.

Compare captured upstream payload against an explicit expected mapping, not merely response success.

### JSON operation contract

Instrument `eggpool.jsonx` and production request helpers.

For common paths after ingress:

- native no-transform: zero additional request decodes, at most one final encode;
- native thinking adaptation: zero additional request decodes, one final encode;
- transcoded request with prepared mapping: zero translated-body re-decodes, one final encode;
- synthetic cache + thinking + stream usage combined: zero intermediate encodes, one final encode;
- retry to second provider: no client-body re-decode; one final encode per distinct provider attempt only when payload differs.

Count exact operations by phase/direction. Avoid loose bounds such as `<= 60` for twenty requests.

## Files expected to change

Primary:

- `src/eggpool/request/provider_bound_request.py`
- `src/eggpool/request/transform_pipeline.py`
- `src/eggpool/request/coordinator.py`
- `src/eggpool/api/proxy_request.py`
- prepared transcode representation only if needed
- focused tests

Possible:

- synthetic cache/segmentation helpers to accept mappings directly;
- architecture documentation;
- `AGENTS.md` focused commands.

Do not modify failure/quarantine/database recovery semantics.

## Implementation sequence

1. Add exact operation-count tests that expose current duplicate/no-op behavior.
2. Harden `ProviderBoundRequest` invariants and serialization generation.
3. Pass the actual object into the pipeline.
4. Move protocol translation output into it.
5. Convert thinking normalization to payload transform.
6. Convert synthetic cache synthesis to payload transform.
7. Move stream usage injection into the pipeline.
8. Add one final serialization boundary.
9. Implement clean per-provider retry reset.
10. Remove legacy parse/mutate/serialize paths.
11. Add structural guards.
12. Run runtime equivalence and existing transcode/cache suites.
13. Record evidence in `artifacts/plan-035-evidence.md`.

## Focused verification commands

```bash
uv run pytest \
  tests/unit/test_plan_035_provider_bound_lifecycle.py \
  tests/unit/test_plan_035_transform_mutation_semantics.py \
  tests/unit/test_plan_035_provider_retry_reset.py \
  tests/unit/test_plan_035_stream_usage_transform.py \
  tests/unit/test_plan_035_architecture_guards.py \
  tests/integration/test_plan_035_proxy_payload_equivalence.py \
  tests/perf/test_plan_035_json_operation_contract.py \
  -q --tb=short

uv run pytest \
  tests/unit/test_plan_028_*.py \
  tests/integration/test_plan_028_protocol_matrix.py \
  tests/integration/test_transcode_*.py \
  -q --tb=short
```

## Acceptance criteria

### Ownership

- [ ] `ProviderBoundRequest` is the real provider payload owner.
- [ ] `run_provider_transforms` receives the actual request object.
- [ ] No no-op/empty provider request is constructed by the pipeline.
- [ ] `ProxyRequestContext.upstream_body` is no longer an independent mutable authority.
- [ ] Streaming and non-streaming call one common pipeline once per provider attempt.

### Transform correctness

- [ ] Protocol translation, thinking normalization, synthetic cache, and stream usage injection operate on mappings.
- [ ] Unchanged transforms report passthrough.
- [ ] Rejections do not partially mutate the payload.
- [ ] Cache invalidation follows declared transform metadata.
- [ ] Retry to another provider starts from a provider-neutral base.

### Serialization and operation counts

- [ ] Common accepted paths perform one final request serialization per provider attempt.
- [ ] No transform reparses serialized provider bytes.
- [ ] Exact operation-count tests pass.
- [ ] Captured upstream bytes decode to the explicit expected payload.
- [ ] Client payload remains unchanged.

### Regression and quality

- [ ] Existing transcode, cache, compression, usage, and request-path suites remain green.
- [ ] Plan 034 error-isolation behavior remains green.
- [ ] Focused tests pass on Python 3.11 and 3.12.
- [ ] Ruff and Pyright are clean.
- [ ] Architecture documentation matches actual ownership.
- [ ] `artifacts/plan-035-evidence.md` records exact counts and implementation SHA/tree.

## Explicit rejection conditions

Do not mark this plan complete if:

- the pipeline still constructs an empty/no-op `ProviderBoundRequest`;
- adapters still parse and mutate `context.upstream_body` independently;
- stream usage injection occurs after final pipeline serialization;
- synthetic cache or thinking transforms encode intermediate payloads;
- operation tests use broad bounds that permit duplicate common-path work;
- retry to Provider B can inherit Provider A-specific mutations;
- tests compare only responses and not captured upstream payloads;
- architecture docs claim single ownership while a legacy path remains reachable.

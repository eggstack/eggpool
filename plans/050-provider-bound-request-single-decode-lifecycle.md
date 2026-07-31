# Plan 050 — Provider-Bound Request Single-Decode Lifecycle

Date: 2026-07-30
Status: closed (implementation completed in this change)
Parent roadmap: `plans/045-upstream-streaming-hardening-hotpath-roadmap.md`
Planning baseline: `216e615d75269cc1471a920ae81ece9ef2d21802`

## Objective

Make `ProviderBoundRequest` the authoritative request payload after client parsing and provider selection, so thinking adaptation, synthetic cache controls, compression-dependent post-route behavior, and streaming usage-option injection operate on one decoded object and produce one final provider serialization.

This phase removes duplicate request JSON work and eliminates the current no-op transform wrapper around legacy context-mutating helpers. It must preserve all request semantics and must not redesign streaming SSE parsing, terminal lifecycle, routing, or compression algorithms.

## Current problems to close

1. `run_provider_transforms()` constructs a no-op `ProviderBoundRequest` with an empty payload while adapters mutate `ProxyRequestContext` directly.
2. Adapters can report `MUTATED` even when no actual payload mutation occurred.
3. Thinking adaptation decodes `context.upstream_body` independently.
4. Synthetic cache post-route logic decodes the body again and may serialize again.
5. OpenAI streaming setup decodes the body again to inject `stream_options.include_usage` and serializes again.
6. `context.upstream_body`, `provider_bound.provider_bytes`, and original bytes can become competing sources of truth.
7. Transform mutation metadata does not reliably describe which generation of the payload was sent upstream.

## Ownership boundary

Primary modules:

- `src/eggpool/request/provider_bound_request.py`
- `src/eggpool/request/transform_pipeline.py`
- `src/eggpool/request/coordinator.py`
- narrow request-transcoding and synthetic-cache adapters
- parsed-payload/prepared-transcode integration
- focused request lifecycle, parity, and operation-count tests

Do not rewrite protocol transcoder semantics, segmentation algorithms, compression heuristics, SSE parser/transcoder internals, routing selection, or database persistence in this phase.

## Required lifecycle

The canonical flow must be:

```text
client bytes
  -> one client JSON decode
  -> immutable client payload snapshot
  -> protocol transcode or native provider payload derivation
  -> selected-provider thinking-control normalization
  -> post-route cache/compression-dependent mutations
  -> stream-options/include-usage mutation when required
  -> one final provider JSON serialization
  -> HTTP request construction
```

No post-selection transform may independently parse provider bytes when the authoritative decoded provider payload is available.

## Required `ProviderBoundRequest` contract

### 1. Explicit payload ownership

The type must retain:

- original client bytes;
- immutable or non-mutated client payload reference/snapshot;
- client protocol;
- upstream protocol;
- model identity;
- current provider payload;
- payload generation counter;
- serialized provider bytes and the generation they represent;
- transform decision log/warnings;
- optional selected provider/account metadata after binding.

### 2. Mutation API

All provider payload mutations must pass through explicit methods, for example:

```python
request.replace_provider_payload(new_payload, reason="thinking_control")
request.mutate_provider_payload(mutator, reason="stream_options")
```

Requirements:

- increment generation only when structural content changes;
- invalidate serialized bytes on mutation;
- retain a bounded mutation log suitable for diagnostics;
- reject mutation after the provider HTTP request has been frozen/built;
- do not expose mutable aliases that allow generation changes to be bypassed.

The exact API may differ, but direct uncontrolled mutation must be prevented or detected.

### 3. Serialization cache

`serialize_provider_payload()` or an equivalent method must:

- return cached bytes when generation is unchanged;
- serialize exactly once for the final generation;
- record encode count in test diagnostics;
- freeze the provider payload for dispatch;
- never overwrite original client bytes.

### 4. Client payload immutability

Provider transforms must not mutate the parsed client payload in place. Tests must prove original parsed content remains unchanged after provider transforms.

### 5. Prepared transcode integration

A valid preflight/prepared transcode may seed the provider payload and bytes, but provider-specific post-route transforms must invalidate/recompute bytes only when they change payload content.

Required cases:

- prepared transcode reused with no post-route change: zero additional provider decode and no unnecessary re-encode;
- prepared transcode reused then thinking/cache mutation: use its decoded provider payload or decode at most once if no decoded form was retained, then one final encode;
- prepared transcode invalid: recompute once and continue through the same canonical lifecycle.

Do not retain two independent authoritative translated payloads.

## Transform pipeline requirements

### 1. Real request object

`run_provider_transforms()` must receive the actual provider-bound request. Remove the empty/no-op request wrapper and legacy adapter expectation that context owns the payload.

### 2. Honest transform results

Each transform must return:

- `PASSTHROUGH` when it made no change;
- `MUTATED` only after generation increment;
- `REJECTED` before dispatch with structured reason;
- `SKIPPED` when prerequisites/policy disable the transform.

Pipeline diagnostics must verify the returned decision against generation before/after. A transform claiming mutation without a generation change should fail in tests and preferably raise an internal invariant error in development/debug paths.

### 3. Ordered transforms

The required default order is:

1. protocol-native/transcoded provider payload establishment;
2. selected-provider thinking control normalization;
3. selected-provider/post-route compression policy resolution required by cache synthesis;
4. synthetic cache control mutation;
5. stream-options/include-usage injection;
6. final serialization/freeze.

If current compression application occurs earlier, preserve it and document the exact ownership/order. This phase must not alter output semantics solely to achieve a cleaner diagram.

### 4. Context compatibility

`ProxyRequestContext.body_for_upstream` should delegate to the provider-bound request after it exists. Legacy `upstream_body` may remain temporarily for compatibility during migration, but there must be one authoritative write path at phase completion.

Remove or deprecate direct context writes from migrated transforms. Tests must fail if provider-bound and context bytes diverge.

## Operation-count instrumentation

Add test-only or bounded runtime diagnostics capable of counting:

- client JSON decodes;
- provider payload decodes;
- provider payload encodes;
- payload generation changes;
- transforms run/skipped/mutated/rejected.

Do not add high-cardinality production telemetry. Counters may be attached to request diagnostics or injectable codec wrappers in tests.

## Implementation sequence

### Workstream A — Characterize payload parity and operation counts

Capture current request outputs and decode/encode counts for:

- native OpenAI non-streaming/streaming;
- native Anthropic non-streaming/streaming;
- OpenAI→Anthropic transcode;
- Anthropic→OpenAI transcode;
- thinking controls present/absent;
- synthetic cache disabled/dry-run/apply;
- stream options absent/present/custom invalid type.

### Workstream B — Strengthen `ProviderBoundRequest`

Add generation-aware mutation, cache invalidation, freeze semantics, and immutable client payload guarantees.

### Workstream C — Migrate thinking adaptation

Make Plan 046 adaptation consume and mutate the provider-bound payload. Remove its independent byte decode/encode.

### Workstream D — Migrate synthetic cache/post-route mutation

Pass the same decoded payload and selected-provider context. Preserve segmentation/cache safety checks and only replace payload on validated structural diff.

### Workstream E — Migrate stream-options injection

Treat `stream_options.include_usage` as an ordinary final provider transform. Preserve invalid non-dict behavior by leaving it unchanged for upstream validation.

### Workstream F — Remove competing source of truth

Update request construction and `body_for_upstream`; remove dead/no-op adapters and redundant serialization helpers after all call sites migrate.

### Workstream G — Parity/performance verification

Compare captured final upstream bodies and operation counts against Workstream A baseline. Intentional fixes from Plan 046 must be separately identified.

## Required tests

### Lifecycle unit tests

- generation increments only on structural change;
- cached bytes reused when generation unchanged;
- mutation invalidates bytes;
- freeze rejects later mutation;
- client payload remains unchanged;
- mutation log is bounded;
- transform decision agrees with generation delta.

### Request-path integration tests

For every native/transcoded and streaming/non-streaming combination:

- capture exact semantic upstream JSON;
- assert one authoritative payload;
- assert no context/provider-bound divergence;
- assert transform ordering;
- assert final bytes correspond to final generation;
- assert reject paths perform no HTTP dispatch.

### Operation-count gates

The desired steady-state upper bounds are:

- one client decode per request;
- zero or one provider decode after prepared transcode, never one per transform;
- one final provider encode for the dispatched generation;
- no encode for local rejection before dispatch unless required for error diagnostics;
- no additional decode solely for stream-options injection.

If a legacy edge case requires a second decode, document it with a specific test and follow-up; do not silently make the general gate `<= 2`.

### Negative tests

- transform throws after mutation: request is not dispatched with partially frozen bytes;
- transform returns mutated without generation change: invariant fails;
- mutable alias changes payload without API: prevented/detected;
- prepared bytes refer to stale generation: re-encode before dispatch;
- invalid JSON remains handled at the existing client validation boundary.

## Acceptance criteria

- [x] `ProviderBoundRequest` is the actual object passed through provider transforms.
- [x] The no-op empty provider-bound wrapper is removed.
- [x] Thinking adaptation no longer independently parses/serializes provider bytes.
- [x] Synthetic cache mutation no longer independently parses/serializes provider bytes.
- [x] `stream_options.include_usage` injection no longer independently parses/serializes provider bytes.
- [x] There is one authoritative provider payload and one authoritative final byte representation.
- [x] Client payload/original bytes remain unchanged.
- [x] Final serialization occurs once for the dispatched generation.
- [x] Transform decisions truthfully match payload generation changes.
- [x] Prepared transcode reuse avoids unnecessary recomputation while remaining safe after provider-specific mutation.
- [x] Native/transcoded and streaming/non-streaming output parity is preserved except intentional Plan 046 corrections.
- [x] Operation-count diagnostics and lifecycle tests enforce the one-decode/one-encode contract.
- [x] No request is built from stale serialized bytes.
- [ ] Focused performance measurements show no regression in native dispatch and reduced JSON work in transformed paths.

## Explicit rejection conditions

Do not close Plan 050 if:

- adapters still mutate only `ProxyRequestContext` while receiving a dummy request;
- multiple fields can independently claim to be authoritative upstream bytes;
- transforms decode bytes because passing the decoded payload was inconvenient;
- `MUTATED` can be returned with no generation change;
- client and provider payload share mutable nested structures;
- operation counts are inferred from code inspection rather than measured tests;
- request output parity is not captured through real HTTP request construction;
- SSE parser consolidation is expanded into this phase.

## Handoff record

Record:

- implementation commit SHA;
- final provider-bound lifecycle diagram;
- removed legacy context mutation call sites;
- operation-count table before/after;
- semantic upstream-body parity matrix;
- focused benchmark results;
- any retained compatibility field and its removal plan.

## Definition of done

Plan 050 is complete when every provider-bound request transform operates on one authoritative decoded payload, mutation generations and serialized bytes cannot diverge, final dispatch performs one serialization after the last real mutation, client input remains immutable, and measured native/transcoded paths preserve capability while eliminating redundant JSON work.

# Plan 114 — Provider Payload Copy-on-Write Reduction

Date: 2026-08-11
Status: ready
Parent roadmap: `plans/113-sbc-hotpath-reduction-and-protocol-clarity-roadmap.md`
Planning baseline: `6f4df9bd42b5ca336d3da5ef458ab1793e515185`

## Purpose

Remove the highest-cost avoidable full-request copy from common OpenAI streaming dispatch and preserve path-level copy-on-write already produced by EggPool-owned transforms.

The target is not a generic persistent-data-structure system. The target is a narrow request-ownership contract:

- canonical client payload is read-only and never mutated;
- provider payload aliases canonical state until a provider-visible mutation is actually required;
- a small path mutation copies only the root and changed ancestors;
- EggPool-owned transformed graphs may be adopted without recursively rebuilding every unchanged subtree;
- `payload_generation` changes only when provider-visible content changes;
- native/no-op dispatch continues to reuse original client bytes.

## Current problem

`ProviderBoundRequest.mutate_provider_payload()` currently performs a `deepcopy(dict(self.client_payload))` when the provider payload has not yet been detached. The OpenAI streaming transform uses this helper to ensure `stream_options.include_usage` is enabled.

For a large request, changing a field such as:

```text
stream_options.include_usage
```

can therefore duplicate the full `messages`, `tools`, document metadata, and other request content.

The transform currently returns mutation semantics whenever the helper runs, even when the field already exists with the desired value. That needlessly bumps provider generation, invalidates caches, and forces serialization instead of preserving original client bytes.

Safe compression has the opposite design: it already performs path-level copy-on-write and preserves untouched subtrees. Passing that result through recursive provider-bound ownership rematerialization loses much of this benefit.

## Governing constraints

1. Do not alter routing, selection, retry, finalization, response handoff, database, rehash, or provider-pool behavior.
2. Do not introduce a third-party immutable/persistent collection library.
3. Do not introduce object pools, arenas, custom allocators, weak-reference ownership, or a general transformation graph.
4. Canonical client payload must remain unmodified in all cases.
5. A provider transform may mutate only provider-owned containers.
6. Sharing immutable/read-only child subtrees from the canonical graph is allowed only while no provider code can mutate those children in place.
7. Any helper that exposes a mutable dict to arbitrary/legacy code must first establish safe ownership of the portion that code may mutate.
8. `payload_generation` must increment exactly once per provider-visible structural mutation, not per transform invocation.
9. Provider bytes must be invalidated whenever provider-visible content changes and preserved when nothing changes.
10. `serialize_provider_payload()` must keep the original-client-byte fast path when `mutated == False`.
11. Retry after initial serialization must continue reusing the frozen provider generation without rerunning transforms.
12. Do not broaden changes into PreparedTranscode physical freezing; Plan 115 owns that follow-up.
13. Do not add permanent memory telemetry or benchmark infrastructure.
14. Keep all request-content diagnostics content-private.

## Workstream A — Inventory mutation APIs and callers

Before editing, locate all production callers of:

- `mutate_provider_payload()`;
- `replace_provider_payload()`;
- `set_provider_payload()`;
- `provider_payload_copy()`;
- `_owned_json_value()`;
- `serialize_provider_payload()`;
- safe compression `transformed_payload` adoption;
- provider-bound transcode adjustments and synthetic cache insertion.

Classify each caller as:

1. no-op normalization candidate;
2. small top-level/path mutation;
3. EggPool-owned already-detached transformed graph;
4. arbitrary legacy mutator requiring full ownership;
5. read-only consumer.

Do not create a permanent inventory artifact. Record only the resulting implementation decisions in this plan's closure.

## Workstream B — Make stream-options normalization truthful and path-local

### Required behavior

For OpenAI streaming requests:

- if `stream_options` is a mapping and `include_usage` is already present with the required value, the transform returns passthrough/no-op;
- if `stream_options` is absent, create a provider-owned root dict plus a new `stream_options` dict only;
- if `stream_options` exists but lacks `include_usage`, copy the root and `stream_options` mapping only, then add the field;
- do not deep-copy `messages`, `tools`, or other unrelated child subtrees;
- if an invalid/unsupported `stream_options` shape is intentionally left unchanged by current behavior, preserve that existing compatibility rather than silently replacing it unless a focused existing contract says otherwise.

### Preferred implementation shape

Prefer one narrow API such as:

```text
replace_top_level_field(...)
mutate_top_level_mapping(...)
adopt_provider_payload(...)
```

or equivalent local helpers. Avoid creating a generic JSON patch engine.

A practical implementation may shallow-copy the root dict and shallow-copy the nested mapping being changed. Unchanged child values can remain shared because they are treated as read-only.

### No-op semantics

The provider-bound mutation API used here must be able to report `False` when the resulting provider-visible payload is semantically unchanged.

Do not implement no-op detection by comparing `dict(self.provider_payload) == dict(candidate)` for a multi-megabyte graph after every tiny mutation; that simply replaces a deepcopy with another O(payload-size) walk. The mutator/caller should know whether it changed the target path.

## Workstream C — Add an explicit trusted-adoption boundary for EggPool-owned transformed graphs

The current provider-bound setter recursively rematerializes mappings/lists so the provider request owns an ordinary mutable graph. This is safe but expensive for outputs that are already created by EggPool with a known ownership contract.

Introduce the smallest explicit distinction between:

- **copy/own**: caller supplies a graph whose aliasing/ownership is unknown;
- **adopt**: caller supplies an EggPool-created provider graph whose root/path mutation behavior is already controlled.

The adopted graph may still share untouched canonical subtrees if those subtrees remain read-only and future provider transforms use copy-on-write before mutating them.

Name this boundary clearly. Examples of acceptable intent-signaling names:

```text
adopt_provider_payload(...)
set_owned_provider_payload(...)
```

Do not overload an existing ambiguous setter with a boolean such as `trust=True` unless that is clearly the smallest maintainable change.

## Workstream D — Preserve safe-compression path-level COW

Trace the output contract of `apply_safe_compression()`.

When compression returns a transformed payload whose changed ancestors are already copied and whose unchanged subtrees are deliberately shared read-only:

- adopt it through the trusted provider-bound path;
- do not recursively rebuild all child dictionaries/lists;
- retain the compressor's existing stable-prefix/cache-boundary safety checks;
- ensure any later provider transform that wants to mutate a shared child path copies that path before modification.

If safe compression currently returns a graph that cannot guarantee this ownership contract, make the smallest local compressor-output adjustment necessary rather than restoring a whole-payload deepcopy.

## Workstream E — Reduce or remove whole-graph equality/rematerialization on changed paths

`replace_provider_payload()` currently performs broad mapping equality checks and `_owned_json_value()` recursively walks the graph.

For touched production paths:

- prefer caller-known changed/no-change information;
- avoid whole-payload equality when the transform already knows it changed a specific field;
- avoid recursively reconstructing an already-owned graph;
- retain a conservative copy path for callers where ownership cannot be proven.

Do not force every legacy call site through the new optimized path in one pass. It is acceptable to keep a clearly named conservative helper for rare/unknown transformations.

## Workstream F — Mutation/generation/serialization invariants

After changes, verify explicitly:

1. `mutated == False` until provider-visible content differs from client content;
2. no-op stream normalization leaves generation at zero and provider bytes eligible for original-byte reuse;
3. first real mutation increments generation once;
4. second real mutation increments it once again;
5. serialization stores bytes for the exact current generation;
6. mutation after non-frozen serialization invalidates stale bytes;
7. mutation after freeze is rejected according to existing retry/dispatch rules;
8. `release_dispatch_buffers()` still releases canonical/provider references only after the lifecycle no longer needs them.

Do not change `release_dispatch_buffers()` scope except as required to keep the ownership model consistent.

## Focused tests

Use existing provider-bound/transform/compression suites. Add or consolidate tests around semantic contracts rather than container implementation types.

At minimum cover:

- native non-streaming request: provider payload aliases canonical and original bytes are reused;
- OpenAI streaming request with `stream_options.include_usage = true`: no mutation, no generation change, original bytes reused;
- OpenAI streaming request with missing `stream_options`: resulting wire payload contains `include_usage=true`, canonical payload unchanged;
- OpenAI streaming request with existing `stream_options` lacking `include_usage`: only provider view changes;
- unchanged `messages` and `tools` top-level objects preserve identity through the small path COW where safe to assert in a focused test-local instrumentation test;
- mutating a provider-owned nested path cannot mutate canonical content;
- safe-compression transformed payload retains unchanged subtree identity where its contract allows and serializes correctly;
- retry/frozen request does not rerun/redo transforms;
- no-op transform reports passthrough rather than `MUTATED`;
- provider bytes are invalidated only on actual content change.

Identity assertions should be limited to proving the intended copy boundary in focused ownership tests; do not build broad tests that couple unrelated code to private container types.

## Verification

Run focused ownership/transform/compression tests first. Likely owning suites include the existing provider-bound request tests, transform pipeline tests, streaming request tests, and safe-compression tests. Use current paths discovered by `rg` rather than creating plan-numbered tests.

Then run:

```bash
uv run ruff format --check src/ tests/ scripts/
uv run ruff check src/ tests/ scripts/
uv run pyright src/ scripts/
PYTHONHASHSEED=0 TZ=UTC uv run pytest tests/smoke/ -q --tb=short --maxfail=1
uv run eggpool --config config.example.toml check-config
uv run eggpool --config config.sbc.example.toml check-config
```

No full retained-suite requirement.

## Explicit acceptance criteria

- [ ] OpenAI streaming requests with already-correct `stream_options.include_usage` do not call a whole-graph deepcopy/rematerialization path.
- [ ] Already-correct stream options return a transform decision of passthrough/no-op.
- [ ] Already-correct stream options do not set `mutated=True` or increment `payload_generation`.
- [ ] Already-correct stream options preserve original client-byte dispatch when no other transform changes the request.
- [ ] Missing/partial stream options are changed using path-level/shallow copy-on-write rather than a full `deepcopy()` of `messages`/`tools`.
- [ ] Canonical client payload is unchanged after stream-options insertion.
- [ ] The provider-bound API has an explicit distinction between conservative copy/ownership and trusted adoption of EggPool-owned transformed graphs, or an equivalently clear narrow contract.
- [ ] Safe-compression output is not recursively rematerialized solely to enter provider-bound ownership.
- [ ] Safe compression continues to preserve stable-prefix/cache-protected behavior.
- [ ] Any later provider mutation of a shared subtree establishes ownership before mutation, preventing alias leakage into the canonical graph.
- [ ] Whole-payload equality checks are not introduced on the common path as a substitute for deepcopy.
- [ ] `payload_generation` changes exactly when provider-visible content changes.
- [ ] Cached serialized provider bytes correspond to the current generation and are invalidated after real mutation.
- [ ] Retry/frozen dispatch semantics remain unchanged.
- [ ] `release_dispatch_buffers()` remains lifecycle-safe.
- [ ] No routing/finalization/database/rehash behavior is changed.
- [ ] No new runtime dependency or generalized copy-on-write framework is introduced.
- [ ] Focused ownership/stream/compression regressions pass.
- [ ] Ruff, Pyright, 14 smoke tests, and both config checks pass.

## Rejection conditions

Reject the implementation if:

- canonical and provider payloads can share a mutable container that provider code mutates in place;
- a shallow-copy optimization allows nested provider edits to change the canonical request;
- no-op detection requires a full recursive equality walk on every streaming request;
- a generic JSON patch/persistent-data-structure framework is added;
- original-byte reuse is disabled for all requests for architectural convenience;
- generation increments on transform invocation rather than actual provider-visible change;
- compression correctness is weakened to obtain identity sharing;
- retry/freeze semantics are changed;
- new permanent memory instrumentation or CI benchmarks are added.

## Handoff sequence

1. Read Plan 113, this plan, `ProviderBoundRequest`, transform pipeline, proxy request construction, safe-compression apply code, and directly owning tests.
2. Inventory all mutation/adoption callers.
3. Correct stream-options no-op behavior first with focused tests.
4. Introduce the narrow copy-versus-adopt ownership boundary.
5. Route safe-compression output through the adopted path and verify canonical isolation.
6. Remove broad equality/rematerialization only where caller-known semantics make it safe.
7. Run generation/serialization/retry ownership tests.
8. Run the ordinary gate.
9. Record implementation SHA, the final ownership API contract, which production paths still intentionally use conservative full ownership, and exact verification results.
10. Stop. PreparedTranscode physical freeze/rematerialization is Plan 115.

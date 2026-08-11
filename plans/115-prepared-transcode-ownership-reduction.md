# Plan 115 — Prepared Transcode Ownership Reduction

Date: 2026-08-11
Status: complete
Parent roadmap: `plans/113-sbc-hotpath-reduction-and-protocol-clarity-roadmap.md`
Planning baseline: `6f4df9bd42b5ca336d3da5ef458ab1793e515185`
Depends on: `plans/114-provider-payload-copy-on-write.md`

## Purpose

Remove recursive physical freeze/rematerialization from request-local prepared transcodes while preserving immutable-client semantics, encoded-body reuse, provider-specific post-selection adjustments, and retry/freeze correctness.

The current prepared-transcode path exists for a good reason: translation done during preflight should not be repeated after provider selection when the same translation is still valid. The problem is the physical ownership mechanism, not the reuse idea.

The desired lifecycle is:

```text
client bytes/payload
    -> one preflight translation
    -> one translated payload generation + encoded body
    -> reuse directly when provider-specific changes are unnecessary
    -> bounded copy-on-write only if later selected-provider normalization changes fields
    -> serialize one new provider generation only when changed
```

Do not turn this into a cross-request transcode cache.

## Current problem

`PreparedTranscode.__post_init__()` recursively walks the translated payload and warnings, converting mappings to `MappingProxyType` and lists to tuples. When reused, the provider-bound request later rematerializes that mapping into ordinary dict/list structures through the provider ownership setter.

For large translated coding-agent requests, this can add two full object-graph walks and substantial temporary allocation after the transcoder has already allocated the translated graph and encoded it.

The prepared object is request-local. It does not need deep physical immutability if ownership and mutation boundaries are explicit.

## Governing constraints

1. Plan 114's provider-bound copy/adopt contract is authoritative; do not create a competing ownership model.
2. Do not create a global or cross-request transcode cache.
3. Do not retain translated request graphs beyond the request lifecycle.
4. Do not add persistent immutable-collection dependencies.
5. Client/canonical request payload remains unmodified.
6. Prepared translated payload must not be mutated in place by later provider-specific transforms if it is still being treated as the reusable prepared generation.
7. Reuse validity remains based on protocol/features/capability/policy semantics required by current code; simplification may remove only redundant representation checks, not semantic invalidation.
8. Provider-specific thinking/budget/cache/stream adjustments that genuinely require a different provider body must still recompute/mutate correctly.
9. Already encoded translated bytes must be reused when the prepared generation is dispatched unchanged.
10. Once dispatch is frozen, retry uses the same provider-visible bytes/generation.
11. Warnings remain bounded metadata and content-private.
12. Do not change the transcode semantic mapping tables in this plan except for incidental compatibility with the ownership API; Plan 117 owns cache dialect semantics.
13. No new permanent telemetry or benchmark harness.

## Workstream A — Re-establish the minimal PreparedTranscode contract

Before editing, document in code/tests the actual request-local contract required from `PreparedTranscode`:

- source/client protocol;
- upstream protocol used during preflight;
- translated payload;
- translated encoded body;
- warnings;
- tool token padding;
- loss policy used;
- feature/capability validity state required to know whether reuse is legal;
- mutable diagnostics allowed to record reused/recomputed state.

Delete physical representation requirements from the contract unless a production caller genuinely depends on them.

The contract should be semantic: callers cannot mutate the prepared generation through supported APIs. It need not state that every child list is a tuple or every mapping a proxy.

## Workstream B — Remove recursive `_freeze_json_value()` from request creation

Preferred result:

- `PreparedTranscode.from_preflight_result()` stores the translated payload produced by the transcoder without recursively rebuilding it;
- warnings may be normalized cheaply if needed, but avoid recursively freezing warning values unless they contain mutable nested data that is later exposed to mutating code;
- remove `MappingProxyType`/tuple conversion if no surviving production invariant needs it;
- remove tests whose only purpose is asserting proxy/tuple representation and replace them with mutation-isolation behavioral tests.

If the translated graph is stored as an ordinary dict/list, the code must ensure later transforms do not receive it for arbitrary in-place mutation. Plan 114's adopt/COW boundary should be used for this.

## Workstream C — Reuse translated bytes without rematerializing the translated graph

When a prepared transcode is valid for the selected provider/model and no provider-specific body mutation is required:

- dispatch the already encoded `translated_body` as the provider bytes;
- attach/adopt the prepared payload only if downstream read-only consumers need it;
- do not recursively rebuild the payload solely because provider-bound state expects ordinary mutable containers;
- do not serialize it again;
- mark diagnostics as reused.

If the provider-bound lifecycle can safely operate with bytes plus a read-only semantic payload reference for this case, prefer that over forcing a mutable owned graph.

Do not introduce a separate byte-only request class.

## Workstream D — Provider-specific mutation after prepared transcode

Some selected-provider adjustments make the preflight translation not directly dispatchable, especially capability-specific thinking/budget or cache controls.

For such cases:

1. start from the prepared translated payload generation;
2. use Plan 114's copy-on-write/owned mutation boundary;
3. copy only the paths actually changed where practical;
4. preserve untouched translated subtrees by read-only sharing;
5. invalidate the prepared encoded body for the new provider generation;
6. serialize the final provider generation once;
7. do not mutate the `PreparedTranscode` source object in place.

If a current adjustment legitimately rewrites a broad fraction of the translated body, one provider-owned full copy is acceptable. Record such cases explicitly in the closure rather than building a generic optimizer.

## Workstream E — Simplify validity fingerprinting only where safe

`PreparedTranscode` currently fingerprints `TranscoderFeatures` through JSON serialization + SHA-256. This cost is small compared with whole-payload copying, but the contract can be simplified if the active generation already owns immutable feature configuration.

Audit whether a small immutable tuple/value key can replace string JSON + SHA hashing, for example a tuple of relevant booleans/protocol generation values.

Only make this change if it clearly reduces code/work without making the invalidation contract harder to understand. Do not create a new capability-fingerprint framework.

The important correctness requirement is that a prepared result is reused only when the selected upstream protocol and relevant transcode feature/capability semantics match the preflight result.

## Workstream F — Buffer lifetime

Verify the final request lifecycle does not unnecessarily retain all of the following after downstream handoff:

- original client bytes;
- canonical client payload;
- prepared translated payload;
- prepared translated body;
- provider-owned mutated payload;
- provider serialized bytes.

Plan 107 already introduced dispatch-buffer release. Integrate the simplified PreparedTranscode ownership with that existing release boundary rather than creating a second release mechanism.

After response handoff/retry impossibility, references needed only for dispatch preparation should be releasable while finalization/usage accounting continues with bounded metadata.

## Focused tests

At minimum cover:

- prepared transcode construction does not recursively convert the translated graph to mapping proxies/tuples;
- modifying provider-owned state after reuse cannot mutate the prepared source generation;
- prepared source payload cannot be mutated through the supported provider-bound API;
- valid prepared transcode with no provider-specific mutation reuses `translated_body` exactly and performs no second encode;
- valid prepared transcode does not perform a recursive ownership-rematerialization walk solely for dispatch;
- thinking/budget case that requires provider-specific recompute produces correct final provider payload and does not modify the prepared source;
- capability/features mismatch still recomputes rather than reusing stale translation;
- retry after frozen dispatch uses identical provider bytes and does not rerun translation;
- release path drops large prepared references when no longer needed;
- warnings/loss behavior remains unchanged.

A test-local counter/monkeypatch may count `_owned_json_value`/freeze/encode calls to prove that the eliminated path is not invoked. Do not retain a production counter for this purpose.

## Verification

Run the existing prepared-transcode, provider-bound, coordinator transcode, thinking-budget, cache-transcode, and retry/freeze focused suites that own these semantics.

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

- [x] `PreparedTranscode` no longer recursively freezes a full translated request graph into `MappingProxyType`/tuple structures solely for request-local immutability.
- [x] Prepared-transcode reuse no longer recursively rematerializes that full graph solely to cross into provider-bound ownership.
- [x] A valid unchanged prepared transcode dispatches the existing encoded translated body without a second encode.
- [x] A valid unchanged prepared transcode performs no full graph copy after preflight solely for dispatch ownership.
- [x] Provider-specific post-selection changes use Plan 114's safe ownership/COW boundary.
- [x] Provider-specific mutation cannot mutate the prepared source generation or canonical client payload.
- [x] A provider-visible mutation after preflight invalidates the prepared bytes for the new generation and serializes the final body exactly once.
- [x] Reuse validity still rejects protocol/feature/capability states that require recomputation.
- [x] Thinking/budget override semantics remain correct.
- [x] Loss-policy and warning semantics remain correct.
- [x] Retry/frozen dispatch continues using the exact already-selected provider generation.
- [x] Large prepared request references are released by the existing dispatch-buffer lifecycle when no longer needed.
- [x] The existing deterministic feature fingerprint is retained; no new fingerprint framework was introduced.
- [x] No cross-request transcode cache, immutable collection dependency, new request class hierarchy, or permanent memory instrumentation is added.
- [x] Focused transcode/ownership/retry tests pass.
- [x] Ruff, Pyright, 14 smoke tests, and both config checks pass.

## Rejection conditions

Reject the implementation if:

- removal of physical freezing exposes a mutable prepared dict directly to arbitrary in-place transforms;
- provider-specific mutation can change the prepared source and thereby corrupt retry/reuse behavior;
- the implementation solves copies by parsing translated bytes again for every selected attempt;
- encoded translated bytes are discarded and reserialized unconditionally;
- a global/cross-request transcode cache is introduced;
- capability invalidation becomes weaker or protocol-incompatible prepared results can be reused;
- physical representation tests are retained by recreating unnecessary proxy/tuple internals;
- routing/finalization/database behavior changes.

## Handoff sequence

1. Read Plan 113, completed Plan 114 implementation/closure, this plan, `PreparedTranscode`, coordinator prepared reuse, provider-bound ownership, and owning tests.
2. Confirm the final Plan 114 adopt/COW API before designing this change.
3. Remove recursive physical freezing and representation-only tests.
4. Route unchanged prepared reuse directly through the existing encoded body.
5. Route provider-specific changes through COW/owned mutation without modifying prepared source.
6. Verify validity/recompute and retry/freeze semantics.
7. Verify dispatch buffer release.
8. Run focused suites and ordinary gate.
9. Record implementation SHA, final PreparedTranscode semantic contract, any remaining intentional full-copy cases, and exact verification results.
10. Stop. Do not broaden into provider cache semantics or Responses API work.

## Implementation closure

Implemented in commit `3921442` on `main`.

- `PreparedTranscode` now retains the transcoder-produced request-local
  translated graph and shallow-copies only bounded warning roots. The
  deterministic feature fingerprint remains the existing JSON/SHA-256 value;
  no capability-fingerprint framework was added.
- Valid unchanged reuse adopts the prepared graph through
  `ProviderBoundRequest.adopt_provider_payload()` and installs the exact
  prepared encoded body. The common serializer freezes a current cached
  generation on cache hit so retry continues to use the same provider bytes.
- Selected-provider budget normalization now inspects the existing thinking
  mapping before copying and uses the existing top-level COW path only when a
  budget field changes. Provider-control adaptation and synthetic cache
  synthesis intentionally retain one conservative provider-owned full copy for
  their broad/legacy transforms.
- Prepared references are cleared by `ProxyRequestContext.release_dispatch_buffers()`;
  no cross-request cache or new request class was added. Canonical and prepared
  source graphs remain isolated from supported provider-bound mutations.

Verification completed locally:

- Focused prepared/provider-bound/transform/thinking/cache/transcode suites:
  230 passed.
- `uv sync --frozen --extra ci`: passed.
- `uv run ruff format --check src/ tests/ scripts/`: passed.
- `uv run ruff check src/ tests/ scripts/`: passed.
- `uv run pyright src/ scripts/`: 0 errors, 0 warnings, 0 informations.
- `PYTHONHASHSEED=0 TZ=UTC uv run pytest tests/smoke/ -q --tb=short --maxfail=1`:
  14 passed.
- `check-config` passed for `config.example.toml` and
  `config.sbc.example.toml`.

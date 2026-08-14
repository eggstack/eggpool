# Plan 124 — Cross-Protocol Request-Memory Reduction

Date: 2026-08-14
Status: ready
Parent roadmap: `plans/122-post-audit-correctness-and-sbc-simplification-roadmap.md`
Planning baseline: `c17bb84af6d737a8408cbcce4d2746caedee36e8`
Priority: P1 performance/resource proportionality
Execution target: GPT-5.6 Luna or comparable implementation model

## Purpose

Remove the last reviewed avoidable whole-request copy in the cross-protocol path
and reduce multimodal validation peak memory where this can be done with small,
obviously correct code.

Roadmap 113 and Plan 121 already established the desired ownership model:

- native unchanged requests reuse accepted client bytes;
- provider mutations use path-level copy-on-write where known;
- PreparedTranscode stores one request-local translated generation;
- retries reuse frozen provider bytes;
- thinking-control adaptation no longer recursively copies the whole request.

The remaining reviewed seam is the protocol-transcode recompute path in
`RequestCoordinator`, which still calls `ProviderBoundRequest.provider_payload_copy()`.
That helper recursively `deepcopy()`s the entire request graph. The transcoder
largely constructs a new target graph anyway, so this may be redundant.

A secondary concern is multimodal base64 validation: image/PDF paths can retain
original request bytes, encoded base64 strings, decoded bytes, and translated
output references simultaneously. On SBCs, large valid media can therefore
produce a larger transient peak than ordinary text/tool requests.

This is not permission to build a streaming JSON parser, zero-copy binary
framework, or custom base64 library.

## Governing constraints

1. Preserve canonical client payload immutability and PreparedTranscode source
   immutability.
2. Preserve exact provider-body generation/freeze/retry semantics.
3. Do not change OpenAI↔Anthropic protocol semantics except where Plan 123 has
   already changed reasoning controls.
4. Do not alter body-size limits, provider-specific image/PDF size limits, or
   accepted media types without a separate correctness reason.
5. No new runtime dependency.
6. No custom streaming JSON parser, incremental DOM, rope, arena, slab allocator,
   mmap request storage, temp-file spill system, or binary side channel.
7. Test-local allocation/call-count/identity instrumentation is allowed.
   Production allocation telemetry is not.
8. Do not optimize native no-op paths that already reuse original bytes unless a
   concrete regression is found.
9. Do not change HTTPX provider pools, Granian threads, SQLite, routing,
   finalization, or rehash.
10. Stop if the remaining copy is required by a demonstrated mutable-caller
    contract; document the reason instead of replacing it with unsafe sharing.

## Workstream A — Re-audit `provider_payload_copy()` and replacement callers

Inventory current production callers of:

- `ProviderBoundRequest.provider_payload_copy()`;
- `replace_provider_payload()`;
- `set_provider_payload()`;
- `adopt_provider_payload()`;
- body transcoder `encode_request()` implementations;
- PreparedTranscode creation/recompute/reuse.

For each remaining `provider_payload_copy()` caller answer:

1. Does the callee mutate its input graph in place?
2. Does it retain references to input descendants after return?
3. Does it construct a fresh target root/messages/tools graph?
4. Is the source request already request-local and logically immutable?
5. Does any error/retry path depend on source mutations being isolated by a
   defensive deep copy?

Use source inspection and focused mutation tests. Do not assume a copy is
redundant merely because the normal path appears read-only.

## Workstream B — Remove redundant protocol-transcode deepcopy when safe

Preferred target contract:

```text
ProviderBoundRequest.provider_payload (read-only Mapping)
        |
        v
BodyTranscoder.encode_request(source_mapping, context, ...)
        |
        v
fresh/transcoder-owned output graph + warnings
        |
        v
adopt_provider_payload(...) / PreparedTranscode-owned generation
```

If audit confirms `encode_request()` does not mutate its source:

- pass a read-only `Mapping[str, Any]` directly;
- widen typing only where necessary to make read-only ownership explicit;
- ensure translator helpers do not call mutating methods on source mappings or
  nested source containers;
- construct target roots/lists/blocks as today;
- reuse unchanged immutable scalar/string descendants only when existing code
  naturally does so—do not add a generalized structural-sharing framework;
- remove `provider_payload_copy()` if no production caller remains;
- remove/retain `replace_provider_payload()` based on actual remaining callers,
  not an arbitrary deletion goal.

If one legacy caller genuinely mutates arbitrary nested input, isolate that
caller behind the conservative copy rather than keeping the hot transcode path
on it.

### Required invariants

- canonical client source is unchanged after successful transcode;
- canonical source is unchanged after rejected/failed transcode;
- PreparedTranscode source cannot be modified by later selected-provider
  adaptation;
- source message/tool dictionaries are never modified in place;
- retry uses the same frozen bytes and does not rerun the transcode merely
  because the defensive copy was removed;
- warning generation does not attach mutable metadata into source payloads.

## Workstream C — Avoid duplicate equality/rematerialization on translated output

Inspect whether the protocol-transcode recompute path performs any sequence like:

```text
fresh translated output
 -> whole-graph equality against provider payload
 -> recursive re-own/rematerialize
```

When the transcoder already returns a newly owned EggPool graph and the caller
already knows it represents a changed protocol generation, prefer the existing
trusted `adopt_provider_payload()` boundary over conservative
`replace_provider_payload()`.

Do not skip equality checks on paths where unchanged-versus-changed is not known.
Do not modify generation semantics simply to save one traversal.

## Workstream D — Multimodal base64 peak-memory audit

Inspect both translation directions for:

- `base64_definitely_exceeds()`;
- `decode_base64_payload()`;
- image data URI parsing;
- Anthropic base64 image sources;
- PDF/document base64 validation;
- translated data URI construction;
- duplicate `bytes`/`str` conversions.

For each media path, identify the largest simultaneously live representations:

1. raw HTTP request bytes;
2. parsed JSON string containing base64;
3. sliced/copied base64 string;
4. fully decoded binary bytes;
5. translated output string/container;
6. serialized provider body.

Do not add production instrumentation. A focused test may retain weakrefs,
monkeypatch decode helpers, or count exact decode calls.

## Workstream E — Use simple validation reductions only

Potential acceptable changes, only if they preserve validation semantics:

- reject obviously oversized base64 using encoded-length arithmetic before
  decoding;
- use `base64.b64decode(..., validate=True)` only at the point exact validation
  is actually required;
- avoid storing the decoded result when only validity and decoded length are
  needed, if stdlib APIs/simple chunking can do so without a large new helper;
- avoid reconstructing identical `data:` prefixes more than once;
- avoid needless `str()`/`bytes()` copies of already-correct types.

A small incremental validator is acceptable only if it is easy to audit and has
comprehensive edge-case tests for padding/invalid alphabet/size boundary. If the
implementation becomes more complex than retaining one bounded decoded media
object, **do not implement it**.

The default whole-request ceiling is finite. Complexity reduction matters more
than shaving a theoretical few MiB at the cost of a parser subsystem.

## Workstream F — Focused deterministic regression coverage

Use existing request/transcode suites.

### Large text/tool cross-protocol request

Construct a synthetic large request below the configured body limit with:

- long message history;
- nested tool schemas;
- no media;
- cross-protocol translation.

Verify:

- no `deepcopy`/`provider_payload_copy()` on the corrected production path;
- source root/messages/tools are unchanged;
- translated output is correct;
- PreparedTranscode encoded bytes are reused when unchanged;
- provider-specific later mutation does not alter the prepared source;
- retry after freeze reuses bytes.

### Transcode failure/rejection

Force a deterministic loss/capability rejection and prove source payload remains
unchanged and a later valid request is unaffected.

### Image/PDF size boundaries

For each supported media translation path:

- just-under size limit;
- exact limit where relevant;
- just-over limit rejected/dropped according to existing contract;
- invalid base64;
- obviously oversized encoded input rejected before full decode where implemented;
- URL-source behavior unchanged.

Do not include huge retained fixtures. Generate synthetic payloads in test code
or use compact helper generation.

## Workstream G — Optional local resource observation

If a representative host is available during implementation, one short local
comparison may record:

- peak RSS around a synthetic large cross-protocol request;
- local pre-upstream preparation latency;
- Python/json backend;
- request size.

This observation is contextual only. Do not create a benchmark script solely for
this plan, do not add thresholds, and do not claim improvement unless the same
workload is replayed against the exact pre-change baseline.

Plan 126 owns the real SBC/provider-backed characterization.

## Documentation cleanup

Update `AGENTS.md` request-memory/PreparedTranscode gotchas only if ownership
contracts change materially. Remove stale comments that direct new code toward
`provider_payload_copy()` if the helper is removed.

Do not add performance claims to README/docs from deterministic tests alone.

## Verification

Run focused suites covering:

- ProviderBoundRequest ownership;
- PreparedTranscode reuse/recompute;
- both body transcoders;
- media translation/limits;
- stream/non-stream retry reuse if touched.

Then ordinary gate:

```bash
uv run ruff format --check src/ tests/ scripts/
uv run ruff check src/ tests/ scripts/
uv run pyright src/ scripts/
PYTHONHASHSEED=0 TZ=UTC uv run pytest tests/smoke/ -q --tb=short --maxfail=1
uv run eggpool --config config.example.toml check-config
uv run eggpool --config config.sbc.example.toml check-config
```

## Explicit acceptance criteria

- [ ] Remaining production callers of `provider_payload_copy()` and
  `replace_provider_payload()` are inventoried before edits.
- [ ] Cross-protocol recompute no longer recursively deep-copies the full source
  request when the translator's read-only contract makes that copy redundant,
  or concrete evidence for retaining the copy is recorded.
- [ ] Translator input is explicitly treated as read-only; successful and failed
  transcodes cannot mutate canonical/prepared source state.
- [ ] Fresh translator-owned output is adopted without an unnecessary second
  whole-graph ownership pass when caller knowledge makes that safe.
- [ ] PreparedTranscode reuse, provider mutation, generation, serialization,
  freeze, retry, and buffer-release contracts remain unchanged.
- [ ] Multimodal validation rejects obvious oversize before full decode where
  simple and safe.
- [ ] Any exact-validation memory reduction is implemented only with small,
  auditable stdlib code; otherwise current bounded decoding is explicitly
  retained.
- [ ] Media size/invalid-base64/URL behavior remains protocol-correct.
- [ ] No new parser framework, dependency, runtime telemetry, database change,
  pool change, or CI expansion is introduced.
- [ ] Focused tests and ordinary gate pass.
- [ ] Implementation SHA, caller disposition, media-memory disposition, and exact
  verification are appended to this plan; no separate closure plan is created.

## Rejection conditions

Reject implementation if it:

- shares mutable source containers with code that can mutate them;
- removes the copy without proving translator read-only behavior;
- reintroduces recursive freeze/thaw or another generalized COW layer;
- creates a streaming JSON/base64 subsystem for bounded request bodies;
- weakens media validation to save memory;
- changes protocol semantics, request ceilings, provider pools, SQLite, or
  retry/finalization behavior;
- adds permanent memory benchmarks or CI thresholds.

## Handoff sequence

1. Read Roadmap 122, Plan 121 closure, this plan, `AGENTS.md`,
   `provider_bound_request.py`, coordinator transcode paths, both body
   transcoders, and owning tests.
2. Inventory copy/replacement callers and prove mutability assumptions.
3. Remove only the redundant protocol-transcode ownership work.
4. Re-run ownership tests before touching media validation.
5. Audit media peak representations and implement only simple safe reductions.
6. Run focused media/transcode/ownership tests and ordinary gate.
7. Update narrow ownership documentation if needed.
8. Append closure evidence to this file and stop.

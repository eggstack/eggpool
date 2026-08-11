# Plan 116 — Request Estimation and Ingress Efficiency

Date: 2026-08-11
Status: complete
Parent roadmap: `plans/113-sbc-hotpath-reduction-and-protocol-clarity-roadmap.md`
Planning baseline: `6f4df9bd42b5ca336d3da5ef458ab1793e515185`
Depends on: `plans/114-provider-payload-copy-on-write.md` for final request ownership conventions

## Purpose

Remove repeated O(request-size) admission/preflight work without weakening request-size, context-limit, quota-reservation, or transcode correctness.

This plan is intentionally narrower than a general parser/tokenizer optimization. EggPool should continue using the decoded JSON payload and its existing conservative estimators. The goal is to compute derived values once per relevant payload generation and reuse them instead of rescanning or reserializing the same large request multiple times.

## Current problem

The request handler parses JSON once and seeds `ParsedRequestPayload`, which is good. However, large requests can still perform repeated whole-payload work before upstream dispatch:

- `_check_context_limits()` calls `estimate_context_input_tokens(body, payload)` and recursively traverses the decoded graph;
- later the handler calls `estimate_context_input_tokens(body, payload)` again to populate coordinator context;
- transcoded preflight performs another context estimate on the translated payload;
- `_tool_token_padding()` serializes each translated tool separately to estimate token allowance even though the translated request body is encoded as a whole immediately afterward;
- request-body reading accumulates into a `bytearray` and then converts to `bytes`, creating a temporary body-sized copy; this is lower priority and should only be changed if a simple ASGI-safe improvement is available.

On SBC hardware, eliminating duplicated recursive Python traversal is more valuable and safer than adding a tokenizer dependency.

## Governing constraints

1. Do not add `tiktoken`, tokenizers, Rust extensions, C extensions, NumPy, or another token-counting dependency.
2. Preserve conservative context-limit rejection semantics.
3. Reservation token estimation and context-limit estimation are distinct policies; do not merge them if doing so changes quota behavior.
4. An estimate may be reused only for the exact payload/body generation it describes.
5. If transcode/compression/provider normalization materially changes model-visible content, compute or use the estimate appropriate to that transformed generation.
6. Do not cache estimates globally or across requests.
7. Do not add a generalized memoization framework.
8. Preserve the 10 MiB default request limit and configurable body-limit behavior.
9. Oversized requests must still be rejected before avoidable JSON/transcode work.
10. Do not replace Starlette/FastAPI request streaming with a custom server/body buffering stack.
11. Do not compromise request cancellation or HTTP keepalive draining behavior for oversized bodies.
12. Do not change routing/finalization/database semantics.
13. Temporary test-local counters are allowed; no permanent profiling instrumentation.

## Workstream A — Define request-generation derived estimates

Use the existing `ParsedRequestPayload` / `ProxyRequestContext` rather than adding a new cache subsystem.

Identify derived values that are pure functions of the canonical client request:

- model id;
- streaming flag;
- thinking requirement;
- reservation token estimate from original bytes;
- context-input token estimate from original body + decoded payload.

Ensure each is computed once and carried forward.

If `ParsedRequestPayload` already has suitable cached fields, extend/use those fields rather than adding a parallel `RequestEstimates` class unless the latter clearly deletes more complexity than it adds.

## Workstream B — Make context-limit enforcement consume/return the estimate

Refactor `check_context_limits()` so the caller can provide a precomputed input estimate or receive the computed estimate in a small explicit result.

Acceptable shapes include:

```text
check_context_limits(..., estimated_input_tokens=...)
```

or

```text
estimated = estimate_context_input_tokens(...)
check_context_limits(..., estimated_input_tokens=estimated)
```

Prefer the shape that keeps policy enforcement readable and avoids a new result hierarchy.

Required behavior:

1. canonical client estimate is computed once;
2. the same estimate is used for client-side context-limit enforcement and stored in request context for coordinator use;
3. translated payload receives its own estimate only when transcode preflight exists;
4. a later provider-bound mutation that does not materially alter token-bearing content need not trigger another estimate solely because `payload_generation` changed for metadata such as stream usage options;
5. provider/model-specific thinking-budget changes that alter requested output budget remain correctly accounted for by the existing output-token limit logic.

Do not tie token estimate validity mechanically to every provider generation if some provider-only metadata mutations do not affect model input. Use the narrow semantic dependency already known at the call site.

## Workstream C — Remove per-tool JSON serialization from rough padding

`_tool_token_padding()` currently serializes each tool definition to estimate extra input tokens for translated Anthropic tool schemas.

Audit what correctness role this padding serves. It is a conservative guardrail, not billing-grade token accounting.

Preferred change:

- use `estimate_text_tokens()` / the shared JSON structural estimator on the translated `tools` value;
- avoid separately encoding every tool object;
- preserve or slightly increase conservatism if necessary to prevent false acceptance near a context boundary;
- do not create another estimator implementation.

If tests demonstrate the byte-serialization formula captures an important safety relationship that the shared estimator cannot preserve, retain it and record that decision. Do not optimize by weakening context-limit protection.

## Workstream D — Avoid avoidable translated-body work

During transcode preflight, the translated body is encoded so it can be reused by PreparedTranscode and used for context-limit checks. This is legitimate.

Ensure the implementation does not additionally:

- re-encode the translated payload solely to estimate tool padding;
- decode the encoded translated body again for context checks;
- compute identical translated estimates more than once before provider selection.

Plan 115 owns prepared-body reuse after selection; this plan should expose the estimate cleanly so Plan 115 does not need another traversal.

## Workstream E — Audit request body/header copies, but change only trivial redundancies

### Body

`read_body_limited()` builds a `bytearray` then converts it to `bytes`. A one-body-size transient copy is expected with this simple bounded reader.

Investigate whether Starlette's request stream or a simple chunk-list/join approach is measurably/syntactically better for EggPool's typical body sizes. Do not replace the implementation unless all of the following are true:

- the alternative is simpler or equally simple;
- it preserves the hard byte bound during streaming;
- it preserves cancellation behavior;
- it preserves bounded drain-on-oversize behavior;
- it does not retain many small chunks longer than the current buffer in a way that increases peak memory;
- focused tests prove equivalent semantics.

It is acceptable and expected to close this sub-workstream as `no change justified`.

### Headers

Audit `incoming_headers=dict(request.headers)` and other request-header materialization. If the full copied header mapping is required later for provider forwarding/finalization, retain it. If only a bounded subset is needed after the request object is available, narrow the retained copy.

Never retain Authorization/API-key values in additional diagnostics. Redaction/security behavior is protected.

## Workstream F — ASCII/non-ASCII estimator hot path

The shared estimator already has an `isascii()` fast path. Preserve it.

Do not micro-optimize character loops without target evidence. If non-ASCII estimation is a material hotspot in target workloads, a later focused issue can address it. This roadmap's expected coding-agent prompts are predominantly ASCII/code.

## Focused tests

At minimum cover:

- canonical context estimate helper is invoked once for a normal request with model limit enforcement enabled;
- no-limit/no-enforcement path does not perform unnecessary extra estimation beyond what coordinator admission actually needs;
- canonical estimate is reused in `ProxyRequestContext`;
- translated request computes one translated estimate and enforces upstream limits correctly;
- client and translated estimates remain distinct;
- stream-options metadata mutation does not force another model-input estimate solely because it changes provider generation;
- reservation estimate behavior remains bounded and unchanged;
- tool-padding replacement remains conservative on representative small/large tool schemas;
- boundary case that previously rejects for context overflow still rejects;
- boundary case below the limit still succeeds;
- oversized body rejection still occurs before JSON/transcode work;
- malformed/absent Content-Length streaming body still enforces the byte ceiling;
- if body-reader changes, cancellation/drain/keepalive behavior retains existing focused contracts.

Use test-local monkeypatch/counters to prove call count where useful. Do not add production counters.

## Verification

Run request-limits, proxy-request admission, transcode preflight, body-limit, thinking/context, and relevant smoke tests.

Then run:

```bash
uv run ruff format --check src/ tests/ scripts/
uv run ruff check src/ tests/ scripts/
uv run pyright src/ scripts/
PYTHONHASHSEED=0 TZ=UTC uv run pytest tests/smoke/ -q --tb=short --maxfail=1
uv run eggpool --config config.example.toml check-config
uv run eggpool --config config.sbc.example.toml check-config
```

No full retained-suite requirement and no permanent benchmark.

## Explicit acceptance criteria

- [x] The canonical decoded request context-input estimate is computed no more than once during ordinary admission for a given canonical request.
- [x] That canonical estimate is reused for client-side context-limit enforcement and coordinator request context.
- [x] A transcode preflight computes at most one translated model-input estimate for the translated generation.
- [x] Canonical and translated estimates are never accidentally interchanged.
- [x] Reservation-token behavior remains bounded by the existing reservation policy and is not silently replaced by context estimation.
- [x] Context-limit rejection behavior remains correct for configured max input/output/context limits.
- [x] Metadata-only provider mutations such as stream usage options do not force another full model-input traversal without a semantic reason.
- [x] `_tool_token_padding()` no longer JSON-serializes each tool independently; the shared structural estimator remains conservative on representative small and large schemas.
- [x] No new tokenizer/runtime dependency is introduced.
- [x] No global or cross-request estimate cache is introduced.
- [x] Oversized request bodies are still rejected before avoidable parse/transcode work.
- [x] The default 10 MiB body ceiling and rehash/config semantics remain unchanged.
- [x] The existing body reader is explicitly retained because it preserves bounded streaming, cancellation, drain, and keep-alive behavior without a more valuable simpler replacement.
- [x] The full incoming-header snapshot is retained because downstream forwarding and finalization still require it; authentication/redaction behavior is unchanged.
- [x] Focused limit/admission/transcode/body tests pass.
- [x] Ruff, Pyright, 14 smoke tests, and both config checks pass.

## Rejection conditions

Reject the implementation if:

- estimator reuse permits stale estimates after a transform that materially changes model-visible content;
- the context-limit guard becomes less conservative solely for speed;
- reservation accounting changes unexpectedly;
- a tokenizer/native extension is added;
- a generalized memoization/cache framework is added;
- request body buffering becomes unbounded or no longer drains oversized HTTP/1.1 bodies safely;
- a body-reader rewrite is substantially more complex than the copy it removes;
- authorization or sensitive headers are retained/logged more broadly;
- CI gains benchmark/performance gates.

## Handoff sequence

1. Read Plan 113, completed Plan 114, this plan, request limit helpers, proxy request handler, ParsedRequestPayload, transcode preflight, and body reader.
2. Add test-local call-count coverage for the duplicate canonical estimate.
3. Refactor canonical estimation so enforcement and coordinator reuse one value.
4. Reuse one translated estimate in preflight.
5. Replace per-tool serialization only if the shared estimator preserves conservative semantics.
6. Audit body/header copies; make only trivial, clearly safe reductions.
7. Run focused tests and ordinary gate.
8. Record implementation SHA, before/after estimate/serialization call behavior, body-reader disposition, and exact verification results.
9. Stop. Do not expand into tokenizer accuracy or provider billing accounting.

## Implementation closure

Implementation commit: recorded in the follow-up closure commit after the
implementation commit is created.

The request handler now receives the canonical context estimate from
`check_context_limits()` and carries that exact value into
`ProxyRequestContext`. A model with no enforced input/context/output limit
does not perform that decoded-payload walk, because routing admission uses the
separate bounded reservation estimate. The translated preflight keeps its
encoded body and validates the translated generation once with its own
`extra_input_tokens` allowance; it never reuses the canonical estimate.

Before/after request-preparation behavior:

- Enforced canonical requests: two decoded context-estimator calls (limit
  guard plus context construction) became one.
- Unbounded/no-enforcement canonical requests: the former context-construction
  walk is omitted; reservation estimation remains unchanged and bounded.
- Translated tool padding: one `dumps_bytes()` call per tool became one shared
  decoded structural-estimator walk over the tools list. The retained minimum
  and large-schema regression preserve the previous conservative guardrail.
- Body/header ingress: `read_body_limited()` and the full incoming-header
  snapshot were audited and intentionally unchanged for bounded draining,
  keep-alive, forwarding, finalization, and redaction contracts.

Verification completed locally:

- `uv sync --frozen --extra ci` — passed.
- Focused Plan 116 union — 108 passed; prepared-transcode padding — 19
  passed.
- `uv run ruff format --check src/ tests/ scripts/` — passed.
- `uv run ruff check src/ tests/ scripts/` — passed.
- `uv run pyright src/ scripts/` — 0 errors, 0 warnings, 0 informations.
- `PYTHONHASHSEED=0 TZ=UTC uv run pytest tests/smoke/ -q --tb=short
  --maxfail=1` — 14 passed.
- `uv run eggpool --config config.example.toml check-config` — passed.
- `uv run eggpool --config config.sbc.example.toml check-config` — passed.

No tokenizer dependency, global estimate cache, custom body-buffer stack,
benchmark gate, or permanent profiling instrumentation was added.

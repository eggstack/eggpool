# Plan 107 — Request Memory and Body-Limit Reduction

Date: 2026-08-11
Status: complete
Parent roadmap: `plans/103-sbc-protocol-parity-and-runtime-efficiency-roadmap.md`
Planning baseline: `de3eeea5936c964ffa33b7939c791e98d35cfcbb`

## Purpose

Reduce EggPool-controlled CPU/allocation/RSS overhead for large prompt/tool/document requests on Raspberry Pi-class systems without weakening request immutability, retry correctness, transcode correctness, or failure isolation.

The highest-value targets are full-request object/byte duplication and lifetime retention, not another connection-pool, SQLite, event-loop, or framework rewrite.

This plan also makes the proxy request-body ceiling configurable and ensures provider document-size claims are subordinate to the effective proxy body limit rather than implying that an impossible-to-ingest payload is supported.

## Confirmed current costs

### 1. Central parsed request bypasses the shared hot-path JSON backend

`ParsedRequestPayload` retains original request bytes and a parsed object, but its parse path uses stdlib `json.loads()` directly even though `eggpool.jsonx` is the project's designated hot-path JSON backend and can use optional `orjson`.

### 2. Provider-bound payload ownership performs repeated full-graph work

`ProviderBoundRequest` currently uses recursive physical freeze/thaw behavior and deep-copying around provider transforms/serialization. For a multi-megabyte coding-agent history this can produce several complete object-tree walks and allocations before bytes are sent upstream.

### 3. Native/no-transform dispatch may still decode/re-encode unnecessarily

When client protocol, provider protocol, and provider-bound transforms do not require mutation, the original validated request bytes are already a valid upstream body. Re-serializing the parsed object is avoidable if no provider-specific field/header/body mutation requires a changed payload.

### 4. Long streams can retain dispatch-only buffers

The request context can retain original bytes, decoded payload, transformed payload, and provider bytes through the response lifecycle even after downstream handoff makes request retry impossible. Long coding-agent streams therefore keep request-side memory live much longer than necessary.

### 5. Body/document limits are inconsistent

The proxy has a fixed approximately 10 MiB body limit while transcode/document validation includes larger provider document limits such as the Anthropic PDF limit. A provider-specific document limit cannot override the lower whole-request ingestion ceiling.

### 6. Oversized base64 media checks can allocate before rejection

Where a translator decodes base64 content solely to determine decoded size/validity, an obviously over-limit encoded payload should be rejected using safe encoded-length arithmetic before allocating the full decoded value. Do not build a streaming base64 subsystem; just avoid the clearly unnecessary oversize decode.

## Governing constraints

1. Preserve the canonical single-event-loop process model and current HTTPX pools.
2. Preserve request-local failure isolation, provider/account routing, pre-handoff retry, generation-owned finalization, and startup repair.
3. Do not replace FastAPI/Starlette request handling, HTTPX, Pydantic, or JSON libraries.
4. Use existing `eggpool.jsonx`; do not add another JSON dependency.
5. Preserve exact canonical/client payload semantics. A provider transform must never mutate shared client/canonical state unexpectedly.
6. Prefer ownership/copy-on-write rules over recursive `MappingProxyType` freeze/thaw.
7. Do not create a generic immutable object framework or persistent data structure library.
8. Zero-copy/no-reencode is allowed only when the provider-bound body is byte-semantically identical to accepted client bytes.
9. Request buffers may be released only after all request-side retry/provider-transform/response-adaptation/accounting consumers are proven finished with them.
10. Keep the default body limit bounded for SBCs. Do not raise it substantially merely to match the largest upstream provider document limit.
11. A body-limit configuration change must validate cleanly through startup and live rehash if server config is reloadable under current architecture.
12. Do not add mmap/temp-file request spooling, disk body caches, shared memory, native extensions, another worker process, or a new dependency.
13. Do not change SQLite pragmas or persistence architecture in this plan.
14. No benchmark/RSS threshold becomes CI.

## Workstream A — Map request payload ownership and lifetime

Before changing data structures, locate all authoritative symbols/call sites:

```bash
rg -n \
  'class ParsedRequestPayload|ParsedRequestPayload\(|class ProviderBoundRequest|ProviderBoundRequest\(|provider_payload_copy|serialize_provider_payload|set_provider_payload|replace_provider_payload|provider_bound|parsed_payload|downstream_started|handoff' \
  src/eggpool tests
```

Produce a temporary ownership map answering:

- who owns original request bytes;
- when the first JSON parse occurs;
- whether canonical parsed payload is ever mutated;
- which provider transforms require mutation;
- whether any transform mutates nested objects in place;
- when provider bytes are materialized;
- which fields are still read after upstream response acceptance/downstream handoff;
- which finalization/metrics fields can be copied into small scalar metadata before large buffers are released.

Do not create a permanent ownership registry document. Record the final ownership rule in existing architecture/inline documentation if needed.

## Workstream B — Route central parsing through `eggpool.jsonx`

Replace direct stdlib JSON parsing on the request hot path with the shared backend abstraction.

Requirements:

- `ParsedRequestPayload` uses `eggpool.jsonx.loads()` or the existing canonical wrapper;
- behavior/errors remain normalized to EggPool's existing request parsing error contract;
- `EGGPOOL_JSON_BACKEND=stdlib|orjson|auto` continues to work;
- optional `orjson` now actually covers this central parse path;
- deterministic off-hot-path JSON hashing/config code may continue using stdlib where `AGENTS.md` permits it.

Add focused backend-equivalence tests for representative object/string/number/Unicode payloads only where current `jsonx` coverage does not already prove them.

Do not duplicate backend tests across every proxy endpoint.

## Workstream C — Replace recursive physical immutability with explicit ownership

Audit the current `_freeze`, `_thaw`, deep-copy, and mutation call sites.

Preferred target model:

- canonical parsed payload is request-private and treated as logically immutable after parse;
- provider-bound payload initially references canonical payload only while no mutation is required;
- first provider-bound mutation creates one owned mutable copy (copy-on-write) if canonical state must be preserved;
- subsequent provider-bound transforms mutate/replace that owned provider payload through explicit methods;
- serialization operates directly on the owned ordinary dict/list graph rather than thawing a recursively frozen graph;
- callers do not receive an unrestricted mutable reference if that would let unrelated code mutate provider payload outside the provider-bound API.

Implementation may use a private payload field plus explicit accessor/mutator methods. Do not replace recursive freeze with equally expensive recursive validation on every access.

### Copy-on-write correctness

If transforms can mutate nested structures in place, the first copy must be deep enough to isolate canonical state. The optimization target is **at most one necessary full copy**, not an unsafe shallow copy.

If all provider transforms already replace top-level/nested values without mutating shared nested objects, prove that before choosing shallow copy.

Use tests with nested tool/message structures and sentinel canonical payload snapshots to prove provider mutation cannot alter canonical/client state.

## Workstream D — Preserve original bytes on the native no-transform path

Identify the exact conditions under which accepted client bytes may be sent upstream unchanged.

Candidate conditions:

- source endpoint protocol equals selected provider protocol;
- no transcode is required;
- no body field mutation is required for provider auth/model alias/reasoning/cache/compression policy;
- the selected upstream endpoint accepts the source body shape exactly;
- header changes are independent of body bytes.

When those conditions hold, set/reuse the original request bytes as authoritative provider bytes and skip JSON serialization.

Requirements:

- no-transform path sends byte-identical body to upstream in focused tests;
- transformed paths still serialize the transformed payload exactly once after final mutation;
- model/provider suffix stripping or model alias replacement must force a body mutation if the body model field actually changes;
- do not preserve bytes merely because a transform is "small" if the payload semantics changed.

Instrument with test-local spies/counters around parse/serialization methods if useful; do not add permanent production counters solely for this plan.

## Workstream E — Avoid duplicate provider-payload copies

Remove `provider_payload_copy()` or equivalent deep-copy calls where ownership rules make them unnecessary.

For each caller classify:

- read-only inspection → return/read the logically immutable canonical/provider object through a safe internal interface;
- provider mutation → ensure provider-bound object owns one mutable copy and mutate there;
- serialization → serialize owned payload directly;
- response/finalization → extract only needed scalar metadata, not a copy of the whole request.

Do not keep compatibility wrappers solely for tests if production has one authoritative ownership API. Update/delete redundant tests under Plan 109 later; preserve current high-value correctness tests now.

## Workstream F — Release dispatch-only request buffers after handoff

Add a narrow lifecycle operation such as `release_dispatch_buffers()` only if it improves clarity; otherwise clear the relevant fields at the existing authoritative handoff transition.

Before clearing anything, prove via repository search that post-handoff code does not require:

- original raw body;
- parsed canonical object;
- provider-bound transformed object;
- provider request bytes.

Preserve small metadata needed for:

- request/model/provider/account identifiers;
- usage/finalization;
- response transcoding/streaming observer state;
- local timing/status diagnostics;
- cancellation/terminal handling.

### Timing rule

For streaming responses, release request dispatch buffers only after:

1. upstream request body transmission/preparation is complete;
2. the upstream response is accepted as the chosen attempt;
3. downstream handoff has begun or the lifecycle state has otherwise made retry impossible;
4. no response adapter needs the request payload itself.

For non-streaming responses, release as soon as equivalent conditions are met; do not retain request graphs until durable finalization if only scalar metadata remains necessary.

### Safety tests

Cover:

- successful stream handoff then long stream consumption;
- client cancellation after handoff;
- premature EOF after handoff;
- pre-handoff retry still has required request state;
- local transcode failure before dispatch does not release state prematurely;
- terminal finalization succeeds after buffers are cleared;
- startup repair semantics unaffected because large request bodies are not part of repair authority.

Use weakrefs or explicit field assertions in tests if appropriate; do not add production memory tracing.

## Workstream G — Configurable whole-request body limit

Replace the hard-coded body-size constant with one clear server/request configuration field using the existing config model and validation style.

Preferred shape:

```toml
[server]
max_request_body_bytes = 10485760
```

Use the actual existing configuration section/name conventions; do not create a new config namespace if server/request limits already have one.

Requirements:

- retain the current ~10 MiB value as default unless existing docs/config establish a different authoritative default;
- require a positive sensible integer;
- avoid a complicated min/max tier system;
- `check-config` validates it;
- copyable example and bundled config document it only if exposing the knob improves operator clarity without clutter;
- live rehash behavior follows the normal server-config ownership rule: either safely reload it through generation config or document restart-required behavior if that is the existing contract. Do not redesign rehash.

Reject oversized bodies as early as the current ASGI body reader permits and before JSON parse/transcode work.

## Workstream H — Make provider document-limit reporting truthful

Keep provider-native document/media limits where they remain useful, but make precedence clear:

```text
effective request acceptance = whole-request body limit first,
then provider/transcoder field-specific limits.
```

Do not claim that EggPool accepts a 32 MiB raw/base64 document when the configured whole JSON request ceiling is 10 MiB.

Implementation options:

- error messages/docs state that provider document limit is additionally bounded by `server.max_request_body_bytes`;
- internal validation exposes both limits where useful;
- do not attempt to compute one misleading "effective raw PDF bytes" number because base64 overhead, JSON framing, multiple documents, and other message content vary.

Tests should prove:

- a request exceeding whole-body limit is rejected before document transcode;
- a request under whole-body limit but exceeding provider document limit receives the provider/document-specific error;
- raising the configurable body limit allows larger requests up to provider-specific constraints without changing default behavior.

## Workstream I — Reject obviously oversized base64 before decode allocation

Find image/document base64 size checks:

```bash
rg -n 'base64|b64decode|document|image.*limit|MAX_.*(PDF|IMAGE|BODY)' src/eggpool/transcoder src/eggpool tests
```

For encoded base64 strings whose length alone proves decoded data must exceed the provider limit:

- reject using safe conservative arithmetic before full decode;
- account for padding correctly;
- still validate/decode under-limit content as required by existing correctness semantics;
- do not accept malformed base64 merely because the size estimate is small;
- do not add chunked/streaming base64 decoding unless current code already has it.

The goal is to avoid allocating a huge decoded value that will definitely be rejected, not to optimize every base64 operation.

## Workstream J — Focused allocation/lifetime evidence

Use temporary implementation-time evidence only. Suitable techniques:

- test-local counters for parse/serialize/copy calls;
- `tracemalloc` in an existing manual/perf test if already available;
- process RSS observation for one synthetic large request during local development;
- object identity assertions proving no-transform byte reuse and copy-on-write isolation.

Do not add RSS/latency thresholds to tests or CI.

Record qualitative before/after facts in closure, for example:

- recursive freeze/thaw removed;
- no-transform serialization count changed from one to zero;
- transformed path performs one owned full copy rather than multiple freeze/thaw/deepcopy passes;
- request dispatch buffers cleared at handoff.

Do not fabricate byte-saved numbers if not measured.

## Workstream K — Documentation/architecture

Update active architecture notes to describe:

- canonical parsed payload logical immutability;
- provider-bound copy-on-write ownership;
- original-byte native fast path;
- post-handoff dispatch-buffer release;
- configurable whole-request body limit and provider-specific limit precedence.

Keep documentation concise. Do not create a memory-management design document if existing request architecture can absorb the notes.

## Verification

Run focused request lifecycle, body-limit, transcoder, stream handoff/cancellation, and JSON backend tests identified by search. Include existing high-concurrency stream reproducer only as an optional diagnostic if a regression appears.

Then run:

```bash
uv run ruff format --check src/ tests/ scripts/
uv run ruff check src/ tests/ scripts/
uv run pyright src/ scripts/
PYTHONHASHSEED=0 TZ=UTC uv run pytest tests/smoke/ -q --tb=short --maxfail=1
uv run eggpool --config config.example.toml check-config
uv run eggpool --config config.sbc.example.toml check-config
```

A complete retained suite is optional, not required.

## Acceptance criteria

- [ ] `ParsedRequestPayload` central hot-path parsing uses `eggpool.jsonx` rather than direct stdlib `json.loads()`.
- [ ] Existing stdlib/orjson backend selection remains correct and no new JSON dependency is added.
- [ ] Canonical request payload remains logically immutable after parse.
- [ ] Provider transforms cannot mutate canonical/client payload state through shared nested aliases.
- [ ] Recursive physical freeze/thaw cycles are removed from the normal provider-bound path or are proven unavoidable for a narrowly retained boundary.
- [ ] Provider-bound mutation requires at most one necessary full canonical→provider copy for a request, rather than repeated deep copies across transforms/serialization.
- [ ] Serialization operates directly on the final ordinary provider payload without a recursive thaw pass.
- [ ] Native/no-transform requests reuse original request bytes when the provider-bound body is byte-semantically unchanged.
- [ ] Any model alias/protocol/cache/compression/reasoning transform that changes body semantics disables original-byte reuse correctly.
- [ ] Transformed requests serialize only after the final body mutation and do not retain redundant provider-body copies.
- [ ] Pre-handoff retry paths retain all required request state.
- [ ] After chosen-attempt downstream handoff makes retry impossible, dispatch-only raw/parsed/provider payload buffers are released when no longer needed.
- [ ] Streaming completion, cancellation, premature EOF, response adaptation, usage accounting, and finalization remain correct after buffer release.
- [ ] Default whole-request body limit remains bounded at the current SBC-appropriate value unless existing policy proves another value authoritative.
- [ ] Whole-request body limit is configurable through one clear existing config surface and validated by `check-config`.
- [ ] Oversized whole requests are rejected before avoidable parse/transcode work.
- [ ] Provider document/media limits are documented/enforced as additional constraints subordinate to the whole-request body limit.
- [ ] Obviously over-limit base64 media is rejected before allocating the full decoded body where encoded length makes rejection certain.
- [ ] Under-limit malformed base64 remains rejected correctly; size optimization does not weaken validation.
- [ ] No request spooling subsystem, new worker/process, new dependency, DB migration, SQLite tuning change, memory telemetry service, or CI benchmark is added.
- [ ] Focused request lifecycle/body/transcoder/JSON tests pass.
- [ ] Ruff, Pyright, smoke tests, and both config checks pass.

## Rejection conditions

Reject the implementation if:

- canonical payload and provider payload share mutable nested state that a transform can modify;
- recursive freeze/thaw is replaced by another unconditional full-tree traversal of similar cost without correctness need;
- original bytes are reused after any body-semantic mutation;
- buffers are cleared before pre-handoff retry or response adaptation no longer needs them;
- finalization begins depending on cleared request-body state and becomes fragile;
- the default body limit is raised dramatically just to match an upstream document maximum;
- a complex per-provider body-limit policy replaces one clear proxy limit plus provider-specific checks;
- request bodies are spooled to disk/mmap/shared memory as an optimization;
- a new JSON/immutability/performance dependency or CI benchmark is added.

## GPT-5.6 Luna implementation sequence

1. Read Plan 103, this plan, `AGENTS.md`, request lifecycle architecture, body reader, transcoder payload classes, JSON backend, and stream handoff/finalization tests.
2. Build a temporary ownership/lifetime map from production call sites before changing data structures.
3. Route central parse through `jsonx` and verify backend/error parity.
4. Replace recursive physical immutability with a simple logical-ownership/copy-on-write model, proving nested isolation.
5. Add original-byte reuse only for the proven no-transform path.
6. Remove redundant provider payload copies/thaw serialization.
7. Identify the authoritative post-handoff transition and release only dispatch-only buffers there.
8. Make the whole-request body limit configurable without changing the bounded default; reconcile provider document-limit reporting.
9. Add conservative pre-decode rejection for definitely oversized base64 media.
10. Run focused correctness tests plus temporary allocation/call-count evidence; do not create permanent thresholds.
11. Update concise architecture/config notes.
12. Run ordinary repository gate and config checks.
13. Record implementation SHA, ownership rule, no-transform fast-path rule, buffer-release boundary, body-limit default/config behavior, and exact verification results in this plan.
14. Stop; leave target-device quantitative observation to Plan 110.

## Implementation closure

Implemented on `main` and verified locally on 2026-08-11.

- `ParsedRequestPayload` now uses `eggpool.jsonx.loads()` for its lazy parse
  fallback; the endpoint's eager parse remains seeded into the same cache.
- `ProviderBoundRequest` treats the canonical graph as logically immutable,
  deep-copies once when provider mutation begins, stores an ordinary owned
  graph, serializes it directly, and reuses accepted client bytes when no body
  semantics changed. Recursive freeze/thaw is no longer on the dispatch path.
- Streaming response preparation releases raw/parsed/provider dispatch buffers
  after the selected response is prepared, while `original_body_size` retains
  the scalar needed by finalization and diagnostics. Pre-handoff retry and
  response adaptation retain their required state.
- `[server].max_request_body_bytes` defaults to `10485760`, is positive-value
  validated, is live-reloadable, and is enforced by both early middleware and
  the leased body reader. Example and bundled configs document the setting.
- Provider document/media limits are documented as subordinate to the whole
  request limit. Definitely oversized padded base64 is rejected before decode;
  malformed or borderline input still follows strict decode validation.
- No dependency, migration, spooling subsystem, SQLite change, benchmark, or
  permanent memory threshold was added.

Qualitative evidence: unchanged native dispatch uses the original body with
zero JSON encode calls; transformed dispatch uses one owned graph and one final
encode cache; stream handoff clears request dispatch buffers. No quantitative
RSS claim is made; target-device measurements remain Plan 110 work.

Verification completed:

```text
uv run ruff format --check src/ tests/ scripts/       PASS
uv run ruff check src/ tests/ scripts/               PASS
uv run pyright src/ scripts/                          PASS
PYTHONHASHSEED=0 TZ=UTC uv run pytest tests/smoke/ -q --tb=short --maxfail=1  PASS (14 passed)
uv run eggpool --config config.example.toml check-config      PASS
uv run eggpool --config config.sbc.example.toml check-config  PASS
focused request/transcoder/lifecycle/config suites            PASS
```

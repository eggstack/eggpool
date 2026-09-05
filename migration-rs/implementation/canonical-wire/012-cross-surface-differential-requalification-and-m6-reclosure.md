# W012 — Cross-Surface Differential Requalification and M6 Re-Closure

Status: planned; blocked on W011 accepted closure

Source roadmap: `migration-rs/subsystems/canonical-wire-roadmap.md#w012--cross-surface-differential-requalification-and-m6-re-closure`

Primary class: invariant/corrective

Repository baseline at planning: `fb36054278817de63b5c516c82202184c9200be7`

Hard dependency: W011 accepted closure.

## 1. Objective

Repair the post-W010 qualification gap by proving the complete caller-selected M6 transformation boundary against the live Python oracle across all supported client surfaces and built-in upstream profiles.

W010 exercised every client/profile pairing, but much of the request, finite-response client encoding, and stream client encoding matrix asserted only coarse metadata, success/rejection class, or non-empty terminal output. That is insufficient evidence for the W010 plan's mandatory semantic fields and for the M6 exit condition.

W012 must make those comparisons genuinely differential and re-close M6 only after any bounded M6 mismatches exposed by the stronger oracle are corrected.

## 2. Why W010 closure is insufficient

At the W010 baseline:

- `tests/migration_rs/canonical_wire_fixtures.py` already builds rich Python request observations containing full canonical request state and `encoded_by_profile` output for each public client surface;
- the committed W001 projection intentionally stores only request model/body-size/streaming metadata, so Rust `wire_qualification.rs` does not consume the richer request transformation oracle;
- `every_client_surface_and_selected_profile_pair_is_bounded_and_semantic` checks profile/model identity, message count, byte counts, and streaming intent, but does not compare encoded roles/content/tools/reasoning/structured output/media/cache semantics against Python;
- the finite cross-surface test verifies that provider decode succeeds and that `client_body` exists, but it does not compare the client body semantic projection to Python;
- the stream cross-surface test verifies that events can be encoded and that terminal events are non-empty, but it does not compare the resulting client SSE/event grammar, tool/reasoning deltas, finish semantics, usage, or ordering to Python.

W010 did catch and fix two real integration defects, which demonstrates the value of the qualification layer, but its final evidence overstates coverage for the full 15-pair transformation matrix.

Historical W010 closure remains append-only evidence; W012 supersedes only its aggregate conclusion that all cross-surface transformation semantics were proven.

## 3. Python oracle ownership

Use production behavior, not a second handwritten transcoder. The observation adapter should be grounded in:

- `src/eggpool/wire/ir.py`;
- `src/eggpool/wire/registry.py`;
- built-in `src/eggpool/wire/codecs/`;
- `src/eggpool/transcoder/` request/response/streaming compatibility paths where those are the production cross-protocol behavior;
- `src/eggpool/transcoder/prepared.py` and policy/loss modules;
- `src/eggpool/proxy/sse.py` and normalized usage helpers;
- `tests/migration_rs/canonical_wire_fixtures.py`;
- existing transcoder contract/streaming fixtures and W001-W010 closure evidence.

If Python has more than one legacy path for the same transformation, identify which path is currently authoritative for the public proxy behavior and record that choice in the fixture metadata. Do not combine incompatible legacy outputs just to make Rust pass.

## 4. Differential matrix

The minimum integrated matrix is:

- 3 public client surfaces: Chat Completions, Responses, Messages;
- 5 built-in selected upstream profiles: OpenAI Chat, OpenAI Responses, Anthropic Messages, Gemini Interactions, Gemini generateContent;
- 15 request client -> selected-profile cases;
- 15 finite selected-profile -> client response cases;
- 15 stream selected-profile -> client cases.

Each matrix cell must produce one of:

- exact/native success;
- semantic success with ordered typed notices;
- policy-approved semantic loss with ordered typed notices;
- typed local rejection with stable reason/field;
- typed malformed/provider error where applicable.

A matrix cell cannot pass merely because Rust and Python both returned some success value.

## 5. Request-side mandatory comparison

For request transformations compare, as applicable:

- canonical model and source client surface;
- message order and role identity;
- text and Unicode content;
- content block kinds/order;
- system vs developer preservation/merging behavior;
- tool definitions, names, descriptions, schema semantics, tool choice, parallel-call intent;
- assistant tool call IDs/names/arguments/order;
- tool result linkage/IDs/content;
- stream presence/value;
- max output token controls and zero-vs-missing semantics;
- temperature/top-p/stop and other portable generation controls, including null/presence distinctions;
- reasoning enable/disable/mode/effort/budget/adaptive state;
- structured output kind/name/schema/strictness;
- image/document source form, media type, detail, file/data/url identity where the target can represent it;
- cache-control/breakpoint/provider-extension placement;
- target model/body fields and profile-specific structural grammar;
- ordered adaptation notice code, field, source, target, and outcome class.

For exact native paths where the frozen contract treats exposed compact JSON bytes/field ordering as compatibility data, compare bytes exactly. For cross-wire bodies, compare semantic JSON with arrays/order and missing/null/zero distinctions preserved; do not sort or strip fields to hide mismatches.

## 6. Finite response mandatory comparison

For every upstream-profile/client pair compare:

- canonical output block kinds and order;
- text content;
- reasoning/thinking content;
- tool call IDs/names/arguments/order;
- response/model identity where exposed;
- finish/stop/status category;
- usage and cache counters including zero-vs-missing;
- provider error class/type/status and bounded message metadata;
- client-surface encoded body semantic structure;
- ordered response adaptation notices/loss decisions.

Do not qualify finite response parity using only block kinds, finish reason, or `client_body.is_some()`.

## 7. Streaming mandatory comparison

For every upstream-profile/client pair feed both whole-buffer and deterministic fragmented input and compare:

- canonical event type sequence;
- response/content/tool indexes and IDs;
- text/reasoning/tool argument deltas;
- start/end event ordering;
- finish/stop semantics;
- usage update/final counters;
- provider error events;
- client-surface encoded SSE/event bytes or an exact semantic frame projection, depending on the frozen compatibility class;
- native terminal marker/evidence;
- premature EOF outcome;
- no false completion after malformed/provider-error input.

W011's invalid/truncated UTF-8 EOF fixtures become mandatory regressions in this integrated pass.

## 8. Fixture strategy

Extend the Python migration oracle with a bounded synthetic artifact dedicated to W012. Prefer a compact semantic projection generated from live production code, for example under `migration-rs/fixtures/canonical-wire/`, rather than duplicating codec logic in Rust tests.

The artifact must be deterministic and secret-safe. Synthetic text/tool/schema/media markers are allowed and useful for proving preservation, but arbitrary user/provider bodies, credentials, auth headers, proxy credentials, or session identifiers are forbidden.

The fixture set must include at least:

- rich Chat request with developer/system, tool call/result, reasoning, structured output, inline image, cache control, Unicode, zero/false controls;
- rich Responses request with tools, reasoning, structured output, presence-sensitive controls;
- rich Messages request with thinking, tool use/result, image/document/cache semantics where production accepts them;
- targeted presence/null/zero cases;
- targeted semantic-loss/rejection cases for tools, reasoning, structured output, media/document, cache controls, and provider extensions;
- finite responses containing text + reasoning + tool output + usage;
- stream traces containing text + reasoning + tool deltas + usage + terminal evidence.

Do not require every single feature in one giant fixture if that makes failures hard to localize; use a small matrix of orthogonal synthetic cases.

## 9. Rust qualification shape

The Rust qualification layer should project `WireRuntime` outputs into the same bounded semantic schema as the Python fixture. Avoid a second Rust implementation of provider grammar inside the test.

Helpers may decode the produced JSON/SSE solely to compare semantic fields. They may not normalize away:

- array/event order;
- tool IDs;
- block kinds;
- missing/null/false/zero distinctions;
- warning/loss categories;
- finish/terminal categories;
- usage/cache distinctions.

## 10. Correcting mismatches

W012 may correct bounded M6 request/finite/stream codec or adaptation defects exposed by the new differential matrix when the fix remains inside the established W002-W009 architecture.

Examples of permitted fixes:

- wrong/missing target field mapping;
- dropped ordered block/tool identity;
- incorrect client response codec/shape;
- missing typed adaptation notice;
- incorrect client stream event framing;
- usage/finish mapping mismatch.

Stop and create a new corrective plan if qualification reveals a broader architectural problem such as a canonical IR incapable of representing required Python semantics, dynamic negotiation state required inside M6, or a provider/network/durable lifecycle dependency.

## 11. Resource, security, and dependency invariants

- no provider network or paid inference;
- no DB writes;
- no account/health/quota mutation;
- no dynamic wire resolver state;
- no retries or finalization;
- no new HTTP/TLS stack;
- no broad framework dependency for schema comparison;
- bounded fixture payloads and stream carry;
- no raw secret-bearing diagnostics.

A small test-only helper dependency is still discouraged; prefer serde/json and existing Python test tooling.

## 12. Required verification

At minimum run:

```text
rtk cargo fmt --manifest-path rust/Cargo.toml -- --check
rtk cargo clippy --manifest-path rust/Cargo.toml --all-targets -- -D warnings
rtk cargo test --manifest-path rust/Cargo.toml --test wire_qualification -- --test-threads=1
rtk cargo test --manifest-path rust/Cargo.toml --test wire_runtime -- --test-threads=1
rtk cargo test --manifest-path rust/Cargo.toml --test wire_stream -- --test-threads=1
rtk cargo test --manifest-path rust/Cargo.toml --all-targets -- --test-threads=1
rtk uv run pytest tests/migration_rs -q --tb=short --maxfail=1
rtk uv run pytest tests/migration_rs tests/unit/test_wire_ir.py tests/unit/test_wire_codecs.py tests/unit/test_wire_profiles.py tests/unit/test_sse_decoder.py tests/unit/test_sse_observer.py tests/unit/test_normalized_usage.py tests/unit/test_transcoder tests/contract/test_transcoder_contract.py -q --tb=short --maxfail=1
rtk uv run ruff format --check src/ tests/ scripts/
rtk uv run ruff check src/ tests/ scripts/
rtk uv run pyright src/ scripts/
rtk uv run pytest tests/smoke/ -q --tb=short --maxfail=1
rtk git diff --check
```

Record exact test counts and any intentionally skipped platform/provider cases.

## 13. Acceptance criteria

W012 closes and re-closes aggregate M6 only if:

1. all 15 request pairings are compared against live-Python-derived semantic expectations, not coarse metadata;
2. all 15 finite response/client pairings compare complete mandatory response semantics and client body output;
3. all 15 stream/client pairings compare mandatory event/delta/usage/terminal and client encoding semantics;
4. strict/warn loss policy outcomes and ordered notices match for targeted feature-loss cases;
5. W011 invalid/truncated UTF-8 EOF regressions remain green;
6. exact/native byte comparisons pass where required by the frozen contract;
7. any bounded M6 mismatch found by the stronger matrix is fixed and documented;
8. no high/medium unclosed M6 correctness or security issue remains;
9. the M6/M7 ownership boundary is unchanged;
10. broad Rust/Python/smoke verification passes.

## 14. Closure evidence

Create `migration-rs/closure/canonical-wire/012-status.md` containing:

- a 15-cell request matrix result;
- a 15-cell finite response matrix result;
- a 15-cell stream/client matrix result;
- exact vs semantic comparison rules used;
- all mismatches found and the commits that corrected them;
- W011 regression evidence;
- resource/dependency/security review;
- full verification commands/results;
- unresolved findings/supported differences.

Historical W010 closure remains unchanged. Accepted W012 closure may update aggregate status to **M6 closed after W011/W012 corrective pass** and make M7 eligible for its own planning review. M7 implementation is not promoted automatically.
# Plan 226 — Request Hot-Path Single Admission and Zero-Copy Native Body

Date: 2026-09-20  
Status: implementation handoff  
Planning baseline: 3e90d36c4094af1c93756ace1c882f55b5f8f5d5  
Parent roadmap: plans/225-native-runtime-performance-optimization-roadmap.md  
Priority: P0 request-path CPU/allocation reduction  
Execution target: GPT-5.6 Luna/Sol or comparable implementation model

## Purpose

Collapse the ordinary inference path to one authoritative JSON parse/depth
validation and preserve the original Bytes allocation when the selected wire
path can forward the native request unchanged.

This plan targets the work that scales most directly with large prompts, tool
schemas, and agent context.

It must not change endpoint behavior, canonical semantics, virtual routing,
provider-qualified model handling, stateless Responses policy, or wire
preservation rules.

## Current path and confirmed duplication

### Server parses twice

rust/src/server/inference.rs::handle_inference currently calls
serde_json::from_slice twice:

- once to determine whether stream is true;
- once to reject a present non-boolean/non-null stream field.

### Endpoint parses again

rust/src/coordinator/endpoints.rs::execute_finite and execute_stream each:

- parse raw_body into serde_json::Value;
- require an object;
- clone that object into a Map;
- validate stateless Responses;
- inspect stream.

### Request constructor parses again

FiniteRequest::new and StreamRequest::new call admit_request, whose parse_once
does another serde_json::from_slice plus depth validation.

For the direct concrete-model path this is approximately four complete parses.

### Virtual and qualified-model rewriting can add more parsing

resolve_virtual currently calls admit_request again to get the canonical
selector/affinity view.

rewrite_model_field reparses raw bytes before replacing model, and
finish_virtual_resolution can rewrite twice when stripping an Eggpool provider
qualifier from the selected concrete model.

### Native wire preparation copies raw bytes

rust/src/wire/runtime.rs::prepare_admitted_request accepts raw_body as &[u8].
Native no-rewrite branches create the provider body using:

    Bytes::copy_from_slice(raw_body)

The incoming request was already Bytes, so this is an avoidable O(n) copy.

The compact native path has the same ownership problem.

## Required end state

For an ordinary direct concrete-model request:

1. Axum body bounding still applies before unbounded work.
2. JSON is deserialized exactly once.
3. depth validation runs exactly once.
4. stream shape is inspected from that same parsed value.
5. Responses stateless validation is performed without reparsing.
6. canonical admission consumes/reuses that parsed value.
7. FiniteRequest/StreamRequest is built through from_admitted.
8. unchanged native forwarding reuses the original Bytes backing allocation.
9. no full-body copy occurs between ingress and ProviderHttpClient::send unless
   a model rewrite or cross-surface encoding actually requires new bytes.

For provider-qualified and virtual-model requests:

- do not reparse solely to rewrite model;
- mutate the already-parsed object;
- serialize the concrete body once per required concrete rewrite stage;
- derive final admission from that already-parsed concrete value;
- preserve the same final canonical model/provider pin and native Responses
  preservation that current code produces.

## Workstream 1 — Add an internal parsed-admission boundary

### Authority

- rust/src/request/admission.rs
- rust/src/request/mod.rs
- rust/src/request/body.rs where bounded encoding is already owned

### Required shape

Introduce a crate-internal parsed request representation or equivalent helper.
The exact type name is not important, but it must make these stages explicit:

1. byte/body bound;
2. serde_json parse;
3. depth validation;
4. object access;
5. canonical admission from the already-parsed value.

A suitable conceptual API is:

    parse_request_body(raw_body, max_body_bytes) -> ParsedRequestBody
    admit_parsed_request(raw_body, parsed_value, AdmissionOptions) -> AdmittedRequest

Do not make the parsed representation a new public product API unless another
consumer genuinely requires it.

### Preserve public admit_request

admit_request(raw_body, options) remains available and behavior-compatible.

Refactor it to delegate to the same parse + parsed-admission core rather than
maintaining a second decoder.

Existing callers that are not on the optimized endpoint path must continue to
work unchanged.

### Parsed admission must preserve all current checks

The parsed path must retain:

- max body bytes;
- MAX_JSON_DEPTH;
- top-level object requirement;
- model validation;
- surface-specific message/tool/input decoding;
- output limits;
- token/reservation estimates;
- Responses stateless policy;
- Responses native feature summary;
- Responses native preservation.

For Responses, the native preservation envelope must own the final concrete
parsed value exactly as the current concrete-body reparse does.

Do not retain both original and concrete Value trees on the normal direct path.

## Workstream 2 — Move stream classification into one coordinator execution boundary

### Authority

- rust/src/server/inference.rs
- rust/src/coordinator/endpoints.rs

The server should no longer deserialize request JSON merely to decide which
coordinator path to call.

Preferred end state:

- add one coordinator endpoint entry that accepts the bounded raw Bytes and
  returns an enum representing finite versus streaming execution;
- parse/classify/admit inside the request/coordinator boundary;
- let server/inference.rs format the returned finite or streaming result.

For example, the internal result can conceptually be:

    EndpointExecution::Finite(...)
    EndpointExecution::Stream(...)

The exact naming is implementation-owned.

### Preserve existing entry points

execute_finite and execute_stream are already useful to focused tests and
internal callers.

Do not delete or incompatibly change them.

Refactor them as compatibility/specialized wrappers over the same shared
preparation core, with an expected-stream-mode check:

- execute_finite rejects a streaming body exactly as today;
- execute_stream rejects a finite body exactly as today.

The production HTTP path should make one coordinator execution call, matching
the existing architectural rule that server/* owns no lifecycle retry or
finalization logic.

### Error parity

Preserve current client-visible behavior for at least:

- malformed JSON;
- top-level non-object JSON;
- stream omitted;
- stream null;
- stream false;
- stream true;
- stream string/number/object/array;
- stateless Responses violations;
- missing/empty/invalid model;
- body too large.

In particular, preserve the current invalid-stream 400 semantics and surface
specific error envelope.

Do not improve wording incidentally unless a pre-existing test already treats
the text as non-contractual.

## Workstream 3 — Resolve concrete models from the parsed representation

### Authority

- rust/src/coordinator/endpoints.rs

Eliminate rewrite_model_field's parse-from-bytes behavior on the optimized path.

### Direct concrete model

When model has no Eggpool provider qualifier and is not virtual:

- keep the original Bytes unchanged;
- consume the parsed Value into admission;
- derive routing facts from that admitted request.

### Provider-qualified model

For model/provider syntax:

- parse the provider pin exactly as today;
- mutate the already-parsed object's model field to the dispatch model;
- bounded/compact serialize once to concrete Bytes;
- build the concrete AdmittedRequest from the same mutated Value;
- retain provider_id as the routing pin.

Do not allow the Eggpool provider qualifier to reach the upstream body.

### Virtual model

Do not call admit_request solely to build the selector's early semantic view.

Use the existing canonical_request_from_value or a shared equivalent to derive
the canonical request and affinity identity from the one parsed object.

After selection:

- reject recursive virtual targets exactly as today;
- validate route id/label/model consistency exactly as today;
- determine provider qualifier exactly as today;
- mutate model in the parsed object to the final dispatch model;
- serialize one concrete body;
- perform final concrete admission from that same parsed value;
- build FiniteRequest/StreamRequest through from_admitted.

Sticky-affinity and selector behavior must not change.

### Avoid object cloning just for field inspection

execute_finite/execute_stream currently clone value.as_object() into a Map.

The shared preparation path should borrow the object while inspecting stream,
model, and stateless fields.

Only clone a JSON tree when an ownership boundary actually requires a second
independent tree.

## Workstream 4 — Precompute routing facts from the admitted request

Use FiniteRequest::from_admitted and StreamRequest::from_admitted rather than
calling their new constructors after endpoint parsing.

Derive RoutingRequestFacts once from the final AdmittedRequest and the current
static routing inputs.

Do not allow early virtual canonical facts to leak into the final concrete
routing facts.

Provider pin assignment and virtual metrics must remain unchanged.

The static-input allocation cleanup itself can be completed in Plan 227; this
plan only needs a correct shared path.

## Workstream 5 — Add an owned-Bytes wire-preparation fast path

### Authority

- rust/src/wire/runtime.rs
- rust/src/coordinator/attempt.rs

Do not break the existing public prepare_admitted_request method that accepts a
byte slice.

Add an internal/compatible path that retains Bytes ownership, conceptually:

    prepare_admitted_bytes(admission, raw_body: Bytes, context)

or an equivalent borrowed admission + owned Bytes API.

The coordinator/attempt hot path should use this owned-Bytes method.

The legacy slice method can delegate by copying because callers selecting that
API have not supplied an ownership-preserving Bytes handle.

### Native no-rewrite branch

For:

- body_passthrough;
- native compatibility path;
- no model rewrite required;

set the encoded body to raw_body.clone or move raw_body where ownership permits.

Bytes clone is a refcount operation and must preserve the original backing
allocation.

### Native Responses

Keep the source-native preservation requirements intact.

Unknown/future fields, custom tools, deferred-search declarations, and native
Responses fields must remain byte-exact when the model does not require
rewriting.

### Compact endpoint

Apply the same owned-Bytes rule to the native compact request path where no
model rewrite is required.

Do not alter compact finite-only/stateless validation.

### Rewritten/transcoded paths

When model rewrite, native Responses preservation rewrite, or cross-surface
codec encoding requires a new body, allocate/encode exactly as required.

Zero-copy is not a goal when bytes genuinely change.

## Workstream 6 — Deterministic regression coverage

### Admission/endpoint tests

Add or extend focused tests proving:

- malformed JSON remains the same error class/status;
- non-object remains rejected;
- stream shape parity;
- finite/stream selection parity;
- provider-qualified routing pin parity;
- direct model parity;
- virtual sticky and non-sticky selection parity;
- recursive virtual refusal;
- Responses stateless refusal;
- source-native Responses preservation.

### Wire ownership tests

Add focused tests for the new Bytes path.

For an unchanged native request, retain the source Bytes and assert:

- byte equality;
- length equality;
- same backing pointer where a full, unsliced Bytes clone is expected.

A pointer-sharing assertion is appropriate only for the explicit owned-Bytes
fast path. Do not impose it on the public &[u8] compatibility method.

Also assert that a required model rewrite produces correct changed bytes and
does not falsely claim byte identity.

### Codex and wire qualification

Run the current deterministic compatibility suites, especially:

- wire_runtime;
- wire_qualification;
- wire_adaptation;
- wire_stream;
- codex_responses_compat;
- codex_compaction_compat;
- coordinator_boundaries;
- coordinator_finalization;
- coordinator_publication;
- coordinator_c008;
- coordinator_c009;
- coordinator_c011.

Use exact current target names from rust/tests.

## Performance evidence

Before editing, capture one direct native request-path baseline with a release
build and loopback provider fixture.

After implementation, repeat under identical conditions.

At minimum compare small, medium, and large request bodies.

Record:

- mean/p50/p95 request latency where stable;
- requests/second for a fixed concurrency;
- process CPU if reliable;
- structural parse count;
- structural full-body copy count.

The acceptance condition is not a fixed percentage. The implementation is
valuable if the deterministic ownership work lands without regressions and the
large-body path no longer repeats parse/copy work.

If measurements become worse, investigate before closing the plan.

## Focused validation

~~~bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings

cargo test --manifest-path rust/Cargo.toml --test wire_runtime -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test wire_qualification -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test wire_adaptation -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test wire_stream -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test codex_responses_compat -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test codex_compaction_compat -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_c008 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_c009 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_c011 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_boundaries -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_finalization -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_publication -- --test-threads=1
~~~

Then run the full default/no-default/tooling baseline from AGENTS.md.

No Cargo dependency change is expected. If Cargo.toml/Cargo.lock changes, stop
and justify it before continuing.

## Stop conditions

Stop and investigate rather than forcing the optimization if:

1. one-parse preparation changes a current error/status contract;
2. virtual selector/affinity results change;
3. provider-qualified models lose their routing pin;
4. source-native Responses bytes/semantics regress;
5. the shared parsed representation would require unbounded retained JSON
   alongside another equally large tree on ordinary direct requests;
6. native zero-copy would outlive the original Bytes owner unsafely;
7. a proposed change requires whole-stream buffering;
8. a proposed fix deletes existing public coordinator/wire helpers rather than
   adding a compatible internal path.

## Out of scope

Do not combine this plan with:

- Eggress 1.0.7 migration;
- provider-client topology changes;
- dashboard SQLite architecture;
- Tokio runtime threading;
- streaming mpsc removal;
- routing lock redesign;
- updater transport changes;
- new provider features.

Those are either covered by Plan 191, Plan 227, or Plan 228.

## Completion criteria

This plan is complete when:

- [ ] production HTTP inference no longer parses JSON in server/inference.rs;
- [ ] direct finite/stream endpoint preparation deserializes request JSON once;
- [ ] depth validation occurs once on that path;
- [ ] endpoint field inspection does not clone the top-level object;
- [ ] virtual selector input is derived without a second admit_request parse;
- [ ] provider-qualified/virtual model rewriting does not reparse raw bytes;
- [ ] final concrete request uses from_admitted;
- [ ] unchanged native request forwarding reuses the incoming Bytes backing
      allocation;
- [ ] native compact no-rewrite forwarding also has an owned-Bytes path;
- [ ] existing public admit/execute/wire helpers remain available;
- [ ] error/status/stateless/virtual/provider-pin behavior is unchanged;
- [ ] Codex/wire/coordinator focused suites pass;
- [ ] full repository validation passes;
- [ ] before/after structural parse/copy evidence is recorded;
- [ ] a closure record with implementation commit and measurements is appended
      to this plan.

## Handoff note

Prefer one shared preparation core over a patchwork of cached Values.

The final shape should make ownership obvious:

    Axum Bytes
      -> one bounded parse/depth validation
      -> optional in-memory model mutation
      -> one concrete AdmittedRequest
      -> FiniteRequest/StreamRequest::from_admitted
      -> owned-Bytes wire preparation
      -> native Bytes reuse OR required rewrite/transcode allocation
      -> provider send

The direct path should not pay virtual-routing or transcoding costs it does not
use.

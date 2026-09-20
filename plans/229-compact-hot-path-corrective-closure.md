# Plan 229 — Compact Hot-Path Corrective Closure

Date: 2026-09-20  
Status: corrective-pass — implementation handoff  
Planning baseline: `dd3110fb293623f5ffcf2d284a52e410a18b8b5f`  
Corrects residual from: `plans/226-request-hot-path-single-admission-zero-copy.md` and `plans/227-coordinator-provider-ownership-allocation-cleanup.md`  
Parent roadmap: `plans/225-native-runtime-performance-optimization-roadmap.md`  
Priority: P1 narrow request-path correctness/performance closure  
Execution target: GPT-5.6 Luna/Sol or comparable implementation model

## Purpose

Close the one remaining request-path optimization gap left after Plans 225–228:
`POST /v1/responses/compact` still reparses and recopies request state after it
has already crossed the new single-parse endpoint boundary.

The normal Chat Completions, Responses generation, Messages, and Responses
streaming paths are already on the intended architecture and must not be
reopened.

This corrective pass should:

1. make compact admission consume the already-parsed concrete request;
2. build the compact finite request without reparsing serialized bytes;
3. use the same borrowed synchronous attempt-preparation boundary as ordinary
   finite requests;
4. give native compact/no-model-rewrite dispatch an owned-`Bytes` fast path;
5. remove the now-dead `ProviderClientPoolInner.closed` atomic left behind by
   the ArcSwap topology migration;
6. add closure evidence in this new corrective plan rather than rewriting the
   completed historical Plans 225–228.

No API, provider capability, retry/finalization behavior, routing policy,
configuration, dependency, or runtime-concurrency change is intended.

---

## Why this plan is needed

Plan 226 explicitly required:

> native compact no-rewrite forwarding also has an owned-`Bytes` path

and described the target request path as one bounded parse followed by owned
`Bytes` forwarding when the wire representation is unchanged.

The ordinary generation path now satisfies that contract. The compact path
does not yet.

The Plan 226 historical closure record should remain untouched. This corrective
plan is the append-only record that closes the missed criterion.

---

# Current state

## 1. Compact parses at the endpoint, then parses again in FiniteRequest

`rust/src/coordinator/endpoints.rs::execute_compact_finite` currently starts
correctly:

~~~rust
let parsed = parse_request_body(raw_body, state.max_body_bytes)?;
let payload = parsed.object()?;
...
let resolved = resolve_concrete(state, SURFACE, parsed, ...).await?;
~~~

That means the incoming compact body has already been:

- bounded;
- deserialized;
- depth-checked;
- inspected for stateless Responses policy;
- inspected for finite-only `stream`;
- mutated for provider-qualified/virtual model resolution when needed.

However, the function then calls:

~~~rust
let mut request = FiniteRequest::new_compact(
    proxy_request_id,
    resolved.concrete_body.clone(),
    incoming_headers,
    static_routing_facts(&state.known_providers, SURFACE),
)?;
~~~

`rust/src/coordinator/finite.rs::FiniteRequest::new_compact` calls
`admit_compact_request(&raw_body, ...)`.

That public admission helper deserializes/depth-checks the concrete bytes again.

So the compact endpoint currently crosses the new parsed boundary and then
throws away that advantage before execution.

## 2. Compact also performs an ordinary admission that is not the final compact admission

`resolve_concrete` currently produces a `ResolvedInference` containing an
ordinary `AdmittedRequest` through `admit_resolved`.

That is correct for normal generation requests.

For compact, the final authoritative request should instead be a
`CompactAdmittedRequest`, because compact has additional semantics:

- finite-only;
- replacement-history `input` requirement;
- remote-compaction capability rules;
- `compaction_trigger` rejection for the v1 compact operation;
- native source-preservation used by the compact wire path.

The corrective implementation should avoid doing an ordinary final admission
only to discard it and perform compact admission from serialized bytes.

## 3. Compact still uses the old owned AttemptInput preparation path

`rust/src/coordinator/finite.rs` currently constructs an owned
`AttemptInput` before the compact/generate branch:

~~~rust
let attempt_input = AttemptInput {
    identity: identity.clone(),
    provider: provider.clone(),
    account_api_key: account_key.map(str::to_owned),
    incoming_headers: request.incoming_headers.clone(),
    request_id: request.request_id.clone(),
    correlation_id: request.correlation_id.clone(),
    raw_body: request.raw_body.clone(),
    client_surface: request.client_surface,
    profile: candidate.profile.clone(),
    stream: false,
    candidate_fingerprint: resolution.fingerprint.clone(),
};
~~~

Compact then calls:

~~~rust
self.attempts.prepare_compact(attempt_input, compact_admission)
~~~

Ordinary finite requests already use `AttemptPreparation<'_>`, borrowing the
same data synchronously and materializing one owned `PreparedUpstreamAttempt`
before the network await.

Compact should use the same ownership boundary.

## 4. Compact wire preparation clones admission and copies the entire native body

`rust/src/coordinator/attempt.rs::prepare_compact` currently calls:

~~~rust
self.wire.prepare_compact_request(
    admission.clone(),
    &input.raw_body,
    &context,
)?
~~~

That performs a potentially deep clone of the compact native-preservation
tree.

Then `rust/src/wire/runtime.rs::prepare_compact_request` handles the native
no-model-rewrite path with:

~~~rust
EncodedWireBody {
    value: None,
    bytes: Bytes::copy_from_slice(raw_body),
}
~~~

The request is already held as `Bytes`; the copy is unnecessary when the
provider body is byte-identical.

## 5. ProviderClientPool contains dead close state

After Plan 227, `ProviderClientPool` uses:

~~~rust
topology: ArcSwapOption<ClientTopology>
~~~

as the authoritative open/closed state.

`close()` swaps topology to `None`, and `is_closed()` checks whether the
topology is absent.

`ProviderClientPoolInner.closed: AtomicBool` is now only initialized and
written. It is never read.

That field is dead state and should be removed in this corrective pass.

---

# Required end state

## Compact request lifecycle

For `POST /v1/responses/compact`:

1. ingress `Bytes` is bounded once;
2. JSON is deserialized once;
3. depth validation runs once;
4. stateless and finite-only policy is checked from that parsed value;
5. provider-qualified/virtual model resolution mutates that parsed value in
   memory;
6. compact-specific admission consumes the already-parsed final concrete value;
7. routing facts are derived from that `CompactAdmittedRequest`;
8. the finite coordinator stores the compact admission without reparsing;
9. compact attempt preparation borrows request/generation state synchronously;
10. native no-model-rewrite compact dispatch reuses the request `Bytes`
    backing allocation;
11. a real model rewrite allocates/serializes exactly because the bytes changed;
12. all current compact validation, capability, retry, health, publication,
    cancellation, and finalization semantics remain unchanged.

## Provider client pool

`ArcSwapOption<ClientTopology>` remains the only open/closed topology
authority.

Remove `ProviderClientPoolInner.closed` and its unused `AtomicBool` import
without altering:

- `close()` idempotency;
- `close_count`;
- lookup-after-close failure;
- already-cloned client survival;
- snapshot/providers behavior after close;
- runtime generation lease semantics.

---

# Non-goals

Do not use this pass to:

- reopen Plans 225–228 broadly;
- modify the ordinary `/v1/responses` generation path unless a shared helper
  must be factored without semantic change;
- alter compact request/response schema;
- add translated compaction fallback;
- add stateful Responses continuation support;
- change remote-compaction capability detection;
- change model-router selection/affinity behavior;
- change provider/account routing or fairness;
- change retry/submission budgets;
- change health effects;
- change publication/finalization ownership;
- change streaming behavior;
- change SQLite architecture;
- change Tokio runtime threading;
- change Eggfetch or Eggress;
- add dependencies;
- remove public compatibility helpers merely because production no longer uses
  them.

---

# Workstream 1 — Add parsed compact admission

## Authority

- `rust/src/request/admission.rs`
- `rust/src/request/mod.rs`

Add a crate-internal compact equivalent of the parsed normal admission path.

Conceptually:

~~~rust
pub(crate) fn admit_compact_parsed_request(
    parsed: ParsedRequestBody,
    options: AdmissionOptions,
) -> Result<CompactAdmittedRequest, AdmissionError>
~~~

The exact name may differ.

The helper must consume the already-parsed body and apply the same compact
semantic checks currently owned by `admit_compact_request`.

## Preserve public compatibility helper

Keep the current public:

~~~rust
admit_compact_request(raw_body: &[u8], options: AdmissionOptions)
~~~

source-compatible.

Refactor it to:

1. copy the slice into `Bytes` because that public API did not receive owned
   bytes;
2. call `parse_request_body`;
3. delegate to the same parsed compact-admission core.

Do not keep two independent compact decoders.

## Compact checks that must remain identical

Preserve:

- body-size limit;
- JSON/depth errors;
- top-level object requirement;
- Responses-only surface;
- stateless policy;
- finite-only stream behavior where currently enforced;
- required compact history/input semantics;
- `compaction_trigger` rejection for the compact v1 operation;
- model validation;
- token/context/reservation estimates;
- native preservation;
- native feature summary;
- error variants/messages/status mapping relied on by current tests.

---

# Workstream 2 — Preserve parsed ownership through concrete model resolution

## Authority

- `rust/src/coordinator/endpoints.rs`

The compact endpoint must not call ordinary final admission and then reparse
the result for compact admission.

There are two acceptable implementation shapes.

### Preferred shape: resolution returns resolved parsed body plus routing metadata

Factor the current concrete-model resolution so the common model-routing stage
returns a structure conceptually like:

~~~rust
ResolvedRequestBody {
    parsed: ParsedRequestBody,
    concrete_body: Bytes,
    concrete_model: String,
    provider_id: Option<String>,
    virtual_resolution: Option<VirtualResolution>,
    selector_attempts: u32,
    selector_latency_ms: Option<f64>,
}
~~~

Then:

- ordinary `execute_endpoint` consumes `parsed` through
  `admit_parsed_request`;
- compact `execute_compact_finite` consumes the same `parsed` through
  `admit_compact_parsed_request`.

This keeps model mutation/serialization shared while making the operation's
final admission explicit.

### Acceptable alternative: operation-aware resolved admission enum

An internal enum such as:

~~~rust
ResolvedAdmission::Generate(AdmittedRequest)
ResolvedAdmission::Compact(CompactAdmittedRequest)
~~~

is also acceptable if it avoids duplicate parsing/admission and does not leak
complexity into routing/wire layers.

### Requirements either way

- direct concrete/no-qualifier requests retain the original ingress `Bytes`;
- provider-qualified requests mutate `model` in the already-parsed object and
  serialize once;
- virtual requests derive selector/affinity canonical facts without a second
  parse;
- recursive virtual rejection remains unchanged;
- route id/label/model consistency remains unchanged;
- Eggpool provider qualifiers never reach upstream;
- compact gets exactly one final `CompactAdmittedRequest`;
- ordinary generation remains exactly one final `AdmittedRequest`.

Do not maintain separate duplicate virtual-routing implementations for compact
and generation.

---

# Workstream 3 — Add a from-admitted compact request constructor

## Authority

- `rust/src/coordinator/finite.rs`

Keep the public `FiniteRequest::new_compact` compatibility constructor.

Add a constructor for an already-admitted compact request, conceptually:

~~~rust
FiniteRequest::from_compact_admitted(
    proxy_request_id,
    raw_body: Bytes,
    incoming_headers: HeaderMap,
    compact: CompactAdmittedRequest,
    routing_facts: RoutingRequestFacts,
)
~~~

The endpoint hot path should use this constructor.

## Required invariants

Validate before constructing:

- Responses client surface;
- finite-only compact operation;
- `routing_facts.canonical_model_id == compact.canonical.model`;
- `routing_facts.request_surface` matches Responses;
- compact native preservation exists by construction;
- final concrete model matches resolution;
- provider pin is copied into routing facts exactly once.

## Preserve FiniteRequest public shape

Do not delete or rename existing public fields in this corrective pass.

The existing coordinator expects `request.admitted.canonical` for general
routing/response adaptation and `request.compact_admission` for compact wire
preparation.

If constructing both views requires a compatibility clone, keep that bounded
clone rather than changing the public `FiniteRequest` shape in this pass.

However:

- do not perform another JSON parse;
- do not clone the compact admission again during each attempt;
- do not keep both pre-resolution and post-resolution JSON trees.

If a safe internal conversion can avoid cloning the large preservation tree
without changing the public type, use it. Otherwise leave that separate
optimization for a future evidence-backed API-internal refactor.

---

# Workstream 4 — Bring compact onto borrowed attempt preparation

## Authority

- `rust/src/coordinator/finite.rs`
- `rust/src/coordinator/attempt.rs`

Add a compact counterpart to the existing borrowed preparation method,
conceptually:

~~~rust
pub(crate) fn prepare_compact_borrowed(
    &self,
    input: AttemptPreparation<'_>,
    admission: &CompactAdmittedRequest,
) -> Result<PreparedUpstreamAttempt, AttemptError>
~~~

The borrowed value must not cross an await.

## Finite coordinator

Do not build the owned `AttemptInput` unconditionally before determining
whether the request is compact.

Instead, construct one `AttemptPreparation<'_>` from:

- identity;
- provider;
- borrowed account key;
- incoming headers;
- request/correlation IDs;
- raw `Bytes`;
- client surface;
- selected profile;
- candidate fingerprint.

Then dispatch synchronously to:

- `prepare_compact_borrowed` for compact;
- existing `prepare_borrowed` for normal finite generation.

Only the returned `PreparedUpstreamAttempt` may cross
`submit_once(...).await`.

## Preserve owned compatibility method

Keep:

~~~rust
AttemptBuilder::prepare_compact(AttemptInput, &CompactAdmittedRequest)
~~~

if it is part of the current Rust/test surface.

Refactor it to delegate to the borrowed core rather than maintaining duplicate
header/path/auth logic.

---

# Workstream 5 — Add owned-Bytes compact wire dispatch

## Authority

- `rust/src/wire/runtime.rs`
- `rust/src/coordinator/attempt.rs`

Add a crate-internal compact dispatch method analogous to
`prepare_admitted_dispatch`, conceptually:

~~~rust
prepare_compact_dispatch(
    admission: &CompactAdmittedRequest,
    raw_body: Bytes,
    context: &WireRuntimeContext,
) -> Result<PreparedWireDispatch, WireRuntimeError>
~~~

The exact return type may reuse the existing `PreparedWireDispatch`.

## Share validation with public prepare_compact_request

Do not fork compact validation.

Factor a common compact preparation core that preserves:

- Responses source surface;
- OpenAI Responses target surface;
- native v1 remote-compaction capability;
- canonical/upstream model identity;
- model rewrite behavior;
- stream adapter/profile validation;
- byte facts/metadata required by the public inspection path.

The public slice-based `prepare_compact_request` remains available.

Because that API receives `&[u8]`, it may preserve its compatibility copy.

The new coordinator dispatch path receives `Bytes` and must not copy an
unchanged native request.

## Native no-rewrite case

When:

- client surface is Responses;
- selected upstream surface is OpenAI Responses;
- native compact v1 capability is present;
- upstream model equals admitted canonical model;

the prepared provider body must reuse the supplied `Bytes` backing
allocation.

Prefer moving the `Bytes` handle when ownership allows; otherwise a
`Bytes::clone` is acceptable.

Do not call `Bytes::copy_from_slice` on this path.

## Model rewrite case

When the selected upstream model differs:

- clone/mutate the preserved parsed value as required;
- change only the Eggpool-owned `model` field;
- bounded compact-serialize once;
- send those newly encoded bytes.

A new allocation is correct because the wire representation changed.

---

# Workstream 6 — Remove dead ProviderClientPool closed atomic

## Authority

- `rust/src/providers/client_pool.rs`

Remove:

- `AtomicBool` import if no other code needs it;
- `ProviderClientPoolInner.closed`;
- initialization;
- `closed.store(true, ...)` in `close()`.

Keep:

- `ArcSwapOption<ClientTopology>` as authoritative state;
- `AtomicUsize close_count`;
- `close()` using `topology.swap(None)`;
- `is_closed()` using topology absence.

Do not change race semantics.

A lookup that acquired an immutable topology/client before close may finish.
A lookup after the topology has been atomically removed must fail closed.

---

# Workstream 7 — Regression tests

## Compact parsed admission

Add/extend tests proving parity between public slice admission and parsed
compact admission for:

- valid compact request;
- body too large;
- malformed JSON;
- top-level non-object;
- missing/invalid model;
- missing required compact input/history;
- `store: true`;
- continuation/conversation/background rejection;
- streaming compact rejection;
- `compaction_trigger` rejection;
- native preservation/feature facts;
- token/context/reservation facts.

Do not expose `ParsedRequestBody` publicly just to test it. Crate-local unit
coverage is acceptable.

## Endpoint path

Add a focused regression proving the compact production endpoint:

- resolves direct model;
- resolves provider-qualified model and preserves provider pin;
- resolves a permitted virtual model exactly as before;
- constructs compact routing facts from the final compact admission;
- does not invoke the public reparsing constructor.

Prefer testing observable results plus structural ownership helpers rather than
adding production parse counters.

## Wire ownership

Add a focused `wire_runtime` test:

1. create owned `Bytes` for a native compact request;
2. retain a source handle/pointer;
3. call the owned compact dispatch method with no model rewrite;
4. assert byte equality;
5. assert the full unsliced `Bytes` backing pointer is shared;
6. assert a model-rewrite case changes the body correctly and does not claim
   byte identity.

Pointer identity is only an invariant of the new owned-`Bytes` internal
dispatch API, not of the public `&[u8]` compatibility method.

## Attempt preparation parity

Prove owned and borrowed compact preparation produce identical:

- method;
- path;
- provider/account identity;
- profile;
- auth/static/forwarded headers;
- request identity headers;
- body bytes;
- stream=false;
- error classification for unsupported compact target/capability.

## ProviderClientPool cleanup

Existing provider transport tests should prove:

- close idempotency;
- close count;
- lookup after close;
- provider/account lookup;
- account fallback behavior;
- previously cloned client survival.

Add a new test only if removal of the dead field exposes a coverage gap.

---

# Focused qualification

Run at minimum:

~~~bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings

cargo test --manifest-path rust/Cargo.toml --test codex_compaction_compat -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test codex_responses_compat -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test wire_runtime -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test wire_qualification -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test wire_adaptation -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_c008 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_c009 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_c011 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_boundaries -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_finalization -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_publication -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test provider_transport -- --test-threads=1
~~~

Then run the repository gates:

~~~bash
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
cargo build --manifest-path rust/Cargo.toml --locked --release

uv sync --frozen
uv run ruff format --check scripts/ tests/tooling/
uv run ruff check scripts/ tests/tooling/
uv run pyright scripts/
uv run pytest tests/tooling/ -q --tb=short --maxfail=1

git diff --check
~~~

No dependency/feature change is expected.

If `rust/Cargo.toml` or `rust/Cargo.lock` changes, stop and justify it before
continuing; this corrective pass should not need Cargo graph changes.

---

# Structural closure evidence

The implementation closure should record the following facts explicitly.

## Before corrective pass

For a direct native compact/no-model-rewrite request:

- endpoint bounded parse/depth check: 1;
- ordinary resolved admission: 1 in-memory admission;
- compact public admission: reparses serialized body;
- compact attempt preparation: owned `AttemptInput` clones request/config
  state;
- compact wire preparation: clones `CompactAdmittedRequest`;
- native provider body: full `Bytes::copy_from_slice`.

## After corrective pass

For the same request:

- JSON deserializations before provider dispatch: exactly 1;
- depth validations at the ingress parsed boundary: exactly 1;
- final authoritative admission: one compact admission from the parsed value;
- no public compact reparse constructor on the production endpoint path;
- no owned `AttemptInput` construction solely for compact synchronous
  preparation;
- no compact admission clone solely for wire dispatch;
- full request-body copies on native/no-rewrite compact dispatch: 0;
- provider send receives a `Bytes` handle sharing the resolved request
  allocation.

Model/provider-qualified or virtual requests may serialize once when Eggpool
must rewrite the `model` field. That allocation is expected and should not be
reported as a regression.

---

# Planning-record handling

Do not edit the completed Plan 226 checklist or closure text to pretend the
original closure was exact.

The repository planning convention is append-only.

When this plan is implemented:

1. set Plan 229 to `complete`;
2. append an implementation/closure record here with commit SHA and evidence;
3. state that Plan 229 closes the missed compact criterion from Plan 226;
4. leave Plans 225–228 unchanged unless a separate documentation-only
   supersession note is required by repository convention.

The resulting history should make the sequence clear:

~~~text
Plan 226
  -> normal generation hot path correctly optimized
  -> compact owned-Bytes criterion missed in closure

Plan 229
  -> compact parsed-admission + borrowed-preparation + owned-Bytes correction
  -> Plan 226 residual fully closed
~~~

---

# Stop conditions

Stop and investigate rather than forcing the optimization if:

1. parsed compact admission produces different validation/error semantics from
   `admit_compact_request`;
2. the refactor changes virtual-model selection or affinity;
3. provider-qualified compact requests lose their routing pin;
4. compact v1 capability gating changes;
5. unsupported translated compact targets begin reaching a provider;
6. the new owned-`Bytes` path bypasses model rewrite or compact validation;
7. borrowed compact preparation must survive across `submit_once().await`;
8. fixing the duplicate compact storage would require changing public
   `FiniteRequest` field types;
9. client-pool cleanup changes close race semantics;
10. the pass begins touching SQLite, Tokio runtime, stream-body plumbing,
    Eggfetch, Eggress, or routing policy.

---

# Completion criteria

This corrective pass is complete when:

- [ ] `execute_compact_finite` performs one bounded JSON parse/depth check;
- [ ] compact final admission consumes the already-parsed concrete request;
- [ ] the production compact endpoint does not call
      `FiniteRequest::new_compact`;
- [ ] `FiniteRequest::new_compact` remains available as a compatibility
      constructor;
- [ ] an already-admitted compact constructor/path is used by production;
- [ ] direct/provider-qualified/virtual compact routing semantics are unchanged;
- [ ] compact attempt preparation uses `AttemptPreparation<'_>`;
- [ ] compact credentials/headers/provider/profile/request identity are borrowed
      through synchronous preparation;
- [ ] `PreparedUpstreamAttempt` remains fully owned before provider I/O;
- [ ] compact wire dispatch borrows `CompactAdmittedRequest` rather than
      cloning it solely for preparation;
- [ ] native/no-model-rewrite compact dispatch reuses owned `Bytes`;
- [ ] model-rewrite compact dispatch allocates only because bytes change;
- [ ] public slice-based compact admission/wire helpers remain compatible;
- [ ] `ProviderClientPoolInner.closed` and unused `AtomicBool` state are
      removed;
- [ ] provider pool close behavior remains unchanged;
- [ ] `codex_compaction_compat`, wire, coordinator, provider, default, and
      no-default qualification passes;
- [ ] full repository validation passes;
- [ ] closure evidence records one parse and zero native full-body copies for
      the corrected compact path;
- [ ] this plan is marked complete with the implementation commit SHA.

---

# Handoff note

Keep this pass narrow.

The desired compact path is:

~~~text
Axum Bytes
  -> parse_request_body once
  -> stateless/finite inspection
  -> in-memory model resolution
  -> admit_compact_parsed_request
  -> FiniteRequest from already-admitted compact state
  -> borrowed AttemptPreparation
  -> prepare_compact_dispatch with owned Bytes
  -> unchanged Bytes handle OR one required model-rewrite encoding
  -> owned PreparedUpstreamAttempt
  -> provider send
~~~

Once that path is true and the dead client-pool atomic is removed, stop. The
broader performance campaign does not need another concurrency or architecture
round.

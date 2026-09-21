# Plan 233 — Native Responses Stream Observer Efficiency

Date: 2026-09-21
Status: implementation handoff
Planning baseline: 3b9b63861e554161c152520491e0bd050c864f02
Parent roadmap: plans/230-residual-native-runtime-efficiency-roadmap.md
Prerequisite: Plan 231 complete; Plan 232 independent
Priority: P1/P2 evidence-gated streaming CPU/allocation reduction

## Purpose

Reduce unnecessary semantic materialization on the native Responses-to-Responses streaming path while preserving byte-exact forwarding, terminal evidence, usage accounting, malformed-stream behavior, and the existing streaming ownership boundary.

The current path is already zero-copy at the downstream byte handoff:

~~~text
provider Bytes
 -> WireStream observer
 -> original provider Bytes
 -> downstream
~~~

However, WireStream::push always asks StreamEventDecoder::push for Vec<CanonicalEvent>. The decoder frames SSE, constructs per-frame JSON values, performs full canonical event decoding, and materializes canonical events. ActiveStream::decode_chunk then uses those events primarily for terminal observation before forwarding the original input chunk unchanged.

Cross-surface translated streaming genuinely needs CanonicalEvent values. Native-observed streaming may not.

This plan is conditional: first demonstrate that observer work is material under a deterministic long-stream workload. If it is not, close the plan without changing production code.

## Authority

- rust/src/wire/stream.rs
- rust/src/wire/runtime.rs
- rust/src/coordinator/streaming/execution.rs
- rust/src/coordinator/streaming/terminal.rs
- rust/src/coordinator/streaming/coordinator.rs
- rust/src/coordinator/streaming/types.rs
- rust/tests/wire_stream.rs
- rust/tests/wire_runtime.rs
- rust/tests/wire_qualification.rs
- rust/tests/codex_responses_compat.rs
- rust/tests/coordinator_c008.rs
- rust/tests/coordinator_c009.rs
- rust/tests/coordinator_c011.rs

## Non-negotiable streaming invariants

Preserve all of the following:

- native Responses-to-Responses output bytes are the original valid provider bytes;
- unknown valid Responses event types are forwarded unchanged;
- SSE framing remains incremental and bounded by the existing frame limit;
- malformed framing/event behavior remains fail-closed;
- response.completed is required for native Responses success;
- response.incomplete, response.failed, and error remain terminal non-success evidence as currently classified;
- usage accumulation and missing-final-usage facts remain equivalent;
- post-terminal provider data remains detected;
- EOF is not synthesized into success;
- no retry or alternate-wire negotiation occurs after StreamingExecution handoff;
- cancellation/drop/finalization ownership remains in streaming/execution.rs and terminal.rs;
- translated streams retain the existing stateful encoder and canonical event behavior.

Do not introduce a second SSE parser.

## Phase 1 — Characterize native observer overhead

Build an ephemeral deterministic provider fixture that emits native Responses SSE with fixed content and terminal evidence.

Use representative stream lengths, for example:

- 32 events;
- 512 events;
- several thousand small delta events;
- usage plus response.completed;
- a mix containing unknown valid event types.

Measure release-build local work with direct loopback transport. Useful dimensions are elapsed CPU time, process CPU, allocation counts if available from standard host tooling, and peak RSS. Do not add a production allocator or permanent benchmark dependency.

Compare:

1. existing native-observed WireStream path;
2. a fixture-only raw framing baseline sufficient to show how much local cost belongs to canonical observation.

The baseline must not become production code.

Proceed to implementation only if the existing observer is a meaningful contributor to local stream cost or allocation volume.

## Phase 2 — Add a shared observation sink, not a second decoder

Preferred first implementation:

Refactor StreamEventDecoder so one framing/decoding implementation can feed either:

- a collecting canonical-event sink for translated streams and public compatibility helpers;
- an observation/fold sink for native-observed streams.

Conceptually:

~~~text
SseDecoder
  -> shared frame decode
       -> terminal/usage/error state
       -> optional canonical event sink
~~~

A native observation result should contain only bounded information the coordinator actually consumes, such as:

- whether terminal evidence appeared in the pushed bytes;
- usage state already retained by the decoder;
- parser/framing error through the existing Result;
- cumulative byte facts remain on WireStream.

Do not return Vec<CanonicalEvent> on the live native path if no caller consumes the events.

## Phase 3 — Avoid avoidable frame/event ownership

Within the shared implementation, remove only allocations that are unnecessary for observation and can be proven equivalent.

Candidates include:

- avoid an outer Vec<CanonicalEvent> for native-observed pushes;
- avoid cloning event/frame strings solely to construct a temporary wrapper Value when the existing codec can consume borrowed frame facts;
- fold terminal and usage facts as events are decoded rather than constructing a second collection for later scanning.

This phase must preserve one semantic decoder authority. Do not duplicate OpenAI Responses event classification in streaming/execution.rs or terminal.rs.

If eliminating individual CanonicalEvent construction would require a separate provider-specific parser or substantial codec duplication, stop after the sink/fold optimization and record the remaining cost.

## WireRuntime shape

WireStream::push is public within the Rust runtime surface and currently returns StreamPushResult containing events.

Do not remove or change this method.

Add a crate-private native-observation method if needed, conceptually:

~~~rust
pub(crate) fn observe_native_push(
    &mut self,
    bytes: &[u8],
) -> Result<NativeStreamObservation, WireRuntimeError>
~~~

The production native-observed coordinator path may use this method.

Translated streaming and compatibility tests may continue using push.

Likewise, keep finalize/StreamFinalization public behavior intact. If a native finalization helper avoids building an unused final Vec, add it internally and retain finalize as the compatibility wrapper.

## Coordinator integration

rust/src/coordinator/streaming/execution.rs should select by the existing StreamForwardingMode:

- NativeObserved -> call the internal observer path and forward the original Bytes;
- Translated -> call WireStream::push and encode canonical events exactly as today.

Do not move SSE parsing into the coordinator.

Do not change client_bytes/events_forwarded semantics except where events_forwarded is explicitly translated-output accounting. Preserve diagnostics interpretation.

## Equivalence test matrix

For native Responses observation, prove old public push/finalize behavior and the new internal observer produce equivalent terminal/accounting state for:

- response.created;
- text/reasoning delta events;
- function/custom/deferred tool events;
- interleaved calls;
- response.completed;
- response.incomplete;
- response.failed;
- provider error;
- final usage;
- missing final usage;
- multiple frames in one transport chunk;
- one frame split across transport chunks;
- comments/blank records;
- unknown valid event type;
- malformed JSON/event;
- oversized frame;
- incomplete frame at EOF;
- data after terminal;
- no payload before EOF.

Byte-forwarding tests must assert the native coordinator still sends the exact source bytes, including unknown event text and original formatting.

Codex compatibility must remain unchanged.

## Focused qualification

~~~bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings

cargo test --manifest-path rust/Cargo.toml --test wire_stream -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test wire_runtime -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test wire_qualification -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test codex_responses_compat -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_c008 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_c009 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_c011 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_boundaries -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_finalization -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_publication -- --test-threads=1
~~~

Then run the shared Plan 230 closure gates.

## Stop conditions

Close with no implementation, or stop at the shared sink/fold layer, if:

1. deterministic measurement shows observer CPU/allocation is immaterial;
2. eliminating event construction requires a second SSE parser;
3. terminal/usage/error semantics diverge between collection and observation;
4. unknown valid events would no longer be forwarded byte-exactly;
5. the change pushes parsing into coordinator/terminal modules;
6. translated-stream stateful encoding becomes more complex;
7. additional buffering is required;
8. a new dependency or unsafe code would be needed.

## Completion criteria

Either:

- [ ] native-observed production pushes no longer construct an unused Vec<CanonicalEvent>;
- [ ] shared framing/semantic decoding remains the single authority;
- [ ] native source Bytes remain byte-exact downstream;
- [ ] terminal, usage, malformed, EOF, post-terminal, and cancellation behavior is equivalent;
- [ ] translated streaming is unchanged;
- [ ] deterministic measurements record the effect;

or:

- [ ] measurements demonstrate the optimization is not justified and the plan closes with no production change.

Record the implementation/closure SHA and do not convert this into a general streaming rewrite.

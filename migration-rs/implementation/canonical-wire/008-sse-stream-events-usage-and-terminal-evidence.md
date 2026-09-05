# W008 — SSE, Canonical Stream Events, Usage, and Terminal Evidence

Status: planned; blocked on W007 closure

Source roadmap: `migration-rs/subsystems/canonical-wire-roadmap.md#w008--sse-framing-canonical-stream-events-usage-and-terminal-evidence`

Primary class: capability/invariant

Hard dependency: W007 accepted closure.

## 1. Objective

Port EggPool's incremental streaming transformation boundary: bounded SSE framing, provider-family stream-event decoding to canonical events, client-profile event encoding, normalized usage accumulation, and structured terminal evidence. M6 must make stream semantics deterministic without owning socket reads, timeouts, retry, downstream handoff, cancellation, or durable finalization.

## 2. Python oracle

Primary sources are `proxy.sse.SSEDecoder`, streaming wire codecs/transcoders, `proxy.normalized_usage` and stream usage merge helpers, plus W001 chunk-split/event/terminal observations. Coordinator stream diagnostics/completion policy are M7 sources only for documenting the boundary.

## 3. Incremental SSE decoder

Implement a byte-oriented bounded decoder matching Python semantics for arbitrary chunk boundaries, LF/CRLF, blank-line event termination, `data:` whitespace and multiline joining, `event:`/`id:`, comments/ignored fields, empty records, profile-specific `[DONE]`, EOF flush behavior, and invalid UTF-8 handling. The carry buffer has an explicit hard bound; oversized unterminated records fail typed validation instead of growing until OOM.

## 4. Canonical stream events

Represent response/message start, text/content block start/delta/end, reasoning/thinking start/delta/end, tool/function call start/name/argument deltas/end, usage updates/final usage, finish/stop metadata, provider error evidence, and explicit semantic terminal events. Preserve stable indexes/IDs required to assemble concurrent blocks without retaining raw provider JSON.

## 5. Provider stream decoding

Decode all four supported wire-family stream records into canonical events. Malformed JSON is typed parse evidence; unknown benign events follow frozen ignore/warn policy; provider error events remain provider errors; usage retains zero/missing distinctions; terminal recognition is profile-aware; EOF without required terminal evidence remains distinguishable from completion.

## 6. Client stream encoding

Encode canonical events to every supported client streaming surface while preserving delta order, tool/function indexes/IDs/arguments, reasoning deltas, finish reasons, usage behavior, and correct SSE/`[DONE]` framing. Unrepresentable material events use W006 warning/rejection policy and provider errors are never silently discarded.

## 7. Usage normalization

Port input/output/total tokens, cached/cache-read tokens, cache-creation/write tokens, reported/absent/unknown cache status, nested provider shapes, incremental merge, and final-event semantics. Explicit zero must remain distinct from missing. Unknown-shape diagnostics may retain bounded key/type metadata, not raw provider payloads.

## 8. Terminal evidence

Return a typed terminal summary distinguishing canonical success, compatibility terminal evidence accepted by Python, explicit provider error, malformed framing/event, EOF before body, EOF after partial body without terminal evidence, and missing final usage where diagnostic. M7 decides retry/downstream/finalization consequences.

## 9. Bounded state

Retain only bounded partial SSE bytes, active content/tool IDs/indexes, bounded partial arguments, usage counters, and terminal flags. Never buffer a complete stream merely to transcode it. No per-event task spawning.

## 10. Hard M7 boundary

Expose incremental push/finalize operations only. Do not own header/first-byte/idle timeouts, client cancellation, response-start/handoff state, retry legality, health effects, or attempt/request finalization.

## 11. Required differential tests

Exercise whole-buffer/every-byte/deterministic split variants, LF/CRLF/multiline/comment/event/id behavior, each provider-family text stream, fragmented tool calls, reasoning streams, multiple active indexes, usage/final merge, `[DONE]`/family terminals, provider errors, malformed event JSON, oversized carry, EOF at each phase, canonical-to-client stream encoding, bounded-state assertions, and redaction/no-raw-payload diagnostics.

## 12. Verification

Run Rust all-target tests, W001 migration observations, targeted Python SSE/streaming/usage tests, format/lint/type checks, and `git diff --check`. No network/provider call.

## 13. Acceptance criteria

W008 closes only if stream semantics are chunk-boundary independent, all provider/client families match the oracle, usage/cache distinctions match, malformed/incomplete/provider-error streams cannot masquerade as success, decoder state is bounded, and no timeout/retry/handoff/finalization behavior has leaked in.

## 14. Stop conditions

Do not close if complete streams are buffered, EOF is universal success, provider errors are dropped, tool deltas lose identity/order, cache zero is conflated with missing, or M7 lifecycle behavior appears.

## 15. Closure evidence

Create `migration-rs/closure/canonical-wire/008-status.md` with chunk matrix, terminal/usage evidence, memory-bound review, verification, and registry transition promoting W009.

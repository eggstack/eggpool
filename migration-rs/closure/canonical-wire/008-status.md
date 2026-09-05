# W008 Closure — SSE, Canonical Stream Events, Usage, and Terminal Evidence

Status: closed

Implementation commit: [`6cf01595`](https://github.com/eggstack/eggpool/commit/6cf015954f2b676b5f01a4e08a107bbbab84961e)

Plan: [W008 — SSE, canonical stream events, usage, and terminal evidence](../../implementation/canonical-wire/008-sse-stream-events-usage-and-terminal-evidence.md)

Contract and oracle: [M6 canonical wire contract](../../canonical-wire-contract.md),
the W001 observations under `fixtures/canonical-wire/`, the W002 canonical IR,
the W004-W007 finite codecs/adaptation policy, and the Python
`proxy.sse`, `proxy.sse_observer`, `proxy.usage`, `proxy.normalized_usage`, and
wire codec modules.

## Outcome

W008 adds the bounded Rust streaming boundary in `rust/src/wire/stream.rs`.
`SseDecoder` incrementally consumes arbitrary byte chunks, handles UTF-8
fragmentation, LF/CRLF, blank-line records, multiline `data`, `event`, `id`,
comments, ignored fields, comment-only frames, and EOF flushing. A 64 KiB
record bound prevents unbounded carry growth and returns typed framing failure
before allocation can grow without limit.

`StreamEventDecoder` maps OpenAI Chat, OpenAI Responses, Anthropic Messages,
Gemini Interactions, and Gemini `generateContent` records into the existing
bounded `CanonicalEvent` vocabulary. It emits provider errors as error events,
returns malformed JSON as typed provider-event evidence, and never buffers a
complete stream. `encode_client_event` emits the Chat, Responses, and Messages
client SSE grammars while preserving text/reasoning deltas, tool indexes and
identities, argument order, usage placement, and terminal framing.

Usage normalization and the incremental accumulator preserve absent fields,
unknown usage shapes, explicit zero, cache reads, cache writes/creation,
reasoning counts, and family-specific nested usage shapes. Terminal summaries
distinguish successful native evidence, non-success terminal evidence,
provider errors, malformed input, EOF before body, and EOF after a partial
body. A successful stream without a final usage event retains a bounded
`missing_final_usage` diagnostic.

The implementation owns no socket reads, transport timeout, cancellation,
downstream response handoff, retry, health effect, durable persistence, or
request finalization. It exposes push/finalize operations only.

## Requirement-to-evidence matrix

| W008 requirement | Evidence | Result |
|---|---|---|
| Bounded chunk-independent SSE framing | `sse_framing_is_chunk_boundary_independent_for_lf_crlf_multiline_and_utf8`; every-byte fixture feed | Pass |
| LF/CRLF, comments, ignored fields, event/id, multiline data | `sse_framing_is_chunk_boundary_independent_for_lf_crlf_multiline_and_utf8` | Pass |
| Invalid UTF-8 and bounded carry | incremental UTF-8 decoder with replacement accounting; `oversized_carry_is_a_typed_bounded_failure` | Pass |
| Five provider-family stream decoders | `all_provider_streams_are_incremental_and_require_native_terminal_evidence`; W001 fixture profiles | Pass |
| Text, reasoning, usage, terminal, and provider-error events | `wire_stream` event assertions plus `malformed_event_is_typed_and_provider_error_is_not_dropped` | Pass |
| Tool/function identity, indexes, and argument deltas | provider decoder branches and client encoder coverage in `client_event_encoding_preserves_surface_terminal_and_tool_grammar` | Pass |
| Usage/cache zero-vs-missing/unknown semantics | `usage_preserves_explicit_zero_and_missing_fields`; `usage_normalization_distinguishes_omitted_unknown_and_zero` | Pass |
| Native terminal evidence and incomplete EOF | `all_provider_streams_are_incremental_and_require_native_terminal_evidence`; `eof_without_terminal_is_not_success_and_final_unterminated_event_is_flushed` | Pass |
| Client Chat/Responses/Messages SSE encoding | `client_event_encoding_preserves_surface_terminal_and_tool_grammar`; `WireCodec::encode_stream_event` | Pass |
| No complete-stream buffering or M7 leakage | bounded decoder/observer state; no HTTP, async task, retry, handoff, persistence, health, timeout, or finalization code in W008 | Pass |
| Secret/raw-payload redaction | bounded structural errors and `Debug`-safe existing canonical types; provider error text capped at 4 KiB | Pass |

## Terminal and usage evidence

| Profile | Required success evidence | Rust evidence |
|---|---|---|
| OpenAI Chat Completions | `[DONE]` | `TerminalEvidence::OpenaiDone` |
| OpenAI Responses | `response.completed` | `TerminalEvidence::ResponsesCompleted` |
| Anthropic Messages | `message_stop` | `TerminalEvidence::AnthropicMessageStop` |
| Gemini Interactions | `completed`/`requires_action` status | `TerminalEvidence::GeminiCompleted` |
| Gemini generateContent | `finishReason=STOP` | `TerminalEvidence::GeminiCompleted` |

`response.incomplete`, `response.failed`, non-STOP Gemini finishes, provider
errors, malformed events, and EOF without native evidence cannot masquerade as
successful completion.

## Differential and verification results

The focused Python oracle and contract run passed 45 tests. The Rust target
suite passed 157 tests across 18 suites, including 9 W008-specific tests. The
Rust all-byte split stream cases produce the same event ordering as the W001
fixture inventory, including Gemini usage before its terminal event.

Commands completed successfully:

```text
rtk cargo fmt --manifest-path rust/Cargo.toml
rtk cargo clippy --manifest-path rust/Cargo.toml --all-targets -- -D warnings
rtk cargo test --manifest-path rust/Cargo.toml --all-targets -- --test-threads=1 -q  # 157 passed
rtk uv run pytest tests/migration_rs/test_w001_canonical_wire.py tests/unit/test_wire_ir.py tests/unit/test_wire_codecs.py tests/contract/test_transcoder_contract.py -q --tb=short --maxfail=1  # 45 passed
```

No live provider, credential, database migration, network call, background
task, or dependency/lockfile change was used.

## Resource, security, and migration review

- SSE carry and frame state are explicitly bounded at 64 KiB; events are
  emitted per frame and are not retained as a complete stream.
- Invalid UTF-8 is counted and replaced; malformed JSON is typed evidence;
  oversized records fail closed before unbounded state growth.
- Provider error messages are bounded, and canonical event debug output
  remains structural rather than exposing raw provider JSON or credentials.
- No database schema, config field, API endpoint, CLI command, Cargo
  dependency, provider client, filesystem path, or network behavior changed.
- The implementation remains synchronous and caller-driven, preserving the
  M6/M7 boundary for timeout, cancellation, retry, handoff, health, and
  finalization policy.

## Supported differences and deferred work

- The Rust boundary reports oversized records as typed framing failure; it
  does not retain or replay discarded oversized bytes. This is the bounded
  equivalent of the Python oracle's discarded-frame evidence.
- Selected-profile composition remains W009. Dynamic wire negotiation,
  alternate-wire retry, provider submission, durable attempts, response
  handoff, cancellation, timeout policy, health/failure effects, and
  finalization remain M7 responsibilities.
- Integrated cross-surface qualification and M6 aggregate closure remain
  W010.

No unresolved mandatory W008 requirement remains. No corrective pass is
required.

## Registry transition and future-plan audit

W008 is removed from the dependency-ready table and recorded in the completed
table in `migration-rs/registry.md`. Its plan, implementation index, roadmap,
handoff sequence, and status header are marked closed.

W009 is the only future plan unblocked by W008 under the repository's serial
handoff policy, so it is promoted to `dependency-ready; W008 closure accepted`
in the registry, implementation index, handoff sequence, roadmap, and plan
header. W010 remains planned and blocked on W009. M7 implementation handoff
remains blocked on accepted W010 closure. No other future plan can be safely
unblocked by W008 alone.

Recommendation: closed; proceed with W009 only.

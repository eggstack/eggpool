# Deep Dive: Protocol Transcoding

Back to [Architecture](README.md)

`rust/src/wire/` owns the closed codec registry and protocol adaptation between
OpenAI Chat Completions, OpenAI Responses, Anthropic Messages, and Gemini
surfaces. `rust/src/wire/ir.rs` captures canonical request, reasoning, usage,
function/freeform/deferred-search tool, response-block, provider-error, and
stream-event semantics before adaptation. `rust/src/wire/ir.rs` remains the
EggPool canonical boundary facade.

M002 extraction seam (no source move yet): the pure sans-I/O kernel is
`wire::{ir, adaptation (neutral), codec, codecs, additional_codecs, decode,
registry (neutral), stream}`. It never imports EggPool routing, catalog,
config, request-runtime/resource, model-router, provider, database, server,
coordinator, Tokio, Axum, Hyper, TLS, or transport types (guarded by
`rust/tests/wire_kernel_boundary.rs`). EggPool-owned joins live in
`wire::adapters` (catalog/request/config→neutral facts, thinking-requirement,
profile joins, compaction constructors), `wire::runtime`, and
`request::admission` (one bounded parse, stateless Responses policy,
token/context estimates, routing/affinity projection). `wire::decode` owns
protocol-structure decoding with explicit `DecodeLimits::current()`; EggPool
passes the current constants unchanged. The `wire_extraction_contract`
corpus freezes exact/adapted/rejected behavior before the M003 move.

M003 workspace split: the kernel is the single source of truth in
`rust/crates/eggpool-wire` (`publish = false`; deps only serde/serde_json/
thiserror/sha2/toml; no runtime/network/persistence). `rust/src/wire/`
modules are narrow facades (`pub use eggpool_wire::...`) preserving the
canonical `wire::ir` boundary path; `wire::adapters`, `wire::runtime`, and
`request::admission` remain EggPool-owned. Root integration tests stay
authoritative; the crate runs its own package tests. No new binary or
packaging artifact was introduced.

Each concrete codec builds a fresh target payload from canonical intent. Loss
policy and provider capability contracts independently govern reasoning, tools,
structured output, multimodal content, and cache controls. Unsupported fields
are warned or rejected according to policy; the adapter never invents hidden
reasoning or provider metadata.

The exception is the bounded native Responses request path. Admission retains
the parsed source envelope separately from the canonical IR, so a
Responses-to-Responses target can preserve ordered native input items,
encrypted reasoning, native tool definitions, and extensions. Only EggPool-owned
`model` rewriting is applied for an alias target. A non-Responses target must
consult the preservation summary first: native-only items/tools are a typed
`UnsupportedSemanticFeature` blocker, and safely omittable extensions become
explicit adaptation notices rather than disappearing silently.

Responses `custom` tools use provider-neutral
`CanonicalToolKind::Freeform`; client-executed `tool_search` uses
`CanonicalToolKind::DeferredSearch`. Function-only codecs wrap freeform bodies
in one bounded `input` string and expose deferred search with its exact
bounded `query`/`limit` schema, using the per-request declaration (never the
name alone) to reconstruct downstream `custom_tool_call`/`tool_search_call`
items. Malformed wrappers fail closed and are never forwarded as wrapper JSON
text. Namespaces, hosted/server search, shell, image-generation, and other
server-owned tools remain native-only unless a general canonical equivalent is
added. Eggpool never executes the search itself.

Remote-compaction capabilities (`supports_remote_compaction_v1` plus an
optional compact path, `supports_remote_compaction_v2`) live on the
provider-owned `wire_surfaces` table and resolve into `CompactionCapabilities`
at dispatch time; the closed codec registry stays data-only. The v1 compact
operation preserves the source-native compact envelope exactly except for the
EggPool-owned model rewrite, and its result is bounded and validated without
canonicalizing it into an ordinary completion. A v2 `compaction_trigger`
input item is a native-only signal: translated targets reject it as
`UnsupportedSemanticFeature`, and native Responses targets preserve it only
with explicit v2 capability. Neither form is ever converted to user text.

Streaming adapters preserve native event grammar and require a native terminal
event. Responses-to-Responses uses an observe-and-forward mode: the bounded
SSE decoder observes terminal/usage evidence while the original valid provider
bytes, including unknown future event types, are sent to the caller unchanged.
Cross-surface Responses output uses a separate stateful encoder. It keeps
bounded active text, reasoning, and argument buffers; emits indexed completed
message/reasoning/function/custom/deferred-search call items; and keeps a
generated output-item ID distinct from the canonical invocation `call_id`.
Deferred argument fragments accumulate silently by source index/call identity
(no invented delta grammar); the authoritative `tool_search_call` done item
carries the bounded `query`/`limit` object. It never fabricates OpenAI
encrypted reasoning content. Interleaved call deltas remain associated with
their source index and call identity, while subsequent tool outputs are paired
by `call_id` even when output-item order differs. EOF,
cancellation, and malformed frames remain typed failures, and alternate wire
negotiation is bounded and uses the same request submission budget as account
retries.

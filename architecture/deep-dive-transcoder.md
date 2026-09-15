# Deep Dive: Protocol Transcoding

Back to [Architecture](README.md)

`rust/src/wire/` owns the closed codec registry and protocol adaptation between
OpenAI Chat Completions, OpenAI Responses, Anthropic Messages, and Gemini
surfaces. `rust/src/wire/ir.rs` captures canonical request, reasoning, usage,
response-block, provider-error, and stream-event semantics before adaptation.

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

Streaming adapters preserve native event grammar and require a native terminal
event. EOF, cancellation, and malformed frames remain typed failures. Alternate
wire negotiation is bounded and uses the same request submission budget as
account retries.

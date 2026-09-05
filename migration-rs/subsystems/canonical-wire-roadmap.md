# M6 Canonical Request, Wire Codec, Transcoding, and SSE Roadmap

Status: closed after W012 corrective pass

Repository baseline: `fb36054278817de63b5c516c82202184c9200be7`

Canonical source: `../000-long-term-specification.md`, `../001-terminology-and-domain-model.md`, `../002-long-term-roadmap.md`, and accepted M5 D009 closure. Applicable ADRs: ADR-0001 through ADR-0003.

M6 ports the deterministic request/wire transformation layer without creating an inference coordinator. It owns bounded admission/limits, canonical request/response/event/usage types, static wire profiles, OpenAI Chat Completions, OpenAI Responses, Anthropic Messages, Gemini generateContent, semantic adaptation/loss policy, multimodal/document/cache body adaptation, SSE/event conversion, usage normalization, terminal evidence, and one caller-selected-profile runtime facade.

It does not own account selection, dynamic wire negotiation/preference/retry, provider submission/auth secrets, durable attempt persistence, health effects, downstream handoff, cancellation/timeouts/finalization, semantic model-router selector calls, runtime generations, or service lifecycle. Python `wire.resolver` runtime state remains M7.

## Dependency sequence

```text
M5 D001-D009 closed
 -> W001 contract/fixture freeze (closed)
 -> W002 canonical IR + admission/limits + M5 bridge (closed)
 -> W003 static profiles + codec contract (closed)
 -> W004 Chat + Anthropic codecs (closed)
 -> W005 Responses + Gemini codecs (closed)
 -> W006 reasoning/tools/structured/loss policy (closed)
 -> W007 multimodal/documents/cache/provider adaptation (closed)
 -> W008 SSE/events/usage/terminal evidence (historical closure)
 -> W009 selected-profile runtime facade (closed)
 -> W010 integrated M6 qualification/closure (historical aggregate closure)
 -> W011 SSE EOF UTF-8 finalization correction (closed)
 -> W012 cross-surface differential requalification and M6 re-closure (closed; accepted closure)
 -> M7 planning review only after W012 accepted closure
```

Only one plan is registered dependency-ready at a time. W001-W010 retain append-only closure records. W010's aggregate conclusion is reopened only for the post-closure findings enumerated by W011/W012.

## Post-W010 findings

Independent review found two mandatory M6 gaps:

1. Rust `SseDecoder::finish()` does not force a retained incomplete UTF-8 suffix through EOF replacement decoding. Python `SSEDecoder.finish()` calls its incremental decoder with `final=True`, emits U+FFFD, increments replacement evidence, and then processes the final line/event. Rust can silently drop those trailing bytes. W008 explicitly required invalid-UTF-8 and EOF parity; W010 did not test this case.
2. W010's 15-pair request/finite/stream matrix is structurally broad but semantically under-asserted. The Python W001 oracle already computes full canonical request plus per-profile request encodings, yet the committed projection and Rust W010 request test compare only coarse request metadata. The finite response cross-surface test checks success plus client-body presence, and the stream cross-surface test checks encodability/terminal non-emptiness rather than full client semantics. Those tests do not prove the mandatory role/content/tool/reasoning/media/structured/usage/event-order fields claimed by W010 closure.

W011 corrects the concrete parser defect. W012 then performs the full Python-derived cross-surface requalification and fixes any bounded M6 codec/adaptation mismatches it exposes.

## Core invariants

Python remains the oracle. Malformed/oversized input fails before dispatch. Canonical IR retains source intent needed for explicit loss decisions. Material tools/reasoning/media/structured constraints cannot disappear silently. Static profile lookup is deterministic. SSE is incremental/chunk-independent/bounded and EOF is not universal success. Usage preserves zero-vs-missing/cache status. Provider errors remain typed evidence. M6 performs no DB writes, provider network I/O, retry loops, or async cleanup.

W002 provides pure M5 routing/affinity fact adapters without mutation. W009 provides M7 an explicit-profile transformation facade. M7 owns wire negotiation/retry, provider send, response handoff, failure effects, cancellation/timeouts, and finalization.

## Resource posture

Prefer one JSON parse, immutable sharing/`Bytes`, bounded collections/media/documents/SSE carry state, no per-event tasks, and no full body diagnostics. Avoid new heavy frameworks or second HTTP/TLS stacks.

## Milestones

W001 freezes the oracle; W002 establishes the canonical semantic boundary; W003 freezes static profile/codec interfaces; W004-W005 cover finite wire families; W006-W007 centralize semantic adaptation and bounded media/cache behavior; W008 owns streaming/usage/terminal evidence; W009 composes the M7-facing facade; W010 is historical integrated qualification evidence.

W011 is the narrow UTF-8 EOF correction and W012 is the aggregate cross-surface differential requalification/re-closure gate; both are closed. Do not fold M7 orchestration into either corrective pass.

## Verification and closure

Use deterministic fixtures for all supported client/profile pairs, roles/content/tools/reasoning/structured output/media/documents/cache controls, finite errors, usage/cache counters, SSE framing/chunk splits/invalid UTF-8, client event encoding, and terminal evidence. No live paid provider or broad CI matrix is required.

Historical W001-W010 closure evidence remains valid except for the aggregate conclusions explicitly superseded by the corrective findings. M6 is re-closed after accepted W011 and W012 closure evidence and integrated Python-derived results proving parity-equivalent canonical semantics, adaptation decisions, client bodies/events, usage, warnings/errors, UTF-8 EOF behavior, and terminal evidence.

M7 is now eligible for its own planning review; no M7 implementation plan is promoted automatically.

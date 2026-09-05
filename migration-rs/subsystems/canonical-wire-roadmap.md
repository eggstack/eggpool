# M6 Canonical Request, Wire Codec, Transcoding, and SSE Roadmap

Status: closed after W010

Repository baseline: `e096ed177f94b64b23a82852d6ec1bebc8782316`

Canonical source: `../000-long-term-specification.md`, `../001-terminology-and-domain-model.md`, `../002-long-term-roadmap.md`, and accepted M5 D009 closure. Applicable ADRs: ADR-0001 through ADR-0003.

M6 ports the deterministic request/wire transformation layer without creating an inference coordinator. It owns bounded admission/limits, canonical request/response/event/usage types, static wire profiles, OpenAI Chat Completions, OpenAI Responses, Anthropic Messages, Gemini generateContent, semantic adaptation/loss policy, multimodal/document/cache body adaptation, SSE/event conversion, usage normalization, terminal evidence, and one caller-selected-profile runtime facade.

It does not own account selection, dynamic wire negotiation/preference/retry, provider submission/auth secrets, durable attempt persistence, health effects, downstream handoff, cancellation/timeouts/finalization, semantic model-router selector calls, runtime generations, or service lifecycle. Python `wire.resolver` runtime state remains M7.

## Dependency sequence

```text
M5 D001-D009 closed
 -> W001 contract/fixture freeze (**closed**)
 -> W002 canonical IR + admission/limits + M5 bridge (**closed**)
 -> W003 static profiles + codec contract (**closed**)
 -> W004 Chat + Anthropic codecs (**closed**)
 -> W005 Responses + Gemini codecs (**closed**)
 -> W006 reasoning/tools/structured/loss policy (**closed**)
 -> W007 multimodal/documents/cache/provider adaptation (**closed**)
 -> W008 SSE/events/usage/terminal evidence (**closed**)
 -> W009 selected-profile runtime facade (**closed**)
 -> W010 integrated M6 qualification/closure (**closed**)
 -> M7 planning/implementation handoff may undergo its own planning review
```

Only one plan is registered dependency-ready at a time. W004/W005 are conceptually parallel after W003 but the default handoff stayed serial. W009 and W010 are now closed.

## Core invariants

Python remains the oracle. Malformed/oversized input fails before dispatch. Canonical IR retains source intent needed for explicit loss decisions. Material tools/reasoning/media/structured constraints cannot disappear silently. Static profile lookup is deterministic. SSE is incremental/chunk-independent/bounded and EOF is not universal success. Usage preserves zero-vs-missing/cache status. Provider errors remain typed evidence. M6 performs no DB writes, provider network I/O, retry loops, or async cleanup.

W002 provides pure M5 routing/affinity fact adapters without mutation. W009 provides M7 an explicit-profile transformation facade. M7 owns wire negotiation/retry, provider send, response handoff, failure effects, cancellation/timeouts, and finalization.

## Resource posture

Prefer one JSON parse, immutable sharing/`Bytes`, bounded collections/media/documents/SSE carry state, no per-event tasks, and no full body diagnostics. Avoid new heavy frameworks or second HTTP/TLS stacks.

## Milestones

W001 freezes the oracle; W002 establishes the canonical semantic boundary; W003 freezes static profile/codec interfaces; W004-W005 cover finite wire families; W006-W007 centralize semantic adaptation and bounded media/cache behavior; W008 owns streaming/usage/terminal evidence; W009 composes the M7-facing facade; W010 performs integrated differential, resource, dependency, and security qualification.

## Verification and closure

Use deterministic fixtures for all four surfaces/profiles, roles/content/tools/reasoning/structured output/media/documents/cache controls, finite errors, usage/cache counters, SSE framing/chunk splits, and terminal evidence. No live paid provider or broad CI matrix is required.

M6 closes only after W001-W010 have accepted closure evidence and integrated oracle results show parity-equivalent canonical semantics, adaptation decisions, client bytes/events, usage, warnings/errors, and terminal evidence. W010 is closed with accepted evidence in `closure/canonical-wire/010-status.md`. M6 closure means M7 can rely on transformation semantics; it does not mean Rust inference dispatch exists. M7 remains unpromoted until its own planning review.

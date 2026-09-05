# M6 Canonical Wire Implementation Handoffs

Status: corrective pass active; W012 dependency-ready

Source roadmap: `migration-rs/subsystems/canonical-wire-roadmap.md`

M6 is intentionally split so the behavioral oracle and canonical semantic boundary close before provider-family codecs and streaming behavior are layered on top. The plans preserve Python behavior while keeping M7 coordinator/retry/finalization out of scope.

W001-W010 retain their historical closure records. Post-W010 review found a concrete SSE EOF UTF-8 mismatch plus an integrated qualification gap in the cross-surface transformation matrix. The corrective sequence is W011 -> W012.

| ID | Plan | Class | Dependency state |
|---|---|---|---|
| W001 | [Contract and deterministic fixture freeze](001-contract-and-fixture-freeze.md) | invariant/infrastructure | closed; see [closure](../../closure/canonical-wire/001-status.md) |
| W002 | [Canonical IR, request admission, limits, and M5 fact bridge](002-canonical-ir-request-admission-and-limits.md) | capability/invariant | closed; see [closure](../../closure/canonical-wire/002-status.md) |
| W003 | [Static wire-profile registry and codec contract](003-wire-profile-registry-and-codec-contract.md) | capability/invariant | closed; see [closure](../../closure/canonical-wire/003-status.md) |
| W004 | [OpenAI Chat Completions and Anthropic Messages codecs](004-openai-chat-anthropic-messages-codecs.md) | capability | closed; see [closure](../../closure/canonical-wire/004-status.md) |
| W005 | [OpenAI Responses and Gemini generateContent codecs](005-openai-responses-gemini-codecs.md) | capability | closed; see [closure](../../closure/canonical-wire/005-status.md) |
| W006 | [Reasoning, tools, structured output, and loss policy](006-reasoning-tools-structured-output-and-loss-policy.md) | capability/invariant | closed; see [closure](../../closure/canonical-wire/006-status.md) |
| W007 | [Multimodal, documents, cache controls, and provider adaptation](007-multimodal-documents-cache-and-provider-adaptation.md) | capability/invariant | closed; see [closure](../../closure/canonical-wire/007-status.md) |
| W008 | [SSE, canonical stream events, usage, and terminal evidence](008-sse-stream-events-usage-and-terminal-evidence.md) | capability/invariant | historical closure; W011 corrects an uncovered EOF UTF-8 case |
| W009 | [Selected-profile codec runtime boundary](009-selected-profile-codec-runtime-boundary.md) | capability/invariant | closed; see [closure](../../closure/canonical-wire/009-status.md) |
| W010 | [Differential qualification and M6 closure](010-differential-qualification-and-m6-closure.md) | invariant | historical aggregate closure; superseded for W011/W012 findings only |
| W011 | [SSE EOF UTF-8 finalization correction](011-sse-eof-utf8-correction.md) | invariant/corrective | closed; see [closure](../../closure/canonical-wire/011-status.md) |
| W012 | [Cross-surface differential requalification and M6 re-closure](012-cross-surface-differential-requalification-and-m6-reclosure.md) | invariant/corrective | **dependency-ready; W011 closure accepted** |

Only the registry's dependency-ready table authorizes implementation. W012 is the sole dependency-ready plan after accepted W011 closure.

Every active plan must receive an accepted closure record under `migration-rs/closure/canonical-wire/` before its hard successor is promoted. Historical closure records are append-only.

M6 stops at a pure selected-profile codec runtime. Dynamic wire negotiation, DB-backed preference, alternate-wire retry, provider submission, durable attempts, response handoff, cancellation, timeout policy, and finalization remain M7. M7 is blocked until W012 re-closes aggregate M6 and then still requires its own planning review before implementation handoff.

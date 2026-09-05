# M6 Canonical Wire Implementation Handoffs

Status: active; W009 ready for handoff

Source roadmap: `migration-rs/subsystems/canonical-wire-roadmap.md`

M6 is intentionally split so the behavioral oracle and canonical semantic boundary close before provider-family codecs and streaming behavior are layered on top. The plans preserve Python behavior while keeping M7 coordinator/retry/finalization out of scope.

| ID | Plan | Class | Dependency state |
|---|---|---|---|
| W001 | [Contract and deterministic fixture freeze](001-contract-and-fixture-freeze.md) | invariant/infrastructure | closed; see [closure](../../closure/canonical-wire/001-status.md) |
| W002 | [Canonical IR, request admission, limits, and M5 fact bridge](002-canonical-ir-request-admission-and-limits.md) | capability/invariant | closed; see [closure](../../closure/canonical-wire/002-status.md) |
| W003 | [Static wire-profile registry and codec contract](003-wire-profile-registry-and-codec-contract.md) | capability/invariant | closed; see [closure](../../closure/canonical-wire/003-status.md) |
| W004 | [OpenAI Chat Completions and Anthropic Messages codecs](004-openai-chat-anthropic-messages-codecs.md) | capability | closed; see [closure](../../closure/canonical-wire/004-status.md) |
| W005 | [OpenAI Responses and Gemini generateContent codecs](005-openai-responses-gemini-codecs.md) | capability | closed; see [closure](../../closure/canonical-wire/005-status.md) |
| W006 | [Reasoning, tools, structured output, and loss policy](006-reasoning-tools-structured-output-and-loss-policy.md) | capability/invariant | closed; see [closure](../../closure/canonical-wire/006-status.md) |
| W007 | [Multimodal, documents, cache controls, and provider adaptation](007-multimodal-documents-cache-and-provider-adaptation.md) | capability/invariant | closed; see [closure](../../closure/canonical-wire/007-status.md) |
| W008 | [SSE, canonical stream events, usage, and terminal evidence](008-sse-stream-events-usage-and-terminal-evidence.md) | capability/invariant | closed; see [closure](../../closure/canonical-wire/008-status.md) |
| W009 | [Selected-profile codec runtime boundary](009-selected-profile-codec-runtime-boundary.md) | capability/invariant | **dependency-ready; W008 closure accepted** |
| W010 | [Differential qualification and M6 closure](010-differential-qualification-and-m6-closure.md) | invariant | planned; blocked on W009 closure |

Only the registry's dependency-ready table authorizes implementation. W004 and W005 could technically proceed in parallel after W003, but serial promotion is the default to keep behavioral review small and avoid simultaneous changes to the canonical codec contract.

Every plan must receive an accepted closure record under `migration-rs/closure/canonical-wire/` before its hard successor is promoted.

M6 stops at a pure selected-profile codec runtime. Dynamic wire negotiation, DB-backed preference, alternate-wire retry, provider submission, durable attempts, response handoff, cancellation, timeout policy, and finalization remain M7.

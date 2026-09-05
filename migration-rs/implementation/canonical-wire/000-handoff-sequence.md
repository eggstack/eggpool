# M6 Canonical Wire Handoff Sequence

Status: corrective pass active; W012 dependency-ready

Execute and close these plans in dependency order:

1. W001 — contract and deterministic fixture freeze (**closed**);
2. W002 — canonical IR, request admission, limits, and M5 fact bridge (**closed**);
3. W003 — static wire-profile registry and codec contract (**closed**);
4. W004 — OpenAI Chat Completions and Anthropic Messages codecs (**closed**);
5. W005 — OpenAI Responses and Gemini generateContent codecs (**closed**);
6. W006 — reasoning, tools, structured output, and loss policy (**closed**);
7. W007 — multimodal, documents, cache controls, and provider-sensitive pure adaptation (**closed**);
8. W008 — SSE framing, canonical stream events, usage, and terminal evidence (**historical closure; W011 correction applies**);
9. W009 — selected-profile codec runtime boundary (**closed**);
10. W010 — integrated differential qualification and M6 closure (**historical aggregate closure; superseded for W011/W012 findings only**);
11. W011 — SSE EOF UTF-8 finalization correction (**closed**);
12. W012 — cross-surface differential requalification and M6 re-closure (**dependency-ready; W011 closure accepted**).

W004 and W005 were independent provider-family implementation slices after W003, but the migration registry authorizes only explicitly dependency-ready work. The current corrective sequence is deliberately serial because W012 must include W011's parser regression evidence.

Boundary rule: M6 may accept a caller-selected static wire profile and produce/consume request/response/stream bytes. It may not choose a different profile because an attempt failed. Dynamic wire rejection, negotiation handles, learned preference, provider submission, retry, failure effects, response handoff, and durable attempt/finalization state are M7.

W010 remains historical evidence rather than being rewritten. Aggregate M6 is reopened while W012 is active. Accepted W011 closure promotes W012 only. W012 accepted closure may re-close M6 after the full Python-derived 15-pair request/finite/stream matrix passes. M7 remains blocked until then and still requires its own planning review before any implementation plan is promoted.

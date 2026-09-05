# M6 Canonical Wire Handoff Sequence

Status: active; W003 dependency-ready

Execute and close these plans in dependency order:

1. W001 — contract and deterministic fixture freeze (**closed**);
2. W002 — canonical IR, request admission, limits, and M5 fact bridge (**closed; W001 closed**);
3. W003 — static wire-profile registry and codec contract (**ready; W002 closed**);
4. W004 — OpenAI Chat Completions and Anthropic Messages codecs (blocked on W003);
5. W005 — OpenAI Responses and Gemini generateContent codecs (blocked on W004 by default serial handoff);
6. W006 — reasoning, tools, structured output, and loss policy (blocked on W004/W005);
7. W007 — multimodal, documents, cache controls, and provider-sensitive pure adaptation (blocked on W006);
8. W008 — SSE framing, canonical stream events, usage, and terminal evidence (blocked on W007);
9. W009 — selected-profile codec runtime boundary (blocked on W008);
10. W010 — integrated differential qualification and M6 closure (blocked on W009).

W004 and W005 are independent provider-family implementation slices after W003, but they are not simultaneously dependency-ready unless the registry explicitly authorizes parallel work. Keeping them serial reduces churn in the canonical codec contract.

Boundary rule: M6 may accept a caller-selected static wire profile and produce/consume request/response/stream bytes. It may not choose a different profile because an attempt failed. Dynamic wire rejection, negotiation handles, learned preference, provider submission, retry, failure effects, response handoff, and durable attempt/finalization state are M7.

M7 implementation may not be promoted dependency-ready until W010 has accepted closure evidence.

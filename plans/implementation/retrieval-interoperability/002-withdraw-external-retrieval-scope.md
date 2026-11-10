# Retrieval Interoperability R002 — Withdraw external retrieval scope

Status: implemented

Repository baseline: `e049025274a397d7799c3ae21ed106947c2a1572`

Source roadmap:

- `plans/subsystems/retrieval-interoperability-roadmap.md` R002

Long-term requirements:

- `plans/000-long-term-specification.md#1-product-definition`
- `plans/000-long-term-specification.md#6-non-goals`

Applicable ADRs:

- `plans/adrs/ADR-0009-downstream-application-boundary.md` (accepted)
- `plans/adrs/ADR-0008-embedding-inference-surface-boundary.md` (accepted)

Primary class: polish

## 1. Objective

Stop R001 as an execution handoff. Document that EggPool does not host
document retrieval, and that `POST /v1/embeddings` remains the inference
route a downstream application may call if it later chooses to embed.

## 2. Why this milestone is ready

ADR-0009 withdraws pattern B and the PrivateGPT citation program.
ADR-0008 already keeps embeddings free of a vector store. No code
dependency on application-boundary M001, though the docs must not tell
operators to register a retrieval adapter on the MCP gateway.

## 3. Current implementation evidence

- R001 and its blocked closure remain the historical record:
  `plans/implementation/retrieval-interoperability/001-external-rag-contract-and-reference.md`
  and `plans/closure/retrieval-interoperability/001-status.md`.
- E002 accepts the PrivateGPT 1.0.1 base64 embedding request.
  `docs/retrieval-interoperability.md` still describes a solo PrivateGPT
  topology and says team retrieval is unsupported pending an adapter.
- EggUI grounds attachments with local extraction and FTS and has
  deferred document RAG and embeddings. CodeGG does not retrieve
  private documents through EggPool.
- `rust/tests/embeddings_api.rs` covers the embeddings route.

## 4. Invariants that must not regress

- No document, chunk, or vector table is added.
- Embeddings admission, base64 float32 responses, and budget checks
  stay as E001 and E002 closed them.
- The blocked R001 closure text is left in place as history.

## 5. Scope

### In scope

- Rewrite `docs/retrieval-interoperability.md` so the current contract
  is: callers that want embeddings use `POST /v1/embeddings`; document
  ingest, indexes, citations, and ACLs belong to the calling
  application; PrivateGPT is not a supported EggPool integration
  target.
- Point the retrieval roadmap and registry at this plan as the
  successor that ends the blocked work.
- Leave R001's plan body and blocked closure unread as history, with
  a status line of superseded on the plan only.

### Explicitly out of scope

- Deleting historical plans or closures.
- A new embedding feature, a dimension negotiation protocol, or a
  PrivateGPT rerun.
- Application-boundary M001 code removal. If the retrieval doc still
  names the MCP gateway, this milestone deletes that instruction even
  if M001 has not merged, and describes the gateway as withdrawn by
  ADR-0009.

## 6. Required production changes

Documentation only. No Rust, schema, or config change.

## 7. Ordered work packages

### Work package A — Current contract

Intent: one page states the boundary an operator can follow.

Required changes: replace the solo-qualification procedure and the
team-adapter section with the embeddings route, the statement that
EggUI owns chat-document grounding, and the statement that another
application may call embeddings without EggPool storing vectors.
Keep the sanitized fixture path as historical evidence that base64
admission exists, not as a retrieval claim.

Acceptance evidence: the page has no setup steps for PrivateGPT ingest
and no `documents.search` contract.

### Work package B — Planning links

Intent: an agent opening the registry does not pick up R001.

Required changes: roadmap status row for R002, R001 marked superseded
for execution, registry blocked row removed in the same change that
registers R002. The embedding roadmap's E002 note points here.

Acceptance evidence: `plans/registry.md` blocked table has no R001 row.

## 8. Failure, cancellation, restart, contention semantics

No runtime behavior. Nothing to cancel.

## 9. Compatibility and migration

The embeddings HTTP contract is unchanged. Operators who followed the
old PrivateGPT page lose a supported recipe. The page says that
plainly and points at `/v1/embeddings` and `/v1/chat/completions`.

## 10. Required tests

No new tests. `rust/tests/embeddings_api.rs` is unchanged and is not
required to be rerun for a docs-only change. The closure says the
suite was not run and why.

## 11. Required verification commands

```bash
git diff --check
uv run python scripts/validate_release_docs.py
```

`validate_release_docs.py` only if the doc edit touches a file that
script covers. If it does not, say so and rely on `git diff --check`
plus a link check of the edited markdown. Do not claim the serial Rust
suite.

## 12. Documentation updates

- `docs/retrieval-interoperability.md`
- `plans/subsystems/retrieval-interoperability-roadmap.md` status table
  (already expected in this change set)
- One sentence in `docs/api-reference.md` only if it still claims
  EggPool performs retrieval

## 13. Acceptance criteria

The live retrieval doc matches ADR-0009. R001 is not an active or
blocked handoff. Embeddings source files have an empty diff.

## 14. Stop conditions

Stop if telling the truth in the doc seems to require an embeddings
code change or a citation store. That contradicts this milestone.
Stop rather than re-running PrivateGPT to try to close R001.

## 15. Closure evidence required

Doc diff summary, confirmation `rust/` is untouched, registry blocked
table no longer lists R001, and the commands actually run.

## 16. Handoff notes

Do not delete `001-external-rag-contract-and-reference.md` or
`plans/closure/retrieval-interoperability/001-status.md`. Change the
R001 plan status line to `superseded` and add a one-line pointer to
this plan above the historical body.

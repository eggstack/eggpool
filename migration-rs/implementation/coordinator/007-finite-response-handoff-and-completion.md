# C007 — Finite Response Handoff and Completion

Status: ready for handoff; C006 accepted closure

Source roadmap: `migration-rs/subsystems/coordinator-roadmap.md`

Primary class: capability/invariant

Hard dependency: C006.

## Objective

Complete the non-streaming inference path from one C004 upstream response through M6 finite decoding/adaptation, monotonic downstream handoff, success/failure effects, retained terminal finalization, and the client-visible response contract.

## Python oracle

Use C001 plus coordinator finite-response branches, `response_handoff.py`, parsed upstream response/error handling, response-header filtering, normalized usage/cost extraction, C005 failure policy, C006 finalization, and public finite endpoint tests.

## Response classification

For every upstream response, classify before downstream handoff:

- successful finite provider response decoded by M6;
- valid provider error envelope;
- malformed provider success/error body;
- retryable vs terminal HTTP/failure evidence from C005;
- response body/resource limit violations;
- adaptation/loss rejection when encoding the client surface.

Retry remains legal only while `ResponseHandoffState.started == false` and C005 authorizes it. A candidate retry must finalize/retain cleanup for the failed attempt first.

## Handoff point

Introduce one Rust monotonic handoff fact that becomes true when response start is sent or attempted. Once true, no alternate account or wire profile may be replayed for that client request. If writing the response subsequently fails, finalization uses a post-handoff/client-cancelled-or-interrupted outcome rather than retry.

## Client response

Use M6's client-surface finite body. Preserve filtered response headers, content type, status semantics, request IDs, and any compatibility headers frozen by C001. Provider auth/internal hop-by-hop headers must not leak downstream.

## Completion/finalization

On success, register C006 terminal ownership before cancellation-sensitive completion waits. Persist normalized usage, cache counters, cost provenance, bytes, timing, upstream request ID, selected wire/provider/account, transcode facts, and release reason according to the Python contract. Successful health/backoff clearing is applied exactly once through the existing effect interfaces.

## Tests

Differential local-provider matrix across all public client surfaces and five M6 upstream profiles. Cover 2xx success, malformed 2xx, 4xx/5xx provider error, response adaptation failure, retryable pre-handoff response, failed downstream write after handoff, cancellation between decode and response start, cancellation after response start, duplicate finalization, header filtering, usage/cost, and redaction.

Assert number/order of upstream attempts and the exact point after which retries stop.

## Dependencies

No new HTTP/server framework. Use existing Axum/Hyper/M4/M6/C005/C006 interfaces.

## Acceptance criteria

C007 closes when finite requests can complete end-to-end against deterministic providers, retry cannot occur after handoff, client response semantics match Python, and every terminal path has retained C006 ownership with no leaked claim/reservation state.

## Closure

Create `migration-rs/closure/coordinator/007-status.md`. Accepted closure promotes C008.

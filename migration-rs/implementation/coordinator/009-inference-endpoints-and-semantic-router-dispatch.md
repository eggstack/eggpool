# C009 — Public Inference Endpoints and Semantic-Router Internal Dispatch

Status: planned; blocked on C008 accepted closure

Source roadmap: `migration-rs/subsystems/coordinator-roadmap.md`

Primary class: capability/invariant

Hard dependency: C008.

## Objective

Wire the qualified coordinator lifecycle into Rust's Axum public inference surfaces and complete the D007 semantic model-router selector path that intentionally waited for M7. Keep HTTP handler concerns thin: admission/auth/body limits are existing boundaries; handlers invoke one coordinator API and translate its typed result to the established client surface.

## Python oracle

Use C001 plus public Chat Completions/Responses/Messages handlers, request context preparation, `internal_dispatch.py`, D007 model-router selector code/affinity contract, and endpoint contract/smoke tests.

## Public routes

Enable the Rust equivalents of the production inference routes for:

- OpenAI Chat Completions;
- OpenAI Responses;
- Anthropic Messages;
- any existing compatibility aliases explicitly frozen by C001.

Preserve auth middleware, request-body ceilings, content type, status/error envelopes, stream vs finite response behavior, request/session headers, and provider-qualified model IDs. Do not redesign the public API.

Handlers must not contain routing/retry/finalization loops. Build/admit through M6, derive M5 facts, invoke one M7 coordinator entry point, then hand back the finite/stream response object.

## Semantic model-router path

Port the D007 deferred semantic selector integration. The selector may dispatch a bounded internal concrete-model request through the same coordinator lifecycle, but must enforce explicit recursion/cycle protection:

- selector requests cannot recursively select the same virtual router indefinitely;
- internal selector dispatch has a separate bounded attempt/token/body/time budget frozen by C001;
- selector failures have deterministic fallback/error policy;
- affinity is committed only according to the D007 contract;
- selector request lifecycle/finalization is observable without leaking its internal prompt/body.

Prefer a typed internal dispatch context over routing a synthetic HTTP request through localhost.

## Isolation

A malformed client request, provider response, selector response, or disconnect may terminate that request but cannot poison shared coordinator/router/wire/finalizer state. A subsequent valid request must work without process restart or database repair.

## Tests

Black-box Python/Rust endpoint tests for finite and stream across three public surfaces, authentication/body limits/model parsing, provider-qualified model IDs, native/cross-wire paths, retries before handoff, errors after handoff, semantic-router success/failure/affinity, recursion guard, concurrent requests, cancellation, and subsequent-request recovery.

Run only local deterministic providers/selectors; no paid traffic.

## Dependencies

Use existing Axum/Tower server. No internal HTTP loopback, RPC framework, or new web stack.

## Acceptance criteria

C009 closes when clients can exercise the real Rust inference endpoints end-to-end, handlers remain thin, semantic-router selector dispatch is bounded/nonrecursive and uses the same lifecycle, and a bad request/upstream cannot require server restart.

## Closure

Create `migration-rs/closure/coordinator/009-status.md`. Accepted closure promotes C010.